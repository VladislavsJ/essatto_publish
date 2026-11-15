"""Run the same questions in a fresh process and print fingerprints (hash of value + plan + trace)."""
import json, random, sys
from common import *
from bp.ask import ask
random.seed(3)
Q = load_questions(); T = json.load(open("cache/tricky_questions.json"))
pick = random.sample([q for q in Q if source(q) == "swiss_dwellings"], 120) + random.sample([q for q in Q if source(q) == "ifc_models"], 80) + \
       random.sample(T, 120) + [dict(path=r["path"], question=r["question"]) for r in json.load(open("cache/ifc_bench_bp.json"))][:80]
out = {f"{q['path']}|{q['question']}": ask(f"{ROOT}/{q['path']}", q["question"], allow_llm=True)["fingerprint"] for q in pick}
json.dump(out, open(sys.argv[1], "w"))
