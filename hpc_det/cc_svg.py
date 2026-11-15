"""Parse a CubiCasa5k model.svg into symbols and rooms, in F1_original.png pixel coordinates.
Each item: {"cls": full class string, "box": [x0,y0,x1,y1], "poly": [(x,y)...]}. Transforms (matrix) are applied along the
whole ancestor chain. Door/Window boxes cover every polygon of the group (leaf, swing arc, frame)."""
import re, numpy as np
from lxml import etree
from PIL import Image
NS = "{http://www.w3.org/2000/svg}"
def _mat(el):
    t = el.get("transform")
    if not t: return np.eye(3)
    m = re.search(r"matrix\(([^)]*)\)", t)
    if m:
        a, b, c, d, e, f = [float(v) for v in re.split(r"[ ,]+", m.group(1).strip())]
        return np.array([[a, c, e], [b, d, f], [0, 0, 1]])
    m = re.search(r"translate\(([^)]*)\)", t)
    if m:
        v = [float(x) for x in re.split(r"[ ,]+", m.group(1).strip())]; return np.array([[1, 0, v[0]], [0, 1, v[1] if len(v) > 1 else 0], [0, 0, 1]])
    return np.eye(3)
def _local_pts(p):
    """corner points of one SVG shape in its own coordinates"""
    t = p.tag.replace(NS, "")
    try:
        if t == "polygon" or t == "polyline":
            v = [float(x) for x in re.split(r"[ ,]+", (p.get("points") or "").strip()) if x]
            return [(v[i], v[i + 1]) for i in range(0, len(v) - 1, 2)]
        if t == "rect":
            x, y, w, h = (float(p.get(k) or 0) for k in ("x", "y", "width", "height")); return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        if t in ("circle", "ellipse"):
            cx, cy = float(p.get("cx") or 0), float(p.get("cy") or 0); rx = float(p.get("r") or p.get("rx") or 0); ry = float(p.get("r") or p.get("ry") or 0)
            return [(cx - rx, cy - ry), (cx + rx, cy - ry), (cx + rx, cy + ry), (cx - rx, cy + ry)]
        if t == "line":
            return [(float(p.get("x1") or 0), float(p.get("y1") or 0)), (float(p.get("x2") or 0), float(p.get("y2") or 0))]
        if t == "path" and p.get("d"):
            from svgpathtools import parse_path
            x0, x1, y0, y1 = parse_path(p.get("d")).bbox(); return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    except Exception: pass
    return []
SHAPES = {NS + k for k in ("polygon", "polyline", "rect", "circle", "ellipse", "line", "path")}
def _pts(el, M, only_polygons=False):
    """all shape corners below el (el included), transformed by M and by every transform between el and the shape"""
    out = []
    for p in el.iter():
        if p.tag not in SHAPES or (only_polygons and p.tag != NS + "polygon"): continue
        chain = []; x = p
        while x is not None and x is not el: chain.append(x); x = x.getparent()
        Mp = M
        for c in reversed(chain): Mp = Mp @ _mat(c)
        for (a, b) in _local_pts(p):
            q = Mp @ np.array([a, b, 1.0]); out.append((q[0], q[1]))
    return out
def parse(folder):
    W0, H0 = Image.open(folder + "/F1_original.png").size; W1, H1 = Image.open(folder + "/F1_scaled.png").size
    sx, sy = W0 / W1, H0 / H1
    root = etree.parse(folder + "/model.svg").getroot(); items = []; floors = 0; cur = [0]
    def walk(el, M, in_item):
        nonlocal floors
        M = M @ _mat(el); cls = re.sub(r"\s+", " ", el.get("class") or "").strip(); iid = el.get("id") or ""
        if cls.startswith("Floorplan"): floors += 1; cur[0] = floors
        kind = None
        if not in_item:
            if iid in ("Door", "Window") or cls.startswith("Door ") or cls.startswith("Window "): kind = cls or iid
            elif cls.startswith("FixedFurniture ") or cls.startswith("Space "): kind = cls
        if kind:
            P = None
            if kind.startswith("FixedFurniture"):
                bp = next((g for g in el.iter(NS + "g") if (g.get("class") or "").strip() == "BoundaryPolygon"), None)
                if bp is not None:
                    chain = []; x = bp.getparent()
                    while x is not None and x is not el: chain.append(x); x = x.getparent()
                    Mb = M
                    for c in reversed(chain): Mb = Mb @ _mat(c)
                    P = _pts(bp, Mb @ _mat(bp))
            elif kind.startswith("Space"):
                poly = next((c for c in el if c.tag == NS + "polygon"), None)
                if poly is not None: P = [tuple((M @ np.array([a, b, 1.0]))[:2]) for a, b in _local_pts(poly)]
            if not P:
                P = []
                for ch in el:
                    if isinstance(ch.tag, str) and not (ch.get("class") or "").startswith(("FixedFurniture", "Space")): P += _pts(ch, M @ _mat(ch) if ch.tag not in SHAPES else M)
            if P:
                P = [(x * sx, y * sy) for x, y in P]; xs = [p[0] for p in P]; ys = [p[1] for p in P]
                lab = None
                if kind.startswith("Space"):
                    for ch in el.iter(NS + "g"):
                        if "NameLabel" in (ch.get("class") or ""):
                            lab = " ".join("".join(x.itertext()).strip() for x in ch.iter(NS + "text")).strip().upper(); break
                items.append({"cls": kind, "box": [min(xs), min(ys), max(xs), max(ys)], "poly": P, "label": lab, "floor": cur[0]})
            if kind.startswith("Door") or kind.startswith("Window"): return     # openings do not nest
            # rooms contain fixtures, and a fixture group may contain other fixtures: keep walking
        for ch in el:
            if isinstance(ch.tag, str): walk(ch, M, in_item)
    walk(root, np.eye(3), False)
    fb = {}
    for it in items:      # one box per floor drawing = the extent of everything annotated on that floor
        b = fb.setdefault(it["floor"], list(it["box"])); x0, y0, x1, y1 = it["box"]
        b[0], b[1], b[2], b[3] = min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1)
    return {"size": [W0, H0], "floors": floors, "items": items, "floor_boxes": [fb[k] for k in sorted(fb)]}
