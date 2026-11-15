import json, re, concurrent.futures as F, collections as C
from common import *
from bp.ask import ask, store_for
import re
def _num(x):
    m = re.search(r"-?\d[\d,]*\.?\d*", str(x).replace(",", ""))
    return float(m.group()) if m else None
def _norm(x): return re.sub(r"[^a-z0-9/.\"']+", "", str(x).lower())
def od_correct(q, ans):
    """opening_design: counts exact, areas +-2%, free text = lenient match (normalised containment / same numbers)."""
    if ans is None: return False
    a = q["answer"]
    if q["type"] in ("count", "area") and isinstance(a, (int, float)):
        n = _num(ans)
        if n is None: return False
        return n == a if q["type"] == "count" else abs(n - a) <= 0.02 * abs(a)
    ga, pa = _norm(a), _norm(ans)
    if ga == pa or ga in pa or pa in ga: return True
    gn = re.findall(r"\d+(?:\.\d+)?", str(a).replace(",", "")); pn = re.findall(r"\d+(?:\.\d+)?", str(ans).replace(",", ""))
    return bool(gn) and gn == pn

def _collapse_same(val):
    if isinstance(val, str):
        val = re.sub(r"\s+m[²³23]$", "", val.strip())
        if "; " in val:
            ps = [p.strip() for p in val.split("; ") if p.strip()]
            if len(ps) > 1 and len(set(ps)) == 1: return ps[0]
    return val

def _format_list_rows(rows, sub_plan, q_text):
    if not rows: return None
    flds = [k for k in (sub_plan.get("fields") or list(rows[0].keys())) if k not in ("id", "label", "number") and not k.endswith("_rule")]
    if sub_plan.get("distinct") and "count" in flds and "count" not in (sub_plan.get("fields") or []):
        flds = [k for k in flds if k != "count"]
    eq_fields = {f.get("field") for f in (sub_plan.get("filters") or []) if f.get("op") == "eq"}
    ql = q_text.lower()
    if re.search(r"\bhow many\b", ql) and "count" in flds and not sub_plan.get("distinct"):
        target = ["count"]
    else:
        non_key = [k for k in flds if k not in eq_fields and not re.search(r"^(?:room_name|room_number|door_number|type_mark|mark)$", k)]
        named = [k for k in non_key if any(tok in ql for tok in k.split("_") if len(tok) >= 4)]
        target = named or non_key or flds
    seen, lines = set(), []
    for r in rows:
        vals = [str(r[k]) for k in target if r.get(k) is not None and str(r[k]).strip() != ""]
        s = ", ".join(vals)
        if s and s not in seen: seen.add(s); lines.append(s)
    return "; ".join(lines) if lines else None

def format_answer(q_text, a):
    v = a.get("value")
    plan = a.get("plan") or {}
    # Undo accidental m²->ft² conversion when an imperial PDF schedule cell was already in SF
    if isinstance(v, (int, float)) and plan.get("convert") == "ft2" and (a.get("store_meta") or {}).get("extractor") == "x_pdf":
        v = round(v / 10.7639104, 2)
        if abs(v - round(v)) < 1e-2: v = int(round(v))
    ent = a.get("entity")
    if ent and re.search(r"\b(which|what (?:room|space|door|window|wall))\b", q_text, re.I):
        ns = f"{v:g}" if isinstance(v, (int, float)) else str(v)
        return f"{ent.get('label') or ent['id']} ({ns})"
    if plan.get("parts") and isinstance(v, str):
        # If every part in parts is a single 'list' op, format from trace rows directly
        if len(plan["parts"]) == 1 and plan["parts"][0].get("op") == "list":
            list_step = next((s for s in (a.get("trace") or []) if s.get("op") == "list" and isinstance(s.get("values"), list)), None)
            if list_step:
                res = _format_list_rows(list_step["values"], plan["parts"][0], q_text)
                if res is not None: return res
        lines = v.splitlines()
        if len(lines) == len(plan["parts"]):
            parsed = []
            for i, (p, ln) in enumerate(zip(plan["parts"], lines)):
                lab = p.get("label") or f"part {i + 1}"
                raw = ln[len(lab) + 2:] if ln.startswith(lab + ": ") else ln
                parsed.append((lab, p.get("op"), _collapse_same(raw)))
            wants_breakdown = bool(re.search(r"\b(breakdown|each|list|by type|per type)\b", q_text, re.I))
            scalars = [(lab, op, rv) for lab, op, rv in parsed if op not in ("list", "group_count", "group_sum")]
            chosen = parsed if (wants_breakdown or not scalars) else scalars
            if len(chosen) == 1:
                return chosen[0][2]
            # If question asks for a single 'how many' total and multiple sum parts partition the same 'count' column, combine them
            if re.search(r"\bhow many\b", q_text, re.I) and not re.search(r"\band\b.*\b(what|area|lavator|shower)\b", q_text, re.I) and all(op == "sum" and re.match(r"^\d+(?:\.\d+)?$", str(rv)) for _, op, rv in chosen):
                if all(p.get("attr") == "count" for p in plan["parts"] if p.get("op") == "sum"):
                    tot = sum(float(rv) for _, _, rv in chosen)
                    return int(tot) if tot.is_integer() else round(tot, 3)
            out = []
            for lab, _, rv in chosen:
                lbl = re.sub(r"\b\w*\d\w*\b", "", lab or "")
                lbl = re.sub(r"\s+", " ", lbl).strip(" :_-(),")
                out.append(f"{rv} ({lbl})" if lbl else str(rv))
            return ", ".join(out)
    if isinstance(v, dict) and isinstance(v.get("rows"), list) and v["rows"]:
        res = _format_list_rows(v["rows"], plan, q_text)
        if res is not None: return res
    return _collapse_same(v)

Q = [q for q in load_questions() if source(q) == "opening_design"]
for p in {q["path"] for q in Q}: store_for(f"{ROOT}/{p}")
def one(q):
    a = ask(f"{ROOT}/{q['path']}", q["question"]); return q, a
with F.ThreadPoolExecutor(8) as ex: R = list(ex.map(one, Q))
ok = 0; rows = []
for q, a in R:
    v = format_answer(q["question"], a); g = a["status"] == "ok" and od_correct(q, v if not isinstance(v, dict) else json.dumps(v)); ok += g
    rows.append(dict(question=q["question"], reference=q["answer"], value=v, status=a["status"], ok=g, plan=a.get("plan"), src=a.get("plan_source"), reason=a.get("reason")))
print(f"opening_design: {ok}/{len(Q)}", C.Counter(r["src"] for r in rows), C.Counter(r["status"] for r in rows))
json.dump(rows, open("cache/od_bp.json", "w"), indent=1, default=str, ensure_ascii=False)
for r in [r for r in rows if not r["ok"]][:14]: print(" ✗", r["question"][60:150], "| ref", r["reference"], "| got", str(r["value"])[:60], r["status"], (r["reason"] or "")[:60])
