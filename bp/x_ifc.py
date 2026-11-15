"""Extractor: IFC model -> Store. Each quantity records where it came from (quantity set name or geometry)."""
import ifcopenshell, ifcopenshell.util.element as UE, ifcopenshell.util.unit as UU
from .core import Entity, Store
from . import ifc_qty as Q
import sys as _sys
from .vocab import rtype_of_label

CLASSES = {  # kind -> IFC classes (subclasses included by ifcopenshell.by_type)
 "door": ["IfcDoor"], "window": ["IfcWindow"], "wall": ["IfcWall"], "slab": ["IfcSlab"], "column": ["IfcColumn"],
 "beam": ["IfcBeam"], "stair": ["IfcStair"], "railing": ["IfcRailing"], "roof": ["IfcRoof"], "covering": ["IfcCovering"],
 "furniture": ["IfcFurnishingElement"], "ramp": ["IfcRamp"], "member": ["IfcMember"], "plate": ["IfcPlate"],
 "curtainwall": ["IfcCurtainWall"],
}
_GS = None
def _geom(e, what):
    global _GS
    import ifcopenshell.geom, ifcopenshell.util.shape as US
    if _GS is None: _GS = ifcopenshell.geom.settings(); _GS.set("use-world-coords", True)
    try: g = ifcopenshell.geom.create_shape(_GS, e).geometry
    except Exception: return None
    return {"footprint": US.get_footprint_area, "volume": US.get_volume}[what](g)

def _qto(e, names):
    """first numeric quantity among names, searched in quantity sets; returns (value, 'Pset.Name')"""
    try: qs = UE.get_psets(e, qtos_only=True)
    except Exception: return None, None
    for n in names:
        for qn, q in qs.items():
            v = q.get(n)
            if isinstance(v, (int, float)) and v > 0: return float(v), f"{qn}.{n}"
    return None, None

def _level(e, cache):
    if e.id() in cache: return cache[e.id()]
    if e.is_a("IfcBuildingStorey"): return e.Name
    lv = None
    try:
        c = UE.get_container(e)
        if c is None:
            p = UE.get_aggregate(e)
            if p is not None: lv = _level(p, cache)
        elif c.is_a("IfcBuildingStorey"): lv = c.Name
        else: lv = _level(c, cache) if c.is_a("IfcSpace") or c.is_a("IfcElement") else None     # element placed in a space: use the space's storey
    except Exception: pass
    cache[e.id()] = lv; return lv

def _type_name(e):
    try:
        t = UE.get_type(e)
        if t is not None and t.Name: return t.Name
    except Exception: pass
    return getattr(e, "ObjectType", None) or None

def _common(e):
    """IsExternal / FireRating / LoadBearing from Pset_*Common, material names"""
    out = {}
    try:
        ps = UE.get_psets(e, psets_only=True)
        for n, p in ps.items():
            if n.endswith("Common"):
                for k, a in (("IsExternal", "is_external"), ("FireRating", "fire_rating"), ("LoadBearing", "load_bearing"), ("AcousticRating", "acoustic_rating"), ("ThermalTransmittance", "u_value")):
                    if p.get(k) not in (None, ""): out[a] = p[k]
    except Exception: pass
    try:
        mats = UE.get_materials(e)
        if mats: out["material"] = "; ".join(sorted({m.Name for m in mats if getattr(m, "Name", None)}))[:120]
    except Exception: pass
    return out

LINEAR = ("IfcFlowSegment", "IfcBeam", "IfcColumn", "IfcMember", "IfcPile", "IfcReinforcingBar")
def _enrich(e, a, kind, ls):
    """properties, material layers, systems, containing space, and definition-based quantities for values the model
    does not state in a quantity set. Nothing here overwrites a value that came from a quantity set."""
    pr = Q.props(e, 40)
    if pr: a["props"] = {_sys.intern(k): v for k, v in pr.items()}
    txt, th, names = Q.layers(e, ls)
    if txt: a["material_layers"] = txt
    if th: a["thickness"] = th; a["thickness_rule"] = "sum of the material layer thicknesses of its layer set"
    if names and not a.get("material"): a["material"] = "; ".join(sorted(set(n for n in names if n)))[:120]
    sy = Q.systems(e)
    if sy: a["system"] = "; ".join(sy)[:120]
    sp, _ = Q.space_of(e)
    if sp: a["space"] = sp
    if a.get("predefined") is None:
        try:
            pd = getattr(e, "PredefinedType", None)
            if pd in (None, "NOTDEFINED", "USERDEFINED"):
                t = UE.get_type(e); pd = getattr(t, "PredefinedType", None) if t is not None else pd
            if pd and pd not in ("NOTDEFINED", "USERDEFINED"): a["predefined"] = str(pd)
        except Exception: pass
    if kind == "wall":
        if "length" not in a:
            v, h = Q.axis_length(e, ls)
            if v: a["length"] = round(v, 3); a["length_rule"] = h
        if "height" not in a:
            v, h = _qto(e, ["Height"])
            if v: a["height"] = round(v * ls, 3); a["height_rule"] = f"{h} × unit scale {ls:g}"
            else:
                v, h = Q.extrusion_depth(e, ls)
                if v: a["height"] = round(v, 3); a["height_rule"] = h
        if "thickness" not in a:
            v, h = _qto(e, ["Width"])
            if v: a["thickness"] = round(v * ls, 4); a["thickness_rule"] = f"{h} × unit scale {ls:g}"
        if "side_area" not in a and a.get("length") and a.get("height"):
            a["side_area"] = round(a["length"] * a["height"], 3); a["side_area_rule"] = "length × height (openings not subtracted)"
        if "volume" not in a and a.get("side_area") and a.get("thickness"):
            a["volume"] = round(a["side_area"] * a["thickness"], 3); a["volume_rule"] = "length × height × thickness (openings not subtracted)"
    elif "length" not in a and any(e.is_a(c) for c in LINEAR):
        v, h = Q.extrusion_depth(e, ls)
        if v: a["length"] = round(v, 3); a["length_rule"] = h
    if kind == "slab" and "thickness" not in a:
        v, h = _qto(e, ["Width", "Depth"])
        if v: a["thickness"] = round(v * ls, 4); a["thickness_rule"] = f"{h} × unit scale {ls:g}"
        else:
            v, h = Q.extrusion_depth(e, ls)
            if v: a["thickness"] = round(v, 4); a["thickness_rule"] = h
    if "volume" not in a and kind in ("slab", "column", "beam", "element", "roof", "member"):
        v, h = Q.extrusion_volume(e, ls)
        if v: a["volume"] = round(v, 3); a["volume_rule"] = h
    if kind == "slab" and "area" not in a and a.get("volume") and a.get("thickness"):
        a["area"] = round(a["volume"] / a["thickness"], 3); a["area_rule"] = "volume ÷ thickness of the extruded slab"
    a["guid"] = e.GlobalId

def extract(path):
    m = ifcopenshell.open(path); st = Store(path, "ifc"); st.meta["extractor"] = "x_ifc"; st.meta["schema"] = m.schema
    ls = UU.calculate_unit_scale(m); st.meta["units"] = "m (length scale %g from IfcUnitAssignment)" % ls
    st.meta["model_names"] = [x.Name for t in ("IfcProject", "IfcBuilding", "IfcSite") for x in m.by_type(t) if x.Name] + \
                             [x.LongName for t in ("IfcProject", "IfcBuilding", "IfcSite") for x in m.by_type(t) if getattr(x, "LongName", None)]
    lvl = {}
    st.meta["storeys"] = [(s.Name, s.Elevation) for s in m.by_type("IfcBuildingStorey")]
    # levels and model facts ----------------------------------------------------------------------------------------
    for s in m.by_type("IfcBuildingStorey"):
        el = s.Elevation * ls if s.Elevation is not None else None
        st.add(Entity(s.GlobalId, "level", {"name": s.Name, "label": s.Name, "elevation": round(el, 3) if el is not None else None}, None, {"ifc_class": "IfcBuildingStorey"}))
    lv_ents = sorted([e for e in st.of("level") if e.get("elevation") is not None], key=lambda e: e.get("elevation"))
    for a_, b_ in zip(lv_ents, lv_ents[1:]):
        a_.attrs["height"] = round(b_.get("elevation") - a_.get("elevation"), 3); a_.attrs["height_rule"] = f"elevation of '{b_.get('name')}' minus elevation of '{a_.get('name')}'"
    facts = {"schema": m.schema}
    try:
        apps = m.by_type("IfcApplication")
        if apps: facts["software"] = "; ".join(sorted({f"{a.ApplicationFullName} {a.Version}" for a in apps}))
    except Exception: pass
    try: facts["header"] = str(m.header.file_name.originating_system)
    except Exception: pass
    for t in ("IfcProject", "IfcBuilding", "IfcSite"):
        for o in m.by_type(t):
            for k in ("Name", "LongName", "Description"):
                v = getattr(o, k, None)
                if v: facts[f"{t[3:].lower()}_{k.lower()}"] = v
    for k, v in facts.items(): st.add(Entity(f"fact:{k}", "fact", {"name": k, "label": k, "value": str(v)}, None, {"rule": "model header / project data"}))
    # spaces -> rooms --------------------------------------------------------------------------------------------
    for s in m.by_type("IfcSpace"):
        name = f"{s.LongName} ({s.Name})" if s.LongName and s.Name else (s.LongName or s.Name)
        a = dict(label=s.LongName or s.Name, number=s.Name, name=name, level=_level(s, lvl))
        v, how = _qto(s, ["NetFloorArea", "GrossFloorArea"])
        if v is None:
            v = _geom(s, "footprint") or None; how = "geometry: footprint area of the space solid" if v else None
        if v is not None: a["area"] = round(v, 3); a["area_rule"] = how
        vol, vh = _qto(s, ["NetVolume", "GrossVolume"])
        if vol: a["volume"] = round(vol, 3); a["volume_rule"] = vh
        hv, hh = _qto(s, ["Height", "FinishCeilingHeight", "ClearHeight"])
        if hv: a["height"] = round(hv * ls, 3); a["height_rule"] = f"{hh} × unit scale {ls:g}"
        elif a.get("volume") and a.get("area"): a["height"] = round(a["volume"] / a["area"], 3); a["height_rule"] = "space volume ÷ floor area"
        pr = Q.props(s, 40)
        if pr: a["props"] = pr
        a["rtype"] = rtype_of_label(s.LongName) or rtype_of_label(s.Name); a["guid"] = s.GlobalId
        a["indoor"] = a["rtype"] != "outdoor"
        st.add(Entity(s.GlobalId, "room", a, None, {"ifc_class": s.is_a(), "step_id": s.id()}))
    # building elements ----------------------------------------------------------------------------------------
    seen = set()
    for kind, classes in CLASSES.items():
        for c in classes:
            try: els = m.by_type(c)
            except RuntimeError: continue
            for e in els:
                if e.id() in seen: continue
                if kind == "stair" and any(r.RelatingObject.is_a("IfcStair") for r in getattr(e, "Decomposes", []) or []): continue
                seen.add(e.id())
                a = dict(name=e.Name, label=e.Name, type_name=_type_name(e), level=_level(e, lvl)); a.update(_common(e))
                if kind == "wall":
                    v, h = _qto(e, ["Length"]);           a.update(length=round(v * ls, 3), length_rule=f"{h} × unit scale {ls:g}") if v else None
                    v, h = _qto(e, ["NetSideArea", "GrossSideArea"]); a.update(side_area=round(v, 3), side_area_rule=h) if v else None
                if kind in ("wall", "slab", "column", "beam"):
                    v, h = _qto(e, ["NetVolume", "GrossVolume"]); a.update(volume=round(v, 3), volume_rule=h) if v else None
                if kind == "slab":
                    v, h = _qto(e, ["NetArea", "GrossArea"]); a.update(area=round(v, 3), area_rule=h) if v else None
                    a["predefined"] = getattr(e, "PredefinedType", None)
                if kind in ("door", "window"):
                    for k2 in ("OverallWidth", "OverallHeight"):
                        v = getattr(e, k2, None)
                        if v: a[k2.replace("Overall", "").lower()] = round(v * ls, 3)
                a["ifc_class"] = e.is_a(); _enrich(e, a, kind, ls)
                st.add(Entity(e.GlobalId, kind, a, None, {"ifc_class": e.is_a(), "step_id": e.id()}))
    # terminals: IFC4 has explicit classes; IFC2X3 only IfcFlowTerminal -> classify by name/type (rule recorded)
    import re as _re
    TERM = [("sanitary", r"sanit|toilet|\bwc\b|water ?closet|lavator|sink|basin|urinal|shower|tub|bidet|closet|faucet|fountain"),
            ("light", r"light|lamp|luminaire|leuchte|downlight|fluor|led\b"), ("airterminal", r"diffuser|grille|register|air ?terminal|louver")]
    explicit = {"IfcSanitaryTerminal": "sanitary", "IfcLightFixture": "light", "IfcAirTerminal": "airterminal"}
    done = set()
    for c, k in explicit.items():
        try:
            for e in m.by_type(c):
                a = dict(name=e.Name, label=e.Name, type_name=_type_name(e), level=_level(e, lvl), kind_rule=f"IFC class {c}", ifc_class=e.is_a()); _enrich(e, a, k, ls)
                done.add(e.id()); st.add(Entity(e.GlobalId, k, a, None, {"ifc_class": e.is_a()}))
        except RuntimeError: pass
    try: terms = m.by_type("IfcFlowTerminal")
    except RuntimeError: terms = []
    for e in terms:
        if e.id() in done: continue
        txt = " ".join(str(x) for x in (e.Name, _type_name(e), getattr(e, "ObjectType", None)) if x).lower()
        k = next((k for k, rx in TERM if _re.search(rx, txt)), None)
        if k:
            a = dict(name=e.Name, label=e.Name, type_name=_type_name(e), level=_level(e, lvl), kind_rule=f"IfcFlowTerminal named '{txt[:60]}' -> {k}", ifc_class=e.is_a()); _enrich(e, a, k, ls)
            st.add(Entity(e.GlobalId, k, a, None, {"ifc_class": e.is_a()}))
    # every other physical element (MEP segments, fittings, valves, radiators, tanks, coverings, footings, proxies ...)
    handled = {e.id() for e in m.by_type("IfcSpace")} | {x.attrs.get("_step") for x in []}
    known_cls = tuple(c for cs in CLASSES.values() for c in cs) + ("IfcSanitaryTerminal", "IfcLightFixture", "IfcAirTerminal", "IfcFlowTerminal", "IfcOpeningElement", "IfcVirtualElement", "IfcSpace")
    for e in m.by_type("IfcElement"):
        if any(e.is_a(c) for c in known_cls) and not e.is_a("IfcFlowTerminal"): continue
        if e.is_a("IfcFlowTerminal") and e.id() in done: continue
        a = dict(name=e.Name, label=e.Name, ifc_class=e.is_a(), type_name=_type_name(e), level=_level(e, lvl), guid=e.GlobalId)
        a.update(_common(e))
        v, h = _qto(e, ["Length"]); a.update(length=round(v * ls, 3), length_rule=f"{h} × unit scale {ls:g}") if v else None
        v, h = _qto(e, ["NetVolume", "GrossVolume"]); a.update(volume=round(v, 3), volume_rule=h) if v else None
        v, h = _qto(e, ["NetArea", "GrossArea", "NetSideArea"]); a.update(area=round(v, 3), area_rule=h) if v else None
        try: a["predefined"] = str(getattr(e, "PredefinedType", None) or "") or None
        except Exception: pass
        if a.get("predefined") in ("NOTDEFINED", "USERDEFINED"): a["predefined"] = None
        _enrich(e, a, "element", ls)
        st.add(Entity(e.GlobalId, "element", a, None, {"ifc_class": e.is_a(), "step_id": e.id()}))
    for s in m.by_type("IfcSystem"):
        if s.is_a("IfcZone") or s.is_a("IfcStructuralAnalysisModel"): continue
        members = [o for r in getattr(s, "IsGroupedBy", []) or [] for o in r.RelatedObjects]
        st.add(Entity(s.GlobalId, "system", dict(name=s.Name, label=s.Name, ifc_class=s.is_a(), members=len(members),
                       predefined=str(getattr(s, "PredefinedType", "") or "") or None, guid=s.GlobalId), None, {"ifc_class": s.is_a()}))
    st.meta["supports"] = sorted(set(CLASSES) | {"room", "level", "fact", "sanitary", "light", "airterminal"})
    return st
