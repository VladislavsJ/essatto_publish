"""Score bp on the generated tricky questions. Usage: eval_tricky.py [--llm] [--cat=<substr>]"""
import json, sys, collections as C, re
from common import ROOT
from bp.ask import ask
T = json.load(open("cache/tricky_questions.json")); use_llm = "--llm" in sys.argv
cat = next((a[6:] for a in sys.argv if a.startswith("--cat=")), None)
if cat: T = [t for t in T if cat in t["category"]]
def ok(t, a):
    if t["type"] == "refuse": return a["status"] == "refused"
    if a["status"] != "ok": return False
    v = a["value"]
    if t["type"] == "bool": return str(v).lower() == t["answer"]
    if t["type"] == "count": return isinstance(v, (int, float)) and v == t["answer"]
    try: return abs(float(v) - t["answer"]) <= t["tol"] * abs(t["answer"]) + 1e-9
    except Exception: return False
res = C.defaultdict(lambda: [0, 0]); src = C.Counter(); fails = C.defaultdict(list); rows = []
for t in T:
    a = ask(f"{ROOT}/{t['path']}", t["question"], allow_llm=use_llm); g = ok(t, a)
    res[t["category"]][0] += g; res[t["category"]][1] += 1; src[a.get("plan_source")] += 1
    rows.append(dict(t, got=a.get("value"), status=a["status"], ok=g, plan_source=a.get("plan_source"), reason=a.get("reason"), interpretation=a.get("interpretation")))
    if not g: fails[t["category"]].append((t["path"].split("/")[-1][:24], t["question"][:60], t["answer"], a.get("value"), a["status"], (a.get("reason") or "")[:70]))
tot = sum(v[0] for v in res.values()); n = sum(v[1] for v in res.values())
print(f"TRICKY: {tot}/{n} = {100*tot/n:.1f}%   plan sources {dict(src)}")
for k, (a, b) in sorted(res.items(), key=lambda x: x[1][0] / x[1][1]): print(f"  {k:26s} {a:4d}/{b:<4d} {100*a/b:5.1f}%")
json.dump(rows, open("cache/tricky_results" + ("_llm" if use_llm else "") + ".json", "w"), indent=0, default=str, ensure_ascii=False)
for k, v in fails.items():
    for f in v[:2]: print("  ✗", k, "|", f)
