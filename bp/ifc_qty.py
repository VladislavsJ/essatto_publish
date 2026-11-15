"""Quantities and properties read from the IFC *definition* of an element, without running a geometry kernel.
Used when the model has no quantity set for a value. Each function returns (value, rule) so the trace can say where
the number came from. All lengths are returned in metres (scale = IfcUnitAssignment length scale).

 length   IfcExtrudedAreaSolid.Depth of the body (pipes, ducts, beams, columns, members) or the Axis polyline (walls)
 height   extrusion depth of a wall / thickness of a slab
 volume   profile area x extrusion depth (rectangle, circle, hollow circle, I-shape, arbitrary closed polyline)
 layers   IfcMaterialLayerSet (name + thickness), constituents, profiles; type-level materials are inherited
 props    every property of every property set (instance and type), flattened to {name: value}"""
import math
import ifcopenshell.util.element as UE

def _items(e, ident=None):
    """representation items of the element (mapped items resolved), optionally only one RepresentationIdentifier"""
    out = []
    rep = getattr(e, "Representation", None)
    if rep is None: return out
    for r in rep.Representations or []:
        if ident and r.RepresentationIdentifier != ident: continue
        stack = list(r.Items or [])
        while stack:
            it = stack.pop()
            if it.is_a("IfcMappedItem"): stack += list(it.MappingSource.MappedRepresentation.Items or [])
            elif it.is_a("IfcBooleanResult"): stack.append(it.FirstOperand)       # clipped solid: the base solid carries the size
            else: out.append(it)
    return out

def profile_area(p, ls):
    """area in m² of an IfcProfileDef, or None if the profile type is not handled"""
    try:
        if p.is_a("IfcRectangleHollowProfileDef"):
            t = p.WallThickness; return (p.XDim * p.YDim - (p.XDim - 2 * t) * (p.YDim - 2 * t)) * ls * ls
        if p.is_a("IfcRectangleProfileDef"): return p.XDim * p.YDim * ls * ls
        if p.is_a("IfcCircleHollowProfileDef"): return math.pi * (p.Radius ** 2 - (p.Radius - p.WallThickness) ** 2) * ls * ls
        if p.is_a("IfcCircleProfileDef"): return math.pi * p.Radius ** 2 * ls * ls
        if p.is_a("IfcIShapeProfileDef"):
            return (2 * p.OverallWidth * p.FlangeThickness + (p.OverallDepth - 2 * p.FlangeThickness) * p.WebThickness) * ls * ls
        if p.is_a("IfcArbitraryClosedProfileDef"):
            c = p.OuterCurve; pts = _curve_pts(c)
            if pts and len(pts) >= 3:
                a = abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))) / 2
                if p.is_a("IfcArbitraryProfileDefWithVoids"):
                    for v in p.InnerCurves or []:
                        q = _curve_pts(v)
                        if q and len(q) >= 3: a -= abs(sum(q[i][0] * q[(i + 1) % len(q)][1] - q[(i + 1) % len(q)][0] * q[i][1] for i in range(len(q)))) / 2
                return a * ls * ls
    except Exception: pass
    return None

def _curve_pts(c):
    try:
        if c.is_a("IfcPolyline"): return [tuple(p.Coordinates[:2]) for p in c.Points]
        if c.is_a("IfcIndexedPolyCurve"): return [tuple(p[:2]) for p in c.Points.CoordList]
        if c.is_a("IfcCompositeCurve"):
            pts = []
            for s in c.Segments:
                q = _curve_pts(s.ParentCurve)
                if not q: return None            # arcs: not handled, do not guess
                pts += q
            return pts
    except Exception: pass
    return None

def extrusions(e):
    return [it for it in _items(e) if it.is_a("IfcExtrudedAreaSolid")]

def extrusion_depth(e, ls):
    """(sum of extrusion depths in m, rule). For linear elements this is the element length."""
    ex = extrusions(e)
    if not ex: return None, None
    d = sum(x.Depth for x in ex) * ls
    return (round(d, 4), f"body = {len(ex)} extruded solid(s); sum of IfcExtrudedAreaSolid.Depth × unit scale {ls:g}") if d > 0 else (None, None)

def extrusion_volume(e, ls):
    ex = extrusions(e); tot = 0.0
    if not ex: return None, None
    for x in ex:
        a = profile_area(x.SweptArea, ls)
        if a is None: return None, None            # one unknown profile -> no partial volume
        tot += a * x.Depth * ls
    return (round(tot, 4), f"profile area × extrusion depth of {len(ex)} solid(s) (openings not subtracted)") if tot > 0 else (None, None)

def axis_length(e, ls):
    """length of the Axis representation (walls, beams): sum of polyline segments"""
    for it in _items(e, "Axis"):
        pts = _curve_pts(it) if it.is_a("IfcBoundedCurve") or it.is_a("IfcPolyline") else None
        if it.is_a("IfcTrimmedCurve"):
            try:
                a, b = it.Trim1[0], it.Trim2[0]
                if a.is_a("IfcCartesianPoint") and b.is_a("IfcCartesianPoint"): pts = [tuple(a.Coordinates[:2]), tuple(b.Coordinates[:2])]
            except Exception: pass
        if pts and len(pts) >= 2:
            L = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)) * ls
            if L > 0: return round(L, 4), f"length of the element's Axis polyline × unit scale {ls:g}"
    return None, None

def layers(e, ls):
    """(text, total thickness m, [names]) from the material association (instance or inherited from the type)"""
    try: m = UE.get_material(e, should_inherit=True)
    except Exception: m = None
    if m is None: return None, None, []
    try:
        if m.is_a("IfcMaterialLayerSetUsage"): m = m.ForLayerSet
        if m.is_a("IfcMaterialLayerSet"):
            ls_ = [(l.Material.Name if l.Material else "?", (l.LayerThickness or 0) * ls) for l in m.MaterialLayers or []]
            tot = sum(t for _, t in ls_)
            return "; ".join(f"{n} {t * 1000:g} mm" for n, t in ls_), (round(tot, 4) if tot > 0 else None), [n for n, _ in ls_]
        if m.is_a("IfcMaterialProfileSetUsage"): m = m.ForProfileSet
        if m.is_a("IfcMaterialProfileSet"):
            ns = [(p.Material.Name if p.Material else "?") + (f" [{p.Profile.ProfileName}]" if p.Profile and p.Profile.ProfileName else "") for p in m.MaterialProfiles or []]
            return "; ".join(ns), None, ns
        if m.is_a("IfcMaterialConstituentSet"):
            ns = [c.Material.Name for c in m.MaterialConstituents or [] if c.Material]; return "; ".join(ns), None, ns
        if m.is_a("IfcMaterialList"):
            ns = [x.Name for x in m.Materials or []]; return "; ".join(ns), None, ns
        if m.is_a("IfcMaterial"): return m.Name, None, [m.Name]
    except Exception: pass
    return None, None, []

SKIP_PSETS = ("Pset_ManufacturerTypeInformation",)
def props(e, limit=60):
    """flattened {property name: value} over all property sets and quantity sets of the element and its type.
    Values are scalars only; a name that occurs in two sets keeps the first (instance before type)."""
    out = {}
    try: ps = UE.get_psets(e, should_inherit=True)
    except Exception: return out
    for pn, p in ps.items():
        for k, v in p.items():
            if k == "id" or v in (None, "") or not isinstance(v, (int, float, str, bool)): continue
            if isinstance(v, str) and len(v) > 80: continue
            if k not in out:
                out[k] = round(v, 4) if isinstance(v, float) else v
                if len(out) >= limit: return out
    return out

def systems(e):
    """names of the systems/groups the element is assigned to (IfcRelAssignsToGroup)"""
    out = []
    try:
        for r in getattr(e, "HasAssignments", []) or []:
            if r.is_a("IfcRelAssignsToGroup") and r.RelatingGroup is not None and r.RelatingGroup.Name: out.append(r.RelatingGroup.Name)
    except Exception: pass
    return sorted(set(out))

def space_of(e):
    """the IfcSpace an element is directly contained in (many MEP exports place terminals in spaces)"""
    try:
        c = UE.get_container(e)
        if c is not None and c.is_a("IfcSpace"): return (f"{c.LongName} ({c.Name})" if c.LongName and c.Name else (c.LongName or c.Name)), c
    except Exception: pass
    return None, None
