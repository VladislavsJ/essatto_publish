"""Extractor: raster floor plan (PNG/JPG) -> Store.
- symbols (door, window, toilet, washbasin, shower/bath) from a CACHED vision detection file (Opus, tiled protocol);
  coordinates are fractions of the image. Without a detection file the store has no symbols and questions about
  them are refused, unless on-demand detection is enabled (BP_DETECT=1).
- room labels from OCR (RapidOCR cache), mapped to room types with the vocabulary; printed areas next to labels
  (e.g. 'MH 14.4') become room areas with the OCR box as evidence."""
import os, re, json
from PIL import Image
from .core import Entity, Store
from .vocab import rtype_of_label

HERE = os.path.dirname(__file__)
DET = os.path.join(HERE, "..", "cache", "detections")
DET2 = os.path.join(HERE, "..", "cache", "detections_yolo")      # boxes from the small trained detector (preferred)
OCR = os.path.join(HERE, "..", "cache", "ocr_cc.json")
KIND = {"door": ("door", None), "window": ("window", None), "toilet": ("toilet", None), "sink": ("washbasin", None), "shower": ("shower", None),
        "bathtub": ("bathtub", None), "floor": ("floor", None), "bedroom": ("room", "bedroom"), "kitchen": ("room", "kitchen"), "bathroom": ("room", "bathroom"),
        "wc_room": ("room", "wc"), "sauna_room": ("room", "sauna"), "balcony": ("room", "outdoor")}
MARGIN = 0.12      # a detection this close to its class threshold (either side) is 'borderline': it could flip the count

def _from_detector(st, yf, W, H, name):
    """Entities from the trained detector's boxes. Every box keeps its confidence and the class threshold, so the
    trace can show which detections were kept, and the trust layer can see detections that were nearly kept or nearly dropped."""
    d = json.load(open(yf)); th = d["thresholds"]; n = {}
    st.meta["supports"] = ["door", "window", "toilet", "washbasin", "shower", "bathtub", "room", "floor", "room_label"]
    st.meta["detections"] = f"trained detector {d.get('model')} (boxes with confidence; one threshold per class tuned for exact counts on held-out plans)"
    st.meta["detector"] = {"model": d.get("model"), "thresholds": th, "split": d.get("split")}
    border = {}
    geo = _room_geometry(st, name)
    for cls, conf, x0, y0, x1, y1 in d["boxes"]:
        if cls not in KIND: continue
        if geo and KIND[cls][0] == "room": continue      # rooms come from traced wall geometry on this sheet, not from detector boxes
        kind, rt = KIND[cls]; t = th.get(cls, 0.25); key = rt or kind
        if conf < t:
            if conf >= t - MARGIN: border.setdefault(key, []).append({"conf": conf, "box": [x0, y0, x1, y1], "why": "just below the threshold, not counted"})
            continue
        n[key] = n.get(key, 0) + 1
        a = {"conf": conf, "threshold": t, "x": (x0 + x1) / 2 / W, "y": (y0 + y1) / 2 / H}
        if rt: a.update(rtype=rt, label=rt, indoor=rt != "outdoor")
        if conf < t + MARGIN: a["borderline"] = True; border.setdefault(key, []).append({"conf": conf, "box": [x0, y0, x1, y1], "why": "just above the threshold, counted"})
        st.add(Entity(f"{key}:{n[key]}", kind, a, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                      {"detector": d.get("model"), "rule": f"{cls} box from the trained detector, confidence {conf:.2f} (class threshold {t:.2f})"}))
    st.meta["borderline"] = border
    _link_openings(st)
    st.meta["tiling"] = d.get("tiling")
    cc = d.get("crosscheck")
    if cc:      # second reading of a large sheet at another working scale (see the detector worker in ingest.py)
        out = {}
        for cls, v in (cc.get("classes") or {}).items():
            if cls not in KIND: continue
            kind, rt = KIND[cls]; key = rt or kind; o = out.setdefault(key, {"used": 0, "second": 0, "confirmed": 0})
            for k in o: o[k] += v.get(k, 0)
        st.meta["crosscheck"] = {"scale_used": cc.get("scale_used"), "scale_second": cc.get("scale_second"), "keys": out}
    _ocr_labels(st, name)
    return st

def _link_openings(st):
    """give each detected door / window the width measured by the wall tracer and the names of the rooms it joins"""
    ops = st.meta.pop("_openings", None)
    if not ops: return
    for e in st.entities:
        if e.kind not in ("door", "window") or not e.geom or len(e.geom) != 4: continue
        xs = [p[0] for p in e.geom]; ys = [p[1] for p in e.geom]; x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys); m = 0.25 * max(x1 - x0, y1 - y0)
        inside = [o for o in ops if x0 - m <= o["mid"][0] <= x1 + m and y0 - m <= o["mid"][1] <= y1 + m and (o["kind"] == e.kind or (e.kind == "door" and o["kind"] in ("sliding_door", "open_passage")))]
        if not inside: continue
        o = max(inside, key=lambda o: o["width"] or 0)
        e.attrs["width"] = o["width"]; e.attrs["rooms"] = " | ".join(o["rooms"]) or None
        e.attrs["width_rule"] = "gap between the two wall ends at this opening, measured on the traced walls x scale"

SHEET_ROOMS = os.path.join(HERE, "..", "cache", "sheet_rooms")
def _room_geometry(st, name):
    """Rooms traced from the walls of a large sheet (sheet_rooms.py -> cache/sheet_rooms/<stem>/sheet.db).
    Each room is an outline; area and perimeter are computed from it with the scale printed on the sheet."""
    db = os.path.join(SHEET_ROOMS, name[:-4], "sheet.db")
    if not os.path.exists(db): return False
    import sqlite3
    try:
        con = sqlite3.connect(db); sh = con.execute("select drawing_scale, paper_long_mm, m_per_px, scale_source, scale_bar_m_per_px, wall_thickness_m from sheet").fetchone()
        if not sh or not sh[2]: return False
        rows = con.execute("select id, building, name, area_m2, perimeter_m, door_width_m, doors, windows, baseboard_m, polygon_px from room").fetchall()
        if len(rows) < 2: return False
        try: fl = con.execute("select kind, space_a, space_b, room_id from review_flag").fetchall()
        except Exception: fl = []
        flags = len(fl); merged = {}; leaks = [a_ for k_, a_, b_, r_ in fl if k_.startswith("named space")]
        for k_, a_, b_, r_ in fl:
            if k_.startswith("two names") and r_: merged.setdefault(r_, []).append(f"'{a_}' and '{b_}'")
    except Exception: return False
    src = f"scale 1:{sh[0]:g} and paper long side {sh[1]:g} mm read from the sheet"
    if sh[4]: src += f"; the drawn scale bar gives {sh[4] * 1000:.2f} mm per pixel"
    for rid, b, nm, area, per, dw, nd, nw, base, poly in rows:
        rt = rtype_of_label((nm or "").lower()) if nm else None
        two = "; ".join(merged[rid]) if rid in merged else None
        st.add(Entity(f"room:{rid}", "room", {"label": nm or f"room {rid}", "name": nm, "rtype": rt, "indoor": True, "building": b, "area": area, "perimeter": per, **({"merged_suspect": two} if two else {}),
                      "door_width": dw, "doors": nd, "windows": nw, "baseboard": base,
                      "area_rule": f"area of the room outline traced from the walls (wall face to wall face) x scale ({sh[2] * 1000:.2f} mm per pixel)"},
                      [tuple(pt) for pt in json.loads(poly)], {"rule": "room outline traced from wall strokes; door and window gaps closed where the detector found an opening", "db": os.path.basename(os.path.dirname(db))}))
    # doors and windows: the tracer measured each opening between the wall ends and knows which rooms it touches.
    # Kept on the store; _link_openings() copies width and room names onto the detector's door / window entities.
    try:
        names = {r[0]: (r[2] or f"room {r[0]}") for r in rows}
        st.meta["_openings"] = [{"kind": k_, "width": w_, "mid": ((x0 + x1) / 2, (y0 + y1) / 2), "rooms": [names.get(i, f"room {i}") for i in json.loads(rs or "[]")], "room_ids": json.loads(rs or "[]")}
                                for k_, w_, x0, y0, x1, y1, rs in con.execute("select kind, width_m, x0_px, y0_px, x1_px, y1_px, rooms from opening")]
    except Exception: st.meta["_openings"] = []
    ww = {}
    for o in st.meta["_openings"]:
        if o["kind"] in ("window", "glazed_screen"):
            for i in o["room_ids"]: ww[i] = ww.get(i, 0) + (o["width"] or 0)
    for e in st.of("room"):
        try: e.attrs["window_width"] = round(ww.get(int(e.id.split(":")[1]), 0), 3)
        except Exception: pass
    st.meta["dictionary"] = {"room": {"window_width": {"unit": "m", "meaning": "total width of the windows and glazed screens on the room's boundary"}}}
    # SAMPLE sidecar (demo/make_school_demo.py): fire symbols and seat counts that the detector cannot read yet
    side = os.path.join(SHEET_ROOMS, name[:-4], "demo.json")
    if os.path.exists(side):
        try:
            dm = json.load(open(side)); layer = dm.get("layer")
            st.meta["demo"] = {"note": dm.get("note"), "layer": layer, "pupils_total": dm.get("pupils_total"), "building_use": dm.get("building_use")}
            for e in st.of("room"):
                n_ = (dm.get("seats") or {}).get(e.id.split(":")[1])
                if n_: e.attrs["seats"] = int(n_)
            st.meta["dictionary"]["room"]["seats"] = {"unit": "count", "meaning": "number of pupil seats drawn in the room (sample data on this plan)"}
            if layer == "fire":
                for i, dd in enumerate(dm.get("detectors") or [], 1):
                    x, y, r_ = dd["x"], dd["y"], dd.get("r", 30)
                    st.add(Entity(f"fire_detector:{i}", "fire_detector", {"type": dd.get("type"), "room_id": dd.get("room")}, [(x - r_, y - r_), (x + r_, y - r_), (x + r_, y + r_), (x - r_, y + r_)],
                                  {"rule": "fire detector symbol on the fire layout (sample data planted for the demo)"}))
        except Exception: pass
    st.meta["units"] = f"metres ({src})"
    st.meta["room_geometry"] = {"rooms": len(rows), "buildings": len({r[1] for r in rows}), "area_m2": round(sum(r[3] for r in rows), 1), "scale_source": src, "mm_per_px": sh[2] * 1000, "flags": flags, "leaks": leaks}
    return True

def _ocr_labels(st, name):
    for i, b in enumerate(_ocr_boxes(name)):
        t = b["text"].strip(); m = re.match(r"^([A-Za-zÄÖÅäöå+./ ]{1,14})\s*([0-9]{1,3}[.,][0-9])\s*(?:m2|m²)?$", t)
        if m:
            lab = m.group(1).strip(); rt = rtype_of_label(lab.lower())
            st.add(Entity(f"label:{i}", "room_label", {"label": lab, "rtype": rt, "area": float(m.group(2).replace(",", ".")),
                    "area_rule": f"area printed next to the label, read by OCR: '{t}'"}, b["box"], {"ocr_conf": b["conf"]}))
_ocr = None
SYM = {"doors": "door", "windows": "window", "wc_bowls": "toilet", "sinks": "washbasin", "showers_baths": "shower_or_bath"}
ROOM_COUNTS = {"bedrooms": "bedroom", "kitchens": "kitchen", "saunas": "sauna", "bathrooms": "bathroom", "wc_rooms": "wc", "balconies": "outdoor"}

def _ocr_boxes(name):
    global _ocr
    if _ocr is None:
        try: _ocr = json.load(open(OCR))
        except Exception: _ocr = {}
    return (_ocr.get(name) or {}).get("boxes", [])

def extract(path):
    st = Store(path, "raster"); st.meta["supports"] = ["door","window","toilet","washbasin","shower_or_bath","shower","bathtub","room","floor","room_label"]; st.meta["extractor"] = "x_raster"; name = os.path.basename(path)
    W, H = Image.open(path).size; st.meta["image_size"] = [W, H]; st.meta["units"] = "pixels (no scale known)"
    yf = os.path.join(DET2, name[:-4] + ".json")
    if os.path.exists(yf): return _from_detector(st, yf, W, H, name)
    det_f = os.path.join(DET, name + ".json")
    if not os.path.exists(det_f) and os.environ.get("BP_DETECT"):
        from .detect import detect; detect(path, det_f)
    if os.path.exists(det_f):
        d = json.load(open(det_f)); det = d["detections"]; st.meta["detections"] = d.get("protocol")
        for key, kind in SYM.items():
            for i, (x, y) in enumerate(det.get(key, []) or []):
                st.add(Entity(f"{kind}:{i+1}", kind, {"x": x, "y": y}, [(x * W, y * H)], {"detector": "opus-tiled", "file": os.path.basename(det_f), "rule": "symbol located by the vision detector"}))
        # room-type counts reported by the detector (labels it read) - kept as 'roomcount' facts
        for key, rt in ROOM_COUNTS.items():
            if isinstance(det.get(key), int):
                for i in range(det[key]): st.add(Entity(f"room:{rt}:{i+1}", "room", {"rtype": rt, "label": rt, "indoor": rt != "outdoor"}, None, {"detector": "opus-tiled", "rule": f"room counted by the vision detector as {rt}"}))
        if isinstance(det.get("floors"), int):
            for i in range(det["floors"]): st.add(Entity(f"floor:{i+1}", "floor", {}, None, {"detector": "opus-tiled", "rule": "separate floor drawings counted by the vision detector"}))
    else:
        st.meta["warnings"].append("no symbol detections cached for this image (set BP_DETECT=1 to run the detector)")
    # OCR labels with printed areas ------------------------------------------------------------------------------
    for i, b in enumerate(_ocr_boxes(name)):
        t = b["text"].strip(); m = re.match(r"^([A-Za-zÄÖÅäöå+./ ]{1,14})\s*([0-9]{1,3}[.,][0-9])\s*(?:m2|m²)?$", t)
        if m:
            lab = m.group(1).strip(); rt = rtype_of_label(lab.lower())
            st.add(Entity(f"label:{i}", "room_label", {"label": lab, "rtype": rt, "area": float(m.group(2).replace(",", ".")),
                    "area_rule": f"area printed next to the label, read by OCR: '{t}'"}, b["box"], {"ocr_conf": b["conf"]}))
    return st
