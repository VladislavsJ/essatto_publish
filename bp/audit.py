"""Drawing audit: problems in the drawing or in what was extracted from it, found WITHOUT knowing any answer.
audit(store) -> list of issues {code, sev: error|warn|info, text, ids, scope}. `ids` are the entities the issue touches;
`scope` says which questions it matters for (e.g. "kind:door", "apartment:B", "rtype").
table(store) -> the full per-room / per-apartment listing shown to the user before any question is asked."""
from shapely.geometry import Polygon
from shapely.ops import unary_union

TOL = 0.02          # the challenge tolerance for measures

def audit(st):
    if getattr(st, "_audit", None) is not None: return st._audit
    f = {"dxf-layered": _dxf, "ifc": _ifc, "raster": _raster}.get(st.fmt)
    out = f(st) if f else []
    for w in st.meta.get("warnings", []):
        if not any(w == i["text"] for i in out): out.append({"code": "extractor_warning", "sev": "warn", "text": w, "ids": [], "scope": "all"})
    st._audit = out
    return out

def _i(out, code, sev, text, ids=(), scope=None): out.append({"code": code, "sev": sev, "text": text, "ids": list(ids), "scope": scope})

def _dxf(st):
    out = []; rooms = st.of("room"); raw = st.meta.get("raw", {})
    P = {r.id: Polygon(r.geom).buffer(0) for r in rooms if r.geom}
    by_handle = {}
    for r in rooms:
        n = r.get("number")
        if not r.geom: _i(out, "room_no_outline", "error", f"room {n}: no outline polygon contains its number, so it has no area and is missing from every total", [r.id]); continue
        by_handle.setdefault(r.src.get("handle"), []).append(r)
        ap, a = r.get("area_printed"), r.get("area")
        if ap and a is not None and abs(a - ap) > TOL * ap:
            _i(out, "area_mismatch", "error", f"room {n}: the drawing prints {ap:g} m² but its outline measures {a:g} m² ({100 * (a - ap) / ap:+.1f}%). Either the printed number or the outline is wrong", [r.id])
        if r.get("rtype") is None:
            desc = f" ('{r.get('label')}')" if r.get("label") else " has no name and no fixtures"
            _i(out, "room_untyped", "warn", f"room {n}{desc}, so its type is unknown. It is treated as an ordinary indoor room; if it is really a balcony or shaft, totals that exclude those are too high", [r.id], "rtype")
        elif not r.get("label"):
            _i(out, "room_type_inferred", "info", f"room {n} has no name; typed as {r.get('rtype')} because: {r.get('rtype_rule')}", [r.id], "rtype")
    for h, rs in by_handle.items():
        if len(rs) > 1: _i(out, "shared_outline", "error", "rooms " + ", ".join(r.get("number") for r in rs) + " have their numbers inside the same outline polygon: each would be given the full area of that polygon", [r.id for r in rs])
    ids = list(P)
    for i, a in enumerate(ids):
        ba = P[a].bounds
        for b in ids[i + 1:]:
            bb = P[b].bounds
            if ba[2] < bb[0] or bb[2] < ba[0] or ba[3] < bb[1] or bb[3] < ba[1]: continue
            ov = P[a].intersection(P[b]).area
            if ov > 0.05 and ov > 0.01 * min(P[a].area, P[b].area):
                _i(out, "room_overlap", "error", f"rooms {a[5:]} and {b[5:]} overlap by {ov:.2f} m²: that area is counted twice in any total containing both", [a, b])
    # apartments -------------------------------------------------------------------------------------------------
    apts = st.of("apartment"); by_out = {}
    for a in apts:
        nm = a.get("name"); mine = [r for r in rooms if r.get("apartment") == nm]
        if not a.geom: _i(out, "apartment_no_outline", "warn", f"apartment {nm}: no unit outline found, so its rooms cannot be assigned to it", [a.id], f"apartment:{nm}"); continue
        key = a.src.get("handle") or tuple(round(v, 2) for v in Polygon(a.geom).bounds)
        by_out.setdefault(key, []).append(a)
        base_poly = unary_union([Polygon(g).buffer(0) for g in [a.geom] + (a.src.get("extra_geoms") or [])])
        if a.src.get("hole_geoms"): base_poly = base_poly.difference(unary_union([Polygon(g).buffer(0) for g in a.src["hole_geoms"]]))
        oa = a.get("outline_area") or base_poly.area; s = sum(r.get("area") or 0 for r in mine)
        a.attrs.setdefault("outline_area", round(oa, 2)); a.attrs.setdefault("rooms_area", round(s, 2)); a.attrs.setdefault("n_rooms", len(mine))
        if not a.get("shared_outline_split"):
            if s > 1.02 * oa: _i(out, "apartment_overfull", "error", f"apartment {nm}: its rooms add up to {s:.1f} m², more than its own outline ({oa:.1f} m²). A room is probably assigned to the wrong apartment or measured too large", [a.id] + [r.id for r in mine], f"apartment:{nm}")
            elif mine and s < 0.75 * oa: _i(out, "apartment_underfull", "warn", f"apartment {nm}: its rooms add up to only {s:.1f} m² of a {oa:.1f} m² outline. Some rooms may be un-numbered or missing", [a.id], f"apartment:{nm}")
        AP = base_poly.buffer(0.05)
        for r in mine:
            if r.id in P and P[r.id].area > 0 and P[r.id].difference(AP).area > 0.1 * P[r.id].area:
                _i(out, "room_crosses_apartment", "warn", f"room {r.get('number')} is assigned to apartment {nm} (its number is inside the unit outline) but {100 * P[r.id].difference(AP).area / P[r.id].area:.0f}% of its area lies outside that outline", [r.id], f"apartment:{nm}")
    for h, aps in by_out.items():
        if len(aps) > 1:
            names = [a.get("name") for a in aps]
            all_ids = [a.id for a in aps] + [r.id for r in rooms if r.get("apartment") in names]
            if any(a.get("shared_outline_split") for a in aps):
                _i(out, "apartments_share_outline", "warn", "apartments " + ", ".join(names) + " share one unit outline; rooms were split between them by door connectivity and nearest apartment label", all_ids, "apartment:" + "|".join(names))
            else:
                _i(out, "apartments_share_outline", "error", "apartments " + ", ".join(names) + " are labelled inside the same unit outline, so their rooms cannot be told apart reliably", all_ids, "apartment:" + "|".join(names))
    # spaces with no number -----------------------------------------------------------------------------------------
    big = [u for u in raw.get("unlabelled", []) if not u.get("inside") and u["area"] >= 1.0]
    for u in big[:12]:
        _i(out, "space_unnumbered", "warn", f"an enclosed outline of {u['area']:g} m² has no room number; it is not a room for this engine and is in no total", [], "total")
    # symbols --------------------------------------------------------------------------------------------------------
    for e in st.entities:
        if e.get("ambiguous"): _i(out, "fixture_ambiguous", "warn", f"{e.kind} symbol: {e.get('ambiguous')}; it was classified as a {e.kind} but could be a {'washbasin' if e.kind == 'toilet' else 'toilet'}", [e.id], "kind:toilet|washbasin")
        if e.kind in ("toilet", "washbasin", "bathtub", "shower") and not e.get("room"):
            _i(out, "fixture_outside_rooms", "info", f"a {e.kind} symbol lies outside every numbered room", [e.id], f"kind:{e.kind}")
    sh = raw.get("shapes", {})
    d5, d2 = sh.get("DOOR", {}).get(5, 0), sh.get("DOOR", {}).get(2, 0)
    w4, w2 = sh.get("WINDOW", {}).get(4, 0), sh.get("WINDOW", {}).get(2, 0)
    st.meta["pairing"] = {"door": (d5, d2), "window": (w4, w2)}
    if "DOOR" in sh and d5 != d2: _i(out, "door_pairing", "info", f"doors: {d5} door leaves but {d2} swing lines on the DOOR layer. Each door normally has one of each, so the door count may be off by {abs(d5 - d2)}", [], "kind:door")
    if "WINDOW" in sh and w2 != 2 * w4: _i(out, "window_pairing", "warn", f"windows: {w4} window frames but {w2} glazing lines (expected {2 * w4}). The window count may be off", [], "kind:window")
    odd = {l: {n: c for n, c in v.items() if (l, n) not in (("DOOR", 5), ("DOOR", 2), ("WINDOW", 4), ("WINDOW", 2))} for l, v in sh.items() if l in ("DOOR", "WINDOW")}
    for l, v in odd.items():
        if v: _i(out, "unrecognised_shape", "warn", f"{sum(v.values())} shape(s) on layer {l} match no rule (vertex counts {sorted(v)}) and were not counted", [], f"kind:{l.lower()}")
    return out

def _ifc(st):
    out = []; rooms = st.of("room")
    no_area = [r for r in rooms if r.get("area") is None]
    if no_area: _i(out, "space_no_area", "error", f"{len(no_area)} of {len(rooms)} spaces have no area (no quantity set and no usable geometry): " + ", ".join(str(r.get("name")) for r in no_area[:8]) + (" …" if len(no_area) > 8 else "") + ". Totals that include them are too low", [r.id for r in no_area], "kind:room")
    geo = [r for r in rooms if str(r.get("area_rule") or "").startswith("geometry")]
    if geo: _i(out, "space_area_from_geometry", "info", f"{len(geo)} space area(s) are not stated in the model and were measured from the space solid's footprint", [r.id for r in geo], "kind:room")
    srcs = {str(r.get("area_rule")) for r in rooms if r.get("area") is not None}
    if len(srcs) > 1: _i(out, "mixed_area_sources", "warn", "space areas come from different sources in this model (" + "; ".join(sorted(srcs)) + "), so net and gross values may be mixed in one total", [], "kind:room")
    seen = {}
    for r in rooms: seen.setdefault((r.get("name"), r.get("level")), []).append(r)
    for (nm, lv), rs in seen.items():
        if len(rs) > 1 and nm: _i(out, "duplicate_space", "info", f"{len(rs)} spaces are named '{nm}' on level '{lv}'; a question naming that room matches all of them", [r.id for r in rs], "kind:room")
    kinds = {}
    for e in st.entities:
        if e.kind in ("level", "fact", "system"): continue
        k = kinds.setdefault(e.kind, [0, 0]); k[0] += 1; k[1] += e.get("level") is None
    for k, (n, nl) in kinds.items():
        if nl and nl < n: _i(out, "no_storey", "warn", f"{nl} of {n} '{k}' elements are not assigned to any storey; per-level questions cannot see them", [e.id for e in st.of(k) if e.get("level") is None][:200], f"level:{k}")
    named = [e for e in st.entities if str(e.get("kind_rule") or "").startswith("IfcFlowTerminal")]
    if named: _i(out, "classified_by_name", "warn", f"{len(named)} terminals in this IFC2X3 model have no specific class and were classified from their names (sanitary / light / air terminal)", [e.id for e in named][:300], "kind:sanitary|light|airterminal")
    return out

def _raster(st):
    out = []
    if not st.meta.get("detections") and not any(e.kind in ("door", "window", "toilet") for e in st.entities):
        return out
    _i(out, "vision_single_source", "info", "symbols on this scanned plan were located by a vision model once and stored; there is no second measurement to compare them with", [], "all")
    lab = {}
    for e in st.of("room_label"):
        if e.get("rtype"): lab[e.get("rtype")] = lab.get(e.get("rtype"), 0) + 1
    det = {}
    for e in st.of("room"): det[e.get("rtype")] = det.get(e.get("rtype"), 0) + 1
    st.meta["label_vs_detector"] = {k: (det.get(k, 0), lab.get(k, 0)) for k in set(det) | set(lab)}
    return out

# ---------------------------------------------------------------------------------------------------------------------
def table(st):
    """Everything a user should be able to check before asking anything."""
    issues = audit(st); flag = {}
    for i in issues:
        for x in i["ids"]: flag.setdefault(x, []).append({"code": i["code"], "sev": i["sev"], "text": i["text"]})
    out = {"format": st.fmt, "issues": issues, "units": st.meta.get("units"), "scale": st.meta.get("scale"), "rooms": [], "apartments": [], "symbols": {}, "levels": []}
    if st.fmt == "raster":
        for k, n in sorted(st.kinds().items()):
            if k not in ("room", "room_label"): out["symbols"][k] = n
        rt = {}
        for e in st.of("room"): rt[e.get("rtype")] = rt.get(e.get("rtype"), 0) + 1
        out["room_types"] = [{"type": k, "by vision model": v, "labels read by OCR": st.meta.get("label_vs_detector", {}).get(k, (v, 0))[1]} for k, v in sorted(rt.items(), key=lambda kv: str(kv[0]))]
        out["rooms"] = [{"id": e.id, "label": e.get("label"), "type": e.get("rtype"), "area": e.get("area"), "how": e.get("area_rule"), "flags": []} for e in st.of("room_label")]
        return out
    for r in st.of("room"):
        row = {"id": r.id, "number": r.get("number"), "label": r.get("label"), "type": r.get("rtype"), "type_how": r.get("rtype_rule"), "apartment": r.get("apartment"), "level": r.get("level"),
               "area": r.get("area"), "area_gross": r.get("area_gross"), "printed": r.get("area_printed"), "how": r.get("area_rule"), "volume": r.get("volume"), "flags": flag.get(r.id, [])}
        if r.get("area") is not None and r.get("area_printed"): row["diff_pct"] = round(100 * (r.get("area") - r.get("area_printed")) / r.get("area_printed"), 1)
        out["rooms"].append(row)
    for a in st.of("apartment"):
        out["apartments"].append({"id": a.id, "name": a.get("name"), "rooms": a.get("n_rooms"), "rooms_area": a.get("rooms_area"), "outline_area": a.get("outline_area"), "flags": flag.get(a.id, [])})
    for k, n in sorted(st.kinds().items()):
        if k not in ("room", "apartment", "fact", "level"): out["symbols"][k] = n
    if st.fmt == "ifc":
        by = {}
        for r in st.of("room"):
            b = by.setdefault(str(r.get("level")), [0, 0.0]); b[0] += 1; b[1] += r.get("area") or 0
        elev = {e.get("name"): e.get("elevation") for e in st.of("level")}
        out["levels"] = [{"level": k, "elevation": elev.get(k), "spaces": n, "area": round(a, 2)} for k, (n, a) in sorted(by.items(), key=lambda kv: (elev.get(kv[0]) is None, elev.get(kv[0]) or 0))]
    pr = [r for r in out["rooms"] if r.get("printed")]
    out["printed_check"] = {"with_printed": len(pr), "agree": sum(1 for r in pr if abs(r.get("diff_pct", 0)) <= 100 * TOL), "rooms": len(out["rooms"])}
    return out
