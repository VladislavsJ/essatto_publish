"""Plan executor: plain code over the Store. Every filter and aggregate step is written to the Trace."""
import re, statistics, ast
from .core import Answer, Trace

UNITS = {"area": "m²", "side_area": "m²", "length": "m", "perimeter": "m", "volume": "m³", "height": "m", "width": "m", "thickness": "m", "elevation": "m"}

def _norm(s): return re.sub(r"\s+", " ", str(s or "")).strip().lower()

def _match(e, f):
    v = e.get(f["field"]); op = f.get("op", "eq"); want = f.get("value")
    if op == "eq": return _norm(v) == _norm(want) if isinstance(want, str) else v == want
    if op == "in": return v in want
    if op == "nin": return v not in want
    if op == "true": return bool(v)
    if op == "match":
        w = _norm(want); cands = [_norm(e.get(k)) for k in ("name", "label", "number")]
        return any(c and (c == w) for c in cands)
    if op == "contains": return _norm(want) in _norm(v)
    if op == "regex":
        try: return v is not None and re.search(str(want), str(v), re.I) is not None
        except re.error: return False
    if op in ("gt", "lt", "gte", "lte"):
        if not isinstance(v, (int, float)): return False
        return {"gt": v > want, "lt": v < want, "gte": v >= want, "lte": v <= want}[op]
    raise ValueError(f"unknown filter op {op}")

def run(plan, store):
    tr = Trace(); a = Answer(plan=plan); a.interpretation = list(plan.get("notes", []))
    kind = plan["kind"]; attr = plan.get("attr"); op = plan["op"]
    ALIAS = {"shower": "shower_or_bath", "bathtub": "shower_or_bath"}
    if isinstance(kind, str) and kind not in store.kinds() and ALIAS.get(kind) in store.kinds():
        tr.add("alias", f"this drawing's extractor reports showers and bathtubs together as '{ALIAS[kind]}'"); kind = ALIAS[kind]
        a.interpretation.append("showers and bathtubs are counted together for this image")
    if isinstance(kind, list):
        kinds = [ALIAS.get(k, k) if k not in store.kinds() else k for k in kind]; kinds = list(dict.fromkeys(kinds))
        pool = [e for e in store.entities if e.kind in kinds]; kind = "+".join(kinds)
        tr.add("select", f"select all entities of kinds {kinds} (counted together)", ids=[e.id for e in pool])
    else:
        pool = store.of(kind)
        tr.add("select", f"select all '{kind}' entities extracted from the drawing", ids=[e.id for e in pool])
    if plan.get("recipe"): tr.add("recipe", f"recipe '{plan['recipe']}'" + (f" with {plan['args']}" if plan.get("args") else "") + " (described in RECIPES.md)")
    for f in plan.get("filters", []):
        keep = [e for e in pool if _match(e, f)]; drop = [e for e in pool if e not in keep]
        tr.add("filter", f"keep {kind}s where {f['field']} {f.get('op','eq')} {f.get('value')!r}: {len(pool)} -> {len(keep)}", ids=[e.id for e in keep], dropped=[e.id for e in drop][:60])
        pool = keep
    for f in plan.get("exclude") or []:
        drop = [e for e in pool if _match(e, f)]
        tr.add("filter", f"leave out {kind}s where {f['field']} {f.get('op','eq')} {f.get('value')!r}: {len(pool)} -> {len(pool) - len(drop)}" + ("" if drop else " (nothing matched: nothing was left out)"), dropped=[e.id for e in drop][:60])
        if not drop: a.interpretation.append(f"nothing on this drawing matched the exclusion {f.get('value')!r}, so nothing was left out")
        pool = [e for e in pool if e not in drop]
    # a formula over one row's attributes (recipes compile to this; a plain plan may use it too)
    expr = plan.get("expr"); tree = None
    if expr:
        from . import recipes as R
        tree, err = R.safe_expr(expr, R.numeric_attrs(store, kind) if isinstance(kind, str) else set())
        if err: a.status = "refused"; a.reason = err; a.trace = tr.steps; return a
        attr = plan.get("as") or "value"
    def unit_for(at):
        if expr: return plan.get("unit")
        try:
            from . import recipes as R
            return UNITS.get(at) or R.unit_of(store, kind, at)
        except Exception: return UNITS.get(at)
    # fixture-kind with area -> containing room
    if attr and kind in ("toilet", "washbasin", "bathtub", "shower"):
        rooms = {e.get("room") for e in pool if e.get("room")}
        pool = [r for r in store.of("room") if r.id in rooms]
        tr.add("map", f"replace each {kind} by the room that contains it", ids=[e.id for e in pool])
    # fallback: asked for WC rooms but the plan found none -> rooms containing a toilet
    if attr and kind == "room" and not pool and any(f["field"] == "rtype" and f.get("value") == "wc" for f in plan.get("filters", [])):
        others = [f for f in plan["filters"] if not (f["field"] == "rtype")]
        pool = [r for r in store.of("room") if any(x.startswith("toilet:") for x in r.get("fixtures", []) or []) and all(_match(r, f) for f in others)]
        kinds = ", ".join(f"{r.id} ({r.get('rtype') or 'unlabelled'}: {r.get('rtype_rule') or ''})" for r in pool)
        tr.add("fallback", f"no room is labelled WC/toilet -> using rooms that contain a toilet bowl: {kinds}", ids=[e.id for e in pool])
        a.interpretation.append("no separate WC room exists; answered with the room(s) that contain the toilet" + (" — note: it is a bathroom, so the area includes bath/shower space" if any(r.get("rtype") == "bathroom" for r in pool) else ""))
    # 'bedroom' asked but the drawing only uses generic room names (Zimmer / Pièce / Stanza) -> say so and use them
    rt_f = next((f for f in plan.get("filters", []) if f["field"] == "rtype"), None)
    wants_bed = rt_f and (rt_f.get("value") == "bedroom" or (isinstance(rt_f.get("value"), list) and "bedroom" in rt_f["value"]))
    if wants_bed and not any(r.get("rtype") == "bedroom" for r in store.of("room")) and any(r.get("rtype") == "room" for r in store.of("room")):
        others = [f for f in plan["filters"] if f is not rt_f]
        extra = [r for r in store.of("room") if r.get("rtype") == "room" and all(_match(r, f) for f in others)]
        pool = pool + [r for r in extra if r not in pool]
        tr.add("fallback", "no room is labelled as a bedroom; generic habitable rooms (Zimmer/Pièce/Stanza) are counted as bedrooms", ids=[r.id for r in extra])
        a.interpretation.append("this plan names bedrooms generically ('Zimmer', 'Pièce', 'Stanza'); those rooms were used as bedrooms")
    a.evidence = [e.id for e in pool]
    if op in ("group_count", "group_sum", "list"):
        g = plan.get("group_by") or "type_name"; table = {}
        for e in pool:
            k = str(e.get(g)); v = 1 if op == "group_count" else (e.get(attr) or 0) if op == "group_sum" else None
            if op == "list": table.setdefault("rows", []).append({k2: v2 for k2, v2 in {"id": e.id, "label": e.get("label"), "number": e.get("number"), "level": e.get("level"), "value": e.get("value"), "type": e.get("type_name"), **({attr: e.get(attr)} if attr else {}), **{f_: e.get(f_) for f_ in (plan.get("fields") or [])}}.items() if v2 is not None})
            else: table[k] = round(table.get(k, 0) + v, 3)
        if op == "list" and plan.get("distinct") and plan.get("fields"):     # one row per distinct combination of the fields, with a count
            seen = {}
            for r in table.get("rows", []):
                k = tuple(str(r.get(f_)) for f_ in plan["fields"])
                if k in seen: seen[k]["count"] += 1
                else: seen[k] = {**{f_: r.get(f_) for f_ in plan["fields"] if r.get(f_) is not None}, "count": 1}
            table = {"rows": sorted(seen.values(), key=lambda r: -r["count"])}
        if op != "list": table = dict(sorted(table.items(), key=lambda kv: -kv[1]))
        a.value = table; a.unit = unit_for(attr) if op == "group_sum" else ("count" if op == "group_count" else None)
        tr.add(op, f"{op} of {len(pool)} {kind}s" + (f" by {g}" if op != "list" else ""), values=table if op != "list" else table.get("rows", [])[:200])
        a.trace = tr.steps; return a
    if op == "get":
        vals = [str(e.get(attr)) for e in pool if e.get(attr) is not None]
        a.value = "; ".join(vals) if vals else None; a.unit = None
        tr.add("get", f"read column '{attr}' of {len(pool)} matching row(s)", values=[{"id": e.id, "label": e.get("label"), attr: e.get(attr)} for e in pool][:50])
        if not vals: a.status = "refused"; a.reason = f"no matching {kind} has a value for '{attr}'"
        a.trace = tr.steps; return a
    if op == "exists":
        a.value = "yes" if pool else "no"; a.unit = None
        tr.add("exists", f"{len(pool)} matching {kind}(s) -> {a.value}", ids=[e.id for e in pool]); a.trace = tr.steps; return a
    if op == "count":
        a.value = len(pool); a.unit = "count"
        tr.add("count", f"count = {len(pool)}", ids=[e.id for e in pool])
    else:
        if not attr: a.status = "refused"; a.reason = f"op '{op}' needs a measured attribute"; a.trace = tr.steps; return a
        rows, missing = [], []
        for e in pool:
            v = e.get(attr) if tree is None else R.eval_expr(tree, e.attrs)
            if isinstance(v, str):   # schedule cells: '5183 SF', '3\' - 0"' -> first number (feet-inches handled as feet)
                m = re.match(r"\s*(\d+)\s*'\s*-?\s*(\d+(?:\s+\d+/\d+)?)?\s*\"?", v)
                if m and "'" in v:
                    inch = m.group(2) or "0"; parts = inch.split(); frac = eval(parts[1]) if len(parts) > 1 else 0
                    v = round(int(m.group(1)) + (float(parts[0]) + frac) / 12, 4)
                else:
                    m = re.search(r"-?\d[\d,]*\.?\d*", v); v = float(m.group().replace(",", "")) if m else None
            if v is None: missing.append(e.id); continue
            rows.append({"id": e.id, "label": e.get("label") or e.get("name"), "number": e.get("number"), attr: round(v, 3),
                         "how": (f"{expr} with " + ", ".join(f"{n_}={e.get(n_):g}" for n_ in sorted({x.id for x in ast.walk(tree) if isinstance(x, ast.Name)}))) if tree is not None else (e.get(f"{attr}_rule") or e.get("qty_rule")),
                         "printed": e.get(f"{attr}_printed") if tree is None else None})
        tr.add("measure", (f"compute {attr} = {expr} for each selected {kind}" if tree is not None else f"read '{attr}' of each selected {kind}"), values=rows)
        if missing: tr.add("warn", f"{len(missing)} selected {kind}(s) have no '{attr}' and were skipped", ids=missing)
        chk = [r for r in rows if r.get("printed") is not None]
        if chk:
            bad = [r for r in chk if abs(r[attr] - r["printed"]) > 0.02 * max(r["printed"], 0.01)]
            tr.add("check", f"cross-check against the areas printed on the drawing: {len(chk) - len(bad)}/{len(chk)} agree within 2%", values=[{"id": r["id"], "computed": r[attr], "printed": r["printed"]} for r in chk][:40])
        vals = [r[attr] for r in rows]
        if not vals:
            a.status = "refused"; a.reason = f"no '{kind}' entity matched the question" if not pool else f"selected {kind}s have no {attr}"
            a.trace = tr.steps; return a
        if op in ("sum", "value"): a.value = round(sum(vals), 3); tr.add(op, " + ".join(f"{v:g}" for v in vals[:30]) + (" + …" if len(vals) > 30 else "") + f" = {a.value:g}")
        elif op == "avg": a.value = round(statistics.mean(vals), 3); tr.add("avg", f"mean of {len(vals)} values = {a.value:g}")
        elif op in ("max", "min"):
            best = (max if op == "max" else min)(rows, key=lambda r: r[attr]); a.value = best[attr]; a.evidence = [best["id"]]
            tr.add(op, f"{op} {attr} = {best[attr]:g} ({best['id']} {best.get('label') or ''})", ids=[best["id"]])
            a.interpretation.append(f"answer is {best['id']} {best.get('label') or ''}".strip())
            a.entity = {"id": best["id"], "label": best.get("label"), "number": best.get("number"), attr: best[attr]}
        a.unit = unit_for(attr)
        conv = plan.get("convert")
        if conv and isinstance(a.value, (int, float)):
            f, u = {"ft2": (10.76391, "ft²"), "ft": (3.28084, "ft"), "cm": (100, "cm"), "mm": (1000, "mm"), "cm2": (10000, "cm²")}[conv]
            nv = round(a.value * f, 3); tr.add("convert", f"{a.value:g} {a.unit} × {f:g} = {nv:g} {u}"); a.value, a.unit = nv, u
        if len(pool) > 1 and op == "sum" and any(f["field"] == "number" for f in plan.get("filters", [])):
            tr.add("warn", "more than one room matched a room number")
    a.trace = tr.steps
    return a
