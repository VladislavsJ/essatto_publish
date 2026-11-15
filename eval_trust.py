"""Calibration of the trust label: does 'checked' mean right, and are wrong answers flagged 'review'?
Usage: eval_trust.py <source> [limit]   -> cache/trust_<source>.json"""
import sys, json, random, os, collections as C, time
from common import *
import evaluate
from bp.ask import ask
src = sys.argv[1]; lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
Q = [q for q in load_questions() if source(q) == src]
if src == "cubicasa5k":
    have = {f[:-5] for f in os.listdir("cache/detections")}; Q = [q for q in Q if os.path.basename(q["path"]) in have]
if lim: random.seed(0); Q = random.sample(Q, min(lim, len(Q)))
lv = C.defaultdict(lambda: [0, 0]); ck = C.defaultdict(lambda: [0, 0]); ex = C.defaultdict(list); t0 = time.time()
for q in Q:
    a = ask(f"{ROOT}/{q['path']}", q["question"], allow_llm=True)
    ok = a["status"] == "ok" and evaluate.correct(q, a["value"]); t = a.get("trust", {}); L = t.get("level", "none")
    lv[L][0] += ok; lv[L][1] += 1
    for c in t.get("checks", []):
        if c["result"] in ("fail", "caution", "pass"): k = f"{c['result']}: {c['name']}"; ck[k][0] += ok; ck[k][1] += 1
    key = f"{L}|{'right' if ok else 'wrong'}"
    if len(ex[key]) < 40 and (not ok or L == "review"): ex[key].append({"path": q["path"], "question": q["question"], "reference": q["answer"], "value": a.get("value"), "status": a["status"], "headline": t.get("headline")})
n = len(Q); wrong = sum(b - a for a, b in lv.values()); answered_wrong = sum(b - a for k, (a, b) in lv.items() if k != "none")
out = {"source": src, "n": n, "levels": {k: v for k, v in lv.items()}, "checks": dict(ck), "examples": dict(ex),
       "wrong": wrong, "wrong_flagged": lv["review"][1] - lv["review"][0] + lv["none"][1] - lv["none"][0], "silent_wrong": answered_wrong - (lv["review"][1] - lv["review"][0])}
json.dump(out, open(f"cache/trust_{src}.json", "w"), ensure_ascii=False, default=str)
print(f"{src}: n={n}  ({time.time()-t0:.0f}s)")
for k, (a, b) in sorted(lv.items()): print(f"  {k:14s} {b:6d} answers ({100*b/n:5.1f}%)   right {a:6d} = {100*a/max(b,1):6.2f}%   wrong {b-a}")
print(f"  wrong answers: {wrong}; flagged (review or refused): {out['wrong_flagged']}; silent: {out['silent_wrong']}")
for k, (a, b) in sorted(ck.items()): print(f"    {k:40s} n={b:6d} right={100*a/b:6.2f}%")
