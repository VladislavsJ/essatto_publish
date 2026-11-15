"""Plan check against written rules (recipe 'regulation_check').

Rules live in ../regulations/rules.json, each with the exact clause, a link to the official text and a local copy.
A check is plain code over the drawing's database; every finding carries the room, the measured value, the required
value and the clause it rests on. Three outcomes only:  pass | fail | not_checked (with the reason).

What the checks need, and where it comes from:
  rooms, areas, names      traced from the drawing (sheet database)
  fire detectors           symbols on a fire layout. The detector model does not know these symbols yet: on the SAMPLE
                           plan they come from the planted sidecar (demo/school_demo.json) and are labelled as such
  seats per room           furniture count. Also from the sidecar on the SAMPLE plan
  pupils in the school     a project fact, not on the drawing (sidecar / to be asked from the user)"""
import os, json, math

HERE = os.path.dirname(__file__)
RULES = os.path.join(HERE, "..", "regulations", "rules.json")

def rules():
    try: return {r["id"]: r for r in json.load(open(RULES, encoding="utf-8"))}
    except Exception: return {}

def _room_name(e): return e.get("name") or e.get("label") or e.id
def _is_tiny(e): return isinstance(e.get("area"), (int, float)) and e.get("area") < 2.0

def run(st):
    """-> list of findings: {rule, check, status, room (entity id or None), title, measured, required, text}"""
    R = rules(); out = []; demo = st.meta.get("demo") or {}
    rooms = [e for e in st.of("room") if e.geom and len(e.geom) >= 3 and e.get("area_rule")]
    dets = st.of("fire_detector")
    def add(rule, check, status, title, text, room=None, measured=None, required=None, extra=None):
        out.append({"rule": rule, "check": check, "status": status, "room": room.id if room is not None else None, "room_name": _room_name(room) if room is not None else None,
                    "title": title, "text": text, "measured": measured, "required": required, **(extra or {})})
    if not rooms:
        add(None, "rooms", "not_checked", "No rooms", "This drawing has no traced rooms, so no room-based rule can be checked."); return out

    # ---- FI 848/2017 § 38: the school must have an early-warning device; Table 12 says which kind
    r = R.get("FI-848-2017-38")
    if r:
        pupils = demo.get("pupils_total")
        if pupils is None:
            add(r["id"], "fire_warning_system", "not_checked", "Type of fire warning system", "The required type depends on the number of pupils (Table 12), which is not on the drawing.")
        else:
            row = next(x for x in r["table"]["rows"] if x["max_pupils"] is None or pupils <= x["max_pupils"])
            if not demo.get("layer") == "fire":
                add(r["id"], "fire_warning_system", "not_checked", "Fire warning system", f"For {pupils} pupils Table 12 requires: {row['en']}. This sheet is not a fire layout, so the system is not shown on it.", required=row["en"])
            elif dets:
                add(r["id"], "fire_warning_system", "pass", "Fire warning system present", f"For {pupils} pupils Table 12 requires: {row['en']}. The fire layout shows {len(dets)} detectors.", measured=f"{len(dets)} detectors", required=row["en"])
            else:
                add(r["id"], "fire_warning_system", "fail", "No fire warning system", f"For {pupils} pupils Table 12 requires: {row['en']}. The fire layout shows no detector.", measured="0 detectors", required=row["en"])
        # room-by-room coverage on a fire layout
        if demo.get("layer") == "fire":
            from shapely.geometry import Polygon, Point
            covered = set()
            polys = {e.id: Polygon(e.geom).buffer(0) for e in rooms}
            for d in dets:
                x = sum(p[0] for p in d.geom) / len(d.geom); y = sum(p[1] for p in d.geom) / len(d.geom)
                for rid, pg in polys.items():
                    if pg.contains(Point(x, y)): covered.add(rid); break
            missing = [e for e in rooms if e.id not in covered and not _is_tiny(e)]
            ok = [e for e in rooms if e.id in covered]
            tot = len(ok) + len(missing)
            if not missing:
                add(r["id"], "fire_detection_coverage", "pass", "Detector in every room", f"All {tot} rooms have a detector.", measured=f"{len(ok)} of {tot} rooms", required="every room")
            for e in missing:
                add(r["id"], "fire_detection_coverage", "fail", f"{_room_name(e)}: no detector", f"{len(ok)} of {tot} rooms have a fire detector. {_room_name(e)} ({e.get('area'):g} m²) has none: no detector symbol lies inside it.",
                    room=e, measured="0 detectors in this room", required="at least 1")

    # ---- UK BB103: general classroom 55 m² for 30 pupils (guidance)
    r = R.get("UK-BB103-CLA12")
    if r:
        per = r["params"]["area_m2"] / r["params"]["pupils"]; seated = [e for e in rooms if e.get("seats")]
        cls = [e for e in rooms if "classroom" in str(_room_name(e)).lower()]
        for e in seated:
            n = int(e.get("seats")); need = round(per * n, 1); a = float(e.get("area")); fits = int(math.floor(a / per + 1e-9))
            if a + 1e-9 >= need:
                add(r["id"], "classroom_area_for_seats", "pass", f"{_room_name(e)}: area for {n} pupils", f"{a:g} m² for {n} pupils; the guideline area is {need:g} m².", room=e, measured=f"{a:g} m²", required=f"{need:g} m²", extra={"seats": n, "fits": fits})
            else:
                restrictive = n == r["params"]["pupils"] and a >= r["params"]["restrictive_from_m2"]
                add(r["id"], "classroom_area_for_seats", "fail", f"{_room_name(e)}: too small for {n} pupils",
                    f"{a:g} m² for {n} pupils is {a / n:.2f} m² each; a general classroom is {r['params']['area_m2']} m² for {r['params']['pupils']} ({per:.2f} m² each), so {need:g} m² would be needed." +
                    (f" The guideline calls {r['params']['restrictive_from_m2']} to {r['params']['area_m2']} m² for 30 'restrictive'." if restrictive else "") + f" At this area the room fits {fits} pupils.",
                    room=e, measured=f"{a:g} m² ({a / n:.2f} m² per pupil)", required=f"{need:g} m² ({per:.2f} m² per pupil)", extra={"seats": n, "fits": fits, "area": a, "per_pupil": per})
        rest = [e for e in cls if not e.get("seats")]
        if rest: add(r["id"], "classroom_area_for_seats", "not_checked", f"{len(rest)} other classrooms", f"{len(rest)} classrooms have no seat count in the database, so their area per pupil is not checked.")

    # ---- FI 1009/2017 § 9: 6 dm³/s outdoor air per person
    r = R.get("FI-1009-2017-9")
    if r:
        for e in [e for e in rooms if e.get("seats")]:
            n = int(e.get("seats")) + 1; need = r["params"]["dm3_s_per_person"] * n
            add(r["id"], "ventilation_per_person", "not_checked", f"{_room_name(e)}: ventilation for {n} people", f"{n} people (pupils and one teacher) need at least {need} dm³/s of outdoor air. The air flow is not on this drawing; it has to be checked against the ventilation design.",
                room=e, required=f"{need} dm³/s")
    return out

def apply(st, a, question=""):
    """fill the Answer of a 'regulation_check' plan with the findings; failing rooms become the evidence"""
    F = run(st); R = rules()
    for f in F:
        r = R.get(f.get("rule") or "")
        if r: f["clause"] = {"id": r["id"], "title": r["title"], "clause": r["clause"], "status": r["status"], "jurisdiction": r["jurisdiction"], "url": r["url"], "quote": r["quote"],
                             "quote_2": r.get("quote_2"), "quote_translation": r.get("quote_translation"), "scope_note": r.get("scope_note"), "local_copy": r.get("local_copy"), "table": r.get("table")}
    n = {s: sum(1 for f in F if f["status"] == s) for s in ("pass", "fail", "not_checked")}
    a.value = f"{n['fail']} failed, {n['pass']} passed, {n['not_checked']} could not be checked"; a.unit = None
    a.evidence = list(dict.fromkeys(f["room"] for f in F if f["status"] == "fail" and f["room"]))
    a.trace = [s for s in a.trace if s.get("op") in ("recipe",)] + [{"op": "check", "text": f"{f['status'].upper()}: {f['title']} — {f['text']}" + (f" [{f['rule']}, {f['clause']['clause']}]" if f.get("clause") else ""), "ids": [f["room"]] if f["room"] else []} for f in F]
    a.findings = F
    # one mark per room for the drawing: fail wins over pass; rooms no rule looked at stay unmarked
    marks = {}
    for i, f in enumerate(F):
        if f.get("room"):
            cur = marks.get(f["room"])
            if f["status"] == "fail" or cur is None or (cur["status"] == "not_checked" and f["status"] == "pass"): marks[f["room"]] = {"status": f["status"], "finding": i} if not (cur and cur["status"] == "fail") else cur
    if (st.meta.get("demo") or {}).get("layer") == "fire":
        try:
            from shapely.geometry import Polygon, Point
            polys = {e.id: Polygon(e.geom).buffer(0) for e in st.of("room") if e.geom and len(e.geom) >= 3}
            for d in st.of("fire_detector"):
                x = sum(p[0] for p in d.geom) / len(d.geom); y = sum(p[1] for p in d.geom) / len(d.geom)
                for rid, pg in polys.items():
                    if pg.contains(Point(x, y)):
                        if marks.get(rid, {}).get("status") != "fail": marks[rid] = {"status": "pass", "finding": None, "why": "has a fire detector"}
                        break
        except Exception: pass
    a.room_marks = marks
    demo = st.meta.get("demo") or {}
    if demo: a.interpretation.append("SAMPLE plan: the fire symbols and the seats of one classroom were planted for demonstration; room outlines and areas are computed from the drawing")
    a.interpretation.append("rules: regulations/rules.json; statutory and guidance rules are marked as such in each finding")
    return a
