"""3D scene of a drawing for the studio page (web/chat/studio3d.js draws it with three.js).

Ported from the team's viewer toolkit (stoajunction: junction_engine/geometry_3d.py and the scene part of bp_bridge.py),
rebuilt on this engine's own data:

  IFC model       real geometry of the model, triangulated by ifcopenshell. Nothing is assumed.
  traced sheet    the room outlines, doors and windows of the sheet database, stood up as walls.
                  The plan is flat, so every HEIGHT IS ASSUMED (see ASSUMED) and the scene says so.
  layered DXF     room outlines of the store, same treatment.
  anything else   no scene: a table or a sheet without traced rooms has nothing to stand up, and the caller is told why.

Axes follow three.js: X right, Y up, Z towards the viewer (the plan's downward y). Units are metres; the scene is
centred on the origin. Elements:
  prism  {outer:[[x,z]..], holes:[[[x,z]..]..], y0, h}   extruded in the browser (it triangulates holes itself)
  mesh   {verts:[x,y,z..], faces:[i..]}
  cyl    {pos:[x,y,z], r, h}
  box    {pos:[x,y,z], size:[sx,sy,sz]}
each with  cat (wall, floor, door, window, glass, slab, roof, space, device, seat, ...), id (the entity id the rest of the
app uses, so a room clicked in 3D is the same room as on the sheet) and name."""
import os, json, math, hashlib, sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "..", "cache", "scene3d")
VERSION = "4"
ASSUMED = {"wall_height_m": 2.7, "door_height_m": 2.1, "window_sill_m": 0.9, "window_head_m": 2.1}

IFC_CAT = {"IfcWall": "wall", "IfcWallStandardCase": "wall", "IfcCurtainWall": "glass", "IfcSlab": "slab", "IfcRoof": "roof", "IfcSpace": "space", "IfcColumn": "column",
           "IfcBeam": "beam", "IfcStair": "stair", "IfcStairFlight": "stair", "IfcRamp": "stair", "IfcDoor": "door", "IfcWindow": "window", "IfcPlate": "glass",
           "IfcFurnishingElement": "furniture", "IfcFurniture": "furniture", "IfcSanitaryTerminal": "fixture", "IfcFlowTerminal": "fixture", "IfcMember": "beam",
           "IfcRailing": "railing", "IfcCovering": "covering", "IfcFooting": "slab", "IfcBuildingElementProxy": "other", "IfcFlowSegment": "mep", "IfcFlowFitting": "mep"}
IFC_INCLUDE = list(IFC_CAT)
MAX_FLOATS = 2_400_000      # vertex numbers in one scene; elements past this are sent as their bounding box


class NoScene(Exception):
    """this file has nothing that can be shown in 3D; the message is for the user"""


def _ring(coords, cx, cz, k=1.0, nd=3):
    pts = [[round(x * k - cx, nd), round(y * k - cz, nd)] for x, y in coords]
    if len(pts) > 1 and pts[0] == pts[-1]: pts.pop()
    return pts

def _prisms(geom, cx, cz, k, y0, h, **kw):
    """shapely polygon(s) in drawing units -> prism elements in metres"""
    out = []
    for g in (getattr(geom, "geoms", None) or [geom]):
        if g.is_empty or g.geom_type != "Polygon" or g.area * k * k < 0.004: continue
        outer = _ring(g.exterior.coords, cx, cz, k)
        if len(outer) < 3: continue
        out.append({"kind": "prism", "outer": outer, "holes": [r for r in (_ring(i.coords, cx, cz, k) for i in g.interiors) if len(r) >= 3], "y0": y0, "h": h, **kw})
    return out


def _stand_up(rooms, openings, k, wall_t, extra=None):
    """rooms: [(id, name, area, polygon)], openings: [(kind, (x0, y0), (x1, y1))] in drawing units; k = metres per unit.
    Walls are the band around and between the room outlines; doors and windows are cut out of it where the tracer found them."""
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    A = ASSUMED; H = A["wall_height_m"]; t = wall_t / k
    floor = unary_union([p for *_x, p in rooms])
    mass = unary_union([p.buffer(t * 0.62, join_style=2) for *_x, p in rooms]).difference(floor).simplify(t * 0.04)
    x0, y0, x1, y1 = mass.bounds; cx = (x0 + x1) / 2 * k; cz = (y0 + y1) / 2 * k
    cuts = {"door": [], "window": [], "glass": [], "open": []}
    for kind, a, b in openings:
        if math.dist(a, b) * k < 0.3: continue
        rect = LineString([a, b]).buffer(t * 1.3, cap_style=2)
        cuts[{"door": "door", "sliding_door": "door", "window": "window", "glazed_screen": "glass", "open_passage": "open"}.get(kind, "")].append(rect) if kind in ("door", "sliding_door", "window", "glazed_screen", "open_passage") else None
    U = {c: unary_union(v) if v else None for c, v in cuts.items()}
    solid = mass
    for u in U.values():
        if u is not None: solid = solid.difference(u)
    el = _prisms(mass.buffer(0.35 / k), cx, cz, k, -0.12, 0.12, cat="slab", id="slab", name="Floor slab")
    for rid, name, area, p in rooms:
        el += _prisms(p, cx, cz, k, 0.0, 0.03, cat="floor", id=rid, name=name or "Unnamed room", area=area)
    el += _prisms(solid, cx, cz, k, 0.0, H, cat="wall", name="Wall")
    dh, s, wh = A["door_height_m"], A["window_sill_m"], A["window_head_m"]
    if U["door"] is not None: el += _prisms(mass.intersection(U["door"]), cx, cz, k, dh, H - dh, cat="wall", name="Wall over a door")
    if U["window"] is not None:
        w = mass.intersection(U["window"])
        el += _prisms(w, cx, cz, k, 0.0, s, cat="wall", name="Wall under a window") + _prisms(w, cx, cz, k, wh, H - wh, cat="wall", name="Wall over a window")
        el += _prisms(w.buffer(-t * 0.22), cx, cz, k, s, wh - s, cat="glass", name="Window")
    if U["glass"] is not None:
        g = mass.intersection(U["glass"])
        el += _prisms(g, cx, cz, k, dh, H - dh, cat="wall", name="Wall over a glazed screen") + _prisms(g.buffer(-t * 0.22), cx, cz, k, 0.0, dh, cat="glass", name="Glazed screen")
    marks = []
    for rid, name, area, p in rooms:
        c = p.representative_point(); marks.append({"id": rid, "name": name, "c": [round(c.x * k - cx, 3), round(c.y * k - cz, 3)]})
    for e in extra or []:
        e = dict(e); e["pos"] = [round(e["pos"][0] * k - cx, 3), e["pos"][1], round(e["pos"][2] * k - cz, 3)]; el.append(e)
    return {"elements": el, "rooms": marks, "size": [round((x1 - x0) * k, 2), H, round((y1 - y0) * k, 2)], "assumed": dict(A), "origin": [round(cx, 3), round(cz, 3)],
            "note": f"Heights are assumed, because the drawing is a flat plan: walls {H} m, doors {dh} m, windows from {s} to {wh} m. Room outlines, doors and windows are the traced ones."}


def scene_sheet(db, sidecar=None):
    """traced sheet (cache/sheet_rooms/<doc>/sheet.db) -> scene"""
    from shapely.geometry import Polygon
    con = sqlite3.connect(db); mpp, wt = con.execute("select m_per_px, wall_thickness_m from sheet").fetchone()
    if not mpp: raise NoScene("The scale of this drawing was not found, so its rooms cannot be stood up at their real size.")
    rooms = []
    for i, name, area, pg in con.execute("select id, name, area_m2, polygon_px from room"):
        p = Polygon(json.loads(pg)).buffer(0)
        if not p.is_empty and p.area > 0: rooms.append((f"room:{i}", name, area, p))
    if len(rooms) < 1: raise NoScene("No rooms were traced on this drawing, so there is nothing to stand up in 3D.")
    ops = [(k_, (x0, y0), (x1, y1)) for k_, x0, y0, x1, y1 in con.execute("select kind, x0_px, y0_px, x1_px, y1_px from opening")]
    extra = []
    if sidecar and os.path.exists(sidecar):
        dm = json.load(open(sidecar)); H = ASSUMED["wall_height_m"]
        if dm.get("layer") == "fire":
            for d in dm.get("detectors") or []: extra.append({"kind": "cyl", "cat": "device", "name": f"{d.get('type')} detector", "room": f"room:{d.get('room')}", "pos": [d["x"], H - 0.05, d["y"]], "r": 0.16, "h": 0.08})
            for d in dm.get("other_devices") or []:
                nm = {"mcp": "manual call point", "sounder": "alarm sounder", "panel": "control panel"}.get(d["kind"], d["kind"])
                extra.append({"kind": "box", "cat": "device2", "name": nm, "room": f"room:{d.get('room')}", "pos": [d["x"], 1.4, d["y"]], "size": [0.6, 0.8, 0.6] if d["kind"] == "panel" else [0.22, 0.22, 0.22]})
        else:
            for sb in dm.get("seat_boxes") or []:
                b = sb["box"]; extra.append({"kind": "box", "cat": "seat", "name": "seat", "room": f"room:{sb['room']}", "pos": [(b[0] + b[2]) / 2, 0.24, (b[1] + b[3]) / 2], "size": [round((b[2] - b[0]) * mpp, 3), 0.45, round((b[3] - b[1]) * mpp, 3)]})
    sc = _stand_up(rooms, ops, mpp, wt or 0.2, extra); sc["source"] = "traced sheet"
    W, Hh = con.execute("select width_px, height_px from sheet").fetchone(); sc["frame"] = [round(W * mpp, 3), round(Hh * mpp, 3)]   # a point given as a fraction of the image -> metres
    if extra: sc["note"] += " Sample plan: the fire devices and the seats were planted for the demonstration."
    return sc


def scene_store(st):
    """any store with room outlines (layered DXF): rooms stood up the same way. Units are guessed from the size of the plan."""
    from shapely.geometry import Polygon
    rooms = []
    for e in st.of("room"):
        if e.geom and len(e.geom) >= 3:
            p = Polygon(e.geom).buffer(0)
            if not p.is_empty and p.area > 0: rooms.append((e.id, e.get("name") or e.get("label") or e.get("rtype"), e.get("area"), p))
    if not rooms: raise NoScene("This file has no room outlines, so there is nothing to show in 3D. Tables and schedules are read as data only.")
    xs = [c for *_x, p in rooms for c in (p.bounds[0], p.bounds[2])]; ys = [c for *_x, p in rooms for c in (p.bounds[1], p.bounds[3])]
    span = max(max(xs) - min(xs), max(ys) - min(ys)); k = 0.001 if span > 800 else 1.0
    # CAD y points up, the screen's z points down: mirror so the plan is not flipped
    from shapely.affinity import scale as _sc
    rooms = [(i, n, a, _sc(p, 1, -1, origin=(0, 0))) for i, n, a, p in rooms]
    ops = []
    for kind in ("door", "window"):
        for e in st.of(kind):
            g = e.geom or []
            if len(g) >= 2:
                best = max(((math.dist(g[i], g[(i + 1) % len(g)]), g[i], g[(i + 1) % len(g)]) for i in range(len(g))), key=lambda x: x[0])
                ops.append((kind, (best[1][0], -best[1][1]), (best[2][0], -best[2][1])))
    sc = _stand_up(rooms, ops, k, 0.2, None); sc["source"] = "room outlines of the file"
    sc["note"] = sc["note"].replace("the traced ones", "taken from the file") + " Wall thickness is assumed 0.2 m."
    return sc


def scene_ifc(path):
    """IFC model -> its own geometry (world coordinates, metres)"""
    import numpy as np, ifcopenshell, ifcopenshell.geom as G, ifcopenshell.util.element as UE, multiprocessing
    model = ifcopenshell.open(path); s = G.settings()
    try: s.set("use-world-coords", True)
    except Exception: s.set(s.USE_WORLD_COORDS, True)      # ifcopenshell before 0.8
    present = [c for c in IFC_INCLUDE if _has(model, c)]
    if not present: raise NoScene("This model has no building elements with geometry to show.")
    it = G.iterator(s, model, min(8, multiprocessing.cpu_count()), include=present)
    if not it.initialize(): raise NoScene("The geometry of this model could not be read.")
    el = []; lo = np.full(3, 1e18); hi = np.full(3, -1e18); floats = 0; boxed = 0; levels = {}
    def level_of(e):
        try:
            c = UE.get_container(e)
            while c is not None and not c.is_a("IfcBuildingStorey"): c = UE.get_aggregate(c)
            return c.Name if c is not None else None
        except Exception: return None
    while True:
        sh = it.get()
        try: e = model.by_id(int(sh.id)); g = sh.geometry; v = np.asarray(g.verts, dtype=np.float64).reshape(-1, 3); f = np.asarray(g.faces, dtype=np.int64)
        except Exception: e = None
        if e is not None and len(v) >= 3 and len(f) >= 3 and np.isfinite(v).all():
            cat = IFC_CAT.get(e.is_a(), "other")
            if e.is_a("IfcSlab") and str(getattr(e, "PredefinedType", "") or "").upper() == "ROOF": cat = "roof"
            w = np.empty_like(v); w[:, 0] = v[:, 0]; w[:, 1] = v[:, 2]; w[:, 2] = -v[:, 1]
            lo = np.minimum(lo, w.min(0)); hi = np.maximum(hi, w.max(0))
            lv = level_of(e); levels[lv] = levels.get(lv, 0) + 1
            base = {"cat": cat, "id": e.GlobalId, "name": getattr(e, "LongName", None) or e.Name or e.is_a(), "ifc_class": e.is_a(), "level": lv}
            if floats + w.size > MAX_FLOATS:
                a, b = w.min(0), w.max(0); el.append({"kind": "box", "pos": ((a + b) / 2).tolist(), "size": np.maximum(b - a, 0.02).tolist(), **base}); boxed += 1
            else:
                floats += w.size; el.append({"kind": "mesh", "_v": w, "faces": f.tolist(), **base})
        if not it.next(): break
    if not el: raise NoScene("This model has no building elements with geometry to show.")
    c = np.array([(lo[0] + hi[0]) / 2, lo[1], (lo[2] + hi[2]) / 2])
    for x in el:
        if "_v" in x: x["verts"] = np.round(x.pop("_v") - c, 3).ravel().tolist()
        else: x["pos"] = np.round(np.asarray(x["pos"]) - c, 3).tolist(); x["size"] = np.round(x["size"], 3).tolist()
    note = "The model's own geometry. Nothing is assumed."
    if boxed: note += f" {boxed} of {len(el)} elements are shown as their bounding box to keep the scene light."
    return {"elements": el, "rooms": [], "size": np.round(hi - lo, 2).tolist(), "assumed": None, "note": note, "source": "IFC model",
            "levels": [k for k in levels if k]}

def _has(model, cls):
    try: return bool(model.by_type(cls, include_subtypes=False))
    except Exception: return False


def scene_for(st, path=None):
    """-> scene dict (cached on disk by file content), or raises NoScene with the reason"""
    path = path or st.path; fmt = (st.fmt or "").lower()
    db = side = None
    if fmt == "raster":
        d = os.path.join(HERE, "..", "cache", "sheet_rooms", os.path.basename(st.path)[:-4]); db = os.path.join(d, "sheet.db"); side = os.path.join(d, "demo.json")
        if not os.path.exists(db): raise NoScene("No rooms were traced on this drawing, so there is nothing to stand up in 3D.")
    try: stamp = f"{VERSION}:{os.path.getsize(path)}:{os.path.getmtime(path)}:{os.path.getmtime(db) if db else 0}:{os.path.getmtime(side) if side and os.path.exists(side) else 0}:{os.path.abspath(path)}"
    except OSError: stamp = None
    cf = os.path.join(CACHE, hashlib.sha1(stamp.encode()).hexdigest()[:20] + ".json") if stamp else None
    if cf and os.path.exists(cf):
        try: return json.load(open(cf))
        except Exception: pass
    if fmt == "ifc" or str(path).lower().endswith(".ifc"): sc = scene_ifc(path)
    elif db: sc = scene_sheet(db, side)
    else: sc = scene_store(st)
    sc["format"] = "essatto-scene-1"; sc["counts"] = {}
    for e in sc["elements"]: sc["counts"][e["cat"]] = sc["counts"].get(e["cat"], 0) + 1
    if cf:
        os.makedirs(CACHE, exist_ok=True); tmp = cf + ".tmp"
        json.dump(sc, open(tmp, "w"), separators=(",", ":")); os.replace(tmp, cf)
    return sc


if __name__ == "__main__":
    import sys, time
    sys.path.insert(0, os.path.join(HERE, ".."))
    t = time.time(); p = sys.argv[1]
    if p.endswith(".db"): sc = scene_sheet(p, os.path.join(os.path.dirname(p), "demo.json"))
    else: sc = scene_ifc(p)
    js = json.dumps(sc, separators=(",", ":"))
    print(f"{time.time() - t:.1f}s  {len(sc['elements'])} elements  {len(js) / 1e6:.2f} MB  size {sc['size']}  {dict(sorted(((e['cat'], sum(1 for x in sc['elements'] if x['cat'] == e['cat'])) for e in sc['elements'])))}")
    print(sc["note"])
