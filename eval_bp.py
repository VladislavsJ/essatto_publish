"""Score the bp engine on the dataset questions (counts exact, measures +-2%). Usage: eval_bp.py <source> [limit] [--nollm]"""
import sys, json, random, collections as C, time
from common import *
import evaluate
from bp.ask import ask
src = sys.argv[1]; lim = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None; nollm = "--nollm" in sys.argv
Q = [q for q in load_questions() if source(q) == src]
if lim: random.seed(0); Q = random.sample(Q, min(lim, len(Q)))
ok = C.Counter(); tot = C.Counter(); bad = []; t0 = time.time(); srcs = C.Counter()
for q in Q:
    a = ask(f"{ROOT}/{q['path']}", q["question"], allow_llm=not nollm)
    k = (q["type"], a["status"]); tot[k] += 1; srcs[a.get("plan_source")] += 1
    if a["status"] == "ok" and evaluate.correct(q, a["value"]): ok[k] += 1
    else: bad.append((q, a))
print(f"{src}: {sum(ok.values())}/{len(Q)} = {100*sum(ok.values())/len(Q):.2f}%  ({time.time()-t0:.0f}s)  plan sources {dict(srcs)}")
for k in sorted(tot): print(f"  {k}: {ok[k]}/{tot[k]}")
json.dump([{"q": q, "a": a} for q, a in bad], open(f"cache/bad_{src}.json", "w"), default=str)
if not lim:
    json.dump({"source": src, "n": len(Q), "ok": sum(ok.values()), "by_type": {f"{k[0]}|{k[1]}": [ok[k], tot[k]] for k in tot},
               "plan_sources": dict(srcs), "failures": [{"path": q["path"], "question": q["question"], "reference": q["answer"], "value": a.get("value"),
               "status": a["status"], "reason": a.get("reason")} for q, a in bad[:400]]}, open(f"cache/summary_{src}.json", "w"), default=str, ensure_ascii=False)
for q, a in bad[:int(next((x[3:] for x in sys.argv if x.startswith("-v=")), "12"))]:
    print("  ✗", q["path"].split("/")[-1][:28], "|", q["question"][:80], "| gt", q["answer"], "| got", a.get("value"), a["status"], (a.get("reason") or "")[:90])
