"""Entry point: ask(path, question) -> Answer (dict). Deterministic: same file + same question -> same plan, value, trace.
Routing is by FILE CONTENT/format, not by dataset folder name."""
import os, glob, json, pickle, hashlib, time, re
from .core import Answer
from . import plan as P, execute as X

HERE = os.path.dirname(__file__)
CACHE = os.path.join(HERE, "..", "cache", "stores"); os.makedirs(CACHE, exist_ok=True)
from collections import OrderedDict
_MEM = OrderedDict(); _MEM_MAX = 6      # small LRU; the disk cache (pickles) holds everything

def twin(path, exts):
    """same drawing in another format: <name>.<ext> next to it (dataset convention)"""
    base = path.rsplit(".", 1)[0]
    for e in exts:
        if os.path.exists(f"{base}.{e}"): return f"{base}.{e}"
    return None

def ifc_source(path):
    """rendered IFC plan '<model>_<level>.pdf' -> '<model>.ifc' (longest matching prefix)"""
    if path.endswith(".ifc"): return path
    d, b = os.path.dirname(path), os.path.basename(path).rsplit(".", 1)[0]
    c = [p for p in glob.glob(f"{d}/*.ifc") if b.startswith(os.path.basename(p)[:-4])]
    return max(c, key=len) if c else None

def pick(path):
    """returns (extractor_name, file_to_extract, note)"""
    if path.lower().endswith(".ifc"): return "ifc", path, "IFC model"
    dxf = twin(path, ["dxf"])
    if dxf:
        with open(dxf, "rb") as fh: head = fh.read(200000)
        if b"ROOM_OUTLINE" in head or b"ROOM_TEXT" in head: return "dxf", dxf, f"using the DXF twin of this drawing ({os.path.basename(dxf)})"
    src = ifc_source(path)
    if src: return "ifc", src, f"plan rendered from IFC model {os.path.basename(src)}; answering from the model"
    if path.lower().endswith((".png", ".jpg", ".jpeg")): return "raster", path, "raster image: symbols from cached vision detections + OCR"
    if path.lower().endswith(".pdf"): return "pdf", path, "vector PDF: text + tables"
    return None, path, "no extractor for this file type"

def _code_version(name):
    """hash of the extractor + core source: any code change invalidates cached stores (no stale entities)"""
    files = [os.path.join(HERE, f) for f in ("core.py", "vocab.py", f"x_{name}.py") + (("ifc_qty.py",) if name == "ifc" else ())]
    return hashlib.sha1(b"".join(open(f, "rb").read() for f in files if os.path.exists(f))).hexdigest()[:10]

def store_for(path):
    name, f, note = pick(path)
    if name is None: return None, note
    key = hashlib.sha1(f"{name}:{os.path.abspath(f)}:{os.path.getmtime(f)}:{_code_version(name)}".encode()).hexdigest()[:16]
    if key in _MEM: _MEM.move_to_end(key); return _MEM[key], note
    pk = os.path.join(CACHE, key + ".pkl")
    if os.path.exists(pk):
        st = pickle.load(open(pk, "rb"))
    else:
        if name == "dxf": from .x_dxf import extract
        elif name == "ifc": from .x_ifc import extract
        elif name == "raster": from .x_raster import extract
        elif name == "pdf": from .x_pdf import extract
        st = extract(f)
        pickle.dump(st, open(pk, "wb"))
    st.meta["route_note"] = note
    _MEM[key] = st
    while len(_MEM) > _MEM_MAX: _MEM.popitem(last=False)
    return st, note

def _execute(plan, st, question):
    """run a (possibly multi-part) plan -> Answer. Pure code."""
    if plan.get("parts"):   # multi-part question: run each sub-plan, compose the text deterministically
        subs = [(p.get("label") or f"part {i+1}", X.run(p, st)) for i, p in enumerate(plan["parts"])]
        a = Answer(plan=plan); a.interpretation = list(plan.get("notes", []))
        lines, tr = [], []
        for lab, s in subs:
            v = s.value
            if isinstance(v, dict) and "rows" in v: vs = "; ".join(_row_text(r) for r in v["rows"][:80]) or "none"
            elif isinstance(v, dict): vs = ", ".join(f"{k}: {x:g}" if isinstance(x, (int, float)) else f"{k}: {x}" for k, x in list(v.items())[:60]) or "none"
            elif isinstance(v, float): vs = f"{v:g}"
            else: vs = str(v)
            lines.append(f"{lab}: {vs}{(' ' + s.unit) if s.unit and s.unit != 'count' and not isinstance(v, dict) else ''}" if s.status == "ok" else f"{lab}: not available ({s.reason})")
            tr.append({"op": "part", "text": f"{lab}", "plan": {k: v for k, v in s.plan.items() if k != "notes"}})
            tr += s.trace; a.evidence += s.evidence; a.interpretation += s.interpretation
        a.value = "\n".join(lines); a.unit = None; a.trace = tr
        a._parts = [(lab, s.status, s.value, s.reason) for lab, s in subs]
        return a
    a = X.run(plan, st)
    if a.entity and re.match(r"\s*(which|what (?:room|space|door|window|wall)|welche|welcher|quel|quelle|quale|mikä|milline|hvilk|cuál)", question, re.I):
        ent = a.entity; num = a.value
        a.value = f"{ent.get('label') or ent['id']}" + (f" ({ent['number']})" if ent.get("number") and ent.get("number") != ent.get("label") else "") + f": {num:g} {a.unit or ''}".rstrip()
        a.unit = None
    return a

def _row_text(r):
    if "id" not in r: return ", ".join(f"{k}={v}" for k, v in r.items())
    head = str(r.get("label") or r.get("value") or r.get("id")); extra = [f"{k}={v}" for k, v in r.items() if k not in ("id", "label", "value", "number")]
    if r.get("value") is not None and r.get("label"): head += f": {r['value']}"
    return head + (f" ({', '.join(extra)})" if extra else "")

def _empty(v): return v is None or v == {} or v == "" or (isinstance(v, dict) and v.get("rows") == [] and len(v) == 1)

def _problems(a):
    """what a planner should be told about an executed plan: [] if the result is usable"""
    parts = getattr(a, "_parts", None)
    if parts is not None:
        return [f"part '{lab}' returned nothing ({reason or 'no entity matched its filters'})" for lab, stt, v, reason in parts if stt != "ok" or _empty(v)]
    if a.status != "ok": return [f"the plan could not be answered: {a.reason}"]
    if _empty(a.value): return ["the plan matched no entities (empty result): the filter values do not occur in this model"]
    return []

def _llm_plan(question, st, memory_block=""):
    """Cheap model first; its plan is EXECUTED and checked. A plan that fails is sent back once with the failure
    (repair); only if that also fails is the strong model asked. Returns (plan, source, error, answer)."""
    from . import llm
    cheap = os.environ.get("BP_PLANNER_MODEL", "haiku"); strong = os.environ.get("BP_PLANNER_STRONG", "opus")
    best = None; fb = None; err = None
    for stage, model in ((cheap, cheap), (cheap + "+repair", cheap), (strong, strong)):
        if stage != cheap and fb is None: break                       # nothing to repair
        if model in ("none", "", None) or (stage == strong and strong == cheap): continue
        plan, src, err = llm.plan(question, st, model=model, feedback=fb, memory_block=memory_block)
        if plan is None:
            fb = f"invalid plan: {err}"; continue
        try: a = _execute(plan, st, question)
        except Exception as e:
            fb = f"plan execution failed: {type(e).__name__}: {e}"; continue
        pr = _problems(a)
        if best is None or len(pr) < best[3]: best = (plan, "llm:" + stage + ("" if src == "llm" else "-cached"), a, len(pr))
        if not pr: break
        fb = json.dumps({k: v for k, v in plan.items() if k != "notes"}, ensure_ascii=False)[:1500] + " -> " + "; ".join(pr)[:600]
    if best is None: return None, "llm", err, None
    return best[0], best[1], None, best[2]

def ask(path, question, allow_llm=True):
    t0 = time.time()
    try:
        st, note = store_for(path)
    except Exception as e:
        return Answer(status="error", reason=f"extraction failed: {type(e).__name__}: {e}").to_dict()
    if st is None: return Answer(status="refused", reason=note).to_dict()
    plan, why = P.parse(question, st); src = "rules"; a = None
    if question.strip().lower().rstrip(".?!") == "check this plan against the regulations":     # the page's 'check' button: a command, not a question to interpret
        from . import recipes as RCP
        plan, err = RCP.compile({"recipe": "regulation_check", "filters": [], "args": {}}, st)
        if plan is None: why = [err]
    mem = None; mem_note = None
    if plan is None:
        # solved-question memory (bp/memory.py): a small local classifier recognises the kind of question.
        # Confident and all slots filled from the question and this drawing -> the stored pattern is run, no planner call.
        # Otherwise the nearest stored questions go into the planner prompt as worked examples.
        try:
            from . import memory as MEMO, llm as _llm
            mem = MEMO.recall(question)
            if mem and mem["pattern"] != "other" and mem["prob"] >= MEMO.REPLAY_MIN:
                cand, miss = MEMO.fill(mem["pattern"], question, st)
                if cand is not None:
                    err_ = _llm.validate(cand, st)
                    if err_ is None:
                        a_try = _execute(cand, st, question)
                        if not _problems(a_try): plan, a, src = cand, a_try, "memory"
                        else: mem_note = "the stored pattern ran but returned nothing here: " + "; ".join(_problems(a_try))[:160]
                    else: mem_note = "the stored pattern does not fit this drawing: " + str(err_)[:160]
                else: mem_note = miss
            elif mem: mem_note = "not confident enough to reuse a pattern without the planner" if mem["pattern"] != "other" else "no stored pattern for this kind of question"
        except Exception as e:
            mem = None; mem_note = f"memory unavailable: {type(e).__name__}"
    if plan is None and allow_llm:
        mb = ""
        try:
            if mem and mem["pattern"] != "other" and mem["prob"] >= 0.40: mb = MEMO.fewshot_block(mem)      # a weak guess is not shown to the planner: it would mislead more than help
        except Exception: mb = ""
        plan, src, err, a = _llm_plan(question, st, mb)
        if plan is not None and mb: src = src + "+memory"
        if plan is None: why = why + [err]
    if plan is None:
        try:        # say what this drawing's database does hold, so the refusal never contradicts what is visible
            from . import recipes as R
            dd = R.dictionary(st); kinds = st.kinds()
            have = "; ".join(f"{n} {k}(s)" + (f" with {', '.join(sorted(dd[k]))}" if dd.get(k) else "") for k, n in sorted(kinds.items()) if k not in ("room_label", "fact"))
            if have: why = list(why) + [f"What was read from this drawing: {have}"]
        except Exception: pass
        a = Answer(status="refused", reason="; ".join(str(w) for w in why)); a.trace = [{"op": "route", "text": note}]
        d = a.to_dict(); d["trust"] = {"level": "none", "checks": [], "headline": "Not answered: " + str(a.reason)}
        d["reasoning"] = [{"t": "source", "text": "Source: " + note}, {"t": "refused", "text": "Not answered: " + str(a.reason)}]
        return d
    try:
        if a is None: a = _execute(plan, st, question)
    except Exception as e:
        a = Answer(status="error", reason=f"plan execution failed: {type(e).__name__}: {e}", plan=plan)
    if hasattr(a, "_parts"): del a._parts
    # A model-written count that matched nothing by NAME is not a zero: the drawing's database simply has no such thing
    # (e.g. 'parking spaces' on a sheet whose database holds rooms, doors and windows). Refuse and say what it does hold.
    if src != "rules" and plan.get("op") == "count" and a.status == "ok" and a.value == 0 and any(f.get("field") in ("name", "label", "rooms", "type_name", "number") for f in plan.get("filters") or []):
        try:
            from . import recipes as R
            dd = R.dictionary(st); have = "; ".join(f"{n} {k}(s)" for k, n in sorted(st.kinds().items()) if k not in ("room_label", "fact"))
        except Exception: have = ""
        looked = ", ".join(repr(f.get("value")) for f in plan["filters"] if f.get("field") in ("name", "label", "rooms", "type_name", "number"))
        a.status = "refused"; a.value = None; a.unit = None
        a.reason = f"nothing on this drawing is named {looked}, and the drawing's database has no such kind of object, so a count of 0 would be a guess. What was read: {have}"
    if plan.get("regcheck") and a.status == "ok":   # recipe regulation_check: findings with clauses replace the room list
        try:
            from . import regcheck as RC
            a = RC.apply(st, a, question)
        except Exception as e:
            a.status = "refused"; a.reason = f"the plan check could not run: {type(e).__name__}: {e}"
    if plan.get("look") and a.status == "ok":       # recipe look_at_room: the chosen rooms are cropped and read by a vision model
        try:
            from . import look as LK
            a = LK.apply(st, a, question)
        except Exception as e:
            a.status = "refused"; a.reason = f"the room crop could not be read: {type(e).__name__}: {e}"
    a.plan_source = src
    mem_step = []
    if mem:
        used = "pattern reused, no planner model called" if src == "memory" else ("given to the planner as worked examples" if str(src).endswith("+memory") else (mem_note or "not used"))
        mem_step = [{"op": "memory", "text": f"solved-question memory: read as '{mem['pattern']}' (probability {mem['prob']:.2f}, next '{mem['second'][0]}' {mem['second'][1]:.2f}); {used}" + (f" ({mem_note})" if mem_note and src != "memory" and not str(src).endswith("+memory") else ""),
                     "values": [{"similar question": s_["q"], "pattern": s_["pattern"], "similarity": round(s_["sim"], 3)} for s_ in mem["similar"]]}]
        a.memory = {"pattern": mem["pattern"], "prob": round(mem["prob"], 3), "used": "replay" if src == "memory" else ("fewshot" if str(src).endswith("+memory") else "none"), "note": mem_note, "similar": mem["similar"]}
    a.trace = [{"op": "route", "text": note}] + mem_step + [{"op": "plan", "text": "query plan (" + ("rules" if src == "rules" else "pattern from the solved-question memory" if src == "memory" else "planner model, cached") + ")", "plan": plan}] + a.trace
    d = a.to_dict()
    try:
        from . import trust as T
        d["trust"] = T.assess(st, d); d["reasoning"] = T.narrate(st, d)
    except Exception as e:      # the trust label must never break an answer
        d["trust"] = {"level": "single_source", "checks": [], "headline": f"trust checks failed to run: {type(e).__name__}: {e}"}; d["reasoning"] = []
    if d.get("findings") and d.get("status") == "ok":
        nf = sum(1 for f in d["findings"] if f["status"] == "fail"); nn = sum(1 for f in d["findings"] if f["status"] == "not_checked")
        txt = f"{nf} rule(s) failed and {nn} could not be checked from this drawing. Each finding shows the room, the measured value and the clause"
        d["trust"] = {"level": "review" if nf else "single_source", "checks": [{"name": "plan check", "result": "caution" if nf else "info", "text": txt, "ids": d.get("evidence") or []}], "headline": ("Needs review: " if nf else "Not confirmed: ") + txt}
    if d.get("look") and d.get("status") == "ok":   # a model's reading is never 'checked', whatever the geometric checks say
        lk = d["look"]; low = lk.get("confidence") == "low"
        txt = f"this answer was read by a vision model ({lk['model']}) from a crop of the drawing; it was not computed and has no second measurement"
        d["trust"] = {"level": "review" if low else "single_source", "checks": [{"name": "read by a model", "result": "caution" if low else "info", "text": txt + ("; the model said it was unsure" if low else ""), "ids": d.get("evidence") or []}],
                      "headline": ("Needs review: " if low else "Not confirmed: ") + txt}
    d["ms"] = round((time.time() - t0) * 1000, 1); d["store_meta"] = {k: v for k, v in st.meta.items() if k not in ("model_names", "raw")}
    return d
