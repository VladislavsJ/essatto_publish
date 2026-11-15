"""Extractor: vector PDF sheet -> Store with schedule tables and vector-plan plumbing fixtures as entities.
Algorithms (deterministic, recorded in each entity's provenance):
 1 Ruling-line grid reader (PyMuPDF): horizontal rules split into contiguous x-runs + vertical rule coverage -> exact cell grid;
   multi-row / spanning headers resolved by cell containment; column-type validator checks for +/-1 column shifts.
 2 Text-gap fallback (pdfplumber): baseline-grouped words + header x-bands when no ruling-line grid covers the table region.
 3 Sheet type legends (e.g. DOOR TYPE <CODE>) & multi-wide unit counts: joined deterministically with recorded rules.
 4 Enlarged plan vector fixtures: scale-calibrated viewport detection + continuous curved-path loop signatures -> toilet / washbasin / shower."""
import math, re, collections as C, pdfplumber
from .core import Entity, Store

HDR = {"WIDTH", "HEIGHT", "TYPE", "MARK", "MATERIAL", "MATL", "HARDWARE", "FINISH", "NUMBER", "AREA", "ROOM", "LEVEL", "FIRE",
       "RATING", "NAME", "FLOOR", "BASE", "WALL", "CEILING", "QTY", "COUNT", "DESCRIPTION", "FRAME", "DOOR", "GLAZING", "NOTES",
       "NOTE", "KEYNOTE", "CATEGORY", "COMMENTS", "SIZE", "MANUFACTURER", "MODEL", "REMARKS", "GROUP", "SET", "HW",
       "ELEVATION", "WINDOW", "UNIT", "TAG", "SPECS", "SPEC", "CODE", "KEY", "THK", "THICKNESS", "HGT"}

def _key(s): return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "col"

# ---- Deterministic column-shift detector -------------------------------------------------------------------
_COL_TYPES = {
    "COUNT": re.compile(r"^\d+$"),
    "AREA": re.compile(r"^\d[\d,]*(?:\.\d+)?\s*(?:SF|S\.F\.|SQ\.?\s*FT\.?|M2|M²)?$", re.I),
    "DIM": re.compile(r'^(?:\d+\s*[\'’′]\s*-?\s*\d*(?:\s+\d+/\d+)?\s*(?:"|”|″|\'\')?|\d+(?:[- ]\d+/\d+)?\s*(?:"|”|″|\'\')|\d+(?:\.\d+)?\s*(?:MM|CM|M)|-|--|N/?A|VARIES)$', re.I),
    "NO": re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$", re.I),
    "FINISH": re.compile(r"^(?:[A-Z]{1,4}-?\d{0,2}[A-Z]?(?:\s*(?:&|/|,)\s*[A-Z]{1,4}-?\d{0,2}[A-Z]?)*|-|--|N/?A|ELEV\.?|NONE)$", re.I),
    "RATING": re.compile(r"^(?:\d+\s*(?:MIN\.?|HR\.?|HOURS?)|-|--|NONE|N/?A)$", re.I),
}

def _col_kind(name):
    u = name.upper().strip()
    leaf = re.split(r"[/|:]", u)[-1].strip()
    if leaf in ("COUNT", "QTY", "QUANTITY"): return "COUNT"
    if "AREA" in u: return "AREA"
    if any(k in u for k in ("WIDTH", "HEIGHT", "HGT", "THK", "THICKNESS")): return "DIM"
    if "FIRE" in u and "RATING" in u: return "RATING"
    if leaf in ("NO.", "NO", "NUMBER", "MARK", "TYPE MARK", "DOOR NUMBER", "ROOM NUMBER"): return "NO"
    if "FINISH" in u and "COMMENT" not in u: return "FINISH"
    return None

def check_column_shift(col_names, rows):
    """Score typed columns against expected value patterns at shifts (-1, 0, +1).
    Returns {"shifted": bool, "best_shift": int, "scores": {-1: float, 0: float, 1: float}, "reason": str|None}."""
    matrix = [[str(r["cells"].get(c, "") if isinstance(r, dict) and "cells" in r else r.get(c, "")).strip() for c in col_names] for r in rows]
    def _score(shift):
        hit = tot = 0
        for j, nm in enumerate(col_names):
            k = _col_kind(nm)
            if not k or not (0 <= j + shift < len(col_names)): continue
            rx = _COL_TYPES[k]
            for r in matrix:
                v = r[j + shift]
                if not v: continue
                tot += 1; hit += bool(rx.match(v))
        return (hit / tot, tot) if tot else (1.0, 0)
    s_map, tots = {}, {}
    for d in (-1, 0, 1): s_map[d], tots[d] = _score(d)
    best = max((-1, 0, 1), key=lambda d: (s_map[d], -abs(d)))
    # Structural symptoms: bare unit 'SF' in an AREA column, or space-merged code next to a mostly-empty column
    bare_area = False
    for j, nm in enumerate(col_names):
        if _col_kind(nm) == "AREA":
            vals = [r[j] for r in matrix if r[j]]
            if len(vals) >= 2 and sum(1 for v in vals if v.upper() in ("SF", "S.F.", "M2", "M²")) >= 0.5 * len(vals):
                bare_area = True
    merged_code = False
    for j in range(len(col_names) - 1):
        k_next = _col_kind(col_names[j + 1])
        if ("MATL" in col_names[j].upper() or "MATERIAL" in col_names[j].upper()) and (k_next in ("NO", None) and "TYPE" in col_names[j + 1].upper()):
            left_v = [r[j] for r in matrix if r[j]]
            right_v = [r[j + 1] for r in matrix if r[j + 1]]
            if len(left_v) >= 5 and len(right_v) <= 0.2 * len(left_v) and sum(1 for v in left_v if re.search(r"\S+\s+[A-Z0-9]{1,3}$", v)) >= 0.4 * len(left_v):
                merged_code = True
    reason = None
    if bare_area:
        reason = "AREA column contains bare unit strings without numbers"
    elif tots[0] >= 4 and best != 0 and s_map[best] - s_map[0] > 0.25:
        reason = f"column type match {s_map[0]:.0%} rises to {s_map[best]:.0%} under shift {best:+d}"
    elif tots[0] >= 6 and s_map[0] < 0.55:
        reason = f"low column type match ({s_map[0]:.0%})"
    elif merged_code:
        reason = f"adjacent column '{col_names[j + 1]}' appears merged into '{col_names[j]}'"
    return {"shifted": reason is not None, "best_shift": best, "scores": {d: round(s_map[d], 3) for d in (-1, 0, 1)}, "reason": reason}

# ---- Ruling-line schedule table reader ---------------------------------------------------------------------
def _h_runs(segs, gap=4.0):
    """Split horizontal rule segments at the same y into contiguous x-chains so side-by-side tables stay separate."""
    if not segs: return []
    ss = sorted(segs)
    runs = [[ss[0][0], ss[0][1], [ss[0]]]]
    for a, b in ss[1:]:
        if a <= runs[-1][1] + gap:
            runs[-1][1] = max(runs[-1][1], b); runs[-1][2].append((a, b))
        else:
            runs.append([a, b, [(a, b)]])
    return runs

def _vcov(V, x, y0, y1, tol=2.2):
    """Fraction of vertical interval [y0, y1] covered by vertical segments within +/-tol of x."""
    if y1 <= y0: return 0.0
    iv = sorted((max(a, y0), min(b, y1)) for vx, segs in V.items() if abs(vx - x) <= tol for a, b in segs if b > y0 and a < y1)
    cov, cur = 0.0, y0
    for a, b in iv:
        a = max(a, cur)
        if b > a: cov += b - a; cur = b
    return cov / (y1 - y0)

def _split_line_x(ws, max_gap=150.0):
    if not ws: return []
    ws = sorted(ws, key=lambda w: w[0])
    runs = [[ws[0]]]
    for w in ws[1:]:
        if w[0] - runs[-1][-1][2] > max_gap: runs.append([w])
        else: runs[-1].append(w)
    return runs

def grid_tables(path, max_pages=3):
    """Tables whose cells are drawn with ruling lines (2D-aware for side-by-side schedules on large sheets)."""
    import pymupdf
    out = []
    doc = pymupdf.open(path)
    for pi, pg in enumerate(doc):
        if pi >= max_pages: break
        pg.remove_rotation()
        H, V = C.defaultdict(list), C.defaultdict(list)
        for dr in pg.get_drawings():
            for it in dr["items"]:
                segs = []
                if it[0] == "l": segs = [(it[1].x, it[1].y, it[2].x, it[2].y)]
                elif it[0] == "re":
                    r = it[1]
                    segs = [(r.x0, r.y0, r.x1, r.y0), (r.x0, r.y1, r.x1, r.y1),
                            (r.x0, r.y0, r.x0, r.y1), (r.x1, r.y0, r.x1, r.y1)]
                for x0, y0, x1, y1 in segs:
                    if abs(y0 - y1) < 0.6 and abs(x0 - x1) > 6: H[round(y0)].append((min(x0, x1), max(x0, x1)))
                    if abs(x0 - x1) < 0.6 and abs(y0 - y1) > 6: V[round(x0)].append((min(y0, y1), max(y0, y1)))
        merged = []
        for y in sorted(H):
            if merged and y - merged[-1][0] <= 1.5: merged[-1][1].extend(H[y])
            else: merged.append([y, list(H[y])])
        W = [(w[0], w[1], w[2], w[3], w[4]) for w in pg.get_text("words")]
        lines = C.defaultdict(list)
        for w in W: lines[round(w[1] / 3)].append(w)
        def is_head(ws):
            n_h = sum(1 for w in ws if w[4].upper().strip(".:") in HDR)
            n_d = sum(1 for w in ws if re.search(r"\d", w[4]))
            return n_h >= 3 and n_d <= 0.15 * len(ws) and n_h >= 0.3 * len(ws)
        used = []  # (X0, top, X1, yb)
        for k in sorted(lines):
            for ws in _split_line_x(lines[k], 150.0):
                if not is_head(ws): continue
                yh = sum((w[1] + w[3]) / 2 for w in ws) / len(ws)
                hx0, hx1 = min(w[0] for w in ws), max(w[2] for w in ws)
                if any(uy0 - 2 <= yh <= uy1 + 2 and min(hx1, ux1) - max(hx0, ux0) > 20 for ux0, uy0, ux1, uy1 in used):
                    continue
                cand = []
                for y, all_segs in merged:
                    if not (yh < y < yh + 40): continue
                    for rx0, rx1, rsegs in _h_runs(all_segs):
                        if len(rsegs) >= 3 and rx0 <= hx0 + 35 and rx1 >= hx1 - 35:
                            cand.append((y, rx0, rx1, rsegs))
                if not cand: continue
                y1, X0, X1, segs = cand[0]
                bounds = sorted({round(v) for a, b in segs for v in (a, b)})
                cols_x = [bounds[0]]
                for v in bounds[1:]:
                    if v - cols_x[-1] > 4: cols_x.append(v)
                if len(cols_x) < 4: continue
                rows_y = [y1]
                for y, sg in merged:
                    if y <= y1: continue
                    if y - rows_y[-1] > 70: break
                    sub = [(a, b) for a, b in sg if b > X0 - 4 and a < X1 + 4]
                    if not sub: continue
                    if min(a for a, _ in sub) <= X0 + 6 and max(b for _, b in sub) >= X1 - 6 and sum(min(b, X1) - max(a, X0) for a, b in sub) >= 0.8 * (X1 - X0):
                        # Keep row if vertical rules connect prev row -> y OR if horizontal cell-segment endpoints match cols_x
                        v_hits = sum(1 for cx in cols_x if _vcov(V, cx, rows_y[-1] + 0.5, y - 0.5) >= 0.55)
                        seg_pts = {round(v) for a, b in sub for v in (a, b)}
                        c_hits = sum(1 for cx in cols_x if any(abs(v - cx) <= 3 for v in seg_pts))
                        if v_hits < max(2, int(0.4 * len(cols_x))) and c_hits < max(3, int(0.6 * len(cols_x))):
                            break
                        rows_y.append(y)
                if len(rows_y) < 3: continue
                # Full-width top rule of the header band (above yh; stop below any spanning title banner row if another full rule exists)
                full_above = [y for y, sg in merged if yh - 70 < y < yh and sum(min(b, X1) - max(a, X0) for a, b in sg if b > X0 and a < X1) >= 0.75 * (X1 - X0)]
                top = max(full_above) if full_above else (yh - 45)
                # Check for partial sub-header rule inside (top, y1) (e.g. 'Door' / 'Frame' group headers above leaf columns)
                partial_sub = [y for y, sg in merged if top + 6 < y < y1 - 6 and 0.15 * (X1 - X0) <= sum(min(b, X1) - max(a, X0) for a, b in sg if b > X0 and a < X1) < 0.75 * (X1 - X0)]
                sub_y = max(partial_sub) if partial_sub else None
                sub_cov = set()
                if sub_y is not None:
                    sub_segs = [ s for y, sg in merged if abs(y - sub_y) <= 1.5 for s in sg if s[1] > X0 and s[0] < X1 ]
                    for i in range(len(cols_x) - 1):
                        cmid = (cols_x[i] + cols_x[i + 1]) / 2
                        if any(a - 2 <= cmid <= b + 2 for a, b in sub_segs): sub_cov.add(i)
                names = []
                for i in range(len(cols_x) - 1):
                    y_lo = sub_y - 1 if i in sub_cov else top - 1
                    hw = sorted([w for w in W if y_lo <= (w[1] + w[3]) / 2 <= y1 and cols_x[i] <= (w[0] + w[2]) / 2 < cols_x[i + 1]], key=lambda w: (round(w[1] / 3), w[0]))
                    names.append(" ".join(w[4] for w in hw) or f"col {i + 1}")
                # Disambiguate repeated leaf names (e.g. 'Matl' under 'Door' vs 'Frame') using spanning group header cells in [top, sub_y]
                if sub_y is not None:
                    dup = {nm for nm, c in C.Counter(names).items() if c > 1}
                    if dup:
                        v_up = [cx for cx in cols_x if _vcov(V, cx, top + 1, sub_y - 1) >= 0.5]
                        if X0 not in v_up: v_up = [X0] + v_up
                        if X1 not in v_up: v_up = v_up + [X1]
                        v_up = sorted(set(v_up))
                        for i in sub_cov:
                            if names[i] not in dup: continue
                            cmid = (cols_x[i] + cols_x[i + 1]) / 2
                            gx0 = max([v for v in v_up if v <= cmid] or [X0])
                            gx1 = min([v for v in v_up if v >= cmid] or [X1])
                            gw = sorted([w for w in W if top - 1 <= (w[1] + w[3]) / 2 < sub_y and gx0 <= (w[0] + w[2]) / 2 <= gx1], key=lambda w: (round(w[1] / 3), w[0]))
                            grp = " ".join(w[4] for w in gw).strip()
                            if grp and not names[i].upper().startswith(grp.upper()):
                                names[i] = f"{grp} {names[i]}"
                seen = C.Counter()
                for i, nm in enumerate(names):
                    seen[nm] += 1
                    if seen[nm] > 1: names[i] = f"{nm} {seen[nm]}"
                used.append((X0, top, X1, rows_y[-1]))
                title = next((" ".join(w[4] for w in sorted(lines[kk], key=lambda w: w[0]) if X0 - 30 <= w[0] <= X1 + 30)
                              for kk in sorted(lines, reverse=True)
                              if kk * 3 < top + 4 and top - kk * 3 <= 65 and any("SCHEDULE" in w[4].upper() and X0 - 30 <= w[0] <= X1 + 30 for w in lines[kk])), None)
                if not title:
                    title = next((" ".join(w[4] for w in sorted(lines[kk], key=lambda w: w[0]) if X0 - 20 <= w[0] <= X1 + 20)
                                  for kk in sorted(lines, reverse=True)
                                  if kk * 3 < top + 4 and top - kk * 3 <= 40 and any(X0 <= w[0] <= X1 for w in lines[kk])), None) or "table"
                rows = []
                for j in range(len(rows_y) - 1):
                    ya, yb = rows_y[j], rows_y[j + 1]; cells = {}
                    for i in range(len(cols_x) - 1):
                        cw = sorted([w for w in W if ya <= (w[1] + w[3]) / 2 < yb and cols_x[i] <= (w[0] + w[2]) / 2 < cols_x[i + 1]], key=lambda w: (round(w[1] / 3), w[0]))
                        if cw: cells[names[i]] = " ".join(w[4] for w in cw)
                    if cells: rows.append({"cells": cells, "box": [X0, ya, X1, yb]})
                if len(rows) >= 2:
                    out.append({"title": title, "page": pi + 1, "columns": names, "rows": rows, "header_y": yh, "cols_x": cols_x, "bbox": [X0, top, X1, rows_y[-1]]})
    return out

# ---- Door / Frame Type graphical callout legends -----------------------------------------------------------
def _door_type_legends(path, max_pages=3):
    """Extract 'DOOR TYPE <CODE>' callouts when accompanied by descriptive caption lines directly underneath."""
    import pymupdf
    out = {}  # page -> list of {code, description, box}
    doc = pymupdf.open(path)
    for pi, pg in enumerate(doc):
        if pi >= max_pages: break
        pg.remove_rotation()
        W = [(w[0], w[1], w[2], w[3], w[4]) for w in pg.get_text("words")]
        lines = C.defaultdict(list)
        for w in W: lines[round(w[1] / 3)].append(w)
        items = []
        for k in sorted(lines):
            ws = sorted(lines[k], key=lambda w: w[0])
            for idx in range(len(ws) - 2):
                if ws[idx][4].upper() == "DOOR" and ws[idx + 1][4].upper() == "TYPE" and ws[idx + 1][0] - ws[idx][2] < 15:
                    code = ws[idx + 2][4].strip()
                    if ws[idx + 2][0] - ws[idx + 1][2] < 15 and re.match(r"^[A-Z0-9]{1,4}$", code):
                        x0, y0 = ws[idx][0], ws[idx][1]
                        x1 = ws[idx + 2][2]
                        next_x = ws[idx + 3][0] - 8 if idx + 3 < len(ws) else x0 + 130
                        hi_x = min(next_x, x0 + 130)
                        desc_lines = []
                        cur_y = ws[idx][3]
                        for kk in sorted(lines):
                            if kk <= k: continue
                            dy = kk * 3 - cur_y
                            if dy > 18: break
                            sub = [w for w in sorted(lines[kk], key=lambda w: w[0]) if x0 - 6 <= w[0] < hi_x]
                            if not sub: continue
                            if any(w[4].upper() in ("DOOR", "FRAME") and idx2 + 1 < len(sub) and sub[idx2 + 1][4].upper() in ("TYPE", "TYPES") for idx2, w in enumerate(sub)):
                                break
                            txt = " ".join(w[4] for w in sub)
                            if re.match(r'^\d+/\d+"\s*=', txt): break
                            desc_lines.append(txt); cur_y = max(w[3] for w in sub)
                            x1 = max(x1, max(w[2] for w in sub))
                        desc = " ".join(desc_lines).strip()
                        if desc:
                            items.append({"code": code, "description": desc, "box": [x0, y0, x1, cur_y]})
        if items:
            out[pi + 1] = items
    return out

# ---- Vector floor/enlarged plan fixture spotting -----------------------------------------------------------
_ELEV_WORDS = {"NORTH", "SOUTH", "EAST", "WEST", "BACK", "LEFT", "RIGHT", "ELEVATION", "ELEVATIONS", "SECTION", "SECTIONS", "PARTITION", "TYPES", "TYP.", "TYP", "DETAIL"}
_SCALE_RX = re.compile(r'^(\d+(?:\s+\d+/\d+|/\d+)?)"\s*=\s*1\'-0"$')

def _parse_scale_in(txt):
    m = _SCALE_RX.match(txt.strip())
    if not m: return None
    s = m.group(1)
    if " " in s:
        a, b = s.split(); num, den = b.split("/")
        return float(a) + float(num) / float(den)
    if "/" in s:
        num, den = s.split("/")
        return float(num) / float(den)
    return float(s)

def _plan_fixtures(path, max_pages=3):
    """Detect water closets (toilet), lavatories (washbasin) and showers drawn in vector enlarged/floor plan viewports."""
    import pymupdf
    entities = []
    doc = pymupdf.open(path)
    for pi, pg in enumerate(doc):
        if pi >= max_pages: break
        pg.remove_rotation()
        W = [(w[0], w[1], w[2], w[3], w[4]) for w in pg.get_text("words")]
        lines = C.defaultdict(list)
        for w in W: lines[round(w[1] / 3)].append(w)
        # 1. Locate viewport title blocks via scale notations ('1/2" = 1\'-0"')
        views = []
        for k in sorted(lines):
            ws = sorted(lines[k], key=lambda w: w[0])
            for i in range(len(ws) - 2):
                if ws[i + 1][4] == "=" and ws[i + 2][4] == '1\'-0"':
                    sc_in = _parse_scale_in(f"{ws[i][4]} = {ws[i+2][4]}")
                    if not sc_in or not (0.08 <= sc_in <= 1.5): continue
                    sx0, sy0 = ws[i][0], ws[i][1]
                    # Detail/view ID token just left of scale (e.g. '6A', '6B')
                    vid_cand = [w for w in W if sx0 - 55 <= w[0] <= sx0 + 5 and sy0 - 25 <= w[1] <= sy0 + 10 and re.match(r"^[0-9]{1,2}[A-Z]{1,2}$|^[A-Z]{1,2}[0-9]{1,2}$", w[4])]
                    vid = sorted(vid_cand, key=lambda w: abs(w[1] - sy0))[0][4] if vid_cand else ""
                    # Title words directly above the scale line
                    tw = [w for w in W if sx0 - 8 <= w[0] <= sx0 + 260 and sy0 - 30 <= w[1] < sy0 - 2 and w[4] != vid]
                    if not tw: continue
                    # Keep the closest baseline above scale
                    best_k = max(round(w[1] / 3) for w in tw)
                    twords = sorted([w for w in tw if abs(round(w[1] / 3) - best_k) <= 1], key=lambda w: w[0])
                    # Stop at big gap (next viewport's title)
                    t_run = [twords[0]]
                    for w in twords[1:]:
                        if w[0] - t_run[-1][2] > 25: break
                        t_run.append(w)
                    vtitle = " ".join(w[4] for w in t_run).strip()
                    views.append({"id": vid, "title": vtitle, "x": sx0, "y": sy0, "pts_per_ft": sc_in * 72.0})
        if not views: continue
        # Group viewports into horizontal rows to bound each viewport's [vx0, vy0, vx1, vy1]
        views.sort(key=lambda v: (round(v["y"] / 80), v["x"]))
        row_groups = []
        for v in views:
            if row_groups and abs(v["y"] - row_groups[-1][0]["y"]) < 80: row_groups[-1].append(v)
            else: row_groups.append([v])
        for ri, rg in enumerate(row_groups):
            rg.sort(key=lambda v: v["x"])
            vy0 = max(v["y"] for v in row_groups[ri - 1]) + 12 if ri > 0 else 45.0
            for ci, v in enumerate(rg):
                v["vx0"] = (rg[ci - 1]["vx1"] + 10) if ci > 0 else 80.0
                v["vx1"] = (rg[ci + 1]["x"] - 25) if ci + 1 < len(rg) else min(pg.rect.x1 - 260, v["x"] + 650)
                v["vy0"] = vy0
                v["vy1"] = v["y"] - 14
        plan_views = []
        for v in views:
            toks = set(v["title"].upper().split())
            if toks & _ELEV_WORDS: continue
            # Exclude elevation views that contain 'Level <N>' datum markers
            has_datum = any(w[4] == "Level" and v["vx0"] <= w[0] <= v["vx1"] and v["vy0"] <= w[1] <= v["vy1"] for w in W)
            if has_datum: continue
            plan_views.append(v)
        if not plan_views: continue
        drs = pg.get_drawings()
        idx_c = 0
        for v in plan_views:
            vx0, vy0, vx1, vy1 = v["vx0"], v["vy0"], v["vx1"], v["vy1"]
            S = v["pts_per_ft"]  # PDF points per foot (e.g. 36.0 at 1/2" = 1'-0")
            vlabel = f"{v['id']} {v['title']}".strip()
            # Heavy room perimeter wall segments for indoor check + shower alcove check
            wall_h, wall_v = [], []
            curved_segs = []
            slat_v, slat_h = [], []
            for d in drs:
                r = d["rect"]
                if not (vx0 <= r.x0 and r.x1 <= vx1 and vy0 <= r.y0 and r.y1 <= vy1): continue
                col = tuple(round(x, 2) for x in d["color"]) if d["color"] else None
                wth = d["width"] or 0.0
                for it in d["items"]:
                    if it[0] == "l":
                        ax, ay, bx, by = it[1].x, it[1].y, it[2].x, it[2].y
                        dx, dy = abs(ax - bx), abs(ay - by)
                        L = math.hypot(dx, dy)
                        if col == (0.0, 0.0, 0.0) and wth >= 0.7 and L >= 0.8 * S:
                            if dy < 0.8: wall_h.append((ay, min(ax, bx), max(ax, bx)))
                            elif dx < 0.8: wall_v.append((ax, min(ay, by), max(ay, by)))
                        if col and col not in ((0.51, 0.51, 0.51), (0.23, 0.45, 0.61), (0.29, 0.45, 0.59)):
                            # Continuous curved/faceted fixture chords (< 0.245*S filters out 5'-0" ADA circles & door swings)
                            if dx > 0.15 and dy > 0.15 and 0.35 < L < 0.245 * S:
                                curved_segs.append((ax, ay, bx, by, col))
                            if dx < 0.3 and 0.35 * S <= dy <= 1.6 * S:
                                slat_v.append((ax, min(ay, by), max(ay, by)))
                            elif dy < 0.3 and 0.35 * S <= dx <= 1.6 * S:
                                slat_h.append((ay, min(ax, bx), max(ax, bx)))
            def is_indoor(cx, cy):
                if not wall_v or not wall_h: return True
                rx0, rx1 = min(wx for wx, _, _ in wall_v), max(wx for wx, _, _ in wall_v)
                ry0, ry1 = min(wy for wy, _, _ in wall_h), max(wy for wy, _, _ in wall_h)
                return rx0 - 2 <= cx <= rx1 + 2 and ry0 - 2 <= cy <= ry1 + 2

            # Endpoint union-find (< 0.45 pt) on curved fixture chords
            nc = len(curved_segs); par = list(range(nc))
            def find(i):
                while par[i] != i: par[i] = par[par[i]]; i = par[i]
                return i
            grid_b = C.defaultdict(list)
            for i, (ax, ay, bx, by, col) in enumerate(curved_segs):
                for px, py in ((ax, ay), (bx, by)):
                    gx, gy = round(px), round(py)
                    for dxi in (-1, 0, 1):
                        for dyi in (-1, 0, 1):
                            for j, qx, qy in grid_b[(col, gx + dxi, gy + dyi)]:
                                if math.hypot(px - qx, py - qy) < 0.45: par[find(i)] = find(j)
                    grid_b[(col, gx, gy)].append((i, px, py))
            comps = C.defaultdict(list)
            for i in range(nc): comps[find(i)].append(curved_segs[i])
            loops = []
            for m in comps.values():
                if len(m) < 4: continue
                xs = [x for s in m for x in (s[0], s[2])]; ys = [y for s in m for y in (s[1], s[3])]
                loops.append((min(xs), min(ys), max(xs), max(ys), max(xs) - min(xs), max(ys) - min(ys), len(m), m[0][4]))

            # A. Water closets (toilet): elongated bowl curve (~1.4-1.9 ft long, ~0.45-0.8 ft half-width, >=12 chords)
            #    paired with tank corner fillets (~0.22-0.42 ft, 4-10 chords) within 0.6 ft; merge opposing bowl halves.
            wc_halves = [lp for lp in loops if 1.35 * S <= max(lp[4], lp[5]) <= 1.95 * S and 0.45 * S <= min(lp[4], lp[5]) <= 0.80 * S and lp[6] >= 12]
            tank_fillets = [lp for lp in loops if 0.22 * S <= lp[4] <= 0.42 * S and 0.22 * S <= lp[5] <= 0.42 * S and 4 <= lp[6] <= 10]
            wc_used = set()
            for i, h1 in enumerate(wc_halves):
                if i in wc_used: continue
                bx0, by0, bx1, by1 = h1[:4]
                for j, h2 in enumerate(wc_halves):
                    if j <= i or j in wc_used: continue
                    if math.hypot((h1[0] + h1[2] - h2[0] - h2[2]) / 2, (h1[1] + h1[3] - h2[1] - h2[3]) / 2) < 0.9 * S:
                        wc_used.add(j)
                        bx0, by0, bx1, by1 = min(bx0, h2[0]), min(by0, h2[1]), max(bx1, h2[2]), max(by1, h2[3])
                near_t = [tf for tf in tank_fillets if bx0 - 0.8 * S <= (tf[0] + tf[2]) / 2 <= bx1 + 0.8 * S and by0 - 0.8 * S <= (tf[1] + tf[3]) / 2 <= by1 + 0.8 * S]
                if not near_t: continue
                for tf in near_t:
                    bx0, by0, bx1, by1 = min(bx0, tf[0]), min(by0, tf[1]), max(bx1, tf[2]), max(by1, tf[3])
                wc_used.add(i); idx_c += 1
                cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
                box = [round(bx0, 1), round(by0, 1), round(bx1, 1), round(by1, 1)]
                entities.append(Entity(f"toilet:{pi+1}:{idx_c}", "toilet",
                    {"label": f"water closet ({vlabel})", "level": vlabel, "room": v["title"], "indoor": is_indoor(cx, cy)},
                    [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])],
                    {"page": pi + 1, "bbox": box, "coords": "page_unrotated", "rule": f"vector WC bowl+tank path signature in plan '{vlabel}'"}))

            # B. Lavatories (washbasin): oval basin loop (~1.3-1.85 ft x ~0.9-1.3 ft, >=20 chords) with nearby faucet loop;
            #    deduplicate nested inner/outer basin ovals within 0.8 ft.
            lav_cands = sorted([lp for lp in loops if 1.25 * S <= max(lp[4], lp[5]) <= 1.85 * S and 0.85 * S <= min(lp[4], lp[5]) <= 1.30 * S and lp[6] >= 20],
                               key=lambda lp: -(lp[4] * lp[5]))
            lav_kept = []
            for lp in lav_cands:
                cx, cy = (lp[0] + lp[2]) / 2, (lp[1] + lp[3]) / 2
                if any(math.hypot(cx - (k[0] + k[2]) / 2, cy - (k[1] + k[3]) / 2) < 0.8 * S for k in lav_kept): continue
                lav_kept.append(lp); idx_c += 1
                box = [round(lp[0], 1), round(lp[1], 1), round(lp[2], 1), round(lp[3], 1)]
                entities.append(Entity(f"washbasin:{pi+1}:{idx_c}", "washbasin",
                    {"label": f"lavatory ({vlabel})", "level": vlabel, "room": v["title"], "indoor": is_indoor(cx, cy)},
                    [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])],
                    {"page": pi + 1, "bbox": box, "coords": "page_unrotated", "rule": f"vector oval lavatory basin path signature in plan '{vlabel}'"}))

            # C. Showers (shower): slotted folding shower seat (>=6 parallel equal-span slats inside a ~1.0-2.5 ft cluster
            #    with adjacent spray/valve curves inside a walled stall alcove).
            for orient_slats, is_vert in ((slat_v, True), (slat_h, False)):
                # Group slats with identical [a, b] span (within 1.5 pt) that sit within 1.5 ft of each other
                by_span = C.defaultdict(list)
                for pos, a, b in orient_slats:
                    by_span[(round(a / 2), round(b / 2))].append((pos, a, b))
                for (_, _), grp in by_span.items():
                    xs_p = sorted({round(p, 1) for p, _, _ in grp})
                    if len(xs_p) >= 5 and 0.4 * S <= (xs_p[-1] - xs_p[0]) <= 1.8 * S:
                        a0 = min(a for _, a, _ in grp); b0 = max(b for _, _, b in grp)
                        bx0, by0, bx1, by1 = (xs_p[0], a0, xs_p[-1], b0) if is_vert else (a0, xs_p[0], b0, xs_p[-1])
                        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
                        if not is_indoor(cx, cy): continue
                        # Expand to enclosing stall walls if close (<= 3.5 ft)
                        w_l = max([wx for wx, y0, y1 in wall_v if cx - 3.5 * S <= wx < bx0 and y0 - 8 <= cy <= y1 + 8] or [bx0])
                        w_r = min([wx for wx, y0, y1 in wall_v if bx1 < wx <= cx + 3.5 * S and y0 - 8 <= cy <= y1 + 8] or [bx1])
                        w_t = max([wy for wy, x0, x1 in wall_h if cy - 3.5 * S <= wy < by0 and x0 - 8 <= cx <= x1 + 8] or [by0])
                        w_b = min([wy for wy, x0, x1 in wall_h if by1 < wy <= cy + 3.5 * S and x0 - 8 <= cx <= x1 + 8] or [by1])
                        box = [round(w_l, 1), round(w_t, 1), round(w_r, 1), round(w_b, 1)]
                        if any(e.kind == "shower" and e.src["page"] == pi + 1 and abs(e.src["bbox"][0] - box[0]) < S for e in entities):
                            continue
                        idx_c += 1
                        entities.append(Entity(f"shower:{pi+1}:{idx_c}", "shower",
                            {"label": f"shower ({vlabel})", "level": vlabel, "room": v["title"], "indoor": True},
                            [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])],
                            {"page": pi + 1, "bbox": box, "coords": "page_unrotated", "rule": f"vector shower stall & slotted seat signature in plan '{vlabel}'"}))
    return entities

LABEL_RX = re.compile(r"number|mark|\bno\b|tag|\bid\b", re.I)
NAME_RX = re.compile(r"room[_\s]*name|^name$", re.I)
WIDE_RX = re.compile(r"\b(\d+)\s*-\s*WIDE\b", re.I)

def _col_values_summary(rows_attrs, cols, max_unique=30, max_val_len=52):
    """Compact inventory of distinct values per categorical/type column so the planner sees values beyond row[:2]."""
    out = {}
    for c in cols:
        if c in ("area", "count", "n_wide_units", "comments", "type_comments", "ds_hardware_set_sft", "hardware_set", "hardware_group_see_specs"):
            continue
        vals, seen = [], set()
        for a in rows_attrs:
            v = str(a.get(c, "")).strip()
            if v and v not in seen:
                seen.add(v); vals.append(v[:max_val_len])
        if not vals: continue
        if c in ("width", "height"):
            if len(vals) <= 3: out[c] = vals
            continue
        if re.search(r"room_name|number|mark", c) and len(vals) > 10:
            continue
        if len(vals) <= max_unique:
            out[c] = vals
    return out

def _enrich_table_rows(t_title, col_keys, row_entities, dt_map):
    """Attach deterministic derived attributes (descriptive label, multi-wide n_wide_units, door_type_description)."""
    num_col = next((c for c in col_keys if LABEL_RX.search(c)), col_keys[0] if col_keys else None)
    name_col = next((c for c in col_keys if NAME_RX.search(c)), None)
    has_wide = any(WIDE_RX.search(str(e.attrs.get("type", ""))) for e in row_entities) and "count" in col_keys
    added_cols = []
    for e in row_entities:
        a = e.attrs
        nv = a.get(num_col) if num_col else None
        nm = a.get(name_col) if name_col else None
        if nv: a["number"] = nv
        if nm: a["name"] = nm
        if nv and nm and "room" in (num_col or ""):
            a["label"] = f"Room {nv} {nm}"
        else:
            a["label"] = nv or nm
        if has_wide:
            cm = re.match(r"^\s*(\d+)\s*$", str(a.get("count", "")))
            wm = WIDE_RX.search(str(a.get("type", "")))
            if cm:
                mult = int(wm.group(1)) if wm else 1
                a["n_wide_units"] = int(cm.group(1)) * mult
                a["n_wide_units_rule"] = f"count ({cm.group(1)}) × {mult} ({mult}-WIDE assembly)"
                if "n_wide_units" not in added_cols: added_cols.append("n_wide_units")
        if dt_map and a.get("door_type") in dt_map:
            a["door_type_description"] = dt_map[a["door_type"]]
            a["door_type_description_rule"] = "joined from sheet DOOR TYPES legend"
            if "door_type_description" not in added_cols: added_cols.append("door_type_description")
    return added_cols

def extract(path):
    st = Store(path, "pdf-vector"); st.meta["supports"] = ["schedule_row"]; st.meta["extractor"] = "x_pdf"; st.meta["units"] = "drawing text (imperial / as written)"
    n = 0; grid_boxes = []
    try: dt_legends = _door_type_legends(path)
    except Exception: dt_legends = {}
    try: G = grid_tables(path)
    except Exception as e: G = []; st.meta["warnings"].append(f"line-grid table reader failed: {type(e).__name__}: {e}")
    for t in G:
        grid_boxes.append((t["page"], t["bbox"]))
        shift = check_column_shift(t["columns"], t["rows"])
        if shift["shifted"]:
            st.meta["warnings"].append(f"table '{t['title']}' (page {t['page']}): columns look shifted — {shift['reason']}")
        col_keys = [_key(c) for c in t["columns"]]
        t_ents = []
        for r in t["rows"]:
            n += 1; a = {"table": t["title"], "page": t["page"]}
            for name, txt in r["cells"].items(): a[_key(name)] = txt
            ent = Entity(f"row:{t['page']}:{n}", "schedule_row", a, None,
                         {"page": t["page"], "bbox": r["box"], "coords": "unrotated", "cols_x": t["cols_x"],
                          "rule": f"row of table '{t['title']}': cells taken from the drawn grid lines (header at y={t['header_y']:.0f})"})
            st.add(ent); t_ents.append(ent)
        dt_map = {it["code"]: it["description"] for it in dt_legends.get(t["page"], [])}
        extra = _enrich_table_rows(t["title"], col_keys, t_ents, dt_map)
        all_cols = col_keys + [c for c in extra if c not in col_keys]
        t_meta = {"title": t["title"], "page": t["page"], "columns": all_cols, "rows": len(t["rows"]),
                  "method": "ruling-line grid", "col_type_score": shift["scores"][0], "shifted": shift["shifted"]}
        if "area" in all_cols and any("SF" in str(e.attrs.get("area", "")).upper() for e in t_ents):
            t_meta["area_unit"] = "SF (already ft2; do NOT use convert)"
        vals = _col_values_summary([e.attrs for e in t_ents], all_cols)
        if vals: t_meta["values"] = vals
        st.meta.setdefault("tables", []).append(t_meta)

    # Emit DOOR TYPES legend table when descriptions were found on the sheet
    for page_no, items in sorted(dt_legends.items()):
        t_ents = []
        for it in items:
            n += 1
            a = {"table": "DOOR TYPES", "page": page_no, "door_type": it["code"], "description": it["description"],
                 "label": it["code"], "number": it["code"]}
            ent = Entity(f"row:{page_no}:{n}", "schedule_row", a, None,
                         {"page": page_no, "bbox": it["box"], "coords": "unrotated", "cols_x": [it["box"][0], it["box"][2]],
                          "rule": "door type callout legend from sheet"})
            st.add(ent); t_ents.append(ent)
        st.meta.setdefault("tables", []).append({
            "title": "DOOR TYPES", "page": page_no, "columns": ["door_type", "description"], "rows": len(items),
            "method": "callout legend", "values": _col_values_summary([e.attrs for e in t_ents], ["door_type", "description"])
        })

    # Text-gap fallback for unruled schedules (only when region is not already covered by a ruling-line grid table)
    pdf = pdfplumber.open(path)
    for pi, pg in enumerate(pdf.pages[:3]):
        W = pg.extract_words(keep_blank_chars=False, use_text_flow=False)
        lines = C.defaultdict(list)
        for w in W: lines[round(w["top"] / 3)].append(w)
        keys = sorted(lines)
        for k in keys: lines[k].sort(key=lambda w: w["x0"])
        def is_head(ws):
            n_h = sum(1 for w in ws if w["text"].upper().strip(".:") in HDR)
            n_d = sum(1 for w in ws if re.search(r"\d", w["text"]))
            return n_h >= 3 and n_d <= 0.15 * len(ws) and n_h >= 0.3 * len(ws)
        heads = [k for k in keys if is_head(lines[k])]
        for hk in heads:
            hw = lines[hk]
            cols = []
            for w in hw:
                if cols and w["x0"] - cols[-1]["x1"] < 9: cols[-1]["text"] += " " + w["text"]; cols[-1]["x1"] = w["x1"]
                else: cols.append({"text": w["text"], "x0": w["x0"], "x1": w["x1"]})
            run = [cols[0]]
            for c in cols[1:]:
                if c["x0"] - run[-1]["x1"] > 160: break
                run.append(c)
            if len(run) < 3: continue
            above = [w for kk in keys if 0 < (hk - kk) * 3 <= 14 for w in lines[kk]]
            for i, c in enumerate(run):
                lo = c["x0"] - 6; hi = run[i + 1]["x0"] - 6 if i + 1 < len(run) else c["x1"] + 120
                c["lo"], c["hi"] = lo, hi
                pre = " ".join(w["text"] for w in above if lo <= (w["x0"] + w["x1"]) / 2 < hi)
                c["name"] = (pre + " " + c["text"]).strip()
            seen = C.Counter()
            for c in run:
                seen[c["name"]] += 1
                if seen[c["name"]] > 1: c["name"] = f'{c["name"]} {seen[c["name"]]}'
            x_lo, x_hi = run[0]["lo"], run[-1]["hi"]
            yh = hk * 3
            if any(p == pi + 1 and bx[1] - 15 <= yh <= bx[3] + 15 and min(x_hi, bx[2]) - max(x_lo, bx[0]) > 30 for p, bx in grid_boxes):
                continue
            title = next((" ".join(w["text"] for w in lines[kk] if x_lo - 20 <= w["x0"] <= x_hi) for kk in reversed(keys) if kk < hk and (hk - kk) * 3 <= 40 and any("SCHEDULE" in w["text"].upper() for w in lines[kk])), None)
            if not title:
                tw = [w["text"] for kk in keys if 0 < (hk - kk) * 3 <= 60 for w in lines[kk] if x_lo - 20 <= w["x0"] <= x_hi]
                title = " ".join(tw[-8:]) if tw else "table"
            rows, cur, last_y = [], None, hk * 3
            for kk in keys:
                if kk <= hk: continue
                ws = [w for w in lines[kk] if x_lo <= (w["x0"] + w["x1"]) / 2 < x_hi]
                if not ws: continue
                if kk in heads and is_head(ws): break
                if kk * 3 - last_y > 60: break
                last_y = kk * 3
                cells = C.defaultdict(list)
                for w in ws:
                    cx = (w["x0"] + w["x1"]) / 2
                    c = next((c for c in run if c["lo"] <= cx < c["hi"]), None)
                    if c: cells[c["name"]].append(w)
                if not cells: continue
                first = run[0]["name"]
                if cells.get(first):
                    cur = {"cells": {k: " ".join(w["text"] for w in v) for k, v in cells.items()},
                           "box": [min(w["x0"] for w in ws), min(w["top"] for w in ws), max(w["x1"] for w in ws), max(w["bottom"] for w in ws)]}
                    rows.append(cur)
                elif cur:
                    for k, v in cells.items():
                        cur["cells"][k] = (cur["cells"].get(k, "") + " " + " ".join(w["text"] for w in v)).strip()
                    cur["box"][2] = max(cur["box"][2], max(w["x1"] for w in ws))
                    cur["box"][3] = max(cur["box"][3], max(w["bottom"] for w in ws))
            if len(rows) < 2: continue
            col_names = [c["name"] for c in run]
            shift = check_column_shift(col_names, rows)
            if shift["shifted"]:
                st.meta["warnings"].append(f"table '{title}' (page {pi + 1}): columns look shifted — {shift['reason']}")
            col_keys = [_key(c) for c in col_names]
            t_ents = []
            for r in rows:
                n += 1; a = {"table": title, "page": pi + 1}
                for name, txt in r["cells"].items(): a[_key(name)] = txt
                ent = Entity(f"row:{pi+1}:{n}", "schedule_row", a, None,
                             {"page": pi + 1, "bbox": r["box"], "rule": f"text-gap schedule '{title}' (header at y={hk*3:.0f})"})
                st.add(ent); t_ents.append(ent)
            dt_map = {it["code"]: it["description"] for it in dt_legends.get(pi + 1, [])}
            extra = _enrich_table_rows(title, col_keys, t_ents, dt_map)
            all_cols = col_keys + [c for c in extra if c not in col_keys]
            st.meta.setdefault("tables", []).append({
                "title": title, "page": pi + 1, "columns": all_cols, "rows": len(rows),
                "method": "text gaps", "col_type_score": shift["scores"][0], "shifted": shift["shifted"]
            })

    # Vector plan plumbing fixtures (toilet, washbasin, shower)
    try:
        fx = _plan_fixtures(path)
        levels = {e.get("level") for e in fx if e.get("level")}
        for e in fx:
            st.add(e)
            if len(levels) <= 1 and e.kind not in st.meta["supports"]:
                st.meta["supports"].append(e.kind)
    except Exception as e:
        st.meta["warnings"].append(f"vector plan fixture extractor failed: {type(e).__name__}: {e}")
    return st
