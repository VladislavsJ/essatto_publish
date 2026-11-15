"""Trust label for one answer, computed WITHOUT knowing the right answer.
assess(store, answer) -> {"level": checked | single_source | review, "checks": [...], "headline": str}
  checked        an independent check exists and passed (e.g. measured areas equal the areas printed on the drawing)
  single_source  nothing contradicts the answer, but nothing independent confirms it either
  review         a check failed, or the drawing has a problem that touches this answer: look before using it
Each check: {name, result: pass | fail | caution | info, text, ids}.
narrate(store, answer) -> plain-language reasoning lines built from the plan and the trace (no model involved)."""
import os, json
from .audit import audit, TOL

HERE = os.path.dirname(__file__)
_TR = None
def _track():
    """measured accuracy of each raster question type on the evaluation set: the prior a user should know about"""
    global _TR
    if _TR is None:
        try: _TR = {k: (a, n) for k, (a, n) in json.load(open(os.path.join(HERE, "..", "cache", "summary_cubicasa5k.json")))["by_type"].items()}
        except Exception: _TR = {}
    return _TR

def _plans(plan): return plan.get("parts") or [plan]

def _scope_hit(scope, plans):
    """does an audit issue with this scope matter for these plans?"""
    if not scope: return False
    if scope == "all": return True
    for p in plans:
        kinds = p.get("kind") if isinstance(p.get("kind"), list) else [p.get("kind")]
        fl = p.get("filters", [])
        if scope.startswith("kind:") and set(scope[5:].split("|")) & set(kinds): return True
        if scope.startswith("apartment:"):
            names = scope[10:].split("|")
            if any(f["field"] == "apartment" and str(f.get("value")) in names for f in fl): return True
        if scope.startswith("level:") and scope[6:] in kinds and any(f["field"] == "level" for f in fl): return True
        if scope == "total" and "room" in kinds and p.get("op") in ("sum", "count") and not any(f["field"] in ("number", "name", "label") or f.get("op") == "match" for f in fl): return True
    return False

def assess(st, ans):
    if ans.get("status") != "ok":
        return {"level": "none", "checks": [], "headline": "Not answered: " + str(ans.get("reason") or "")}
    plan = ans.get("plan") or {}; plans = _plans(plan); ev = set(ans.get("evidence") or []); checks = []
    add = lambda name, result, text, ids=(): checks.append({"name": name, "result": result, "text": text, "ids": list(ids)[:200]})
    trace = ans.get("trace") or []
    kinds = set(); ops = set(); has_type_filter = False; type_excl = False
    for p in plans:
        kinds |= set(p["kind"] if isinstance(p.get("kind"), list) else [p.get("kind")]); ops.add(p.get("op"))
        for f in p.get("filters", []):
            if f["field"] == "rtype": has_type_filter = True; type_excl |= f.get("op") == "nin"
    # 1. measured values against values printed on the drawing --------------------------------------------------------
    rows = [r for s in trace if s.get("op") == "measure" for r in (s.get("values") or [])]
    pr = [r for r in rows if r.get("printed") is not None]
    if rows and pr:
        attr = next(k for k in ("area", "length", "volume", "perimeter", "side_area") if k in rows[0]) if any(k in rows[0] for k in ("area", "length", "volume", "perimeter", "side_area")) else None
        bad = [r for r in pr if attr and abs(r[attr] - r["printed"]) > TOL * max(r["printed"], 0.01)]
        if bad: add("printed areas", "fail", f"{len(bad)} of {len(pr)} measured rooms disagree with the area printed on the drawing: " + "; ".join(f"{r.get('number') or r['id']}: measured {r[attr]:g}, printed {r['printed']:g}" for r in bad[:5]), [r["id"] for r in bad])
        elif len(pr) == len(rows): add("printed areas", "pass", f"all {len(rows)} measured room area(s) equal the area printed on the drawing within 2%", [r["id"] for r in pr])
        else: add("printed areas", "info", f"{len(pr)} of {len(rows)} measured rooms have a printed area and all of those agree; the other {len(rows) - len(pr)} cannot be cross-checked", [r["id"] for r in pr])
    elif rows and st.fmt == "dxf-layered":
        add("printed areas", "info", "this drawing prints no areas for these rooms, so the measured areas cannot be cross-checked")
    for s in trace:
        if s.get("op") == "warn": add("skipped elements", "fail", s["text"] + ": the result is a lower bound", s.get("ids") or [])
    # 2. problems of the drawing that touch this answer ---------------------------------------------------------------
    for i in audit(st):
        touches = bool(ev & set(i["ids"])); scoped = _scope_hit(i.get("scope"), plans)
        if i["code"] in ("room_untyped", "room_type_inferred"):
            if not (touches and has_type_filter) and not (has_type_filter and scoped and _in_scope(st, i["ids"], plans)): continue
            if i["code"] == "room_type_inferred": add("room type", "info", i["text"], i["ids"]); continue
            add("room type", "caution", i["text"], i["ids"]); continue
        if i["code"] in ("fixture_outside_rooms", "vision_single_source", "space_area_from_geometry", "door_pairing", "duplicate_space"):
            if touches or scoped: add(i["code"].replace("_", " "), "info", i["text"], i["ids"])
            continue
        if i["code"] in ("apartment_overfull", "apartments_share_outline", "apartment_underfull", "room_crosses_apartment", "apartment_no_outline"):
            if scoped or (touches and i["code"] == "room_crosses_apartment" and any(f["field"] == "apartment" for p in plans for f in p.get("filters", []))): add("apartment outline", "fail" if i["sev"] == "error" else "caution", i["text"], i["ids"])
            continue
        if i["code"] == "area_mismatch": continue      # already reported by check 1 for the measured rooms
        if touches and i["ids"] or (scoped and not i["ids"]) or (scoped and i["code"] in ("no_storey", "classified_by_name", "mixed_area_sources", "space_no_area") and _relevant(i, plans, ev)):
            if not rows and i["code"] in ("room_overlap", "shared_outline", "room_no_outline", "space_no_area", "mixed_area_sources"): continue   # area problems do not affect counts
            add(i["code"].replace("_", " "), "fail" if i["sev"] == "error" else "caution", i["text"], i["ids"])
    # 3. positive structural checks ----------------------------------------------------------------------------------
    if st.fmt == "dxf-layered":
        pair = st.meta.get("pairing", {})
        for k, exp in (("door", lambda a, b: a == b), ("window", lambda a, b: b == 2 * a)):
            if k in kinds and "count" in ops | {"exists"} and k in pair and pair[k][0] and exp(*pair[k]):
                add(f"{k} symbols", "pass", f"every {k} on the drawing has its matching {'swing line' if k == 'door' else 'pair of glazing lines'} ({pair[k][0]} {k}s, {pair[k][1]} lines), so none was missed or double-counted")
        for p in plans:
            apt = next((f.get("value") for f in p.get("filters", []) if f["field"] == "apartment" and f.get("op", "eq") == "eq"), None)
            if apt and p.get("kind") == "room" and p.get("attr") == "area" and p.get("op") == "sum":
                a = next((x for x in st.of("apartment") if str(x.get("name")) == str(apt)), None)
                if a is not None and a.get("outline_area") and not any(c["name"] == "apartment outline" for c in checks):
                    add("apartment outline", "info", f"apartment {apt}: all its rooms add up to {a.get('rooms_area'):g} m² inside a unit outline of {a.get('outline_area'):g} m² (the difference is walls)", [a.id])
    if st.fmt == "raster" and st.meta.get("detector"):
        bl = st.meta.get("borderline", {}); ents = {e.id: e for e in st.entities}
        keys = set()
        for p in plans:
            ks = p.get("kind") if isinstance(p.get("kind"), list) else [p.get("kind")]
            rt = [f.get("value") for f in p.get("filters", []) if f["field"] == "rtype"]
            for k in ks:
                if k == "room": keys |= {x for v in rt for x in (v if isinstance(v, list) else [v])}
                else: keys.add(k)
        near = [(k, b) for k in sorted(keys) for b in bl.get(k, [])]
        if near:
            up = sum(1 for _, b in near if b["why"].startswith("just below")); dn = len(near) - up
            add("detector confidence", "caution", f"{len(near)} detection(s) sit close to the confidence threshold: {dn} counted that could be false, {up} not counted that could be real. The count could be off by that much", [i for i in ev if ents.get(i) is not None and ents[i].get("borderline")])
        elif keys and not any(c["result"] in ("fail", "caution") for c in checks):
            confs = [ents[i].get("conf") for i in ev if ents.get(i) is not None and ents[i].get("conf") is not None]
            add("detector confidence", "pass", f"every counted detection is confidently above its threshold" + (f" (lowest confidence {min(confs):.2f})" if confs else "") + " and no near-miss detection exists for this class")
        rg = st.meta.get("room_geometry"); traced = [i for i in ev if ents.get(i) is not None and ents[i].kind == "room" and "traced" in str(ents[i].get("area_rule"))]
        if rg and traced:
            bad = [i for i in traced if ents[i].get("merged_suspect")]
            if bad:
                add("room outline", "caution", f"{len(bad)} room outline(s) used here hold two room names ({'; '.join(sorted({ents[i].get('merged_suspect') for i in bad}))}): an opening between them was not closed, so the outline covers more than one room and its area and perimeter are too large for either", bad)
            elif rg.get("leaks") and len(traced) > 0.5 * rg["rooms"]:
                add("room outline", "caution", f"{len(rg['leaks'])} named space(s) could not be enclosed and are missing from this total: {', '.join(rg['leaks'][:6])}")
            else:
                add("room outline", "info", f"room outlines traced from the walls; {rg['scale_source']}. No reference area is printed on the sheet to compare with")
        cc = st.meta.get("crosscheck") or {}
        for k in sorted(keys):
            v = (cc.get("keys") or {}).get(k)
            if not v: continue
            if v["used"] == v["second"] == v["confirmed"]:
                add("second reading", "pass", f"the sheet was read twice at different working scales ({cc.get('scale_used')} and {cc.get('scale_second')} of full size); both readings found the same {v['used']} '{k}' symbols on the same spots")
            else:
                add("second reading", "caution", f"the sheet was read twice at different working scales: this answer uses the reading with {v['used']} '{k}', the second reading found {v['second']}; {v['confirmed']} are on the same spots in both. The count is uncertain by about {max(v['used'], v['second']) - v['confirmed']}")
    if st.fmt == "raster" and not st.meta.get("detector"):
        tr = _track(); lv = st.meta.get("label_vs_detector", {})
        for p in plans:
            k = p.get("kind"); rt = next((f.get("value") for f in p.get("filters", []) if f["field"] == "rtype"), "")
            key = f"{k}:{rt}"
            if isinstance(rt, str) and rt and k == "room" and rt in lv:
                d, l = lv[rt]
                if l and d == l: add("labels read by OCR", "pass", f"the vision model counted {d} {rt} room(s) and OCR independently read {l} matching label(s)")
                elif l and d != l: add("labels read by OCR", "caution", f"the vision model counted {d} {rt} room(s) but OCR read {l} matching label(s)")
            if key in tr and tr[key][1] >= 20:
                a, n = tr[key]; acc = a / n
                add("track record", "fail" if acc < 0.75 else "info", f"on the evaluation set this kind of question on scanned plans was exactly right {100 * acc:.0f}% of the time ({a}/{n})" + ("; treat this number as an estimate" if acc < 0.75 else ""))
    if st.fmt == "ifc":
        srcs = sorted({str(r.get("how")) for r in rows if r.get("how")})
        if srcs: add("quantity source", "info", "values come from: " + "; ".join(srcs[:4]) + ". They are stated by the model's authoring tool and were not re-measured")
        elif "count" in ops: add("quantity source", "info", "counted from the model's element list by IFC class; a model has no second representation to compare with")
    if ans.get("plan_source", "rules") != "rules":
        add("question reading", "info", "the rules did not recognise this phrasing, so the query plan was written by a planner model and checked by running it. Check the 'read as' line: the arithmetic is exact, the reading of the question is the part that can be wrong")
    fails = [c for c in checks if c["result"] in ("fail", "caution")]; passes = [c for c in checks if c["result"] == "pass"]
    level = "review" if fails else ("checked" if passes else "single_source")
    head = {"review": "Needs review: " + fails[0]["text"] if fails else "", "checked": "Checked: " + (passes[0]["text"] if passes else ""),
            "single_source": "Single source: nothing contradicts this answer, but the drawing offers no second way to confirm it"}[level]
    return {"level": level, "checks": checks, "headline": head}

def _in_scope(st, ids, plans):
    """is an untyped room inside the apartment / level the question is about?"""
    ents = {e.id: e for e in st.of("room")}
    for p in plans:
        if p.get("kind") != "room": continue
        fl = [f for f in p.get("filters", []) if f["field"] in ("apartment", "level") and f.get("op", "eq") == "eq"]
        for x in ids:
            e = ents.get(x)
            if e is not None and all(str(e.get(f["field"])).lower() == str(f.get("value")).lower() for f in fl): return True
    return False

def _relevant(i, plans, ev):
    if i["ids"]: return bool(ev & set(i["ids"])) or i["code"] == "no_storey"
    return True

# ---------------------------------------------------------------------------------------------------------------------
OPW = {"count": "count", "sum": "add up the {attr} of", "avg": "average the {attr} of", "max": "find the largest {attr} among", "min": "find the smallest {attr} among",
       "exists": "check whether there are any", "get": "read '{attr}' of", "list": "list", "group_count": "count, per {g},", "group_sum": "add up the {attr}, per {g}, of", "value": "read the {attr} of"}
FOP = {"eq": "is", "in": "is one of", "nin": "is not", "match": "is named", "contains": "contains", "regex": "matches", "gt": "is above", "lt": "is below", "gte": "is at least", "lte": "is at most", "true": "is set"}
FLD = {"rtype": "room type", "apartment": "apartment", "level": "level", "number": "room number", "type_name": "type", "ifc_class": "IFC class", "is_external": "external flag"}

def read_as(plan):
    if plan.get("parts"): return " ; ".join(f"{p.get('label') or 'part'}: {read_as(p)}" for p in plan["parts"])
    k = plan.get("kind"); k = " + ".join(k) if isinstance(k, list) else str(k)
    s = OPW.get(plan.get("op"), plan.get("op")).format(attr=plan.get("attr"), g=plan.get("group_by") or "type") + f" all {k}s"
    fl = [f"{FLD.get(f['field'], f['field'])} {FOP.get(f.get('op', 'eq'), f.get('op'))} {f.get('value') if not isinstance(f.get('value'), list) else ' / '.join(map(str, f['value']))}" for f in plan.get("filters", [])]
    if fl: s += " where " + " and ".join(fl)
    if plan.get("convert"): s += f", converted to {plan['convert']}"
    return s

def narrate(st, ans):
    """ordered reasoning lines: {t: source|read|note|step|result|refused, text, ids}"""
    out = []; plan = ans.get("plan")
    for s in ans.get("trace") or []:
        if s["op"] == "route": out.append({"t": "source", "text": "Source: " + s["text"]})
    if plan: out.append({"t": "read", "text": "Question read as: " + read_as(plan) + (" (reading by rules)" if ans.get("plan_source") == "rules" else f" (reading by the planner model [{str(ans.get('plan_source')).replace('llm:', '')}], cached)")})
    for n in ans.get("interpretation") or []: out.append({"t": "note", "text": n})
    fl = list((plan or {}).get("filters", [])) if plan and not plan.get("parts") else []
    for s in ans.get("trace") or []:
        op = s["op"]
        if op == "select": out.append({"t": "step", "text": f"Found {s.get('n', 0)} in the drawing ({s['text'].replace('select all ', '').replace(' extracted from the drawing', '')})", "ids": s.get("ids")})
        elif op == "filter":
            f = fl.pop(0) if fl else None; n1 = s.get("n", 0); n0 = n1 + len(s.get("dropped") or [])
            txt = (f"Kept those where {FLD.get(f['field'], f['field'])} {FOP.get(f.get('op', 'eq'), f.get('op'))} {f.get('value') if not isinstance(f.get('value'), list) else ' / '.join(map(str, f['value']))}: {n1} remain" if f else s["text"][0].upper() + s["text"][1:])
            out.append({"t": "step", "text": txt, "ids": s.get("ids")})
        elif op == "part": fl = list((s.get("plan") or {}).get("filters", [])); out.append({"t": "read", "text": "Part: " + s["text"]})
        elif op in ("fallback", "alias", "map"): out.append({"t": "note", "text": s["text"], "ids": s.get("ids")})
        elif op == "measure":
            v = s.get("values") or []; hows = sorted({str(r.get("how")) for r in v if r.get("how")})
            out.append({"t": "step", "text": f"Measured {len(v)} element(s)" + (". Method: " + hows[0] if len(hows) == 1 else (f". {len(hows)} different methods, see the table" if hows else "")), "ids": [r["id"] for r in v]})
        elif op in ("sum", "avg", "max", "min", "count", "exists", "convert", "get", "group_count", "group_sum", "list"): out.append({"t": "result", "text": s["text"]})
        elif op == "warn": out.append({"t": "note", "text": s["text"], "ids": s.get("ids")})
    if ans.get("status") != "ok": out.append({"t": "refused", "text": "Not answered: " + str(ans.get("reason"))})
    return out
