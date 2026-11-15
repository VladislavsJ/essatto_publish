import sys, collections as C
from common import *
def correct(q, pred):
    a = q["answer"]
    if pred is None: return False
    if q["type"] == "count": return float(pred) == float(a)
    if q["type"] in ("area", "length", "volume"):
        try: return abs(float(pred) - float(a)) <= 0.02 * abs(float(a))
        except: return False
    return str(pred).strip().lower() == str(a).strip().lower()
def run(src, solver, verbose=0, only_ext=None):
    Q = [q for q in load_questions() if source(q) == src and (only_ext is None or q["path"].endswith(only_ext))]
    ok = C.Counter(); tot = C.Counter(); bad = []
    for q in Q:
        try: pred, how = solver(q)[:2]
        except Exception as e: pred, how = None, f"ERR {type(e).__name__}: {e}"
        k = (q["type"], how if isinstance(how, str) and not how.startswith("ERR") else "err"); tot[k] += 1
        if correct(q, pred): ok[k] += 1
        else: bad.append((q, pred, how))
    for k in sorted(tot): print(f"{k[0]:6s} {k[1]:18s} {ok[k]:4d}/{tot[k]:4d}  {100*ok[k]/tot[k]:5.1f}%")
    print("TOTAL", sum(ok.values()), "/", sum(tot.values()), f"{100*sum(ok.values())/max(1,sum(tot.values())):.1f}%")
    for q, p, h in bad[:verbose]: print("  ✗", q["path"].split("/")[-1][:30], "|", q["question"][:70], "| gt", q["answer"], "pred", p, h)
    return bad
if __name__ == "__main__":
    import swiss
    run("swiss_dwellings", swiss.answer, verbose=int(sys.argv[1]) if len(sys.argv) > 1 else 0, only_ext=".pdf")
