"""Sheet -> room database (prototype). Geometry only: no number comes from a detection box.

  python sheet_rooms.py <sheet.png> <detections.json> <out_dir> [--scale 200 --paper-mm 594]

Steps
  1 walls      solid dark strokes thicker than a thin line (morphological opening) -> wall mask
  2 openings   a detected door/window box only says WHERE a wall is interrupted; inside the box the collinear wall ends
               are joined, and the joined length is measured from the wall mask (opening width)
  3 rooms      connected regions of free space enclosed by the closed walls; each is traced to a polygon;
               area and perimeter are computed from the polygon (wall face to wall face = net internal area)
  4 buildings  rooms whose walls are connected belong to one building
  5 scale      printed ratio x paper size -> metres per pixel; checked against the drawn scale bar when OCR finds it
  6 labels     OCR text whose centre lies inside a room names the room
  7 database   SQLite: sheet, building, room, opening, room_opening  (+ overlay image)
"""
import os, sys, json, re, sqlite3, argparse, math
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", str(2 ** 40))
import cv2, numpy as np

PAPER = {"A0": 1189, "A1": 841, "A2": 594, "A3": 420, "A4": 297}

def ocr_tiles(g):
    """OCR of the working-scale image in overlapping tiles -> [{text, conf, x, y, h}] (command-line use; the server passes its own OCR)"""
    H, W = g.shape; ocr = []
    try:
        from rapidocr_onnxruntime import RapidOCR
        eng = RapidOCR(); TS, OV = 2400, 240
        def grid(nn):
            xs = list(range(0, max(nn - TS, 0) + 1, TS - OV))
            if xs[-1] + TS < nn: xs.append(nn - TS)
            return xs
        for ty in grid(H):
            for tx in grid(W):
                tile = g[ty:ty + TS, tx:tx + TS]
                if (tile < 200).mean() < 0.0005: continue
                res, _ = eng(cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR))
                for b, tt, cf in (res or []):
                    cx = sum(q[0] for q in b) / 4 + tx; cy = sum(q[1] for q in b) / 4 + ty
                    if any(o["text"] == tt and abs(o["x"] - cx) < 15 and abs(o["y"] - cy) < 15 for o in ocr): continue
                    ocr.append({"text": tt.strip(), "conf": float(cf), "x": cx, "y": cy, "h": abs(b[2][1] - b[0][1])})
    except Exception as e: print("OCR unavailable:", e)
    return ocr

def trace(image, det_path, work=0.5, closures=None):
    """Phase A (needs no text): walls, openings, enclosed regions. Runs at the same time as OCR."""
    g0 = cv2.imread(image, cv2.IMREAD_GRAYSCALE); H0, W0 = g0.shape
    k = work; g = cv2.resize(g0, (round(W0 * k), round(H0 * k)), interpolation=cv2.INTER_AREA); H, W = g.shape
    det = json.load(open(det_path)); th = det["thresholds"]
    boxes = [(c, s, np.array(b) * k) for c, s, *b in det["boxes"] if s >= th.get(c, 0.25)]
    doors = [b for c, s, b in boxes if c == "door"]; wins = [b for c, s, b in boxes if c == "window"]

    # ---- 1 walls: dark AND thick. Stroke thickness of thin lines is measured, walls are several times thicker.
    dark = (g < 110).astype(np.uint8)
    dist = cv2.distanceTransform(dark, cv2.DIST_L2, 3)
    half_w = float(np.percentile(dist[dist > 2.5], 75)) if (dist > 2.5).any() else 4.0     # typical wall half-thickness (px, working scale)
    t = max(3, int(round(half_w * 0.9)))
    walls = cv2.morphologyEx(dark, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (t, t)))
    # drop small solid blobs that are not walls (arrow heads, filled symbols): keep components with a long extent
    n, lab, st, _ = cv2.connectedComponentsWithStats(walls, 8)
    keep = np.zeros(n, bool)
    for i in range(1, n):
        w_, h_, ar = st[i, 2], st[i, 3], st[i, 4]
        if max(w_, h_) > 12 * t and not (ar < 0.6 * w_ * h_ and max(w_, h_) < 2 * min(w_, h_) and ar < 200 * t * t): keep[i] = True     # (not a filled arrow / north mark)
        elif min(w_, h_) >= 1.2 * half_w and ar >= 0.85 * w_ * h_: keep[i] = True      # short solid pier / door jamb between two openings
    walls = keep[lab].astype(np.uint8)
    wall_t = 2 * half_w

    # door jambs and short piers are solid but smaller than a wall is thick; they only count next to a detected opening
    t2 = max(2, t // 2)
    solid2 = cv2.morphologyEx(dark, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (t2, t2)))
    # filled arrows / north marks are solid too but are not wall: drop compact, half-filled blobs (a triangle fills ~50% of its box)
    n2, lab2, st2, _ = cv2.connectedComponentsWithStats(solid2, 8); keep2 = np.ones(n2, bool)
    for i in range(1, n2):
        w_, h_, ar = st2[i, 2], st2[i, 3], st2[i, 4]
        if ar < 0.62 * w_ * h_ and max(w_, h_) < 2 * min(w_, h_) and min(w_, h_) > 2 * wall_t and ar < 200 * t * t: keep2[i] = False
    solid2 = (keep2[lab2] & (solid2 > 0)).astype(np.uint8)
    for c_, s_, b in boxes:
        if c_ not in ("door", "window"): continue
        x0, y0, x1, y1 = b; m = 0.20 * max(x1 - x0, y1 - y0) + wall_t
        X0, Y0, X1, Y1 = int(max(0, x0 - m)), int(max(0, y0 - m)), int(min(W, x1 + m)), int(min(H, y1 + m))
        walls[Y0:Y1, X0:X1] |= solid2[Y0:Y1, X0:X1]

    # ---- 2 openings: join collinear wall ends inside each detected door / window box, measure the gap
    closed = walls.copy(); openings = []
    def close_box(b, kind):
        x0, y0, x1, y1 = b; w, h = x1 - x0, y1 - y0; m = 0.20 * max(w, h) + wall_t
        X0, Y0, X1, Y1 = int(max(0, x0 - m)), int(max(0, y0 - m)), int(min(W, x1 + m)), int(min(H, y1 + m))
        sub = walls[Y0:Y1, X0:X1]
        best = None
        for axis in (0, 1):                                   # 0: wall runs horizontally (gap along x), 1: vertically
            prof = sub.sum(axis=1 - axis) if axis == 0 else sub.sum(axis=0)     # wall pixels per row / per column
            # rows (or columns) that carry wall on both sides of the box centre
            L = sub.shape[1 - axis] if axis == 0 else sub.shape[0]
            for r in range(sub.shape[0] if axis == 0 else sub.shape[1]):
                line = sub[r, :] if axis == 0 else sub[:, r]
                idx = np.flatnonzero(line)
                if len(idx) < 4: continue
                gaps = np.flatnonzero(np.diff(idx) > 0.6 * wall_t)
                for gi in gaps:
                    g0_, g1_ = idx[gi], idx[gi + 1]; gap = g1_ - g0_ - 1
                    c = (g0_ + g1_) / 2 + (X0 if axis == 0 else Y0); lo, hi = (x0, x1) if axis == 0 else (y0, y1)
                    rr = r + (Y0 if axis == 0 else X0); plo, phi = (y0, y1) if axis == 0 else (x0, x1)
                    if not (lo - 0.1 * (hi - lo) <= c <= hi + 0.1 * (hi - lo)): continue        # gap centre inside the box along the wall
                    if not (plo - wall_t <= rr <= phi + wall_t): continue
                    if gap > 1.25 * max(w, h): continue
                    # both ends must be real wall ends: enough wall pixels on each side
                    if (idx <= g0_).sum() < 0.45 * wall_t or (idx >= g1_).sum() < 0.45 * wall_t: continue
                    best = best or {}
                    best.setdefault((axis, int(g0_), int(g1_)), []).append(r)
        if not best: return None
        # every distinct gap between wall ends inside the box is closed: a door box often also holds a glazed side light.
        cands = sorted(((kk, rows) for kk, rows in best.items() if len(rows) >= 0.4 * wall_t), key=lambda kv: -(kv[0][2] - kv[0][1]))
        if not cands: cands = [max(best.items(), key=lambda kv: (len(kv[1]), kv[0][2] - kv[0][1]))]
        out = []
        for (axis, g0_, g1_), rows in cands:
            if any(ax2 == axis and not (g1_ <= a2 or g0_ >= b2) and abs(r2 - np.mean(rows)) < 2 * wall_t for ax2, a2, b2, r2 in [(o_["_k"]) for o_ in out]): continue      # same gap seen on other rows
            r0, r1 = min(rows), max(rows)
            if axis == 0: cv2.rectangle(closed, (X0 + g0_, Y0 + r0), (X0 + g1_, Y0 + r1), 1, -1); seg = (X0 + g0_, Y0 + (r0 + r1) / 2, X0 + g1_, Y0 + (r0 + r1) / 2)
            else: cv2.rectangle(closed, (X0 + r0, Y0 + g0_), (X0 + r1, Y0 + g1_), 1, -1); seg = (X0 + (r0 + r1) / 2, Y0 + g0_, X0 + (r0 + r1) / 2, Y0 + g1_)
            out.append({"kind": kind if not out else ("glazed_screen" if kind == "door" else kind), "seg": seg, "width_px": g1_ - g0_ - 1, "box": [float(v) for v in b], "_k": (axis, g0_, g1_, float(np.mean(rows)))})
        return out
    unclosed = []
    def why_unclosed(b):
        x0, y0, x1, y1 = [int(v) for v in b]; pad = int(wall_t)
        sub = closed[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
        if sub.sum() < wall_t * wall_t: return "not in a wall", "no thick wall inside or next to the detection box (symbol drawn on thin lines only)"
        # a continuous wall already runs through the box: the neighbouring opening's closure covers it (e.g. a side light next to a door)
        if (sub.sum(axis=1) >= 0.9 * sub.shape[1]).any() or (sub.sum(axis=0) >= 0.9 * sub.shape[0]).any(): return "covered", "the wall is already continuous here (closed together with the neighbouring opening)"
        return "unresolved", "no pair of collinear wall ends found inside the detection box"
    for kind, bs in (("door", doors), ("window", wins)):
        for b in sorted(bs, key=lambda b: -(b[2] - b[0]) * (b[3] - b[1])):
            o = close_box(b, kind)
            if o: openings.extend(o)
            else: unclosed.append({"kind": kind, "box": [float(v) for v in b]})
    for o in unclosed: o["status"], o["why"] = why_unclosed(o["box"])
    # reviewed closures: gaps proposed by the bottleneck search (sheet_review.py) and confirmed by a model looking at the crop.
    # The model only says what the gap IS; position and width come from the wall mask.
    if closures and os.path.exists(closures):
        for cl in json.load(open(closures)):
            x0, y0, x1, y1 = [v * k for v in cl["seg"]]
            cv2.line(closed, (int(round(x0)), int(round(y0))), (int(round(x1)), int(round(y1))), 1, max(3, t // 2))
            openings.append({"kind": cl["kind"], "seg": (x0, y0, x1, y1), "width_px": math.hypot(x1 - x0, y1 - y0), "box": [x0, y0, x1, y1], "by": cl.get("by")})

    # ---- 3 rooms: enclosed free space
    free = (closed == 0).astype(np.uint8)
    n, lab, st, cen = cv2.connectedComponentsWithStats(free, 4)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])))
    return {"image": image, "g": g, "k": k, "W0": W0, "H0": H0, "W": W, "H": H, "closed": closed, "free": free, "lab": lab, "st": st, "cen": cen, "n": n,
            "border": border, "openings": openings, "unclosed": unclosed, "wall_t": wall_t, "t": t, "doors": doors, "wins": wins}

def finish(A, ocr, out, scale=None, paper=None, debug=False):
    """Phase B (needs the text): scale, room outlines, names, flags, database. ocr = [{text, conf, x, y, h}] in working pixels."""
    os.makedirs(out, exist_ok=True); image_name = os.path.basename(A["image"])
    g, k, W0, H0, W, H = A["g"], A["k"], A["W0"], A["H0"], A["W"], A["H"]; closed, free, lab, st, cen, n = A["closed"], A["free"], A["lab"], A["st"], A["cen"], A["n"]
    border, openings, unclosed, wall_t, t, doors, wins = A["border"], A["openings"], A["unclosed"], A["wall_t"], A["t"], A["doors"], A["wins"]
    txt = " | ".join(o["text"] for o in ocr)
    notes = []
    if scale is None:
        m = re.search(r"\b1\s*:\s*(\d{2,4})\b", txt); scale = float(m.group(1)) if m else None
        if scale: notes.append(f"scale 1:{scale:g} read by OCR")
    if paper is None:
        sizes = [o["text"].upper() for o in ocr if re.fullmatch(r"A[0-4]", o["text"].upper())]
        if sizes: paper = PAPER[sizes[0]]; notes.append(f"paper size {sizes[0]} read from the title block")
    m_per_px = None
    if scale and paper:
        m_per_px = (paper / max(W0, H0)) * scale / 1000.0 / k          # metres per working pixel
    # scale bar check: '0' ... '10m' ... '20m' texts on one line
    bar = None
    try:
        ms = [(float(re.fullmatch(r"(\d+)\s*m", o["text"]).group(1)), o) for o in ocr if re.fullmatch(r"(\d+)\s*m", o["text"])]
        if len(ms) >= 2:
            ms.sort(key=lambda v: v[0]); (v0, o0), (v1, o1) = ms[0], ms[-1]
            if abs(o0["y"] - o1["y"]) < 40 and v1 > v0:
                # tick positions: the long horizontal bar line just above the numbers
                bar = {"m_per_px_from_text": (v1 - v0) / abs(o1["x"] - o0["x"]), "from": o0["text"], "to": o1["text"]}
    except Exception: pass

    # ---- rooms -> polygons
    MIN_M2 = 1.0
    rooms = []
    for i in range(1, n):
        if i in border: continue
        area_px = st[i, cv2.CC_STAT_AREA]
        if m_per_px and area_px * m_per_px ** 2 < MIN_M2: continue
        if not m_per_px and area_px < 40 * wall_t ** 2: continue
        mask = (lab == i).astype(np.uint8)
        x, y, w, h = st[i, :4]
        cs, hier = cv2.findContours(mask[y:y + h, x:x + w], cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE, offset=(int(x), int(y)))
        outer = max((c for c, hh in zip(cs, hier[0]) if hh[3] < 0), key=cv2.contourArea)
        holes = [c for c, hh in zip(cs, hier[0]) if hh[3] >= 0 and cv2.contourArea(c) > 4 * wall_t ** 2]
        # thin furniture lines inside a room do not split it (they were never walls), so the region is the room
        poly = cv2.approxPolyDP(outer, 1.0, True)
        A = cv2.contourArea(outer) - sum(cv2.contourArea(c) for c in holes)
        P = cv2.arcLength(poly, True)
        rooms.append({"cc": i, "poly": poly[:, 0, :].tolist(), "holes": [cv2.approxPolyDP(c, 1.0, True)[:, 0, :].tolist() for c in holes],
                      "area_px": float(A), "perim_px": float(P), "bbox": [int(x), int(y), int(x + w), int(y + h)], "cx": float(cen[i][0]), "cy": float(cen[i][1])})

    # ---- 4 buildings: walls + rooms connected
    solid = closed.copy()
    for r in rooms: solid[lab == r["cc"]] = 1
    nb, labb, stb, _ = cv2.connectedComponentsWithStats(solid, 8)
    bid = {}
    for r in rooms:
        b = int(labb[int(r["poly"][0][1]), int(r["poly"][0][0])]) or int(np.bincount(labb[lab == r["cc"]]).argmax())
        r["building"] = bid.setdefault(b, len(bid) + 1)

    # ---- 6 labels
    for r in rooms:
        inside = [o for o in ocr if 0 <= int(o["y"]) < H and 0 <= int(o["x"]) < W and lab[int(o["y"]), int(o["x"])] == r["cc"] and o["conf"] >= 0.6 and re.search(r"[A-Za-z]{2}", o["text"])]
        inside.sort(key=lambda o: (round(o["y"] / 12), o["x"]))
        r["name"] = " ".join(o["text"] for o in inside) or None

    # ---- openings -> rooms (an opening belongs to every room whose region touches its closing segment)
    rid_of = {r["cc"]: j for j, r in enumerate(rooms)}
    for r in rooms: r["openings"] = []
    for oi, o in enumerate(openings):
        x0, y0, x1, y1 = o["seg"]; pad = int(wall_t * 1.2) + 2
        X0, Y0, X1, Y1 = int(max(0, min(x0, x1) - pad)), int(max(0, min(y0, y1) - pad)), int(min(W, max(x0, x1) + pad)), int(min(H, max(y0, y1) + pad))
        ids, cnt = np.unique(lab[Y0:Y1, X0:X1][free[Y0:Y1, X0:X1] > 0], return_counts=True)
        o["rooms"] = [rid_of[i] for i, c in zip(ids, cnt) if i in rid_of and c >= 0.3 * o["width_px"]]
        for j in o["rooms"]: rooms[j]["openings"].append(oi)

    # ---- 7 database
    db = os.path.join(out, "sheet.db")
    if os.path.exists(db): os.remove(db)
    con = sqlite3.connect(db); c = con.cursor()
    c.executescript("""
    create table sheet(file text, width_px int, height_px int, drawing_scale real, paper_long_mm real, m_per_px real, scale_source text, scale_bar_m_per_px real, wall_thickness_m real);
    create table building(id int primary key, rooms int, floor_area_m2 real, bbox_px text);
    create table room(id int primary key, building int, name text, area_m2 real, perimeter_m real, door_width_m real, doors int, windows int, baseboard_m real,
                      cx_px real, cy_px real, bbox_px text, polygon_px text, area_px real, perimeter_px real);
    create table opening(id int primary key, kind text, width_m real, x0_px real, y0_px real, x1_px real, y1_px real, rooms text);
    create table unresolved(kind text, box_px text, status text, why text);
    """)
    f = (m_per_px or float("nan")); inv = 1 / k
    c.execute("insert into sheet values(?,?,?,?,?,?,?,?,?)", (image_name, W0, H0, scale, paper, (m_per_px * k) if m_per_px else None, "; ".join(notes) or "given on the command line",
                                                              (bar["m_per_px_from_text"] * k) if bar else None, wall_t * f if m_per_px else None))
    for j, r in enumerate(rooms):
        dw = sum(openings[o]["width_px"] for o in r["openings"] if openings[o]["kind"] in ("door", "sliding_door", "open_passage"))
        r["area_m2"] = r["area_px"] * f * f; r["perim_m"] = r["perim_px"] * f; r["door_m"] = dw * f; r["base_m"] = (r["perim_px"] - dw) * f
        c.execute("insert into room values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (j + 1, r["building"], r["name"], round(r["area_m2"], 2), round(r["perim_m"], 2), round(r["door_m"], 2),
                  sum(1 for o in r["openings"] if openings[o]["kind"] in ("door", "sliding_door", "open_passage")), sum(1 for o in r["openings"] if openings[o]["kind"] in ("window", "glazed_screen")), round(r["base_m"], 2),
                  r["cx"] * inv, r["cy"] * inv, json.dumps([v * inv for v in r["bbox"]]), json.dumps([[p[0] * inv, p[1] * inv] for p in r["poly"]]), r["area_px"] * inv * inv, r["perim_px"] * inv))
    for oi, o in enumerate(openings):
        c.execute("insert into opening values(?,?,?,?,?,?,?,?)", (oi + 1, o["kind"], round(o["width_px"] * f, 3), *[v * inv for v in o["seg"]], json.dumps([j + 1 for j in o["rooms"]])))
    for o in unclosed: c.execute("insert into unresolved values(?,?,?,?)", (o["kind"], json.dumps([v * inv for v in o["box"]]), o["status"], o["why"]))
    for b in sorted(set(r["building"] for r in rooms)):
        rs = [r for r in rooms if r["building"] == b]
        bb = [min(r["bbox"][0] for r in rs) * inv, min(r["bbox"][1] for r in rs) * inv, max(r["bbox"][2] for r in rs) * inv, max(r["bbox"][3] for r in rs) * inv]
        c.execute("insert into building values(?,?,?,?)", (b, len(rs), round(sum(r["area_m2"] for r in rs), 2), json.dumps(bb)))
    con.commit()

    # ---- quick flags (no model, no search): a room holding two separate names, a named space that is not enclosed.
    # sheet_review.py later finds the exact gap for each and asks a model; until then answers using these rooms say 'Needs review'.
    words = sorted([o for o in ocr if o["conf"] >= 0.6 and re.search(r"[A-Za-z]{3}", o["text"])], key=lambda o: (o["y"], o["x"])); labels = []
    for o in words:
        for Lb in labels:
            if abs(Lb["x"] - o["x"]) < 90 and 0 < o["y"] - Lb["y1"] < 2.2 * max(o["h"], 12): Lb["text"] += " " + o["text"]; Lb["y1"] = o["y"]; Lb["n"] += 1; break
        else: labels.append({"text": o["text"], "x": o["x"], "y": o["y"], "y1": o["y"], "n": 1})
    bbs = []
    for b in sorted(set(r["building"] for r in rooms)):
        rs = [r for r in rooms if r["building"] == b]; bbs.append((min(r["bbox"][0] for r in rs), min(r["bbox"][1] for r in rs), max(r["bbox"][2] for r in rs), max(r["bbox"][3] for r in rs)))
    c.execute("create table review_flag(kind text, space_a text, space_b text, note text, room_id int)"); by = {}
    for Lb in labels:
        x, y = int(Lb["x"]), int((Lb["y"] + Lb["y1"]) / 2)
        if not (0 <= x < W and 0 <= y < H) or not any(b0 <= x <= b2 and b1 <= y <= b3 for b0, b1, b2, b3 in bbs): continue
        cc = int(lab[y, x])
        if cc == 0:
            v = lab[max(0, y - 6):y + 7, max(0, x - 6):x + 7]; v = v[v > 0]; cc = int(np.bincount(v).argmax()) if len(v) else 0
        Lb["yc"] = y; by.setdefault(cc, []).append(Lb)
    for cc, Ls in by.items():
        if cc in rid_of:
            Ls = sorted(Ls, key=lambda q: -q["n"]); base = Ls[0]
            for q in Ls[1:]:
                if m_per_px and math.hypot(q["x"] - base["x"], q["yc"] - base["yc"]) * m_per_px > 1.2:
                    c.execute("insert into review_flag values(?,?,?,?,?)", ("two names in one region", base["text"], q["text"], "not yet reviewed", rid_of[cc] + 1))
        elif cc in border:
            for q in Ls: c.execute("insert into review_flag values(?,?,?,?,?)", ("named space is not enclosed", q["text"], "outside", "not yet reviewed", None))
    con.commit()
    np.savez(os.path.join(out, "state.npz"), closed=closed, lab=lab.astype(np.int32), k=k, wall_t=wall_t, m_per_px=(m_per_px or 0), room_cc=np.array([r["cc"] for r in rooms]))
    json.dump(ocr, open(os.path.join(out, "ocr.json"), "w"))
    summary = {"rooms": len(rooms), "buildings": len(set(r["building"] for r in rooms)), "area_m2": round(sum(r["area_m2"] for r in rooms), 1) if m_per_px else None,
               "doors_closed": sum(o["kind"] == "door" for o in openings), "doors": len(doors), "windows_closed": sum(o["kind"] == "window" for o in openings), "windows": len(wins), "scale": scale, "paper": paper}
    if not debug: return summary
    ov = cv2.cvtColor(g, cv2.COLOR_GRAY2BGR); rng = np.random.RandomState(3); tint = np.zeros_like(ov)
    for j, r in enumerate(rooms):
        col = tuple(int(v) for v in rng.randint(90, 235, 3)); tint[lab == r["cc"]] = col
    ov = np.where(tint.any(2, keepdims=True), (0.55 * ov + 0.45 * tint).astype(np.uint8), ov)
    for j, r in enumerate(rooms):
        cv2.polylines(ov, [np.array(r["poly"], np.int32)], True, (40, 40, 40), 2)
        s = f"{j + 1}: {r['area_m2']:.1f} m2" if m_per_px else f"{j + 1}"
        cv2.putText(ov, s, (int(r["cx"]) - 60, int(r["cy"]) + 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 180), 2, cv2.LINE_AA)
    for o in openings:
        x0, y0, x1, y1 = map(int, o["seg"]); cv2.line(ov, (x0, y0), (x1, y1), (0, 170, 0) if o.get("by") else (0, 0, 255) if o["kind"] == "door" else (255, 120, 0), 6)
    for o in unclosed:
        if o["status"] != "unresolved": continue
        x0, y0, x1, y1 = map(int, o["box"]); cv2.rectangle(ov, (x0, y0), (x1, y1), (0, 200, 255), 3)
    cv2.imwrite(os.path.join(out, "overlay.jpg"), ov, [cv2.IMWRITE_JPEG_QUALITY, 82])
    cv2.imwrite(os.path.join(out, "walls.png"), closed * 255)

    print(f"image {W0}x{H0}, working scale {k}, wall thickness {wall_t:.1f} px" + (f" = {wall_t * f * 1000:.0f} mm" if m_per_px else ""))
    print(f"scale: 1:{scale} paper {paper} mm -> {m_per_px * k * 1000 if m_per_px else None} mm per original pixel | notes {notes} | scale bar {bar}")
    import collections
    print(f"openings closed: {sum(o['kind']=='door' for o in openings)} of {len(doors)} doors, {sum(o['kind']=='window' for o in openings)} of {len(wins)} windows, {sum(o['kind']=='glazed_screen' for o in openings)} side lights; not closed: {dict(collections.Counter((o['kind'], o['status']) for o in unclosed))}")
    print(f"rooms {len(rooms)} in {len(set(r['building'] for r in rooms))} buildings; total {sum(r['area_m2'] for r in rooms):.1f} m2")
    for row in c.execute("select id, rooms, floor_area_m2 from building"): print(" building", row)
    for row in c.execute("select id, building, name, area_m2, perimeter_m, doors, door_width_m, baseboard_m from room order by area_m2 desc limit 45"): print(" ", row)

    return summary

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("image"); ap.add_argument("det"); ap.add_argument("out")
    ap.add_argument("--scale", type=float, default=None, help="drawing scale denominator, e.g. 200 for 1:200 (else read by OCR)")
    ap.add_argument("--paper-mm", type=float, default=None, help="long side of the paper in mm (else from the size printed in the title block)")
    ap.add_argument("--work", type=float, default=0.5, help="working scale for the masks")
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--ocr-cache", default=None, help="json file: OCR result is read from it if present, else written to it")
    ap.add_argument("--closures", default=None, help="json list of reviewed gap closures [{kind, seg:[x0,y0,x1,y1] in original px, by}]")
    ap.add_argument("--debug", action="store_true", help="also write overlay.jpg and walls.png and print the room list")
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    A = trace(a.image, a.det, a.work, a.closures); ocr = []
    if a.ocr_cache and os.path.exists(a.ocr_cache): ocr = json.load(open(a.ocr_cache))
    elif not a.no_ocr: ocr = ocr_tiles(A["g"])
    if a.ocr_cache and ocr and not os.path.exists(a.ocr_cache): json.dump(ocr, open(a.ocr_cache, "w"))
    print(finish(A, ocr, a.out, a.scale, a.paper_mm, debug=a.debug))
