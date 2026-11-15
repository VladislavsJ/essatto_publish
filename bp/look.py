"""Room crop -> vision model (recipe 'look_at_room').

Used only when the answer is visible on the drawing but is not in the database (furniture, notes inside a room, layout).
Code locates the room(s), cuts them from the ORIGINAL image at full resolution and sends just those crops with the
question. Two levels: a cheap model first; a stronger one only when the first is unsure or returns nothing usable.

The answer is text read by a model. It is labelled as such, shown with the crop, never marked as checked, and cached
by (crop bytes, question, model) so the same file and question always give the same answer.

 BP_LOOK_L1 / BP_LOOK_L2   model names. Defaults: Gemini 3.8 Flash -> Gemini 4 Carbon when a Gemini key is configured
                           (the server), otherwise Claude Haiku -> gpt-6.1-sol through their command-line logins (laptop).
 A name starting with 'gemini' uses the Gemini API, 'gpt' uses `codex exec`, anything else `claude -p`."""
import os, io, re, json, time, base64, hashlib, subprocess, threading
from PIL import Image

HERE = os.path.dirname(__file__)
DIR = os.path.join(HERE, "..", "cache", "look"); CACHE = os.path.join(DIR, "answers.json"); _LOCK = threading.Lock()
MAX_ROOMS = 4; MAX_SIDE = 3200

def _gemini_key():
    k = os.environ.get("GEMINI_API_KEY")
    if k: return k
    try: return open(os.path.expanduser("~/.config/bp_gemini_key")).read().strip()
    except Exception: return None

def models():
    g = bool(_gemini_key())
    return (os.environ.get("BP_LOOK_L1") or ("gemini-3.8-flash" if g else "haiku"), os.environ.get("BP_LOOK_L2") or ("gemini-4-carbon" if g else "gpt-6.1-sol"))

def crop_rooms(st, rooms):
    """one PNG per room: its bounding box plus a margin, at the original resolution (reduced only above MAX_SIDE)"""
    os.makedirs(DIR, exist_ok=True); Image.MAX_IMAGE_PIXELS = None
    im = Image.open(st.path).convert("RGB"); W, H = im.size; out = []
    for r in rooms:
        xs = [p[0] for p in r.geom]; ys = [p[1] for p in r.geom]; m = 0.06 * max(max(xs) - min(xs), max(ys) - min(ys)) + 40
        box = (int(max(0, min(xs) - m)), int(max(0, min(ys) - m)), int(min(W, max(xs) + m)), int(min(H, max(ys) + m)))
        c = im.crop(box)
        if max(c.size) > MAX_SIDE: k = MAX_SIDE / max(c.size); c = c.resize((int(c.width * k), int(c.height * k)), Image.LANCZOS)
        b = io.BytesIO(); c.save(b, "PNG"); data = b.getvalue(); f = os.path.join(DIR, hashlib.sha1(data).hexdigest()[:16] + ".png")
        if not os.path.exists(f): open(f, "wb").write(data)
        out.append({"room": r.id, "name": r.get("name") or r.get("label"), "box": list(box), "file": os.path.abspath(f), "bytes": data})
    return out

def _prompt(question, crops, files=True):
    names = ", ".join(f"'{c['name']}'" for c in crops)
    return ("You are shown full-resolution crop(s) of one architectural floor plan: the room(s) " + names + ". Thick black strokes are walls; thin lines are "
            "furniture, fixtures, door swings and notes.\n" + ("Crop files: " + ", ".join(c["file"] for c in crops) + "\n" if files else "") +
            f"Question: {question}\n"
            "Answer ONLY from what is drawn or written inside these crops. Do not estimate areas or lengths. If the crops do not show it, say so.\n"
            'Reply with JSON only: {"found": true or false (false when the crops do not show what is asked), "answer": "short direct answer", '
            '"seen": "one sentence: what in the crop supports it", "confidence": "high" or "low"}')

def _parse(txt):
    m = re.search(r"\{.*\}", txt or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else None
        return d if isinstance(d, dict) and d.get("answer") else None
    except Exception: return None

def _call(model, question, crops, timeout=180):
    t = time.time()
    try:
        if model.startswith("gemini"):
            import urllib.request
            parts = [{"inline_data": {"mime_type": "image/png", "data": base64.b64encode(c["bytes"]).decode()}} for c in crops] + [{"text": _prompt(question, crops, files=False)}]
            body = {"contents": [{"parts": parts}], "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
            req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", data=json.dumps(body).encode(),
                                         headers={"content-type": "application/json", "x-goog-api-key": _gemini_key()})
            r = json.load(urllib.request.urlopen(req, timeout=timeout)); txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
            try:      # the picture and the question are paid for too: same cost log as the planner (cache/llm_cost.jsonl)
                u = r.get("usageMetadata", {}) or {}
                with _LOCK: open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache", "llm_cost.jsonl"), "a").write(json.dumps({"model": model, "usd": None, "in": u.get("promptTokenCount"),
                    "out": (u.get("candidatesTokenCount") or 0) + (u.get("thoughtsTokenCount") or 0), "cache_read": u.get("cachedContentTokenCount"), "cache_write": None, "ms": round((time.time() - t) * 1000), "q": question[:80], "use": "look"}) + "\n")
            except Exception: pass
        elif model.startswith("gpt"):
            r = subprocess.run(["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "-m", model, "-i", ",".join(c["file"] for c in crops)], input=_prompt(question, crops), capture_output=True, text=True, timeout=timeout)
            txt = r.stdout
        else:
            r = subprocess.run(["claude", "-p", "--model", model, "--allowedTools", "Read", "--output-format", "json", _prompt(question, crops)], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            txt = json.loads(r.stdout).get("result")
        return _parse(txt), round(time.time() - t, 1), None
    except Exception as e:
        return None, round(time.time() - t, 1), f"{type(e).__name__}: {str(e)[:120]}"

def _cached(key):
    try: return json.load(open(CACHE)).get(key)
    except Exception: return None

def _store(key, val):
    with _LOCK:
        try: cur = json.load(open(CACHE))
        except Exception: cur = {}
        cur[key] = val; os.makedirs(DIR, exist_ok=True); tmp = CACHE + f".{os.getpid()}.tmp"; json.dump(cur, open(tmp, "w"), indent=1, ensure_ascii=False); os.replace(tmp, CACHE)

def apply(st, a, question):
    """a = executed 'look' plan (its evidence = the chosen rooms). Fills a.value with the model's reading, or refuses."""
    rooms = [e for e in st.of("room") if e.id in set(a.evidence) and e.geom and len(e.geom) >= 3]
    if not rooms: a.status = "refused"; a.value = None; a.reason = "no located room matched the question, so there is nothing to crop"; return a
    if len(rooms) > MAX_ROOMS:
        a.status = "refused"; a.value = None; a.reason = f"{len(rooms)} rooms matched; a crop is sent for at most {MAX_ROOMS}. Name the room(s) you mean"; return a
    crops = crop_rooms(st, rooms); l1, l2 = models(); used = None; res = None; steps = []
    for level, model in ((1, l1), (2, l2)):
        if model in ("none", "", None): continue
        key = hashlib.sha1(b"v2" + b"".join(c["bytes"] for c in crops) + question.encode() + model.encode()).hexdigest()
        hit = _cached(key)
        if hit is not None: r, secs, err, src = hit, 0, None, "cached"
        elif os.environ.get("BP_NO_LLM"): r, secs, err, src = None, 0, "model calls are disabled (BP_NO_LLM)", "off"
        else:
            r, secs, err = _call(model, question, crops); src = "called"
            if r is not None: _store(key, r)
        steps.append({"level": level, "model": model, "source": src, "seconds": secs, "error": err, "confidence": (r or {}).get("confidence")})
        if r is not None: res, used = r, (level, model)
        if r is not None and r.get("confidence") != "low": break
    a.trace.append({"op": "look", "text": f"cut {len(crops)} room crop(s) from the drawing at full resolution and asked a vision model: " +
                    "; ".join(f"level {s['level']} {s['model']} ({s['source']}" + (f", {s['error']}" if s["error"] else "") + ")" for s in steps),
                    "ids": [r.id for r in rooms], "values": [{"id": c["room"], "label": c["name"], "crop_box_px": c["box"]} for c in crops]})
    if res is None:
        a.status = "refused"; a.reason = "the vision model returned nothing usable for this crop" + (f" ({steps[-1]['error']})" if steps and steps[-1]["error"] else ""); return a
    if res.get("found") is False:      # the model looked and it is not there: that is a refusal, with the room still outlined
        a.status = "refused"; a.value = None; a.reason = f"not shown on the drawing: {used[1]} looked at a crop of {', '.join(str(c['name']) for c in crops)} and reports: {res['answer']}"
        a.look = {"model": used[1], "level": used[0], "confidence": res.get("confidence"), "seen": res.get("seen"), "found": False}; return a
    a.value = str(res["answer"]); a.unit = None
    a.interpretation.append(f"read by {used[1]} (level {used[0]}) from a crop of {', '.join(str(c['name']) for c in crops)}: {res.get('seen') or ''}".strip())
    a.look = {"model": used[1], "level": used[0], "confidence": res.get("confidence"), "seen": res.get("seen"), "crops": [{"room": c["room"], "box": c["box"]} for c in crops]}
    return a
