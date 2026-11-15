"""LLM fallback: ONLY used when the rule parser cannot build a plan. Opus translates the question into a plan made of the
engine's operations; it never produces the number. Plans are validated against the store and cached (deterministic replays)."""
import json, hashlib, os, subprocess, re, threading
_LOCK = threading.Lock()

HERE = os.path.dirname(__file__)
CACHE = os.path.join(HERE, "..", "cache", "llm_plans.json")
OPS = ["count", "exists", "get", "sum", "avg", "max", "min", "list", "group_count", "group_sum"]
ATTRS = ["area", "length", "volume", "side_area", "perimeter", "width", "height", "thickness", None]
FIELDS = ["id", "guid", "ifc_class", "members", "label", "number", "name", "rtype", "level", "apartment", "type_name", "indoor", "predefined", "room", "is_external", "fire_rating", "load_bearing", "material", "value", "elevation", "area", "volume", "length", "width", "height"]
FOPS = ["eq", "in", "nin", "match", "contains", "regex", "true", "gt", "lt", "gte", "lte"]

def _load():
    try: return json.load(open(CACHE))
    except Exception: return {}

def _attrs_by_kind(store):
    out = {}
    for e in store.entities:
        s = out.setdefault(e.kind, set())
        for k, v in e.attrs.items():
            if isinstance(v, (int, float, str, bool)) and not k.endswith("_rule"): s.add(k)
    return {k: sorted(v)[:20] for k, v in out.items()}

def _inventory(store):
    """what exists in this model, so the planner filters on values that are really there: IFC classes with counts and
    their commonest type names / predefined types, and the property names available per kind"""
    import collections as C
    cls = C.defaultdict(lambda: [0, C.Counter(), C.Counter()])
    props = C.defaultdict(C.Counter); systems = C.Counter()
    for e in store.entities:
        ic = e.attrs.get("ifc_class")
        if ic:
            c = cls[(e.kind, ic)]; c[0] += 1
            if e.attrs.get("type_name"): c[1][str(e.attrs["type_name"])[:50]] += 1
            if e.attrs.get("predefined"): c[2][str(e.attrs["predefined"])] += 1
        for k in (e.attrs.get("props") or {}): props[e.kind][k] += 1
        if e.attrs.get("system"): systems[e.attrs["system"]] += 1
    lines = [f"  kind={k} ifc_class={ic} n={c[0]}" + (f" predefined={dict(c[2].most_common(4))}" if c[2] else "") + (f" types={[t for t, _ in c[1].most_common(4)]}" if c[1] else "")
             for (k, ic), c in sorted(cls.items(), key=lambda kv: -kv[1][0])[:45]]
    pl = [f"  {k}: {[n for n, _ in v.most_common(28)]}" for k, v in props.items() if k != "fact"]
    return "\n".join(lines), "\n".join(pl[:14]), [s_ for s_, _ in systems.most_common(25)]

def _recipes_block(store):
    """the memory file (RECIPES.md) and the data dictionary, given when the drawing has rooms a recipe can work on"""
    try:
        from . import recipes as R
        if "room" not in store.kinds(): return ""
        d = R.dictionary(store)
        if not d.get("room"): return ""
        lines = [f" {k}.{a_}: {v['meaning']}" + (f" [{v['unit']}]" if v.get("unit") else "") for k, at in d.items() for a_, v in at.items()]
        return ("DATA DICTIONARY (what the attributes of THIS drawing mean; numeric ones can be used as attr or inside expr):\n" + "\n".join(lines) +
                "\n\nRECIPES (scripts you can choose instead of writing a plan; read what each computes and pick by meaning, not by keyword):\n" + R.recipes_text() + "\n")
    except Exception: return ""

def prompt_for(question, store, feedback=None, memory_block=""):
    S = store.summary()
    inv, pinv, sysn = _inventory(store) if store.fmt == "ifc" else ("", "", [])
    rtypes = sorted({str(e.get("rtype")) for e in store.of("room")})
    tnames = sorted({str(e.get("type_name")) for e in store.entities if e.get("type_name")})[:40]
    return f"""You translate a question about a construction drawing into a JSON query plan for a deterministic engine.
You never compute the answer yourself; the engine runs the plan on entities extracted from the drawing.

ENTITIES IN THIS DRAWING: {json.dumps(S["kinds"])}
ATTRIBUTES PER KIND: {json.dumps(_attrs_by_kind(store))}
ROOM LABELS: {json.dumps(S["room_labels"][:80], ensure_ascii=False)}
ROOM TYPES (rtype): {json.dumps(rtypes)}
LEVELS: {json.dumps(S["levels"], ensure_ascii=False)}
APARTMENTS: {json.dumps(S["apartments"])}
TYPE NAMES (sample): {json.dumps(tnames, ensure_ascii=False)}
TABLES (kind "schedule_row"; each row's attributes are the column keys): {json.dumps([dict(t, sample=[{k: v for k, v in e.attrs.items()} for e in store.of("schedule_row") if e.get("table") == t["title"]][:2]) for t in store.meta.get("tables", []) if t["rows"]], ensure_ascii=False)[:6000]}
 Use op "get" with attr = a column key to read a cell value (e.g. a door's width); "count" with filters on column keys to count rows;
 filter values must match the cell text as written in the drawing (case-insensitive); use "contains"/"regex" for partial matches.
IFC CLASSES IN THIS MODEL (filter on ifc_class / predefined / type_name with values from this list):
{inv}
PROPERTY-SET PROPERTIES PER KIND (usable as filter field, attr, group_by, or in "fields"):
{pinv}
SYSTEM NAMES ON ELEMENTS (attr "system"): {json.dumps(sysn, ensure_ascii=False)}
MODEL FACT NAMES (kind "fact"): {json.dumps(sorted(e.get("name") for e in store.of("fact")))}

PLAN SCHEMA (JSON object):
 op: one of {OPS}
 kind: one of the entity kinds above (or a list of kinds to count together)
 attr: one of {ATTRS[:-1]} or a column key, or null (required for sum/avg/max/min/group_sum/get)
 filters: list of {{"field": one of {FIELDS}, "op": one of {FOPS}, "value": ...}}  (all must hold)
 group_by: attribute name for group_count/group_sum, else null
 convert: optional unit conversion of the final value: "ft2" | "ft" | "cm" | "mm" | "cm2" (base units are m, m², m³)
 numeric thresholds use filter ops gt/lt/gte/lte in base units (m, m²); "exists" answers yes/no
 notes: list of short strings explaining how you interpreted the question (shown to the user)
MULTI-PART questions (several facts asked at once): return {{"parts": [<plan>, <plan>, ...], "notes": [...]}} where each
part is a plan as above with an extra "label" naming that part. Model facts (software, schema, project name) are entities of
kind "fact" with attrs name/value: use op "list" with a filter on name. Levels are entities of kind "level" (name, elevation).
Other IFC elements (pipes, valves, radiators, tanks, ducts, fittings, coverings, footings, proxies) are kind "element" with
attrs ifc_class (e.g. IfcPipeSegment, IfcValve, IfcSpaceHeater, IfcTank, IfcDuctSegment, IfcCovering, IfcFooting), type_name,
level, length, volume, material. Systems are kind "system" (name, ifc_class, members). Every IFC entity has attr guid.
Level entities have elevation and height (= distance to the next level up).
For element properties use filters on is_external, fire_rating, load_bearing, material, predefined.
More IFC attributes: thickness (m), material_layers (text "name thickness mm; ..."), height, width, side_area, space (name of
the room an element is placed in), system (system names of an element), predefined, ifc_class. Lengths/areas/volumes are in m, m², m³.
For op "list" you may add "fields": [attribute or property names] to show those columns for each row; add "distinct": true
to get one row per distinct combination of those fields with a count (use this for "per type" questions such as the
thickness or material layers of each wall type: fields ["type_name","thickness","material_layers"], distinct true).
A question that asks for several things (total AND breakdown by level AND by type) must use "parts", one part per thing asked.
Use group_count/group_sum with group_by level / type_name / ifc_class / predefined / material / system / space / thickness for breakdowns.
If the question cannot be answered from these entities, return {{"op": "refuse", "notes": ["why"]}}.

EXAMPLES
Q: How many bathrooms are on this floor? -> {{"op":"count","kind":"room","attr":null,"filters":[{{"field":"rtype","op":"eq","value":"bathroom"}}],"group_by":null,"notes":["bathroom = room type bathroom"]}}
Q: What wall types are used, with their quantities? -> {{"op":"group_count","kind":"wall","attr":null,"filters":[],"group_by":"type_name","notes":[]}}
Q: Which room is the largest? -> {{"op":"max","kind":"room","attr":"area","filters":[],"group_by":null,"notes":["largest = max floor area"]}}

{_recipes_block(store)}
{memory_block}QUESTION: {question}
{("YOUR PREVIOUS PLAN FAILED WHEN EXECUTED: " + feedback + chr(10) + "Write a corrected plan (different filters or kind; use only values listed above).") if feedback else ""}
Answer with the JSON plan only."""

def validate(plan, store):
    if not isinstance(plan, dict): return "plan is not an object"
    if plan.get("parts"):
        for p in plan["parts"]:
            e = validate(p, store)
            if e: return "part " + str(p.get("label")) + ": " + e
        plan.setdefault("notes", []); return None
    if plan.get("op") == "refuse": return "model says: " + "; ".join(plan.get("notes", []))
    if plan.get("recipe") and "op" not in plan:        # a recipe call: compile it to an ordinary plan (in place), then validate that
        from . import recipes as R
        comp, err = R.compile(plan, store)
        if err: return f"recipe '{plan.get('recipe')}': {err}"
        plan.clear(); plan.update(comp)
    if plan.get("expr"):
        from . import recipes as R
        k_ = plan.get("kind")
        _, err = R.safe_expr(plan["expr"], R.numeric_attrs(store, k_) if isinstance(k_, str) else set())
        if err: return err
    if plan.get("op") not in OPS: return f"unknown op {plan.get('op')}"
    ks = plan.get("kind") if isinstance(plan.get("kind"), list) else [plan.get("kind")]
    if not all(k in store.kinds() for k in ks): return f"kind {plan.get('kind')!r} not present in this drawing"
    if plan["op"] in ("sum", "avg", "max", "min", "group_sum") and not plan.get("attr") and not plan.get("expr"): return "measure op without attr"
    known = {k for e in store.entities for k in e.attrs} | set(FIELDS) | {k for e in store.entities for k in (e.attrs.get("props") or {})}
    known |= {"props." + k for k in known} | {k.lower() for k in known}
    for f in (plan.get("filters", []) or []) + (plan.get("exclude") or []):
        if f.get("op", "eq") not in FOPS: return f"bad filter op {f.get('op')}"
        if f.get("field") not in known and str(f.get("field")).lower() not in known: return f"unknown field {f.get('field')!r}"
    plan.setdefault("filters", []); plan.setdefault("notes", []); plan.setdefault("group_by", None); plan.setdefault("attr", None)
    return None

PROMPT_V = 3      # bump when the prompt changes, so cached plans written for an older prompt are not replayed
COST = os.path.join(HERE, "..", "cache", "llm_cost.jsonl")
def _gemini(prompt, model, question, timeout=120):
    """Planner call to the Gemini API. The key is read from the GEMINI_API_KEY environment variable only.
    Model id e.g. 'gemini-4-carbon'; thinking level from BP_GEMINI_THINKING (low | medium | high, default low)."""
    import urllib.request, urllib.error, time
    key = os.environ.get("GEMINI_API_KEY")
    if not key:      # fallback: a private key file outside the project folder (never shipped with the code)
        try: key = open(os.path.expanduser("~/.config/bp_gemini_key")).read().strip()
        except Exception: key = None
    if not key: return None, "GEMINI_API_KEY is not set"
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "thinkingConfig": {"thinkingLevel": os.environ.get("BP_GEMINI_THINKING") or __import__("bp.models", fromlist=["effort"]).effort()}}}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": key})
    t0 = time.time()
    try: r = json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e: return None, f"Gemini API error {e.code}: {e.read()[:200].decode(errors='replace')}"
    except Exception as e: return None, f"Gemini call failed: {type(e).__name__}: {str(e)[:120]}"
    u = r.get("usageMetadata", {}) or {}
    with _LOCK: open(COST, "a").write(json.dumps({"model": model, "usd": None, "in": u.get("promptTokenCount"), "out": (u.get("candidatesTokenCount") or 0) + (u.get("thoughtsTokenCount") or 0),
                "cache_read": u.get("cachedContentTokenCount"), "cache_write": None, "ms": round((time.time() - t0) * 1000), "q": question[:80]}) + "\n")
    try: return "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"]), None
    except Exception: return None, "Gemini returned no text: " + json.dumps(r)[:200]

def plan(question, store, model=None, timeout=180, feedback=None, memory_block=""):
    """returns (plan|None, source, error). Planner model: BP_PLANNER_MODEL (default opus). Every call's tokens and
    cost are appended to cache/llm_cost.jsonl so inference cost is measured, not estimated."""
    from . import models as MODELS
    model = model or MODELS.for_task("plan")[0]
    if MODELS.missing_key(model) and not os.environ.get("BP_NO_LLM"): _nokey = MODELS.missing_key(model)
    else: _nokey = None
    key = hashlib.sha1(json.dumps([question, store.summary()] + ([] if model == "opus" and store.fmt != "ifc" else [model, PROMPT_V]) + ([feedback] if feedback else []), sort_keys=True, default=str).encode()).hexdigest()
    cache = _load()
    if key in cache:
        p = cache[key]; err = validate(p, store)
        return (None, "llm-cache", err) if err else (p, "llm-cache", None)
    if os.environ.get("BP_NO_LLM"): return None, "llm", "LLM fallback disabled (BP_NO_LLM)"
    if _nokey: return None, "llm", _nokey
    from . import claude_api as CA
    if model.startswith("gemini") or CA.is_claude_api(model):
        if CA.is_claude_api(model): out, err = CA.call(model, prompt_for(question, store, feedback, memory_block), question=question, use="plan")
        else: out, err = _gemini(prompt_for(question, store, feedback, memory_block), model, question, timeout)
        if out is None: return None, "llm", err
        try: m = re.search(r"\{.*\}", out, re.S); p = json.loads(m.group(0)) if m else None
        except Exception as e: return None, "llm", f"planner returned invalid JSON: {e}"
        err = validate(p, store)
        with _LOCK:
            cur = _load(); cur[key] = p
            tmp = CACHE + f".{os.getpid()}.tmp"; json.dump(cur, open(tmp, "w"), indent=1, ensure_ascii=False); os.replace(tmp, CACHE)
        return (None, "llm", err) if err else (p, "llm", None)
    try:
        r = subprocess.run(["claude", "-p", "--model", model, "--output-format", "json", "--tools", "", "--no-session-persistence"],
                           input=prompt_for(question, store, feedback, memory_block), capture_output=True, text=True, timeout=timeout)
        js = json.loads(r.stdout) if r.stdout.strip().startswith("{") else None
        out = js["result"] if js else r.stdout
        if js:
            u = js.get("usage", {}) or {}
            with _LOCK: open(COST, "a").write(json.dumps({"model": model, "usd": js.get("total_cost_usd"), "in": u.get("input_tokens"), "out": u.get("output_tokens"),
                        "cache_read": u.get("cache_read_input_tokens"), "cache_write": u.get("cache_creation_input_tokens"), "ms": js.get("duration_ms"), "q": question[:80]}) + "\n")
        m = re.search(r"\{.*\}", out, re.S); p = json.loads(m.group(0)) if m else None
    except Exception as e:
        return None, "llm", f"LLM call failed: {type(e).__name__}: {str(e)[:120]}"
    err = validate(p, store)
    with _LOCK:   # merge-on-write: concurrent planners must never drop each other's cached plans
        cur = _load(); cur[key] = p
        tmp = CACHE + f".{os.getpid()}.tmp"; json.dump(cur, open(tmp, "w"), indent=1, ensure_ascii=False); os.replace(tmp, CACHE)
    return (None, "llm", err) if err else (p, "llm", None)
