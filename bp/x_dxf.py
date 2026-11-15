"""Extractor: Swiss-Dwellings-style layered DXF -> Store.
Layers: ROOM_OUTLINE, ROOM_TEXT, UNIT_OUTLINE, DOOR, WINDOW, FIXTURE, WALL, ANNOTATION. Units: metres (from ANNOTATION 'Masse in Metern').
Fixture signatures (verified against 2,358 preview + 37,584 full questions): 69-vertex outline (or 4-vertex casing with
an r=0.025 m drain circle when the inner basin curve is clipped) = bathtub, 5-vertex = shower,
48-vertex outline with an adjacent cistern line and no tap circle = toilet, other 48-vertex = washbasin."""
import math, re, collections as C, ezdxf
from shapely.geometry import Polygon, Point, LineString
from shapely.ops import unary_union
from .core import Entity, Store
from .vocab import rtype_of_label

NUM = re.compile(r"^[A-Z]*\d*\.\d+$")
APT = re.compile(r"^(?:Whg\.|App\.|Appt\.?|Apt\.?)\s*([A-Z0-9]+)$")
AREA_TXT = re.compile(r"^([\d.]+)\s*m²$")
OUT_LBL = re.compile(r"sitzplatz|wintergarten|jardin d'hiver|giardino d'inverno|\bpatio\b|\bgarten\b|\barkade\b", re.I)
IN_LBL = re.compile(r"^raum$|^local$|^locale$|waschen|trocknen|lavanderia|buanderie|verkauf|surface de vente|technik|locale tecnico|kinderwagen|fahrbahn", re.I)

def _bbox(p):
    xs = [a for a, _ in p]; ys = [b for _, b in p]; return min(xs), min(ys), max(xs), max(ys)

def _centroid(p):
    xs = [a for a, _ in p]; ys = [b for _, b in p]; return sum(xs) / len(xs), sum(ys) / len(ys)

def _win_normal(pts):
    best = max((math.hypot(pts[(i + 1) % 4][0] - pts[i][0], pts[(i + 1) % 4][1] - pts[i][1]), i) for i in range(4))[1]
    dx, dy = pts[(best + 1) % 4][0] - pts[best][0], pts[(best + 1) % 4][1] - pts[best][1]
    L = max(math.hypot(dx, dy), 1e-9)
    cx, cy = _centroid(pts)
    return cx, cy, -dy / L, dx / L

def extract(path):
    st = Store(path, "dxf-layered"); st.meta["supports"] = ["room","apartment","door","window","toilet","washbasin","bathtub","shower"]; st.meta["extractor"] = "x_dxf"; st.meta["units"] = "m"
    msp = ezdxf.readfile(path).modelspace()
    outlines, texts, units, walls, fix, circles, lines, win_pts = [], [], [], [], [], [], [], []
    shapes = {}     # layer -> {vertex count: n}: lets the audit see shapes that no rule recognised
    for e in msp:
        l, t = e.dxf.layer, e.dxftype(); h = e.dxf.handle
        if t == "LWPOLYLINE":
            pts = [(float(x), float(y)) for x, y in e.get_points("xy")]
            shapes.setdefault(l, {}); shapes[l][len(pts)] = shapes[l].get(len(pts), 0) + 1
            if l == "DOOR" and len(pts) == 5:
                st.add(Entity(f"door:{h}", "door", {}, pts, {"layer": l, "handle": h, "rule": "DOOR polyline with 5 vertices = door leaf"}))
            elif l == "WINDOW" and len(pts) == 4:
                win_pts.append(pts)
                st.add(Entity(f"window:{h}", "window", {}, pts, {"layer": l, "handle": h, "rule": "WINDOW 4-vertex polygon = window"}))
            elif l == "FIXTURE":
                (lines if len(pts) == 2 else fix).append((h, pts))
            elif l == "WALL" and len(pts) >= 3: walls.append(pts)
            elif l == "UNIT_OUTLINE" and len(pts) >= 3: units.append((h, pts))
            elif l == "ROOM_OUTLINE" and len(pts) >= 3: outlines.append((h, pts))
        elif t == "CIRCLE" and l == "FIXTURE": circles.append((float(e.dxf.center.x), float(e.dxf.center.y), float(e.dxf.radius)))
        elif t == "TEXT" and l in ("ROOM_TEXT", "ANNOTATION"):
            texts.append((l, e.dxf.text, float(e.dxf.insert.x), float(e.dxf.insert.y), h))
    for _, s, *_ in [x for x in texts if x[0] == "ANNOTATION"]:
        m = re.search(r"(?:Massstab|Échelle|Echelle|Scala|Scale)\s*1\s*:\s*(\d+)", s)
        if m: st.meta["scale"] = f"1:{m.group(1)}"
        if "Metern" in s or "mètres" in s or "metri" in s: st.meta["units_note"] = s
    # fixtures -----------------------------------------------------------------------------------------------
    bowls_69_48 = [Polygon(p).buffer(0) for _, p in fix if len(p) in (48, 69)]
    tub_drains = [Point(cx, cy) for cx, cy, r in circles if abs(r - 0.025) < 1e-4]
    for h, p in fix:
        n = len(p); x0, y0, x1, y1 = _bbox(p); m = 0.12; amb = None
        if n == 69: k, why = "bathtub", "FIXTURE outline with 69 vertices = bathtub"
        elif n == 5: k, why = "shower", "FIXTURE outline with 5 vertices = shower tray"
        elif n == 48:
            # Toilet = bowl with a cistern line and no tap. Two tests are combined so that rotated plans work too:
            #  cistern: a short line with both ends within 16 cm of the bowl outline (or inside its padded bounding box)
            #  tap: a small circle inside the bowl outline (or inside the padded box when no cistern line hugs the outline)
            B = Polygon(p).buffer(0)
            near = lambda q: x0 - m <= q[0] <= x1 + m and y0 - m <= q[1] <= y1 + m
            hug = any(B.distance(Point(a)) <= 0.16 and B.distance(Point(b)) <= 0.16 for _, (a, b) in lines if ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 < 1.0)
            cistern = hug or any(near(a) and near(b) for _, (a, b) in lines)
            tap = any(r < 0.05 and B.distance(Point(cx, cy)) <= 0.02 for cx, cy, r in circles) or (not hug and any(near((cx, cy)) and r < 0.05 for cx, cy, r in circles))
            if cistern and not tap: k, why = "toilet", "48-vertex bowl with a cistern line beside it and no tap circle = toilet"
            else: k, why = "washbasin", "48-vertex bowl with a tap circle (or no cistern line) = washbasin"
            amb = "neither a cistern line nor a tap circle belongs to this bowl" if not cistern and not tap else None
        elif n == 4 and tub_drains:
            # When a bathtub sits against a clipped wall edge, CAD export may drop the 69-vertex inner curve, leaving
            # the 4-vertex tub casing and its 2.5 cm drain circle outside every 69- and 48-vertex basin.
            P4 = Polygon(p).buffer(0)
            if any(P4.buffer(0.01).contains(pt) and not any(B.buffer(0.02).contains(pt) for B in bowls_69_48) for pt in tub_drains):
                k, why = "bathtub", "4-vertex tub casing with a 2.5 cm drain circle (inner 69-vertex basin clipped) = bathtub"
            else: continue
        else: continue
        ent = st.add(Entity(f"{k}:{h}", k, {}, p, {"layer": "FIXTURE", "handle": h, "rule": why}))
        if amb: ent.attrs["ambiguous"] = amb
    # rooms: group ROOM_TEXT columns (same x): [Whg. X] [name] number [area m²] -------------------------------
    polys = [(h, p, Polygon(p).buffer(0)) for h, p in outlines]
    cols = {}
    for l, s, x, y, h in texts:
        if l == "ROOM_TEXT": cols.setdefault(round(x, 3), []).append((y, s, h))
    apt_labels = []     # [name, x, y, text_handle, seed_room_or_None]
    for x, items in cols.items():
        items.sort(key=lambda a: -a[0]); name = None; last_room = None; pending_apt = None
        for y, s, h in items:
            m = APT.match(s)
            if m:
                rec = [m.group(1), x, y, h, None]
                apt_labels.append(rec); pending_apt = rec; continue
            if NUM.match(s):
                pt = Point(x, y)
                hits = [(ph, pp, P) for ph, pp, P in polys if P.contains(pt)]
                hit = min(hits, key=lambda h: h[2].area) if hits else None   # nested outlines: the innermost one is the room
                a = dict(number=s, label=name)
                ent = Entity(f"room:{s}", "room", a, hit[1] if hit else None, {"layer": "ROOM_OUTLINE/ROOM_TEXT", "handle": hit[0] if hit else None, "text_handle": h, "label_xy": [x, y]})
                if hit:
                    a["area_gross"] = round(hit[2].area, 2); a["perimeter"] = round(hit[2].length, 2)
                else: st.meta["warnings"].append(f"room {s}: no ROOM_OUTLINE polygon contains its label")
                st.add(ent); last_room = ent; name = None
                if pending_apt is not None: pending_apt[4] = ent; pending_apt = None
            elif AREA_TXT.match(s) and last_room is not None:
                last_room.attrs["area_printed"] = float(AREA_TXT.match(s).group(1))
            else: name = s
    # net area = outline polygon minus the un-numbered outlines enclosed in it (columns, shafts, ducts).
    used = {r.src["handle"] for r in st.of("room") if r.src.get("handle")}
    free = [(h, p, P) for h, p, P in polys if h not in used]
    owner = {}
    for r in st.of("room"):
        if not r.geom: continue
        P = Polygon(r.geom).buffer(0)
        holes = [(h, p, U) for h, p, U in free if U.area < 0.5 * P.area and P.contains(U)]
        for h, _, _ in holes: owner[h] = r.id
        hole_area = unary_union([U for _, _, U in holes]).area if holes else 0.0     # nested outlines must not be subtracted twice
        net = P.area - hole_area
        r.attrs["area"] = round(net, 2)
        r.attrs["area_rule"] = (f"area of the ROOM_OUTLINE polygon ({len(r.geom)} vertices) containing the room number = {P.area:.2f} m²"
            + (f", minus {len(holes)} enclosed un-numbered outline(s) (columns/shafts) totalling {hole_area:.2f} m²" if holes else ""))
        if holes: r.attrs["holes"] = [{"handle": h, "area": round(U.area, 2)} for h, _, U in holes]; r.src["hole_geoms"] = [p for _, p, _ in holes]
    st.meta["raw"] = {"shapes": shapes, "unlabelled": [{"handle": h, "geom": p, "area": round(U.area, 2), "inside": owner.get(h)} for h, p, U in free]}
    # adjacency: a door (or window) belongs to the (at most two) rooms whose outline is closest to it, within 25 cm.
    rpoly = [(r, Polygon(r.geom).buffer(0)) for r in st.of("room") if r.geom]
    rpoly_map = {r.id: P for r, P in rpoly}
    d_adj = C.defaultdict(list)     # room.id -> [(other_room, room_to_room_wall_gap)]
    for r, _ in rpoly: r.attrs["n_doors"] = 0; r.attrs["n_windows"] = 0; r.attrs["_nb"] = set()
    for k in ("door", "window"):
        for e in st.of(k):
            G = Polygon(e.geom).buffer(0) if len(e.geom) >= 3 else LineString(e.geom)
            cand = sorted(((P.distance(G), r.get("number"), r) for r, P in rpoly if P.distance(G) <= 0.25), key=lambda t: t[:2])
            near = [r for _, _, r in cand[:2]]
            e.attrs["rooms"] = ", ".join(sorted(r.get("number") for r in near)); e.attrs["n_rooms"] = len(near)
            e.attrs["rooms_rule"] = "the (at most two) rooms whose outline is closest to this opening, within 25 cm"
            for r in near:
                r.attrs["n_doors" if k == "door" else "n_windows"] += 1
                if k == "door": r.attrs["_nb"] |= {x.get("number") for x in near if x is not r}
            if k == "door":
                e.attrs["door_kind"] = "between two rooms" if len(near) == 2 else ("to outside or an un-numbered space" if len(near) == 1 else "touches no numbered room")
                if len(near) == 2:
                    r1, r2 = near; gap = rpoly_map[r1.id].distance(rpoly_map[r2.id])
                    d_adj[r1.id].append((r2, gap)); d_adj[r2.id].append((r1, gap))
    # room types: from label, else sanitary fixtures, else exterior balcony/loggia geometry ------------------
    fixtures = [e for e in st.entities if e.kind in ("toilet", "washbasin", "bathtub", "shower")]
    for r in st.of("room"):
        lbl = (r.get("label") or "").strip()
        rt = rtype_of_label(lbl)
        if rt == "elevator":
            rt = "lift"; r.attrs["rtype_rule"] = f"label '{lbl}' (numbered lift room)"
        elif rt is None and lbl:
            if OUT_LBL.search(lbl): rt = "outdoor"; r.attrs["rtype_rule"] = f"label '{lbl}'"
            elif IN_LBL.search(lbl): rt = "room"; r.attrs["rtype_rule"] = f"label '{lbl}'"
        elif rt is not None:
            r.attrs["rtype_rule"] = f"label '{lbl}'"
        if r.geom:
            P = rpoly_map[r.id].buffer(0.05)
            inside = [f for f in fixtures if P.contains(Point(*_centroid(f.geom)))]
            r.attrs["fixtures"] = sorted(f.id for f in inside)
            for f in inside: f.attrs["room"] = r.id
            kinds = {f.kind for f in inside}
            if rt is None:
                if "toilet" in kinds and kinds & {"bathtub", "shower"}: rt, why = "bathroom", "no label; contains toilet + bath/shower"
                elif "toilet" in kinds: rt, why = "wc", "no label; contains a toilet only"
                elif kinds & {"bathtub", "shower"}: rt, why = "bathroom", "no label; contains bath/shower"
                else: why = None
                if why: r.attrs["rtype_rule"] = why
        r.attrs["rtype"] = rt
        r.attrs["indoor"] = rt != "outdoor"
    if any(r.get("rtype") is None and not r.get("label") for r, _ in rpoly):
        _infer_balconies(st, rpoly, d_adj, win_pts, walls, fix, circles)
    # apartments: innermost UNIT_OUTLINE (accounting for core cutout holes), shared-outline split, split-unit merge
    upolys = [(uh, up, Polygon(up).buffer(0)) for uh, up in units]
    u_holes = {uh: [(uh2, up2, U2) for uh2, up2, U2 in upolys if uh2 != uh and U2.area < U.area and U.buffer(0.05).contains(U2)] for uh, _, U in upolys}
    u_net = {uh: (U.difference(unary_union([U2 for _, _, U2 in hs])) if hs else U) for (uh, _, U), hs in ((u, u_holes[u[0]]) for u in upolys)}
    def _inner_unit(x, y):
        hits = [(uh, up, U) for uh, up, U in upolys if U.buffer(0.02).contains(Point(x, y))]
        return min(hits, key=lambda t: t[2].area) if hits else None
    u_apts = C.defaultdict(list)    # uh -> [(apt_entity, x, y, seed_room)]
    seen_apt = {}
    for name, x, y, h, seed_room in apt_labels:
        U = _inner_unit(x, y)
        if U is None and seed_room is not None:
            sx, sy = seed_room.src["label_xy"]; U = _inner_unit(sx, sy)
        if name in seen_apt:
            ap = seen_apt[name]
        else:
            src = {"layer": "UNIT_OUTLINE", "handle": U[0] if U else None, "text_handle": h}
            if U and u_holes[U[0]]: src["hole_geoms"] = [up2 for _, up2, _ in u_holes[U[0]]]
            ap = st.add(Entity(f"apartment:{name}", "apartment", {"name": name}, U[1] if U else None, src))
            if U: ap.attrs["outline_area"] = round(u_net[U[0]].area, 2)
            seen_apt[name] = ap
        if U: u_apts[U[0]].append((ap, x, y, seed_room))
    if not apt_labels:
        for i, (uh, up, _) in enumerate(upolys):
            if any(uh2 != uh and U2.area > upolys[i][2].area and U2.buffer(0.05).contains(upolys[i][2]) for uh2, _, U2 in upolys): continue
            src = {"layer": "UNIT_OUTLINE", "handle": uh}
            if u_holes[uh]: src["hole_geoms"] = [up2 for _, up2, _ in u_holes[uh]]
            ap = st.add(Entity(f"apartment:{i+1}", "apartment", {"name": str(i + 1), "outline_area": round(u_net[uh].area, 2)}, up, src))
            u_apts[uh].append((ap, *_centroid(up), None))
    u_rooms = C.defaultdict(list)
    for r in st.of("room"):
        lx, ly = r.src["label_xy"]
        U = _inner_unit(lx, ly)
        if U: u_rooms[U[0]].append(r)
    for uh, aps in u_apts.items():
        uniq = list({ap.get("name"): (ap, x, y, sr) for ap, x, y, sr in reversed(aps)}.values())[::-1]
        if len(uniq) == 1:
            nm = uniq[0][0].get("name")
            for r in u_rooms[uh]: r.attrs["apartment"] = nm
        elif len(uniq) > 1:
            _split_shared_unit(uniq, u_rooms[uh], d_adj)
    # Top-level unlabelled UNIT_OUTLINE (not an enclosed core hole): merge if its rooms connect via doors to one apartment
    for uh, up, U in upolys:
        if uh in u_apts: continue
        if any(uh2 != uh and U2.area > U.area and U2.buffer(0.05).contains(U) for uh2, _, U2 in upolys): continue
        rs_u = u_rooms[uh]
        if not rs_u or any(r.get("rtype") in ("stair", "lift") for r in rs_u): continue
        conn = {r2.get("apartment") for r in rs_u for r2, _ in d_adj[r.id] if r2.get("apartment")}
        if len(conn) == 1:
            nm = next(iter(conn))
            for r in rs_u: r.attrs["apartment"] = nm
            ap = seen_apt.get(nm)
            if ap is not None:
                ap.src.setdefault("extra_geoms", []).append(up)
                if ap.get("outline_area"): ap.attrs["outline_area"] = round(ap.attrs["outline_area"] + u_net[uh].area, 2)
    # Propagate finalized apartment and room labels to fixtures, openings, and connectivity strings ----------
    for r in st.of("room"):
        for fid in r.get("fixtures") or ():
            f = st.by_id.get(fid) if hasattr(st, "by_id") else next((x for x in fixtures if x.id == fid), None)
            if f: f.attrs["apartment"] = r.get("apartment")
    apts_net = [(a, u_net[a.src["handle"]]) for a in st.of("apartment") if a.src.get("handle") in u_net]
    rm_by_num = {r.get("number"): r for r in st.of("room")}
    for k in ("door", "window"):
        for e in st.of(k):
            touched_apts = {rm_by_num[n].get("apartment") for n in (e.get("rooms") or "").split(", ") if n in rm_by_num and rm_by_num[n].get("apartment")}
            if len(touched_apts) == 1:
                e.attrs["apartment"] = next(iter(touched_apts))
            else:
                c = Point(*_centroid(e.geom))
                hits = [(a, Pn) for a, Pn in apts_net if Pn.buffer(0.3).contains(c) and not a.get("shared_outline_split")]
                if hits: e.attrs["apartment"] = min(hits, key=lambda t: t[1].area)[0].get("name")
    lab = {r.get("number"): (r.get("label") or r.get("rtype") or "?") for r, _ in rpoly}
    for r, _ in rpoly:
        nb = sorted(r.attrs.pop("_nb")); r.attrs["connected_to"] = ", ".join(f"{n} ({lab.get(n)})" for n in nb); r.attrs["n_connected"] = len(nb)
    return st

def _split_shared_unit(aps, rooms, d_adj):
    """Split rooms inside one shared UNIT_OUTLINE between multiple apartments via door-graph BFS + nearest label."""
    in_set = {r.id: r for r in rooms}
    dist_map = {}; owner = {}; q = C.deque()
    for idx, (ap, x, y, sr) in enumerate(aps):
        ap.attrs["shared_outline_split"] = True
        seed = sr if (sr is not None and sr.id in in_set) else None
        if seed is None and rooms:
            seed = min(rooms, key=lambda r: math.hypot(r.src["label_xy"][0] - x, r.src["label_xy"][1] - y))
        if seed is not None and seed.id not in owner:
            owner[seed.id] = idx; dist_map[seed.id] = 0; q.append(seed.id)
    while q:
         cur = q.popleft(); d0 = dist_map[cur]; idx = owner[cur]
         for nb, _ in d_adj[cur]:
             if nb.id not in in_set or nb.get("rtype") in ("stair", "lift"): continue
             if nb.id not in dist_map:
                 dist_map[nb.id] = d0 + 1; owner[nb.id] = idx; q.append(nb.id)
             elif dist_map[nb.id] == d0 + 1 and owner[nb.id] != idx:
                 lx, ly = nb.src["label_xy"]
                 _, x1, y1, _ = aps[owner[nb.id]]; _, x2, y2, _ = aps[idx]
                 if math.hypot(lx - x2, ly - y2) < math.hypot(lx - x1, ly - y1): owner[nb.id] = idx
    for r in rooms:
        if r.id in owner:
            nm = aps[owner[r.id]][0].get("name"); how = "door connectivity"
        else:
            lx, ly = r.src["label_xy"]
            nm = min(aps, key=lambda t: math.hypot(lx - t[1], ly - t[2]))[0].get("name"); how = "nearest apartment label"
        r.attrs["apartment"] = nm; r.attrs["apartment_rule"] = f"shared unit outline split by {how}"

def _infer_balconies(st, rpoly, d_adj, win_pts, walls, fix, circles):
    """Identify unlabelled balconies/loggias from facade windows, open-air parapet thickness, and door wall thickness."""
    W_u = unary_union([Polygon(w).buffer(0) for w in walls]) if walls else Polygon()
    Win_u = unary_union([Polygon(w).buffer(0) for w in win_pts]) if win_pts else Polygon()
    Door_u = unary_union([Polygon(e.geom).buffer(0) for e in st.of("door")]) if st.of("door") else Polygon()
    fix_pts = [Polygon(p).buffer(0).centroid for _, p in fix if len(p) >= 3] + [Point(cx, cy) for cx, cy, _ in circles]
    win_pairs = []
    for pts in win_pts:
        cx, cy, nx, ny = _win_normal(pts); sp = sn = None
        for dist in (0.15, 0.25, 0.35, 0.48, 0.60):
            if sp is None:
                pt = Point(cx + dist * nx, cy + dist * ny); sp = next((r for r, P in rpoly if P.contains(pt)), None)
            if sn is None:
                pt = Point(cx - dist * nx, cy - dist * ny); sn = next((r for r, P in rpoly if P.contains(pt)), None)
        win_pairs.append((sp, sn))
    for r, P in rpoly:
        if r.get("rtype") is not None or r.get("label"): continue
        if any(P.buffer(0.03).contains(pt) for pt in fix_pts): continue
        nbs = d_adj[r.id]
        if not nbs: continue
        nb_types = {r2.get("rtype") for r2, _ in nbs}
        if nb_types & {"stair", "lift", "corridor", "wc"} or len({r2.id for r2, _ in nbs}) >= 3: continue
        min_dgap = min(g for _, g in nbs)
        if "bathroom" in nb_types and min_dgap < 0.20: continue
        w_open = sum(1 for a, b in win_pairs if (a is r and b is None) or (b is r and a is None))
        w_in_rooms = [b if a is r else a for a, b in win_pairs
                      if (a is r and b is not None and b is not r and b.get("rtype") not in ("outdoor", None))
                      or (b is r and a is not None and a is not r and a.get("rtype") not in ("outdoor", None))]
        pts = list(P.exterior.coords); min_ext_wt = 9.9; open_edge = False
        for i in range(len(pts) - 1):
            seg = LineString([pts[i], pts[i + 1]])
            if seg.length < 0.50: continue
            seg_nd = seg.difference(Door_u.buffer(0.15))
            if seg_nd.length < 0.35: continue
            mx, my = seg.interpolate(0.5, normalized=True).coords[0]
            dx, dy = (pts[i + 1][0] - pts[i][0]) / seg.length, (pts[i + 1][1] - pts[i][1]) / seg.length
            nx, ny = (-dy, dx) if not P.contains(Point(mx - 0.05 * dy, my + 0.05 * dx)) else (dy, -dx)
            if any(P2.contains(Point(mx + d * nx, my + d * ny)) for d in (0.35, 0.70, 1.10) for r2, P2 in rpoly if r2 is not r): continue
            if seg.intersection(Win_u.buffer(0.12)).length > 0.20: continue
            slab = Polygon([(pts[i][0] + 0.01 * nx, pts[i][1] + 0.01 * ny), (pts[i + 1][0] + 0.01 * nx, pts[i + 1][1] + 0.01 * ny),
                            (pts[i + 1][0] + 0.65 * nx, pts[i + 1][1] + 0.65 * ny), (pts[i][0] + 0.65 * nx, pts[i][1] + 0.65 * ny)]).difference(Door_u.buffer(0.15))
            wt = W_u.intersection(slab).area / seg_nd.length
            open_edge = True; min_ext_wt = min(min_ext_wt, wt)
        why = None
        if w_in_rooms:
            why = f"no label; window separates it from indoor room {w_in_rooms[0].get('number')}"
        elif w_open == 0 and open_edge and min_ext_wt < 0.16:
            why = f"no label; open-air edge has thin parapet ({min_ext_wt:.2f} m) and no exterior windows"
        elif w_open == 0 and min_dgap >= 0.24 and min_ext_wt < min_dgap - 0.02:
            why = f"no label; outside thick facade wall ({min_dgap:.2f} m vs {min_ext_wt:.2f} m parapet) with no exterior windows"
        if why:
            r.attrs["rtype"] = "outdoor"; r.attrs["indoor"] = False; r.attrs["rtype_rule"] = why
