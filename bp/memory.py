"""Solved-question memory: recognise what KIND of question this is, and hand over the way it was solved before.

  question --embedding (small multilingual model, CPU)--> small trained classifier --> pattern + nearest stored questions

A pattern (memory/patterns.json) is a way to solve a kind of question: the recipe or plan to run, the steps in words,
and the tools used. The bank (memory/questions_train.jsonl) holds questions in five languages, each tagged with its
pattern; they were written by a small model (memory/generate_questions.py). Nothing in the bank is an answer.

Two uses, both shown in the trace:
  replay    the classifier is confident AND every slot of the pattern can be filled from the question and the
            drawing (the room named in the question exists on the drawing; a height or percentage is in the text)
            -> the pattern is run directly. No planner model is called.
  few-shot  otherwise the nearest stored questions and their patterns are put into the planner's prompt as worked
            examples, so the planner starts from a solution that worked for a similar question.

Files
  cache/embed/model_quantized.onnx, tokenizer.json   multilingual-e5-small (downloaded once by memory/fetch_model.sh)
  memory/classifier.npz                              softmax layer on the 384-d embedding (python -m bp.memory train)"""
import os, re, json, hashlib, threading
import numpy as np

HERE = os.path.dirname(__file__); ROOT = os.path.join(HERE, "..")
MEM = os.path.join(ROOT, "memory"); EMB = os.path.join(ROOT, "cache", "embed"); CLF = os.path.join(MEM, "classifier.npz")
REPLAY_MIN = float(os.environ.get("BP_MEMORY_REPLAY_MIN", "0.70"))      # classifier probability needed to run a pattern without the planner
_LOCK = threading.Lock(); _S = {}

def available():
    # switched off unless BP_MEMORY=1: kept in the code, to be evaluated before it is used in the demo
    return bool(os.environ.get("BP_MEMORY")) and os.path.exists(os.path.join(EMB, "model_quantized.onnx")) and os.path.exists(CLF)

def _load():
    with _LOCK:
        if _S: return _S
        import onnxruntime as ort
        from tokenizers import Tokenizer
        tok = Tokenizer.from_file(os.path.join(EMB, "tokenizer.json")); tok.enable_truncation(96); tok.enable_padding(pad_id=1, pad_token="<pad>")
        so = ort.SessionOptions(); so.intra_op_num_threads = 4; so.inter_op_num_threads = 1
        sess = ort.InferenceSession(os.path.join(EMB, "model_quantized.onnx"), so, providers=["CPUExecutionProvider"])
        _S.update(tok=tok, sess=sess, names=[i.name for i in sess.get_inputs()], patterns={p["id"]: p for p in json.load(open(os.path.join(MEM, "patterns.json")))})
        if os.path.exists(CLF):
            z = np.load(CLF, allow_pickle=False); _S.update(W=z["W"], b=z["b"], mu=z["mu"], classes=[str(c) for c in z["classes"]], bankV=z["bankV"], bankY=z["bankY"], bankQ=[str(q) for q in z["bankQ"]])
        return _S

def embed(texts, batch=64):
    S = _load(); out = []
    for i in range(0, len(texts), batch):
        e = S["tok"].encode_batch(["query: " + t for t in texts[i:i + batch]])
        ids = np.array([x.ids for x in e], np.int64); m = np.array([x.attention_mask for x in e], np.int64)
        feed = {"input_ids": ids, "attention_mask": m}
        if "token_type_ids" in S["names"]: feed["token_type_ids"] = np.zeros_like(ids)
        h = S["sess"].run(None, feed)[0]; v = (h * m[..., None]).sum(1) / m.sum(1, keepdims=True)
        out.append(v / np.linalg.norm(v, axis=1, keepdims=True))
    return np.concatenate(out).astype(np.float32)

def _softmax(z): z = z - z.max(1, keepdims=True); e = np.exp(z); return e / e.sum(1, keepdims=True)

def train(epochs=400, lr=0.5, l2=2e-4, seed=0):
    """fit the softmax layer on the training bank, report accuracy on the separately written test bank"""
    rows = [json.loads(l) for l in open(os.path.join(MEM, "questions_train.jsonl"))]; test = [json.loads(l) for l in open(os.path.join(MEM, "questions_test.jsonl"))]
    classes = [p["id"] for p in json.load(open(os.path.join(MEM, "patterns.json")))]; ci = {c: i for i, c in enumerate(classes)}
    X = embed([r["q"] for r in rows]); y = np.array([ci[r["pattern"]] for r in rows]); Xt = embed([r["q"] for r in test]); yt = np.array([ci[r["pattern"]] for r in test])
    mu = X.mean(0, keepdims=True); A = (X - mu) * 8.0; At = (Xt - mu) * 8.0          # centring removes what all questions share; that is what made raw similarity useless
    rng = np.random.RandomState(seed); W = rng.normal(0, 0.01, (A.shape[1], len(classes))).astype(np.float32); b = np.zeros(len(classes), np.float32); Y = np.eye(len(classes), dtype=np.float32)[y]
    for _ in range(epochs):
        P = _softmax(A @ W + b); G = (P - Y) / len(A); W -= lr * (A.T @ G + l2 * W); b -= lr * G.sum(0)
    Pt = _softmax(At @ W + b); pred = Pt.argmax(1); acc = float((pred == yt).mean()); conf = Pt.max(1)
    by_lang = {}
    for r, ok in zip(test, pred == yt): by_lang.setdefault(r["lang"], []).append(bool(ok))
    # nearest-neighbour baseline on the raw embedding, for comparison
    nn = (Xt @ X.T).argmax(1); acc_nn = float((y[nn] == yt).mean())
    hi = conf >= REPLAY_MIN
    rep = {"train_questions": len(rows), "test_questions": len(test), "classes": len(classes), "test_accuracy": round(acc, 4), "nearest_neighbour_baseline": round(acc_nn, 4),
           "by_language": {k: round(sum(v) / len(v), 3) for k, v in sorted(by_lang.items())},
           "confident_share": round(float(hi.mean()), 3), "accuracy_when_confident": round(float((pred[hi] == yt[hi]).mean()), 4) if hi.any() else None, "replay_threshold": REPLAY_MIN,
           "confusions": sorted([[classes[a], classes[p_], int(n)] for (a, p_), n in __import__("collections").Counter(zip(yt[pred != yt].tolist(), pred[pred != yt].tolist())).items()], key=lambda v: -v[2])[:12]}
    np.savez(CLF, W=W, b=b, mu=mu, classes=np.array(classes), bankV=X, bankY=y, bankQ=np.array([r["q"] for r in rows]))
    json.dump(rep, open(os.path.join(MEM, "classifier_report.json"), "w"), indent=1); _S.clear(); return rep

def recall(question, k=3):
    """-> {pattern, prob, second, similar: [{q, pattern, sim}]} or None when the memory is not installed"""
    if not available(): return None
    S = _load(); v = embed([question]); P = _softmax(((v - S["mu"]) * 8.0) @ S["W"] + S["b"])[0]; order = P.argsort()[::-1]
    sims = (S["bankV"] @ v[0]); top = sims.argsort()[::-1][:40]
    same = [i for i in top if S["classes"][S["bankY"][i]] == S["classes"][order[0]]][:k]
    return {"pattern": S["classes"][order[0]], "prob": float(P[order[0]]), "second": (S["classes"][order[1]], float(P[order[1]])),
            "similar": [{"q": S["bankQ"][i], "pattern": S["classes"][S["bankY"][i]], "sim": float(sims[i])} for i in same]}

# ---- slots: what the pattern needs from this question and this drawing
def _room_filter(question, store):
    ql = " " + re.sub(r"[^\w\s]", " ", question.lower()) + " "; best = None
    for e in store.of("room"):
        n = (e.get("name") or "").strip()
        if len(n) < 3: continue
        for cand in {n.lower(), n.lower().rstrip("s")}:
            if f" {cand} " in ql or f" {cand}s " in ql:
                if best is None or len(cand) > len(best): best = n if cand == n.lower() else n
    if best is None:      # a word of the question that is a whole word of exactly one distinct room name ("library" in "Library")
        words = [w for w in ql.split() if len(w) > 3]; hits = {}
        for e in store.of("room"):
            n = (e.get("name") or "").lower()
            for w in words:
                if re.search(rf"\b{re.escape(w)}\b", n): hits.setdefault(w, set()).add(n)
        w = [w for w in hits if w not in ("room", "rooms", "this", "plan", "floor", "need", "many", "much", "what", "have", "does", "with")]
        if len(w) == 1: best = w[0]
    return {"field": "name", "op": "contains", "value": best} if best else None

def fill(pattern_id, question, store):
    """template -> a concrete recipe call or plan, or (None, why) when a slot cannot be filled from the question and the drawing"""
    S = _load(); p = S["patterns"].get(pattern_id); t = p and p.get("template")
    if not t: return None, "this kind of question has no stored pattern"
    out = json.loads(json.dumps(t))
    if out.get("filters") == ["{room}"]:
        f = _room_filter(question, store)
        if not f: return None, "the room named in the question was not found among the room names on this drawing"
        out["filters"] = [f]
    a = out.get("args") or {}
    if a.get("waste_percent") == "{percent?}":
        m = re.search(r"(\d{1,2}(?:[.,]\d)?)\s*(?:%|percent|prosent|prozent|procent)", question.lower()); a.pop("waste_percent")
        if m: a["waste_percent"] = float(m.group(1).replace(",", "."))
    if a.get("height_m") == "{height}":
        m = re.search(r"(\d(?:[.,]\d{1,2})?)\s*(?:m\b|metr|meter|metre)", question.lower())
        if not m: return None, "the ceiling height is not in the question"
        a["height_m"] = float(m.group(1).replace(",", "."))
    if "args" in out: out["args"] = a
    return out, None

def fewshot_block(mem):
    """worked examples for the planner prompt"""
    if not mem or not mem.get("similar"): return ""
    S = _load(); p = S["patterns"].get(mem["pattern"]) or {}
    lines = [f"SIMILAR SOLVED QUESTIONS (from the solved-question memory; the classifier reads this question as '{mem['pattern']}' with probability {mem['prob']:.2f}):"]
    for s in mem["similar"]: lines.append(f" - \"{s['q']}\"  -> pattern {s['pattern']}")
    if p.get("template"): lines += [f"How that pattern is solved: {'; '.join(p['how'])}.", f"Its call (fill {{room}} with a filter on a room name of THIS drawing): {json.dumps(p['template'])}",
                                    "Use it if this question really asks the same thing; otherwise ignore it."]
    return "\n".join(lines) + "\n"

if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["train"]: print(json.dumps(train(), indent=1))
    else:
        for q in sys.argv[1:]: print(q, "->", json.dumps(recall(q), ensure_ascii=False)[:400])
