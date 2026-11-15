"""Evidence overlays.
- DXF: redraw the plan from entities, fill the evidence, print values.
- Raster: mark detections and OCR boxes.
- IFC: highlight matched elements by data-guid in companion storey plan SVGs; fall back to a top-down storey footprint plan.
- PDF: render the sheet page with PyMuPDF and outline matched schedule rows (and queried columns where grid rules exist)."""
import collections as C, copy, glob, hashlib, io, math, os, pickle, re, subprocess, xml.etree.ElementTree as ET
from PIL import Image, ImageDraw, ImageFont

ET.register_namespace("", "http://www.w3.org/2000/svg")
SVG_NS = "{http://www.w3.org/2000/svg}"
FP_CACHE = os.path.join(os.path.dirname(__file__), "..", "cache", "footprints")

COL = {
    "room": (255, 190, 0), "apartment": (230, 60, 140), "door": (220, 40, 40), "window": (30, 110, 230),
    "toilet": (150, 40, 200), "washbasin": (0, 160, 160), "bathtub": (240, 140, 0), "shower": (0, 170, 70),
    "shower_or_bath": (240, 140, 0), "room_label": (255, 120, 0), "floor": (90, 90, 90),
    "wall": (215, 90, 20), "slab": (110, 125, 205), "column": (160, 45, 190), "beam": (15, 140, 150),
    "stair": (0, 155, 95), "railing": (85, 150, 55), "roof": (175, 85, 65), "covering": (180, 135, 55),
    "furniture": (140, 105, 75), "curtainwall": (35, 140, 220), "member": (55, 125, 185), "plate": (75, 135, 190),
    "ramp": (0, 155, 95), "sanitary": (150, 40, 200), "light": (235, 160, 0), "airterminal": (0, 150, 185),
    "element": (220, 65, 35), "schedule_row": (230, 140, 0),
}

def _font_file(bold):
    """a real TrueType font from the system (fontconfig), so that 'm²' and accents render on any machine"""
    for name in (("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),):
        try: ImageFont.truetype(name, 12); return name
        except Exception: pass
    try: return subprocess.run(["fc-match", "-f", "%{file}", "sans:bold" if bold else "sans"], capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception: return None

_FF = {True: _font_file(True), False: _font_file(False)}
def _font(px, bold=False):
    try: return ImageFont.truetype(_FF[bold], int(px))
    except Exception: return ImageFont.load_default()
FONT = _font(13); FONT_B = _font(15, True)
def BIG(px): return _font(px, True)

TYPE_COL = {"bedroom": (120, 170, 255), "room": (150, 190, 255), "living": (255, 205, 110), "kitchen": (255, 150, 120), "bathroom": (110, 215, 215),
            "wc": (190, 140, 235), "corridor": (215, 215, 215), "outdoor": (150, 220, 140), "storage": (220, 190, 150), "stairs": (200, 200, 160),
            "shaft": (170, 170, 170), "elevator": (170, 170, 170), None: (255, 255, 255)}
def type_col(t):
    if t in TYPE_COL: return TYPE_COL[t]
    h = sum(ord(c) * (i + 3) for i, c in enumerate(str(t))); return (140 + h % 90, 140 + (h // 7) % 90, 140 + (h // 49) % 90)

NON_SPATIAL_IFC = ("fact", "level", "system")

def can_render(st, ans):
    if st is None or not ans: return False
    ev = set(ans.get("evidence") or [])
    if st.fmt in ("dxf-layered", "raster"): return True
    if st.fmt == "pdf-vector":
        return any(e.id in ev and ((e.src.get("page") and e.src.get("bbox")) or e.geom) for e in st.entities)
    if st.fmt == "ifc":
        return any(e.id in ev and e.kind not in NON_SPATIAL_IFC for e in st.entities)
    return False

def can_audit(st):
    if st is None: return False
    if st.fmt in ("dxf-layered", "raster"): return True
    if st.fmt == "pdf-vector": return any(e.src.get("page") and e.src.get("bbox") for e in st.entities)
    if st.fmt == "ifc":
        base = st.path[:-4] if st.path.lower().endswith(".ifc") else st.path
        if glob.glob(f"{base}_plan*.svg"): return True
        try: return os.path.getsize(st.path) < 30_000_000 and any(e.kind not in NON_SPATIAL_IFC for e in st.entities)
        except Exception: return False
    return False

def _banner(im, left_txt, right_txt=""):
    W, H = im.size; bh = 32
    out = Image.new("RGB", (W, H + bh), (246, 247, 249))
    out.paste(im, (0, bh))
    d = ImageDraw.Draw(out)
    d.line([(0, bh - 1), (W, bh - 1)], fill=(215, 220, 228), width=1)
    d.text((12, bh // 2), left_txt, fill=(25, 30, 38), font=_font(13, True), anchor="lm")
    if right_txt:
        d.text((W - 12, bh // 2), right_txt, fill=(102, 112, 133), font=_font(12, False), anchor="rm")
    return out

def audit_image(st, issues, size=1500):
    """the whole drawing as the engine understood it: every room coloured by type with its number and area,
    every symbol coloured by kind, problems outlined in red. Shown before any question is asked."""
    bad = {x for i in issues if i["sev"] in ("error", "warn") for x in i["ids"]}
    if st.fmt == "raster":
        # the overview shows WHAT was found (outlines only). No numbers or values are written on the drawing here:
        # a value appears on the drawing only as the answer to a question that asked for it (see _raster).
        im = Image.open(st.path).convert("RGB"); W, H = im.size; d = ImageDraw.Draw(im, "RGBA"); r = max(7, max(W, H) // 120)
        for e in st.entities:
            if not e.geom: continue
            c = COL.get(e.kind, (255, 0, 0))
            if e.kind == "room_label":
                xs = [p[0] for p in e.geom]; ys = [p[1] for p in e.geom]; d.rectangle([min(xs), min(ys), max(xs), max(ys)], outline=c + (255,), width=2); continue
            if len(e.geom) == 4 and not (e.kind == "room" and e.get("area_rule")):
                xs = [p[0] for p in e.geom]; ys = [p[1] for p in e.geom]; bx = [min(xs), min(ys), max(xs), max(ys)]
                cc = type_col(e.get("rtype")) if e.kind == "room" else c
                if e.kind == "floor": d.rectangle(bx, outline=(90, 90, 90, 255), width=2)
                elif e.kind == "room": d.rectangle(bx, outline=cc + (255,), width=3, fill=cc + (55,))
                else: d.rectangle(bx, outline=(220, 30, 30, 255) if e.get("borderline") else c + (255,), width=3)
                continue
            if len(e.geom) >= 3:        # traced room outline
                cc = type_col(e.get("rtype")) if e.kind == "room" else c
                P = [(float(px), float(py)) for px, py in e.geom]; d.polygon(P, fill=cc + (55,)); d.line(P + [P[0]], fill=cc + (255,), width=max(2, int(r / 3)))
                continue
            (x, y), = e.geom
            d.ellipse([x - r, y - r, x + r, y + r], outline=c + (255,), width=3, fill=c + (60,))
        k = RASTER_MAX / max(W, H)
        if k < 1: im = im.resize((int(W * k), int(H * k)), Image.LANCZOS)
        return _png(im)
    if st.fmt == "pdf-vector": return _pdf_audit(st, size)
    if st.fmt == "ifc": return _ifc_audit(st, size)
    if st.fmt != "dxf-layered": return None
    geoms = [e for e in st.entities if e.geom]
    xs = [x for e in geoms for x, _ in e.geom]; ys = [y for e in geoms for _, y in e.geom]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys); pad = 0.8
    s = (size - 60) / max(x1 - x0 + 2 * pad, y1 - y0 + 2 * pad)
    W = int((x1 - x0 + 2 * pad) * s) + 60; H = int((y1 - y0 + 2 * pad) * s) + 60
    T = lambda p: [((x - x0 + pad) * s + 30, H - 30 - (y - y0 + pad) * s) for x, y in p]
    im = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(im, "RGBA")
    for a in st.of("apartment"):
        if a.geom: d.polygon(T(a.geom), outline=(230, 60, 140, 255), width=3)
    for e in st.of("room"):
        if not e.geom: continue
        d.polygon(T(e.geom), fill=type_col(e.get("rtype")) + (150,), outline=(90, 90, 90, 255))
        for h in e.src.get("hole_geoms", []) or []: d.polygon(T(h), fill=(255, 255, 255, 255), outline=(120, 120, 120, 255))
    for u in st.meta.get("raw", {}).get("unlabelled", []):
        if not u.get("inside") and u["area"] >= 1.0: d.polygon(T(u["geom"]), fill=(255, 80, 80, 60), outline=(220, 40, 40, 255), width=2)
    for e in st.entities:
        if e.kind in ("door", "window", "toilet", "washbasin", "bathtub", "shower") and e.geom:
            P = T(e.geom); d.line(P + [P[0]], fill=COL[e.kind] + (255,), width=2)
    for e in st.entities:
        if e.id in bad and e.geom and e.kind != "apartment":
            P = T(e.geom); d.line(P + [P[0]], fill=(220, 30, 30, 255), width=4)
    f1 = BIG(max(10, min(15, s * 0.42))); f2 = BIG(max(9, min(13, s * 0.36)))
    for e in st.of("room"):
        if not e.geom: continue
        from shapely.geometry import Polygon as _P
        c = _P(e.geom).buffer(0).representative_point(); (cx, cy), = T([(c.x, c.y)])
        d.text((cx, cy - 1), str(e.get("number") or ""), fill=(0, 0, 0), font=f1, anchor="mb", stroke_width=2, stroke_fill=(255, 255, 255))
        if e.get("area") is not None: d.text((cx, cy + 1), f"{e.get('area'):g} m²", fill=(170, 0, 0) if e.id in bad else (40, 40, 40), font=f2, anchor="mt", stroke_width=2, stroke_fill=(255, 255, 255))
    for a in st.of("apartment"):
        if a.geom:
            px, py = max(T(a.geom), key=lambda p: -p[1] - p[0] * 0.001); d.text((px + 4, py + 4), f"Apt {a.get('name')}", fill=(230, 60, 140), font=FONT_B, stroke_width=2, stroke_fill=(255, 255, 255))
    return _png(im)

def render(store, answer, size=1300, path=None):
    if store is None or not answer: return None
    if store.fmt == "dxf-layered": return _dxf(store, answer, size)
    if store.fmt == "raster": return _raster(store, answer, size)
    if store.fmt == "pdf-vector": return _pdf(store, answer)
    if store.fmt == "ifc": return _ifc(store, answer, size, req_path=path)
    return None

RASTER_MAX = int(os.environ.get("BP_RASTER_MAX_PX", "16000"))      # scans are shown at their own resolution; only sheets beyond what a browser displays are reduced
def _png(im):
    b = io.BytesIO()
    if im.width * im.height > 8_000_000: im.save(b, "PNG", compress_level=3)      # lossless either way; 'optimize' takes minutes on a full-size sheet
    else: im.save(b, "PNG", optimize=True)
    return b.getvalue()

def _trace_vals(ans):
    """id -> the value the answer measured for that entity (whatever quantity was asked: area, a formula, a width...)"""
    vals = {}
    for s in ans.get("trace", []):
        for r in s.get("values", []) if isinstance(s.get("values"), list) else []:
            if isinstance(r, dict) and "id" in r:
                v = next((r[k] for k in ("area", "length", "perimeter", "volume", "side_area", "width", "height", "thickness", "computed") if k in r), None)
                if v is None: v = next((x for k, x in r.items() if k not in ("id", "label", "number", "how", "printed", "level", "value", "type") and isinstance(x, (int, float)) and not isinstance(x, bool)), None)
                if v is not None: vals[r["id"]] = v
    return vals

def _dxf(st, ans, size):
    ev = set(ans.get("evidence") or []); vals = _trace_vals(ans)
    geoms = [e for e in st.entities if e.geom]
    xs = [x for e in geoms for x, _ in e.geom]; ys = [y for e in geoms for _, y in e.geom]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys); pad = 0.8
    s = (size - 60) / max(x1 - x0 + 2 * pad, y1 - y0 + 2 * pad)
    W = int((x1 - x0 + 2 * pad) * s) + 60; H = int((y1 - y0 + 2 * pad) * s) + 60
    T = lambda p: [((x - x0 + pad) * s + 30, H - 30 - (y - y0 + pad) * s) for x, y in p]
    im = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(im, "RGBA")
    for e in st.of("room"):
        if e.geom: d.polygon(T(e.geom), outline=(185, 185, 185))
    for e in st.entities:
        if e.kind in ("door", "window", "toilet", "washbasin", "bathtub", "shower") and e.geom:
            d.line(T(e.geom) + [T(e.geom)[0]], fill=(140, 140, 140), width=1)
    num = [0]
    for e in st.entities:
        if e.id not in ev or not e.geom: continue
        c = COL.get(e.kind, (255, 0, 0)); P = T(e.geom)
        if e.kind in ("room", "apartment") and len(P) >= 3: d.polygon(P, fill=c + (90,), outline=c + (255,), width=3)
        else:
            d.line(P + [P[0]], fill=c + (255,), width=4); num[0] += 1
            cx = sum(x for x, _ in P) / len(P); cy = sum(y for _, y in P) / len(P)
            d.text((cx + 7, cy - 7), str(num[0]), fill=c + (255,), font=FONT_B, anchor="lb", stroke_width=3, stroke_fill=(255, 255, 255))
    doubt = {x for c in (ans.get("trust") or {}).get("checks", []) if c["result"] in ("fail", "caution") for x in c.get("ids", [])}
    for e in st.entities:
        if e.id in doubt and e.geom and e.kind != "apartment":
            P = T(e.geom); d.line(P + [P[0]], fill=(220, 30, 30, 255), width=5)
    for e in st.of("room"):
        if not e.geom: continue
        cx = sum(x for x, _ in T(e.geom)) / len(e.geom); cy = sum(y for _, y in T(e.geom)) / len(e.geom)
        txt = e.get("number") or ""
        if e.id in vals: txt = (txt + chr(10) + f"{vals[e.id]:g}").strip()
        d.multiline_text((cx, cy), txt, fill=(0, 0, 0) if e.id in ev else (150, 150, 150), font=FONT_B if e.id in ev else FONT, anchor="mm", align="center")
    return _png(im)

def _raster(st, ans, size):
    """The drawing with ONLY this answer's items marked. Nothing is numbered. A value is written inside an item only
    when the question asked for a measured quantity (area -> m² in the asked rooms, baseboard -> m ...)."""
    im = Image.open(st.path).convert("RGB"); W, H = im.size; d = ImageDraw.Draw(im, "RGBA"); ev = set(ans.get("evidence") or [])
    r = max(7, max(W, H) // 110); vals = _trace_vals(ans) if ans.get("status") == "ok" else {}
    unit = ans.get("unit") if ans.get("unit") not in (None, "count") else ((ans.get("plan") or {}).get("unit") or "")
    def tag(xy, eid, anchor="mm"):
        if eid in vals: d.text(xy, f"{vals[eid]:g} {unit}".strip(), fill=(20, 20, 20), font=BIG(max(13, r * 0.5)), anchor=anchor, stroke_width=3, stroke_fill=(255, 255, 255))
    for e in st.entities:
        if not e.geom or e.id not in ev: continue
        c = COL.get(e.kind, (255, 0, 0))
        if e.kind == "room_label":
            xs = [p[0] for p in e.geom]; ys = [p[1] for p in e.geom]; d.rectangle([min(xs), min(ys), max(xs), max(ys)], outline=c + (255,), width=3); continue
        if len(e.geom) == 4 and not (e.kind == "room" and e.get("area_rule")):
            xs = [p[0] for p in e.geom]; ys = [p[1] for p in e.geom]; bx = [min(xs), min(ys), max(xs), max(ys)]; big = e.kind in ("room", "floor")
            d.rectangle(bx, outline=(220, 30, 30, 255) if e.get("borderline") else c + (255,), width=4, fill=c + (40 if big else 70,))
            tag(((bx[0] + bx[2]) / 2, bx[1] - 2), e.id, "mb")
            continue
        if len(e.geom) >= 3:        # traced room outline
            P = [(float(px), float(py)) for px, py in e.geom]; d.polygon(P, fill=c + (60,)); d.line(P + [P[0]], fill=c + (255,), width=max(3, r // 4))
            tag((sum(q[0] for q in P) / len(P), sum(q[1] for q in P) / len(P)), e.id)
            continue
        (x, y), = e.geom
        d.ellipse([x - r, y - r, x + r, y + r], outline=c + (255,), width=4, fill=c + (70,))
    k = RASTER_MAX / max(W, H)       # 'size' is not applied to scans: the evidence is the drawing at its own resolution
    if k < 1: im = im.resize((int(W * k), int(H * k)), Image.LANCZOS)
    return _png(im)

# ---------------------------------------------------------------------------
# PDF schedule & sheet overlays
# ---------------------------------------------------------------------------

def _page_h_rules(pg):
    raw = C.defaultdict(list)
    for dr in pg.get_drawings():
        for it in dr["items"]:
            segs = [(it[1].x, it[1].y, it[2].x, it[2].y)] if it[0] == "l" else (
                [(it[1].x0, it[1].y0, it[1].x1, it[1].y0), (it[1].x0, it[1].y1, it[1].x1, it[1].y1)] if it[0] == "re" else [])
            for x0, y0, x1, y1 in segs:
                if abs(y0 - y1) < 0.8 and abs(x0 - x1) > 12:
                    raw[round(y0, 1)].append((min(x0, x1), max(x0, x1)))
    merged = []
    for y in sorted(raw):
        if merged and y - merged[-1][0] <= 1.2: merged[-1][1].extend(raw[y])
        else: merged.append([y, list(raw[y])])
    out = []
    for y, segs in merged:
        runs = []
        for a, b in sorted(segs):
            if runs and a - runs[-1][1] <= 4.0: runs[-1][1] = max(runs[-1][1], b)
            else: runs.append([a, b])
        out.append((y, [(a, b) for a, b in runs if b - a >= 100]))
    return out

def _snap_row_box(h_rules, b, sp):
    xa = b[0] + 4.0
    above = [(y, rx0, rx1) for y, runs in h_rules for rx0, rx1 in runs if rx0 - 8 <= xa <= rx1 + 8 and b[1] - 14 <= y <= b[1] + 2.5]
    if above:
        ty, tx0, tx1 = max(above, key=lambda t: t[0])
        below = [(y, rx0, rx1) for y, runs in h_rules for rx0, rx1 in runs if rx0 - 8 <= xa <= rx1 + 8 and ty + 5 <= y <= b[1] + 36]
        if below:
            by, bx0, bx1 = min(below, key=lambda t: t[0])
            return (min(tx0, bx0), ty, max(tx1, bx1), by)
    rx0 = min(b[0], sp[0]) - 3 if sp else b[0] - 3
    rx1 = max(b[2], sp[1]) + 3 if sp else b[2] + 3
    return (rx0, b[1] - 2.5, rx1, b[3] + 2.5)

def _pdf(st, ans, zoom=2.0):
    """Draw the PDF page region around the matched schedule rows (from either grid lines or text gaps)
    or vector entities, outline matched rows, mark queried/filtered columns when column bounds exist,
    and number matched rows in table order."""
    import pymupdf
    ev_order = list(dict.fromkeys(ans.get("evidence") or []))
    idx_of = {eid: i + 1 for i, eid in enumerate(ev_order)}
    ev = set(ev_order)
    rows = [e for e in st.entities if e.id in ev and (e.src.get("page") or 1) and (e.src.get("bbox") or e.geom)]
    if not rows: return None
    page = C.Counter(e.src.get("page", 1) for e in rows).most_common(1)[0][0]
    rows = [e for e in rows if e.src.get("page", 1) == page]
    doc = pymupdf.open(st.path); pg = doc[page - 1]; pg.remove_rotation()
    h_rules = _page_h_rules(pg) if any(e.src.get("coords") != "unrotated" for e in rows if e.kind == "schedule_row") else []

    tables_used = {e.get("table") for e in rows if e.get("table")}
    same = [e for e in st.entities if e.src.get("page", 1) == page and e.src.get("bbox") and (
        (e.kind == "schedule_row" and e.get("table") in tables_used) or e.id in ev)]
    tbl_span = {}
    for tbl in tables_used:
        tr = [e for e in st.entities if e.kind == "schedule_row" and e.get("table") == tbl and e.src.get("page", 1) == page and e.src.get("bbox")]
        if tr:
            cx0 = tr[0].src.get("cols_x")
            x_lo = min([e.src["bbox"][0] for e in tr] + ([cx0[0]] if cx0 else []))
            x_hi = max([e.src["bbox"][2] for e in tr] + ([cx0[-1]] if cx0 else []))
            tbl_span[tbl] = (x_lo, x_hi)

    def _eff_box(e):
        b = e.src["bbox"]
        if e.kind != "schedule_row" or e.src.get("coords") == "unrotated": return tuple(b)
        return _snap_row_box(h_rules or _page_h_rules(pg), b, tbl_span.get(e.get("table")))

    eff_boxes = {e.id: _eff_box(e) for e in same if e.src.get("bbox")}
    xs0, ys0, xs1, ys1 = [], [], [], []
    for e in same:
        b = eff_boxes[e.id]
        xs0.append(b[0]); xs1.append(b[2]); ys0.append(b[1]); ys1.append(b[3])
    for e in rows:
        if e.geom:
            xs0.append(min(p[0] for p in e.geom)); xs1.append(max(p[0] for p in e.geom))
            ys0.append(min(p[1] for p in e.geom)); ys1.append(max(p[1] for p in e.geom))
    if not xs0: return None

    clip = pymupdf.Rect(
        max(0, min(xs0) - 36),
        max(0, min(ys0) - 108),
        min(pg.rect.x1, max(xs1) + 20),
        min(pg.rect.y1, max(ys1) + 20),
    )
    z = min(zoom, 2400 / max(clip.width, clip.height, 1))
    pm = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=clip)
    im = Image.frombytes("RGB", (pm.width, pm.height), pm.samples); d = ImageDraw.Draw(im, "RGBA")
    T = lambda x, y: ((x - clip.x0) * z, (y - clip.y0) * z)

    plans = (ans.get("plan") or {}).get("parts") or [ans.get("plan") or {}]
    used = ({str(p.get("attr")) for p in plans if p.get("attr")}
            | {str(f.get("field")) for p in plans for f in p.get("filters", [])}
            | {str(f) for p in plans for f in (p.get("fields") or [])})
    tbl_meta = {(t.get("page", 1), t["title"]): t for t in st.meta.get("tables", []) if "title" in t}

    for e in rows:
        if e.kind == "schedule_row" and e.src.get("bbox"):
            rx0, ry0, rx1, ry1 = eff_boxes.get(e.id) or _eff_box(e)
            p0, p1 = T(rx0, ry0), T(rx1, ry1)
            d.rectangle([p0, p1], fill=(255, 195, 0, 68), outline=(225, 125, 0, 255), width=3)
            tm = tbl_meta.get((page, e.get("table")))
            cx = e.src.get("cols_x") or []
            if tm and cx:
                for i, col_name in enumerate(tm.get("columns", [])):
                    if col_name in used and i + 1 < len(cx):
                        d.rectangle([T(cx[i], ry0), T(cx[i + 1], ry1)], outline=(220, 30, 30, 255), width=3)
            if len(rows) <= 80:
                ym = (p0[1] + p1[1]) / 2
                d.text((max(2, p0[0] - 5), ym), str(idx_of.get(e.id, "")), fill=(195, 75, 0, 255),
                       font=_font(max(11, min(14, int((p1[1] - p0[1]) * 0.78))), True),
                       anchor="rm", stroke_width=3, stroke_fill=(255, 255, 255))
        else:
            c = COL.get(e.kind, (220, 40, 40))
            if e.src.get("bbox"):
                b = e.src["bbox"]; p0, p1 = T(b[0], b[1]), T(b[2], b[3])
            else:
                gx = [p[0] for p in e.geom]; gy = [p[1] for p in e.geom]
                p0, p1 = T(min(gx), min(gy)), T(max(gx), max(gy))
            d.rectangle([p0, p1], fill=c + (65,), outline=c + (255,), width=3)
            if len(rows) <= 80:
                d.text((p1[0] + 3, p0[1]), str(idx_of.get(e.id, "")), fill=c + (255,), font=FONT_B, anchor="lb", stroke_width=3, stroke_fill=(255, 255, 255))

    tnames = ", ".join(sorted(t for t in tables_used if t)) or os.path.basename(st.path)
    total_ev = len(rows)
    return _png(_banner(im, f"PDF Schedule · Page {page} — {tnames}", f"{total_ev} matched row(s) outlined"))

def _pdf_audit(st, size=1500):
    import pymupdf
    rows = [e for e in st.entities if e.src.get("page") and e.src.get("bbox")]
    if not rows: return None
    page = C.Counter(e.src["page"] for e in rows).most_common(1)[0][0]
    rows = [e for e in rows if e.src["page"] == page]
    doc = pymupdf.open(st.path); pg = doc[page - 1]; pg.remove_rotation()
    x0 = max(0, min(e.src["bbox"][0] for e in rows) - 30)
    y0 = max(0, min(e.src["bbox"][1] for e in rows) - 110)
    x1 = min(pg.rect.x1, max(e.src["bbox"][2] for e in rows) + 30)
    y1 = min(pg.rect.y1, max(e.src["bbox"][3] for e in rows) + 30)
    clip = pymupdf.Rect(x0, y0, x1, y1)
    z = min(1.8, size / max(clip.width, clip.height, 1))
    pm = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=clip)
    im = Image.frombytes("RGB", (pm.width, pm.height), pm.samples); d = ImageDraw.Draw(im, "RGBA")
    T = lambda x, y: ((x - clip.x0) * z, (y - clip.y0) * z)
    by_tbl = C.defaultdict(list)
    for e in rows: by_tbl[e.get("table") or "table"].append(e)
    palette = [(36, 87, 214), (18, 128, 92), (195, 95, 15), (150, 40, 200)]
    for idx, (tbl, tr) in enumerate(by_tbl.items()):
        c = palette[idx % len(palette)]
        tx0, ty0 = min(e.src["bbox"][0] for e in tr), min(e.src["bbox"][1] for e in tr)
        tx1, ty1 = max(e.src["bbox"][2] for e in tr), max(e.src["bbox"][3] for e in tr)
        for e in tr:
            b = e.src["bbox"]
            d.rectangle([T(tx0, b[1]), T(tx1, b[3])], outline=c + (110,), width=1)
        p0, p1 = T(tx0 - 3, ty0 - 3), T(tx1 + 3, ty1 + 3)
        d.rectangle([p0, p1], outline=c + (255,), width=3)
        d.text((p0[0] + 4, max(2, p0[1] - 4)), f"{tbl} ({len(tr)} rows)", fill=c + (255,), font=FONT_B, anchor="lb", stroke_width=3, stroke_fill=(255, 255, 255))
    return _png(im)

# ---------------------------------------------------------------------------
# IFC storey plan overlays: Route A (SVG data-guid) + Route B (2D footprints)
# ---------------------------------------------------------------------------

_DECOMP_MEM = {}
def _decomp_parent_map(ifc_path):
    """Map child GlobalId -> ancestor GlobalId across IsDecomposedBy relations (e.g. IfcStairFlight -> IfcStair)."""
    key = (os.path.abspath(ifc_path), os.path.getmtime(ifc_path))
    if key in _DECOMP_MEM: return _DECOMP_MEM[key]
    out = {}
    try:
        import ifcopenshell
        m = ifcopenshell.open(ifc_path)
        for rel in m.by_type("IfcRelAggregates"):
            p = getattr(rel, "RelatingObject", None)
            if p is None or not hasattr(p, "GlobalId") or p.is_a("IfcSpatialStructureElement") or p.is_a("IfcProject"):
                continue
            pg = p.GlobalId
            stack = list(rel.RelatedObjects or [])
            while stack:
                ch = stack.pop()
                if hasattr(ch, "GlobalId"):
                    out[ch.GlobalId] = pg
                for r2 in getattr(ch, "IsDecomposedBy", []) or []:
                    stack.extend(r2.RelatedObjects or [])
    except Exception:
        pass
    if len(_DECOMP_MEM) > 8: _DECOMP_MEM.clear()
    _DECOMP_MEM[key] = out
    return out

def _slug(s): return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")

_PATH_TOK = re.compile(r"([MLAZmlaz])|(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)", re.I)
def _svg_elem_pts(el):
    pts = []
    for ch in el.iter():
        tag = ch.tag.split("}")[-1]
        if tag == "path" and ch.get("d"):
            cmd = None; buf = []
            for m in _PATH_TOK.finditer(ch.get("d")):
                if m.group(1):
                    cmd = m.group(1).upper(); buf.clear()
                else:
                    buf.append(float(m.group(2)))
                    if cmd in ("M", "L") and len(buf) == 2:
                        pts.append((buf[0], buf[1])); buf.clear()
                    elif cmd == "A" and len(buf) == 7:
                        pts.append((buf[5], buf[6])); buf.clear()
        elif tag == "circle" and ch.get("cx") and ch.get("cy"):
            cx, cy, r = float(ch.get("cx")), float(ch.get("cy")), float(ch.get("r") or 0)
            pts.extend([(cx - r, cy - r), (cx + r, cy + r)])
    return pts

def _plan_level_filters(ans):
    plans = (ans.get("plan") or {}).get("parts") or [ans.get("plan") or {}]
    out = set()
    for p in plans:
        for f in p.get("filters", []):
            if f.get("field") == "level" and f.get("value") is not None:
                v = f.get("value")
                out.update(v if isinstance(v, list) else [v])
    return {str(x) for x in out}

def _ifc(st, ans, size=1300, req_path=None):
    ev_order = list(dict.fromkeys(ans.get("evidence") or []))
    idx_of = {eid: i + 1 for i, eid in enumerate(ev_order)}
    by_id = {e.id: e for e in st.entities}
    ev_ents = [by_id[i] for i in ev_order if i in by_id and by_id[i].kind not in NON_SPATIAL_IFC]
    if not ev_ents: return None
    vals = _trace_vals(ans)
    doubt = {x for c in (ans.get("trust") or {}).get("checks", []) if c["result"] in ("fail", "caution") for x in c.get("ids", [])}

    png = _ifc_svg(st, ans, ev_ents, by_id, idx_of, vals, doubt, size, req_path)
    if png is not None: return png
    return _ifc_footprints(st, ans, ev_ents, idx_of, vals, doubt, size, req_path)

def _ifc_svg(st, ans, ev_ents, by_id, idx_of, vals, doubt, size, req_path):
    base = st.path[:-4] if st.path.lower().endswith(".ifc") else st.path.rsplit(".", 1)[0]
    svgs = sorted(glob.glob(f"{base}_plan*.svg"))
    req_svg = None
    if req_path:
        cand = req_path.rsplit(".", 1)[0] + ".svg"
        if os.path.exists(cand) and "_plan" in os.path.basename(cand):
            req_svg = os.path.abspath(cand)
            if req_svg not in [os.path.abspath(x) for x in svgs]: svgs.append(req_svg)
    if not svgs: return None

    svg_txts = {s: open(s, "r", encoding="utf-8", errors="ignore").read() for s in svgs}
    svg_guids = {s: set(re.findall(r'data-guid="([^"]+)"', txt)) for s, txt in svg_txts.items()}
    all_svg_guids = set().union(*svg_guids.values())

    ev_ids = {e.id for e in ev_ents}
    guid_to_ev = {eid: eid for eid in ev_ids if eid in all_svg_guids}
    if len(guid_to_ev) < len(ev_ids):
        pmap = _decomp_parent_map(st.path)
        for cg, pg in pmap.items():
            if pg in ev_ids and cg in all_svg_guids:
                guid_to_ev[cg] = pg

    if not guid_to_ev: return None

    want_lvls = {_slug(x) for x in _plan_level_filters(ans)}
    best_svg, best_matched = None, set()
    if req_svg:
        for s in svgs:
            if os.path.abspath(s) == req_svg and not want_lvls:
                m = {guid_to_ev[g] for g in svg_guids[s] if g in guid_to_ev}
                if m: best_svg, best_matched = s, m; break
    if best_svg is None:
        scored = []
        for idx, s in enumerate(svgs):
            m = {guid_to_ev[g] for g in svg_guids[s] if g in guid_to_ev}
            if not m: continue
            slug_part = re.sub(r"^.*_plan-\d+-", "", os.path.basename(s)[:-4])
            lvl_bonus = 1 if slug_part in want_lvls else 0
            scored.append((len(m), lvl_bonus, -idx, s, m))
        if not scored: return None
        scored.sort(reverse=True)
        _, _, _, best_svg, best_matched = scored[0]

    import pymupdf
    root = ET.fromstring(svg_txts[best_svg])
    vb = [float(x) for x in root.get("viewBox").replace(",", " ").split()]
    vx, vy, vw, vh = vb
    sw_room = max(vw, vh) * 0.0028
    sw_elem = max(vw, vh) * 0.0034

    overlay_g = ET.Element(f"{SVG_NS}g", {"id": "bp-evidence-overlay"})
    ev_boxes = C.defaultdict(list)
    for g in root.iter():
        guid = g.get("data-guid")
        if not guid or guid not in guid_to_ev: continue
        eid = guid_to_ev[guid]
        ent = by_id[eid]
        c = COL.get(ent.kind, (220, 40, 40))
        rgb = f"rgb({c[0]},{c[1]},{c[2]})"
        stroke_col = "#dc1e1e" if eid in doubt else rgb
        clone = copy.deepcopy(g)
        orig_fill = (clone.get("fill") or "").strip().lower()
        if ent.kind == "room":
            clone.set("fill", rgb)
            clone.set("fill-opacity", "0.42")
            clone.set("stroke", stroke_col)
            clone.set("stroke-width", f"{sw_room * (1.4 if eid in doubt else 1.0):.4f}")
        else:
            if orig_fill == "none":
                clone.set("fill", "none")
            else:
                clone.set("fill", rgb)
                clone.set("fill-opacity", "0.62")
            clone.set("stroke", stroke_col)
            clone.set("stroke-width", f"{sw_elem * (1.35 if eid in doubt else 1.0):.4f}")
            clone.set("stroke-linecap", "round")
            clone.set("stroke-linejoin", "round")
        overlay_g.append(clone)
        ev_boxes[eid].extend(_svg_elem_pts(g))

    # Insert a gentle white veil over the plan geometry (before footer text) so highlights stand out sharply
    veil = ET.Element(f"{SVG_NS}rect", {"x": f"{vx}", "y": f"{vy}", "width": f"{vw}", "height": f"{vh}", "fill": "#ffffff", "fill-opacity": "0.44"})
    children = list(root)
    ins_at = next((i + 1 for i, ch in enumerate(children) if ch.tag.endswith("g")), len(children))
    root.insert(ins_at, veil)
    root.insert(ins_at + 1, overlay_g)

    svg_bytes = ET.tostring(root, encoding="utf-8")
    doc = pymupdf.open(stream=svg_bytes, filetype="svg")
    pg = doc[0]
    z = size / max(pg.rect.width, pg.rect.height, 1)
    pm = pg.get_pixmap(matrix=pymupdf.Matrix(z, z))
    im = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
    d = ImageDraw.Draw(im, "RGBA")
    P = lambda x, y: ((x - vx) / vw * pm.width, (y - vy) / vh * pm.height)

    ordered_matched = [e for e in ev_ents if e.id in best_matched]
    for ent in ordered_matched:
        pts = ev_boxes.get(ent.id)
        if not pts: continue
        pxs = [P(x, y) for x, y in pts]
        bx0, by0 = min(p[0] for p in pxs), min(p[1] for p in pxs)
        bx1, by1 = max(p[0] for p in pxs), max(p[1] for p in pxs)
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        c = COL.get(ent.kind, (220, 40, 40))
        if ent.kind != "room" and max(bx1 - bx0, by1 - by0) < 15:
            r = 8
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c + (235,), width=3, fill=c + (45,))
        if ent.kind == "room" and len(ordered_matched) <= 50:
            lbl = str(ent.get("number") or ent.get("label") or "")[:22]
            if ent.id in vals: lbl = (lbl + chr(10) + f"{vals[ent.id]:g}").strip()
            if lbl:
                d.multiline_text((cx, cy), lbl, fill=(20, 20, 20), font=_font(12, True), anchor="mm", align="center",
                                 stroke_width=3, stroke_fill=(255, 255, 255))
        elif ent.kind != "room" and len(ordered_matched) <= 65:
            tag = str(idx_of[ent.id])
            if len(ordered_matched) <= 8 and ent.id in vals: tag += f" ({vals[ent.id]:g})"
            d.text((bx1 + 3, by0 - 1), tag, fill=c + (255,), font=_font(13, True), anchor="lb",
                   stroke_width=3, stroke_fill=(255, 255, 255))

    m_title = re.search(r"Floor plan:\s*([^<]+)</text>", svg_txts[best_svg])
    storey_lbl = m_title.group(1).strip() if m_title else (
        next((e.get("level") for e in ordered_matched if e.get("level")), None)
        or re.sub(r"^.*_plan-\d+-", "", os.path.basename(best_svg)[:-4]))
    n_on, n_tot = len(ordered_matched), len(ev_ents)
    n_lvls = len({e.get("level") for e in ev_ents if e.get("level")})
    right = (f"{n_on} highlighted element(s) · SVG data-guid" if n_on == n_tot
             else f"{n_on} of {n_tot} matched elements on this storey ({ max(2, n_lvls) } storeys total) · SVG data-guid")
    return _png(_banner(im, f"IFC Storey Plan: {storey_lbl}", right))

def _ifc_audit(st, size=1500):
    base = st.path[:-4] if st.path.lower().endswith(".ifc") else st.path.rsplit(".", 1)[0]
    svgs = sorted(glob.glob(f"{base}_plan*.svg"))
    if svgs:
        import pymupdf
        pick = svgs[min(1, len(svgs) - 1)] if len(svgs) > 2 else svgs[0]
        doc = pymupdf.open(pick); pg = doc[0]
        z = size / max(pg.rect.width, pg.rect.height, 1)
        pm = pg.get_pixmap(matrix=pymupdf.Matrix(z, z))
        im = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
        lbl = re.sub(r"^.*_plan-\d+-", "", os.path.basename(pick)[:-4])
        return _png(_banner(im, f"IFC Storey Plan: {lbl}", f"{len(svgs)} companion plan SVG(s) available"))
    rooms = st.of("room") or st.of("wall")
    if not rooms: return None
    fake_ans = {"evidence": [e.id for e in rooms], "trace": [], "plan": {}}
    return _ifc_footprints(st, fake_ans, rooms, {e.id: i + 1 for i, e in enumerate(rooms)}, {}, set(), size, None)

def _local_profile_pts(prof):
    try:
        if prof.is_a("IfcRectangleProfileDef"):
            hx, hy = prof.XDim / 2, prof.YDim / 2
            pts = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
        elif prof.is_a("IfcCircleProfileDef"):
            r = prof.Radius
            pts = [(r * math.cos(2 * math.pi * k / 12), r * math.sin(2 * math.pi * k / 12)) for k in range(12)]
        elif prof.is_a("IfcIShapeProfileDef"):
            hx, hy = prof.OverallWidth / 2, prof.OverallDepth / 2
            pts = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
        elif prof.is_a("IfcArbitraryClosedProfileDef"):
            from .ifc_qty import _curve_pts
            pts = _curve_pts(prof.OuterCurve) or []
        else:
            pts = []
        if pts and getattr(prof, "Position", None) is not None:
            import ifcopenshell.util.placement as UP
            P2 = UP.get_axis2placement(prof.Position)
            pts = [(P2[0, 0] * x + P2[0, 1] * y + P2[0, 3], P2[1, 0] * x + P2[1, 1] * y + P2[1, 3]) for x, y in pts]
        return pts
    except Exception:
        return []

def _fast_ifc_footprint(el, ls):
    """Pure-Python 2D world footprint (in metres) from ObjectPlacement + ExtrudedAreaSolid / Axis / BoundingBox.
    Runs in ~0.1 ms per element with no C++ geometry kernel."""
    import numpy as np, ifcopenshell.util.placement as UP
    from shapely.geometry import MultiPoint, Polygon
    if not getattr(el, "ObjectPlacement", None): return None
    try:
        M = UP.get_local_placement(el.ObjectPlacement).copy()
        M[:3, 3] *= ls
    except Exception:
        return None

    polys, segs = [], []
    rep = getattr(el, "Representation", None)
    if rep:
        stack = []
        for r in rep.Representations or []:
            for it in r.Items or []:
                stack.append((it, M))
        while stack:
            it, Tcur = stack.pop()
            try:
                if it.is_a("IfcMappedItem"):
                    ms = it.MappingSource
                    O = UP.get_axis2placement(ms.MappingOrigin).copy(); O[:3, 3] *= ls
                    mt = it.MappingTarget
                    if mt is not None:
                        # IfcCartesianTransformationOperator3D
                        sc = getattr(mt, "Scale", None) or 1.0
                        tr = np.eye(4)
                        if getattr(mt, "LocalOrigin", None):
                            tr[:3, 3] = [c * ls for c in mt.LocalOrigin.Coordinates[:3]]
                        tr[:3, :3] *= sc
                        Tnext = Tcur @ tr @ O
                    else:
                        Tnext = Tcur @ O
                    for sub in ms.MappedRepresentation.Items or []:
                        stack.append((sub, Tnext))
                elif it.is_a("IfcBooleanResult"):
                    stack.append((it.FirstOperand, Tcur))
                elif it.is_a("IfcExtrudedAreaSolid"):
                    E = np.eye(4)
                    if getattr(it, "Position", None) is not None:
                        E = UP.get_axis2placement(it.Position).copy(); E[:3, 3] *= ls
                    W = Tcur @ E
                    pts2 = _local_profile_pts(it.SweptArea)
                    d3 = np.array(it.ExtrudedDirection.DirectionRatios[:3], dtype=float)
                    dn = np.linalg.norm(d3)
                    if dn > 0: d3 = d3 / dn
                    depth_m = float(it.Depth) * ls
                    wd = W[:3, :3] @ d3
                    if pts2:
                        base_xy = [(float(W[0, 0] * x * ls + W[0, 1] * y * ls + W[0, 3]),
                                    float(W[1, 0] * x * ls + W[1, 1] * y * ls + W[1, 3])) for x, y in pts2]
                        if abs(wd[2]) > 0.94 and len(base_xy) >= 3:
                            polys.append(base_xy)
                        else:
                            top_xy = [(bx + float(wd[0] * depth_m), by + float(wd[1] * depth_m)) for bx, by in base_xy]
                            hull = MultiPoint(base_xy + top_xy).convex_hull
                            if isinstance(hull, Polygon):
                                polys.append(list(hull.exterior.coords)[:-1])
                            else:
                                segs.append( list(hull.coords) )
                    else:
                        p0 = (float(W[0, 3]), float(W[1, 3]))
                        p1 = (float(W[0, 3] + wd[0] * depth_m), float(W[1, 3] + wd[1] * depth_m))
                        segs.append([p0, p1] if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) > 1e-3 else [p0])
                elif it.is_a("IfcBoundingBox"):
                    x0, y0 = it.Corner.Coordinates[0] * ls, it.Corner.Coordinates[1] * ls
                    x1, y1 = x0 + it.XDim * ls, y0 + it.YDim * ls
                    box = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                    polys.append([(float(Tcur[0, 0] * x + Tcur[0, 1] * y + Tcur[0, 3]),
                                   float(Tcur[1, 0] * x + Tcur[1, 1] * y + Tcur[1, 3])) for x, y in box])
                elif it.is_a("IfcPolyline"):
                    pts = [(p.Coordinates[0] * ls, p.Coordinates[1] * ls) for p in it.Points]
                    if len(pts) >= 2:
                        segs.append([(float(Tcur[0, 0] * x + Tcur[0, 1] * y + Tcur[0, 3]),
                                      float(Tcur[1, 0] * x + Tcur[1, 1] * y + Tcur[1, 3])) for x, y in pts])
            except Exception:
                continue

    if polys: return ("polys", polys)
    if segs: return ("segs", segs)
    w = getattr(el, "OverallWidth", None)
    if w and w * ls > 0.05:
        hw = float(w) * ls / 2; ht = 0.08
        box = [(-hw, -ht), (hw, -ht), (hw, ht), (-hw, ht)]
        return ("polys", [[(float(M[0, 0] * x + M[0, 1] * y + M[0, 3]),
                            float(M[1, 0] * x + M[1, 1] * y + M[1, 3])) for x, y in box]])
    return ("pt", [(float(M[0, 3]), float(M[1, 3]))])

def _storey_footprints(ifc_path, target_level, want_ids):
    """Return {guid: ('polys'|'segs'|'pt', list)} in world metres, cached on disk per (ifc_path, target_level)."""
    os.makedirs(FP_CACHE, exist_ok=True)
    ck = hashlib.sha1(f"v1:{os.path.abspath(ifc_path)}:{os.path.getmtime(ifc_path)}:{target_level}".encode()).hexdigest()[:16]
    pk = os.path.join(FP_CACHE, ck + ".pkl")
    cached = {}
    if os.path.exists(pk):
        try: cached = pickle.load(open(pk, "rb"))
        except Exception: cached = {}
    missing = [g for g in want_ids if g not in cached]
    if not missing: return cached

    import ifcopenshell, ifcopenshell.geom, ifcopenshell.util.unit as UU, numpy as np
    from shapely.geometry import MultiPoint, Polygon
    from shapely.ops import unary_union

    m = ifcopenshell.open(ifc_path)
    ls = UU.calculate_unit_scale(m)
    ifc_arch, ifc_fast = [], []
    for g in missing:
        try: el = m.by_guid(g)
        except Exception: continue
        if el.is_a("IfcDistributionElement"):
            ifc_fast.append(el)
        else:
            ifc_arch.append(el)

    if len(ifc_arch) > 320:
        ifc_fast.extend(ifc_arch[320:])
        ifc_arch = ifc_arch[:320]

    if ifc_arch:
        gs = ifcopenshell.geom.settings()
        gs.set("use-world-coords", True)
        gs.set("disable-boolean-result", True)
        gs.set("disable-opening-subtractions", True)
        gs.set("no-wire-intersection-check", True)
        gs.set("no-normals", True)
        gs.set("circle-segments", 8)
        gs.set("mesher-linear-deflection", 0.08)
        try:
            it = ifcopenshell.geom.iterator(gs, m, 16, include=ifc_arch)
            if it.initialize():
                while True:
                    sh = it.get()
                    v = np.array(sh.geometry.verts, dtype=float).reshape(-1, 3)
                    f = np.array(sh.geometry.faces, dtype=int).reshape(-1, 3)
                    xy = v[:, :2]
                    entry = None
                    if len(f) > 0:
                        tris = xy[f]
                        areas = np.abs((tris[:, 1, 0] - tris[:, 0, 0]) * (tris[:, 2, 1] - tris[:, 0, 1])
                                       - (tris[:, 2, 0] - tris[:, 0, 0]) * (tris[:, 1, 1] - tris[:, 0, 1]))
                        keep = tris[areas > 1e-5]
                        if 0 < len(keep) <= 150:
                            u = unary_union([Polygon(t) for t in keep]).buffer(0)
                            geoms = list(u.geoms) if hasattr(u, "geoms") else [u]
                            ps = [list(p.exterior.coords)[:-1] for p in geoms if isinstance(p, Polygon) and len(p.exterior.coords) >= 4]
                            if ps: entry = ("polys", ps)
                        if entry is None and len(xy) >= 3:
                            hull = MultiPoint(xy[::max(1, len(xy) // 200)]).convex_hull
                            if isinstance(hull, Polygon):
                                entry = ("polys", [list(hull.exterior.coords)[:-1]])
                            elif hasattr(hull, "coords") and len(hull.coords) >= 2:
                                entry = ("segs", [list(hull.coords)])
                    if entry is None and len(xy) >= 1:
                        entry = ("pt", [(float(xy[:, 0].mean()), float(xy[:, 1].mean()))])
                    if entry is not None:
                        cached[sh.guid] = entry
                    if not it.next(): break
        except Exception:
            pass

    for el in ifc_arch:
        if el.GlobalId not in cached:
            ifc_fast.append(el)
    for el in ifc_fast:
        fp = _fast_ifc_footprint(el, ls)
        if fp is not None:
            cached[el.GlobalId] = fp

    try: pickle.dump(cached, open(pk, "wb"))
    except Exception: pass
    return cached

def _pick_target_storey(st, ans, ev_ents, req_path=None):
    ev_lvls = C.Counter(e.get("level") for e in ev_ents if e.get("level") is not None)
    if not ev_lvls: return None
    want = _plan_level_filters(ans)
    for w in want:
        for lv in ev_lvls:
            if lv.lower() == w.lower(): return lv
    if req_path and "_plan-" in os.path.basename(req_path):
        slug_part = re.sub(r"^.*_plan-\d+-", "", os.path.basename(req_path).rsplit(".", 1)[0])
        for lv in ev_lvls:
            if _slug(lv) == slug_part: return lv
    elev_of = {e.get("name"): (e.get("elevation") if e.get("elevation") is not None else 999.0) for e in st.of("level")}
    return max(ev_lvls, key=lambda lv: (ev_lvls[lv], -abs(elev_of.get(lv, 999.0))))

def _ifc_footprints(st, ans, ev_ents, idx_of, vals, doubt, size, req_path):
    from shapely.geometry import Polygon
    target_level = _pick_target_storey(st, ans, ev_ents, req_path)
    hl_ents = [e for e in ev_ents if (target_level is None or e.get("level") == target_level)]
    if not hl_ents: hl_ents = ev_ents
    hl_ids = {e.id for e in hl_ents}

    ctx_kinds = ("slab", "room", "wall", "column", "stair", "railing", "door", "window", "curtainwall", "ramp")
    ctx_ents = [e for e in st.entities if e.id not in hl_ids and e.kind in ctx_kinds
                and (e.get("level") == target_level if target_level is not None else True)]
    if len(ctx_ents) > 350:
        prio = {"room": 0, "wall": 1, "door": 2, "window": 3, "column": 4, "stair": 5, "slab": 6}
        ctx_ents = sorted(ctx_ents, key=lambda e: prio.get(e.kind, 9))[:350]

    want_ids = [e.id for e in hl_ents] + [e.id for e in ctx_ents]
    fps = _storey_footprints(st.path, target_level, want_ids)
    if not any(e.id in fps for e in hl_ents): return None

    def _pts_of(eid):
        fp = fps.get(eid)
        if not fp: return []
        gt, data = fp
        return data if gt == "pt" else [p for sub in data for p in sub]

    hl_pts = [p for e in hl_ents for p in _pts_of(e.id)]
    all_pts = hl_pts + [p for e in ctx_ents for p in _pts_of(e.id)]
    if not hl_pts: return None

    hx0, hx1 = min(x for x, _ in hl_pts), max(x for x, _ in hl_pts)
    hy0, hy1 = min(y for _, y in hl_pts), max(y for _, y in hl_pts)
    ax0, ax1 = min(x for x, _ in all_pts), max(x for x, _ in all_pts)
    ay0, ay1 = min(y for _, y in all_pts), max(y for _, y in all_pts)
    h_span = max(hx1 - hx0, hy1 - hy0, 4.0)
    if max(ax1 - ax0, ay1 - ay0) > 5.0 * h_span and max(ax1 - ax0, ay1 - ay0) > 80.0:
        # Guard against distant site/survey origin geometry dwarfing the building plan
        x0, x1 = max(ax0, hx0 - h_span), min(ax1, hx1 + h_span)
        y0, y1 = max(ay0, hy0 - h_span), min(ay1, hy1 + h_span)
    else:
        x0, x1, y0, y1 = ax0, ax1, ay0, ay1

    span = max(x1 - x0, y1 - y0, 1.0)
    pad = max(0.8, span * 0.05)
    s = (size - 80) / max(x1 - x0 + 2 * pad, y1 - y0 + 2 * pad)
    W = max(420, int((x1 - x0 + 2 * pad) * s) + 80)
    H = max(320, int((y1 - y0 + 2 * pad) * s) + 80)
    T = lambda pts: [((x - x0 + pad) * s + 40, H - 40 - (y - y0 + pad) * s) for x, y in pts]

    im = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(im, "RGBA")

    # Background context ordered from floor slabs/rooms up to walls & openings
    order_bg = {"slab": 0, "room": 1, "stair": 2, "ramp": 2, "railing": 3, "wall": 4, "curtainwall": 4, "column": 5, "door": 6, "window": 6}
    for e in sorted(ctx_ents, key=lambda x: order_bg.get(x.kind, 7)):
        fp = fps.get(e.id)
        if not fp: continue
        gt, data = fp
        if gt == "polys":
            for poly in data:
                P = T(poly)
                if len(P) < 3: continue
                if e.kind == "slab":
                    d.polygon(P, fill=(247, 248, 250, 255), outline=(218, 222, 228, 255))
                elif e.kind == "room":
                    d.polygon(P, fill=(244, 247, 252, 180), outline=(195, 202, 212, 255))
                elif e.kind in ("wall", "column", "curtainwall"):
                    d.polygon(P, fill=(210, 214, 220, 255), outline=(145, 150, 160, 255))
                else:
                    d.polygon(P, outline=(165, 170, 178, 255))
        elif gt == "segs":
            for seg in data:
                if len(seg) >= 2: d.line(T(seg), fill=(165, 170, 178, 255), width=2)

    # Faint context room labels
    for e in ctx_ents:
        if e.kind != "room" or e.id not in fps or fps[e.id][0] != "polys": continue
        lbl = str(e.get("number") or e.get("label") or "")[:16]
        if not lbl: continue
        poly = max(fps[e.id][1], key=len)
        if len(poly) >= 3:
            rp = Polygon(poly).buffer(0).representative_point()
            (cx, cy), = T([(rp.x, rp.y)])
            d.text((cx, cy), lbl, fill=(160, 165, 175), font=_font(11), anchor="mm")

    # Highlighted foreground elements
    order_fg = {"slab": 0, "room": 1, "wall": 2, "stair": 3, "beam": 3, "column": 4, "window": 5, "door": 6}
    centers = {}
    for e in sorted(hl_ents, key=lambda x: order_fg.get(x.kind, 7)):
        fp = fps.get(e.id)
        if not fp: continue
        gt, data = fp
        c = COL.get(e.kind, (220, 40, 40))
        out_col = (220, 30, 30, 255) if e.id in doubt else c + (255,)
        px_all = []
        if gt == "polys":
            for poly in data:
                P = T(poly); px_all.extend(P)
                if len(P) >= 3:
                    alpha = 95 if e.kind in ("room", "slab") else 165
                    d.polygon(P, fill=c + (alpha,), outline=out_col, width=4 if e.id in doubt else 3)
                elif len(P) == 2:
                    d.line(P, fill=out_col, width=4)
        elif gt == "segs":
            for seg in data:
                P = T(seg); px_all.extend(P)
                if len(P) >= 2: d.line(P, fill=out_col, width=4)
        elif gt == "pt":
            P = T(data); px_all.extend(P)
            (cx, cy) = P[0]; r = 8
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c + (120,), outline=out_col, width=3)

        if px_all:
            bx0, by0 = min(p[0] for p in px_all), min(p[1] for p in px_all)
            bx1, by1 = max(p[0] for p in px_all), max(p[1] for p in px_all)
            if e.kind == "room" and gt == "polys" and len(data[0]) >= 3:
                rp = Polygon(max(data, key=len)).buffer(0).representative_point()
                (cx, cy), = T([(rp.x, rp.y)])
            else:
                cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
            centers[e.id] = (cx, cy, bx1, by0, max(bx1 - bx0, by1 - by0))
            if e.kind != "room" and max(bx1 - bx0, by1 - by0) < 12:
                r = 8
                d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=out_col, width=3, fill=c + (50,))

    # Labels & evidence numbers
    for e in hl_ents:
        if e.id not in centers: continue
        cx, cy, bx1, by0, bspan = centers[e.id]
        c = COL.get(e.kind, (220, 40, 40))
        if e.kind == "room" and len(hl_ents) <= 55:
            lbl = str(e.get("number") or e.get("label") or "")[:22]
            if e.id in vals: lbl = (lbl + chr(10) + f"{vals[e.id]:g}").strip()
            if lbl:
                d.multiline_text((cx, cy), lbl, fill=(15, 15, 15), font=_font(12, True), anchor="mm", align="center",
                                 stroke_width=3, stroke_fill=(255, 255, 255))
        elif e.kind != "room" and len(hl_ents) <= 65:
            tag = str(idx_of[e.id])
            if len(hl_ents) <= 8 and e.id in vals: tag += f" ({vals[e.id]:g})"
            d.text((bx1 + 4, by0 - 1), tag, fill=c + (255,), font=_font(13, True), anchor="lb",
                   stroke_width=3, stroke_fill=(255, 255, 255))

    # Metric scale bar in bottom-right corner
    bar_m = next((m_len for m_len in (1, 2, 5, 10, 20, 50) if 55 <= m_len * s <= 190), 5)
    bw_px = int(bar_m * s)
    if 30 <= bw_px <= W - 60:
        sx0, sy0 = W - bw_px - 24, H - 18
        d.line([(sx0, sy0), (sx0 + bw_px, sy0)], fill=(40, 45, 55, 255), width=3)
        d.line([(sx0, sy0 - 4), (sx0, sy0 + 4)], fill=(40, 45, 55, 255), width=2)
        d.line([(sx0 + bw_px, sy0 - 4), (sx0 + bw_px, sy0 + 4)], fill=(40, 45, 55, 255), width=2)
        d.text((sx0 + bw_px / 2, sy0 - 5), f"{bar_m} m", fill=(40, 45, 55, 255), font=_font(11, True),
               anchor="mb", stroke_width=2, stroke_fill=(255, 255, 255))

    storey_lbl = target_level or "All Storeys"
    n_on, n_tot = sum(1 for e in hl_ents if e.id in centers), len(ev_ents)
    n_lvls = len({e.get("level") for e in ev_ents if e.get("level")})
    right = (f"{n_on} highlighted element(s) · 2D footprints" if n_on == n_tot
             else f"{n_on} of {n_tot} matched elements on this storey ({max(2, n_lvls)} storeys total) · 2D footprints")
    return _png(_banner(im, f"IFC Storey Plan: {storey_lbl}", right))
