"""Client chat application server on port 8773.
The web application (FastAPI). Routes:
  - GET  /chat (and /web/chat.html, /chat/<asset>)
  - POST /api/chat/upload
  - POST /api/chat/pages
  - GET  /api/chat/files
  - GET  /api/chat/inspect
  - GET  /api/chat/thumb
  - POST /api/chat/ask        (plain JSON, no streaming)
  - GET  /api/chat/evidence   (PNG evidence overlay for PDF, PNG/JPG, DXF, and IFC)
  - GET  /api/chat/session_log
"""
import os, re, io, json, time, uuid, threading
from email.parser import BytesParser
from email.policy import default as EMAIL_DEFAULT
from PIL import Image, ImageDraw

from fastapi import Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse

from fastapi import FastAPI

app = FastAPI(title="Essatto")
from bp import ask as ASK, plan as PLAN, render as RENDER, trust as TRUST, ingest as INGEST

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.realpath(os.path.join(HERE, "web"))
CHAT_ASSETS_DIR = os.path.realpath(os.path.join(WEB_DIR, "chat"))
COST_LOG_FILE = os.path.realpath(os.path.join(HERE, "cache", "llm_cost.jsonl"))

_ASK_LOCK = threading.Lock()
CRLF = bytes.fromhex("0d0a")
LF_CH = chr(10)


# ---------------------------------------------------------------------------
# Multipart Upload Parser (Python stdlib only — no python-multipart required)
# ---------------------------------------------------------------------------
async def _parse_upload(request: Request) -> tuple[str | None, str | None, bytes, list[int] | None]:
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > INGEST.MAX_UPLOAD_BYTES + 2 * 1024 * 1024:
        raise INGEST.IngestError(
            f"Upload size ({int(cl) / (1024 * 1024):.1f} MB) exceeds the {INGEST.MAX_UPLOAD_BYTES / (1024 * 1024):.0f} MB limit.",
            413,
        )

    raw = await request.body()
    ct = request.headers.get("content-type", "")
    sid = request.query_params.get("session_id") or request.headers.get("x-session-id")
    pages_param = request.query_params.get("pages")
    filename = request.query_params.get("filename") or request.headers.get("x-filename")
    file_bytes = b""

    if "multipart/form-data" in ct.lower():
        hdr_block = b"Content-Type: " + ct.encode("utf-8", "replace") + CRLF + b"MIME-Version: 1.0" + CRLF + CRLF
        msg = BytesParser(policy=EMAIL_DEFAULT).parsebytes(hdr_block + raw)
        if not msg.is_multipart():
            raise INGEST.IngestError("Malformed multipart/form-data request.", 400)
        for part in msg.iter_parts():
            field_name = part.get_param("name", header="content-disposition")
            part_fn = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if part_fn is not None or field_name == "file":
                filename = part_fn or filename or "upload"
                file_bytes = payload
            elif field_name == "session_id":
                sid = payload.decode("utf-8", "replace").strip() or sid
            elif field_name == "pages":
                pages_param = payload.decode("utf-8", "replace").strip() or pages_param
    else:
        file_bytes = raw

    pages = None
    if pages_param:
        try:
            if pages_param.strip().startswith("["):
                pages = [int(x) for x in json.loads(pages_param)]
            else:
                pages = [int(x.strip()) for x in pages_param.split(",") if x.strip().isdigit()]
        except Exception:
            pages = None
    return sid, filename, file_bytes, pages


# ---------------------------------------------------------------------------
# Cost Estimation & Session Log Management
# ---------------------------------------------------------------------------
# Per-million-token USD rates when provider API does not return `usd` directly
MODEL_RATES_PER_M = {
    "haiku": (1.00, 5.00, 0.10, 1.25),
    "gemini-4-carbon": (0.30, 2.50, 0.075, 0.30),
    "flash": (0.30, 2.50, 0.075, 0.30),
    "sonnet": (3.00, 15.00, 0.30, 3.75),
    "opus": (15.00, 75.00, 1.50, 18.75),
}


USD_EUR = float(os.environ.get("BP_USD_EUR", "0.89"))      # ECB reference rate in mid November 2026: 1 EUR = about 1.12 USD


def _model_tier(model_name: str | None) -> str:
    if not model_name:
        return "none"
    m = model_name.lower()
    if any(k in m for k in ("opus", "pro", "gpt-5", "sonnet")):
        return "large"
    return "small"


def _estimate_record_usd(rec: dict) -> float:
    if isinstance(rec.get("usd"), (int, float)):
        return float(rec["usd"])
    m = (rec.get("model") or "").lower()
    rates = MODEL_RATES_PER_M["gemini-4-carbon"]
    for k, r in MODEL_RATES_PER_M.items():
        if k in m:
            rates = r
            break
    in_r, out_r, cr_r, cw_r = rates
    tok_in = int(rec.get("in") or 0)
    tok_out = int(rec.get("out") or 0)
    tok_cr = int(rec.get("cache_read") or 0)
    tok_cw = int(rec.get("cache_write") or 0)
    return (tok_in * in_r + tok_out * out_r + tok_cr * cr_r + tok_cw * cw_r) / 1_000_000.0


def _historical_cold_record(question: str) -> dict | None:
    if not os.path.exists(COST_LOG_FILE):
        return None
    q_pref = question[:80]
    found = None
    try:
        with open(COST_LOG_FILE, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if q_pref[:32] not in line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("q") == q_pref:
                        found = r
                except Exception:
                    pass
    except Exception:
        return None
    return found


def _session_log_path(session_id: str) -> str:
    return INGEST.ensure_inside_sessions(os.path.join(INGEST.session_dir(session_id), "session_log.json"))


def load_session_log(session_id: str) -> dict:
    sid = INGEST.clean_id(session_id, "s")
    lp = _session_log_path(sid)
    if os.path.exists(lp):
        try:
            return json.load(open(lp))
        except Exception:
            pass
    return {
        "session_id": sid,
        "created_at": round(time.time(), 2),
        "totals": {
            "questions": 0,
            "rules_count": 0,
            "small_model_count": 0,
            "large_model_count": 0,
            "in_tokens": 0,
            "out_tokens": 0,
            "usd": 0.0,
        },
        "events": [],
    }


def save_session_log(session_id: str, log_obj: dict):
    lp = _session_log_path(session_id)
    tmp = f"{lp}.{os.getpid()}.tmp"
    json.dump(log_obj, open(tmp, "w"), indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, lp)


def _build_cost_info(question: str, st, ans: dict, new_records: list[dict], session_total_before: float, is_override: bool = False) -> dict:
    ps = str(ans.get("plan_source") or "rules")

    if is_override or ps in ("rules", "memory"):
        route_text = "Computed by code, no AI model" if ps != "memory" else "A similar question was solved before: a small local model recognised it and its pattern was reused. No AI service was called"
        return {
            "route": "code",
            "tier": "none",
            "route_line": route_text,
            "model": None,
            "calls": 0,
            "cached_plan": False,
            "in_tokens": 0,
            "out_tokens": 0,
            "total_tokens": 0,
            "usd": 0.0,
            "session_total_usd": round(session_total_before, 6),
            "display_line": f"{route_text} · 0 tokens · $0.00 · Session total: ${session_total_before:.4f}",
        }

    # Parse `llm:<stage>` or `llm:<stage>-cached`
    stage = ps[4:] if ps.startswith("llm:") else ps
    cached = stage.endswith("-cached")
    if cached:
        stage = stage[:-7]
    repaired = "+repair" in stage
    model_used = stage.replace("+repair", "") if stage != "llm" else (new_records[-1].get("model") if new_records else os.environ.get("BP_PLANNER_MODEL", "small-planner"))
    tier = _model_tier(model_used)

    calls = len(new_records)
    in_tok = sum(int(r.get("in") or 0) + int(r.get("cache_read") or 0) + int(r.get("cache_write") or 0) for r in new_records)
    out_tok = sum(int(r.get("out") or 0) for r in new_records)
    turn_usd = round(sum(_estimate_record_usd(r) for r in new_records), 6)

    # Why was a model needed? Inspect rule parser's `why_not`
    _, rule_why = PLAN.parse(question, st) if st else (None, [])
    why_reason = "; ".join(rule_why[:2]) if rule_why else "the phrasing was outside the deterministic rule grammar"

    if tier == "large":
        if calls > 1:
            why_large = f"the small planner failed validation ({why_reason})"
        else:
            why_large = why_reason
        route_text = f"A large AI model was needed because {why_large}; code computed the answer"
    else:
        if repaired and calls >= 2:
            route_text = "A small AI model read the question (its first reading was rejected by a code check and corrected once); code computed the answer"
        else:
            route_text = "A small AI model read the question; code computed the answer"

    new_session_total = round(session_total_before + turn_usd, 6)

    if cached and calls == 0:
        hist = _historical_cold_record(question)
        cold_in = int(hist.get("in") or 0) if hist else 0
        cold_out = int(hist.get("out") or 0) if hist else 0
        cold_usd = round(_estimate_record_usd(hist), 6) if hist else 0.0
        cold_note = (
            f"reused cached plan from {model_used} (0 new tokens, $0.00; cold call was {cold_in + cold_out:,} tok ≈ ${cold_usd:.4f})"
            if hist
            else f"reused cached plan from {model_used} (0 new tokens, $0.00)"
        )
        return {
            "route": "small_model_cached" if tier == "small" else "large_model_cached",
            "tier": tier,
            "route_line": route_text,
            "model": model_used,
            "calls": 0,
            "cached_plan": True,
            "in_tokens": 0,
            "out_tokens": 0,
            "total_tokens": 0,
            "usd": 0.0,
            "cold_tokens": cold_in + cold_out,
            "cold_est_usd": cold_usd,
            "session_total_usd": new_session_total,
            "display_line": f"{route_text} · {cold_note} · Session total: ${new_session_total:.4f}",
        }

    tot_tok = in_tok + out_tok
    return {
        "route": "small_model" if tier == "small" else "large_model",
        "tier": tier,
        "route_line": route_text,
        "model": model_used,
        "calls": calls,
        "cached_plan": False,
        "in_tokens": in_tok,
        "out_tokens": out_tok,
        "total_tokens": tot_tok,
        "usd": turn_usd,
        "session_total_usd": new_session_total,
        "display_line": (
            f"{route_text} · {model_used}: {in_tok:,} in + {out_tok:,} out ({tot_tok:,} tokens) · "
            f"est. ${turn_usd:.4f} · Session total: ${new_session_total:.4f}"
        ),
    }


# ---------------------------------------------------------------------------
# Coordinate-Exact PDF Text-Gap & IFC Evidence Rendering
# ---------------------------------------------------------------------------
def _render_pdf_evidence_fallback(st, ans: dict, zoom: float = 1.65) -> bytes | None:
    """Render highlighted schedule evidence on vector PDFs even when the table had no ruling lines
    (method == 'text gaps', such as the Door Schedule on A-801) or when 0 rows matched."""
    import pymupdf

    ev_ids = list(ans.get("evidence") or [])
    ev_set = set(ev_ids)
    rows = [e for e in st.entities if e.id in ev_set and e.src.get("bbox")]

    doc = pymupdf.open(st.path)
    if not rows:
        # Render overview of page 1 so the user can still inspect the sheet visually
        pg = doc[0]
        pg.remove_rotation()
        z = min(1.2, 1400 / max(pg.rect.width, pg.rect.height, 1))
        pm = pg.get_pixmap(matrix=pymupdf.Matrix(z, z))
        im = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        return buf.getvalue()

    pno = rows[0].src.get("page", 1)
    rows = [e for e in rows if e.src.get("page", 1) == pno]
    pg = doc[pno - 1]
    pg.remove_rotation()

    # Expand crop to include the entire table containing these rows (plus header above row 1)
    tbl_name = rows[0].get("table")
    same_tbl = [
        e for e in st.entities
        if e.src.get("page", 1) == pno and e.get("table") == tbl_name and e.src.get("bbox")
    ] or rows

    x0 = max(0, min(e.src["bbox"][0] for e in same_tbl) - 48)
    y0 = max(0, min(e.src["bbox"][1] for e in same_tbl) - 58)
    x1 = min(pg.rect.width, max(e.src["bbox"][2] for e in same_tbl) + 45)
    y1 = min(pg.rect.height, max(e.src["bbox"][3] for e in same_tbl) + 32)
    clip = pymupdf.Rect(x0, y0, x1, y1)

    pm = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip)
    im = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
    d = ImageDraw.Draw(im, "RGBA")
    f_num = RENDER.BIG(14)

    T = lambda x, y: ((x - clip.x0) * zoom, (y - clip.y0) * zoom)

    # Identify filter regex / target values so we can outline matching cell words inside each matched row
    plan = ans.get("plan") or {}
    val_patterns = []
    for flt in (plan.get("filters") or []):
        if flt.get("field") == "table":
            continue
        op, fv = flt.get("op"), flt.get("value")
        if op == "regex" and fv:
            try:
                val_patterns.append(re.compile(str(fv), re.I))
            except Exception:
                pass
        elif op in ("eq", "contains") and fv:
            val_patterns.append(re.compile(re.escape(str(fv)), re.I))

    page_words = pg.get_text("words") if val_patterns else []

    for idx, e in enumerate(rows, 1):
        bx0, by0, bx1, by1 = e.src["bbox"]
        # Extend row highlight across table width for crisp readability
        rx0 = min(bx0, x0 + 44)
        rx1 = max(bx1, x1 - 40)
        p_tl = T(rx0, by0 - 1.5)
        p_br = T(rx1, by1 + 1.5)
        d.rectangle([p_tl, p_br], fill=(255, 200, 0, 68), outline=(215, 135, 0, 235), width=2)
        # Numbered badge in left margin
        tx, ty = T(rx0 - 26, (by0 + by1) / 2 - 6)
        d.text((max(4, tx), ty), str(idx), fill=(195, 28, 28, 255), font=f_num, stroke_width=3, stroke_fill=(255, 255, 255))

        if page_words:
            for wx0, wy0, wx1, wy1, wtxt, *_ in page_words:
                if wy1 >= by0 - 2 and wy0 <= by1 + 2 and wx0 >= rx0 - 4 and wx1 <= rx1 + 4:
                    if any(rx.search(wtxt) for rx in val_patterns):
                        d.rectangle([T(wx0 - 2, wy0 - 1), T(wx1 + 2, wy1 + 1)], outline=(215, 25, 25, 255), width=3)

    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_evidence_png(doc_meta: dict, st, ans: dict) -> bytes | None:
    """Unified PNG evidence renderer covering ALL four drawing formats (PDF, PNG/JPG, DXF, IFC)."""
    if st is None:
        return None
    png = RENDER.render(st, ans)
    if png:
        return png
    if st.fmt == "pdf-vector":
        return _render_pdf_evidence_fallback(st, ans)
    if st.fmt == "ifc":
        return INGEST.render_ifc_plan_png(doc_meta["saved_path"], st, evidence_ids=ans.get("evidence") or [], size=1150)
    if st.fmt in ("dxf-layered", "raster"):
        return RENDER.audit_image(st, [], size=1200)
    return None


# ---------------------------------------------------------------------------
# Structured Evidence Items & First-Class Refusal Builder
# ---------------------------------------------------------------------------
def _build_evidence_items(st, ans: dict) -> list[dict]:
    if not st:
        return []
    by_id = {e.id: e for e in st.entities}
    measured = {}
    for s in (ans.get("trace") or []):
        if s.get("op") == "measure" and isinstance(s.get("values"), list):
            for v in s["values"]:
                if isinstance(v, dict) and v.get("id"):
                    measured[v["id"]] = v

    items = []
    for idx, eid in enumerate((ans.get("evidence") or [])[:250], 1):
        e = by_id.get(eid)
        if not e:
            items.append({"n": idx, "id": eid, "label": eid, "kind": "entity", "details": {}})
            continue
        m = measured.get(eid, {})
        label = (
            e.get("door_number")
            or e.get("room_number")
            or e.get("number")
            or e.get("label")
            or e.get("name")
            or eid
        )
        if e.get("room_name") and str(e.get("room_name")) != str(label):
            label = f"{label} · {e.get('room_name')}"
        elif e.get("label") and str(e.get("label")) != str(label):
            label = f"{label} ({e.get('label')})"

        details = {}
        _tcols = _UI_table_cols(st, e) if e.kind == "schedule_row" else ()
        for k in _tcols or ("rtype", "apartment", "level", "area", "printed", "width", "height", "door_type", "matl", "matl_2", "frame_type", "floor_finish", "volume", "length", "type_name"):
            val = m.get(k) if k in m else e.get(k)
            if val is not None and val != "":
                details[k] = val
        if e.src.get("conf") is not None:
            details["detector_conf"] = e.src["conf"]
        items.append({
            "n": idx,
            "id": eid,
            "kind": e.kind,
            "label": str(label),
            "details": details,
            "provenance": m.get("how") or e.src.get("rule") or e.src.get("area_src") or "",
            "borderline": bool(e.get("borderline")),
        })
    return items


def _build_refusal_info(question: str, st, doc_meta: dict, ans: dict) -> dict | None:
    if ans.get("status") == "ok":
        return None
    raw_reason = str(ans.get("reason") or "The drawing does not contain the information needed to compute this answer.")
    if st is None:
        can_ask = "Upload a vector PDF with schedules, a scanned floor plan (PNG/JPG), a DXF plan, or an IFC model."
        sugg = []
    else:
        sugg = INGEST.suggest_questions(st, doc_meta)[:4]
        if st.fmt == "raster":
            kinds = st.kinds()
            sym_list = ", ".join(f"{v} {k}s" for k, v in sorted(kinds.items()) if k not in ("floor", "room_label"))
            can_ask = (
                "This is a scanned plan without a known scale. "
                "You can ask for counts of doors, windows, toilets, sinks, showers or bathtubs, and rooms by type."
            )
        elif st.fmt == "pdf-vector":
            tables = [t["title"] for t in (st.meta.get("tables") or [])]
            can_ask = (
                (f"This sheet has {len(tables)} schedule table(s): {', '.join(tables)}. "
                 "You can ask for counts, totals or single values from any column of those tables.")
                if tables else
                "No schedule table was found in this PDF, and drawn plans inside a PDF are not measured. "
                "Upload the DXF or IFC version of the plan, or a sheet with schedule tables."
            )
        elif st.fmt == "dxf-layered":
            can_ask = "On this CAD plan you can ask for room areas, apartment totals, room counts by type, and counts of doors, windows and fixtures."
        elif st.fmt == "ifc":
            can_ask = "On this building model you can ask for element counts, breakdowns by type or storey, wall and slab volumes and areas, and facts about the model."
        else:
            can_ask = doc_meta.get("understood_summary", "")

    return {
        "what_was_asked": question,
        "why_cannot_answer": raw_reason,
        "what_can_be_asked": can_ask,
        "suggested_questions": sugg,
        "file_limits": list((doc_meta or {}).get("unreadable") or [])[:3],
    }


# ---------------------------------------------------------------------------
# UI kit lane: evidence shaped for display (additive; nothing above depends on it)
#   - matched schedule rows as a table with the printed header and the columns the answer used
#   - where each numbered mark sits on the evidence image (normalised 0..1), so a list row can focus it
#   - the reading of the question in the user's words (printed column names, no internal identifiers)
# The mark positions repeat the coordinate transforms of bp/render.py (which this lane may not edit);
# if that renderer changes, _UI_marks must follow, or better: render.py should return the geometry itself.
# ---------------------------------------------------------------------------
def _UI_table_cols(st, e) -> tuple:
    t = next((t for t in (st.meta.get("tables") or []) if t.get("title") == e.get("table")), None)
    return tuple(t.get("columns") or ()) if t else ()


def _UI_plans(ans: dict) -> list[dict]:
    plan = ans.get("plan") or {}
    return [p for p in (plan.get("parts") or [plan]) if isinstance(p, dict)]


_UI_KIND_WORDS = {"schedule_row": "rows", "washbasin": "sinks", "shower_or_bath": "showers and bathtubs", "furniture": "furniture items",
                  "curtainwall": "curtain walls", "room_label": "room labels", "element": "other elements"}
_UI_FIELD_WORDS = {"rtype": "room type", "apartment": "apartment", "level": "storey", "number": "number", "name": "name", "label": "label",
                   "type_name": "type", "ifc_class": "IFC class", "is_external": "external", "area": "area", "volume": "volume",
                   "length": "length", "width": "width", "height": "height", "perimeter": "perimeter", "side_area": "side area"}
_UI_OP_WORDS = {"eq": "is", "in": "is", "nin": "is not", "match": "is named", "contains": "contains", "regex": "matches",
                "gt": "is above", "lt": "is below", "gte": "is at least", "lte": "is at most", "true": "is set"}


def _UI_col_name(st, key, table=None) -> str:
    key = str(key or "")
    if st is not None and st.fmt == "pdf-vector":
        h = INGEST.column_labels(st, table).get(key) or INGEST.column_labels(st).get(key)
        if h:
            return f"{h['group']} › {h['label']}" if h.get("group") else h["label"]
    return _UI_FIELD_WORDS.get(key, key.replace("props.", "").replace("_", " "))


def _UI_reading_one(st, p: dict) -> str:
    kinds = p.get("kind") if isinstance(p.get("kind"), list) else [p.get("kind")]
    flts = list(p.get("filters") or [])
    table = next((f.get("value") for f in flts if f.get("field") == "table" and f.get("op", "eq") == "eq"), None)
    if kinds == ["schedule_row"]:
        things = f"rows of the table “{table}”" if table else "schedule rows"
    else:
        things = " and ".join(_UI_KIND_WORDS.get(str(k), f"{k}s") for k in kinds)
    attr = _UI_col_name(st, p.get("attr"), table) if p.get("attr") else "value"
    grp = _UI_col_name(st, p.get("group_by"), table) if p.get("group_by") else "type"
    head = {
        "count": f"Count the {things}", "sum": f"Add up “{attr}” of the {things}", "avg": f"Average “{attr}” of the {things}",
        "max": f"Find the largest “{attr}” among the {things}", "min": f"Find the smallest “{attr}” among the {things}",
        "exists": f"Check whether there are any {things}", "get": f"Read “{attr}” from the {things}", "value": f"Read “{attr}” from the {things}",
        "list": f"List the {things}", "group_count": f"Count the {things} per {grp}", "group_sum": f"Add up “{attr}” per {grp} of the {things}",
    }.get(str(p.get("op")), f"{p.get('op')} the {things}")
    conds = []
    for f in flts:
        if f.get("field") == "table" and table is not None:
            continue
        v = f.get("value")
        vtxt = " or ".join(map(str, v)) if isinstance(v, list) else ("" if v is None else str(v))
        conds.append(f"“{_UI_col_name(st, f.get('field'), table)}” {_UI_OP_WORDS.get(f.get('op', 'eq'), f.get('op'))} {vtxt}".strip())
    out = head + (" where " + " and ".join(conds) if conds else "")
    if p.get("convert"):
        out += f", converted to {p['convert']}"
    return out


def _UI_reading(st, plan) -> list[dict]:
    if not isinstance(plan, dict):
        return []
    if plan.get("parts"):
        return [{"label": p.get("label") or f"Part {i + 1}", "text": _UI_reading_one(st, p)} for i, p in enumerate(plan["parts"]) if isinstance(p, dict)]
    return [{"label": None, "text": _UI_reading_one(st, plan)}]


def _UI_marks(st, ans: dict) -> dict:
    """{entity id: {"mark": number drawn on the evidence image or None, "box": [x0, y0, x1, y1] as fractions of the image}}"""
    ev_order = list(dict.fromkeys(ans.get("evidence") or []))
    ev = set(ev_order)
    idx_of = {eid: i + 1 for i, eid in enumerate(ev_order)}
    out = {}
    if not ev:
        return out
    if st.fmt == "raster":
        W, H = Image.open(st.path).size
        r = max(7, max(W, H) // 110)
        n = 0
        for e in st.entities:
            if not e.geom or e.id not in ev:
                continue
            xs = [pt[0] for pt in e.geom]; ys = [pt[1] for pt in e.geom]
            if e.kind == "room_label":
                out[e.id] = {"mark": None, "box": [min(xs) / W, min(ys) / H, max(xs) / W, max(ys) / H]}
            elif len(e.geom) == 4:
                n += 1
                out[e.id] = {"mark": n, "box": [min(xs) / W, min(ys) / H, max(xs) / W, max(ys) / H]}
            elif len(e.geom) == 1:
                n += 1
                out[e.id] = {"mark": n, "box": [(xs[0] - r) / W, (ys[0] - r) / H, (xs[0] + r) / W, (ys[0] + r) / H]}
            elif len(e.geom) >= 3:
                out[e.id] = {"mark": None, "box": [min(xs) / W, min(ys) / H, max(xs) / W, max(ys) / H]}
    elif st.fmt == "dxf-layered":
        size = 1300
        geoms = [e for e in st.entities if e.geom]
        xs = [x for e in geoms for x, _ in e.geom]; ys = [y for e in geoms for _, y in e.geom]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys); pad = 0.8
        sc = (size - 60) / max(x1 - x0 + 2 * pad, y1 - y0 + 2 * pad)
        W = int((x1 - x0 + 2 * pad) * sc) + 60; H = int((y1 - y0 + 2 * pad) * sc) + 60
        n = 0
        for e in st.entities:
            if e.id not in ev or not e.geom:
                continue
            px = [(x - x0 + pad) * sc + 30 for x, _ in e.geom]; py = [H - 30 - (y - y0 + pad) * sc for _, y in e.geom]
            mark = None
            if not (e.kind in ("room", "apartment") and len(e.geom) >= 3):
                n += 1; mark = n
            out[e.id] = {"mark": mark, "box": [min(px) / W, min(py) / H, max(px) / W, max(py) / H]}
    elif st.fmt == "pdf-vector":
        import pymupdf, collections
        rows = [e for e in st.entities if e.id in ev and (e.src.get("page") or 1) and (e.src.get("bbox") or e.geom)]
        if not rows:
            return out
        page = collections.Counter(e.src.get("page", 1) for e in rows).most_common(1)[0][0]
        rows = [e for e in rows if e.src.get("page", 1) == page]
        pg = pymupdf.open(st.path)[page - 1]; pg.remove_rotation()
        need_rules = any(e.src.get("coords") != "unrotated" for e in rows if e.kind == "schedule_row")
        h_rules = RENDER._page_h_rules(pg) if need_rules else []
        tables_used = {e.get("table") for e in rows if e.get("table")}
        same = [e for e in st.entities if e.src.get("page", 1) == page and e.src.get("bbox") and (
            (e.kind == "schedule_row" and e.get("table") in tables_used) or e.id in ev)]
        tbl_span = {}
        for tbl in tables_used:
            tr = [e for e in st.entities if e.kind == "schedule_row" and e.get("table") == tbl and e.src.get("page", 1) == page and e.src.get("bbox")]
            if tr:
                c0 = tr[0].src.get("cols_x")
                tbl_span[tbl] = (min([e.src["bbox"][0] for e in tr] + ([c0[0]] if c0 else [])), max([e.src["bbox"][2] for e in tr] + ([c0[-1]] if c0 else [])))

        def eff(e):
            b = e.src["bbox"]
            if e.kind != "schedule_row" or e.src.get("coords") == "unrotated":
                return tuple(b)
            return RENDER._snap_row_box(h_rules, b, tbl_span.get(e.get("table")))

        boxes = {e.id: eff(e) for e in same}
        bx0 = [b[0] for b in boxes.values()]; by0 = [b[1] for b in boxes.values()]
        bx1 = [b[2] for b in boxes.values()]; by1 = [b[3] for b in boxes.values()]
        for e in rows:
            if e.geom:
                bx0.append(min(q[0] for q in e.geom)); bx1.append(max(q[0] for q in e.geom))
                by0.append(min(q[1] for q in e.geom)); by1.append(max(q[1] for q in e.geom))
        if not bx0:
            return out
        cx0 = max(0, min(bx0) - 36); cy0 = max(0, min(by0) - 108)
        cx1 = min(pg.rect.x1, max(bx1) + 20); cy1 = min(pg.rect.y1, max(by1) + 20)
        z = min(2.0, 2400 / max(cx1 - cx0, cy1 - cy0, 1))
        W = (cx1 - cx0) * z; H = (cy1 - cy0) * z + 32      # + banner strip added by the renderer
        numbered = len(rows) <= 80
        for e in rows:
            if e.id in boxes:
                b = boxes[e.id]
            else:
                gx = [q[0] for q in e.geom]; gy = [q[1] for q in e.geom]; b = (min(gx), min(gy), max(gx), max(gy))
            out[e.id] = {"mark": idx_of.get(e.id) if numbered else None,
                         "box": [(b[0] - cx0) * z / W, ((b[1] - cy0) * z + 32) / H, (b[2] - cx0) * z / W, ((b[3] - cy0) * z + 32) / H]}
    elif st.fmt == "ifc":
        for eid in ev_order:
            out[eid] = {"mark": idx_of[eid], "box": None}
    return out


def _UI_evidence_view(st, ans: dict, items: list[dict], alternatives: list[dict]) -> dict:
    view = {"tables": [], "has_marks": False, "reading": []}
    if st is None:
        return view
    try:
        view["reading"] = _UI_reading(st, ans.get("plan"))
    except Exception:
        pass
    try:
        marks = _UI_marks(st, ans)
    except Exception:
        marks = {}
    for it in items:
        m = marks.get(it["id"]) or {}
        it["mark"] = m.get("mark")
        box = m.get("box")
        it["box"] = [round(max(0.0, min(1.0, float(v))), 5) for v in box] if box else None
    if items and all(it.get("mark") is not None for it in items):
        items.sort(key=lambda it: it["mark"])
    view["has_marks"] = any(it.get("box") for it in items)

    # matched schedule rows, grouped by table, with the printed header and the columns this answer used
    try:
        by_id = {e.id: e for e in st.entities}
        plans = _UI_plans(ans)
        tables = {}
        for it in items:
            e = by_id.get(it["id"])
            if e is None or e.kind != "schedule_row":
                continue
            tables.setdefault(e.get("table"), []).append((it, e))
        for title, pairs in tables.items():
            t = next((t for t in (st.meta.get("tables") or []) if t.get("title") == title), None)
            if not t:
                continue
            used = {}
            for p in plans:
                tf = next((f.get("value") for f in (p.get("filters") or []) if f.get("field") == "table"), None)
                if tf is not None and str(tf).lower() != str(title).lower():
                    continue
                for f in p.get("filters") or []:
                    if f.get("field") != "table":
                        used.setdefault(str(f.get("field")), "Filtered on")
                if p.get("attr"):
                    used[str(p["attr"])] = "Value read" if p.get("op") in ("get", "value", "list") else "Value used"
                if p.get("group_by"):
                    used.setdefault(str(p["group_by"]), "Grouped by")
            header = [dict(h, used=used.get(h["key"])) for h in INGEST.table_header_layout(st, t)]
            view["tables"].append({
                "title": title,
                "page": t.get("orig_page") or t.get("page"),
                "total_rows": t.get("rows"),
                "header": header,
                "rows": [{"mark": it.get("mark"), "id": it["id"], "cells": [e.get(h["key"], "") for h in header]} for it, e in pairs],
            })
    except Exception:
        view["tables"] = []

    # alternatives: say which printed column each reading uses
    try:
        for o in alternatives or []:
            rd = _UI_reading(st, o.get("plan"))
            o["reading"] = rd[0]["text"] if rd else None
            others = [x for x in alternatives if x is not o]
            pf = (o.get("plan") or {}).get("filters") or []
            title = None
            for x in others:
                xf = (x.get("plan") or {}).get("filters") or []
                tbl = next((f.get("value") for f in pf if f.get("field") == "table"), None)
                diff = [a.get("field") for a, b in zip(pf, xf) if a.get("field") != b.get("field")]
                if diff:
                    title = f"Use the column “{_UI_col_name(st, diff[0], tbl)}”"
                elif (o.get("plan") or {}).get("attr") != (x.get("plan") or {}).get("attr") and (o.get("plan") or {}).get("attr"):
                    title = f"Read the column “{_UI_col_name(st, o['plan']['attr'], tbl)}”"
                if title:
                    break
            o["title"] = title or o.get("label")
    except Exception:
        pass
    return view



# ---------------------------------------------------------------------------
# Web UI Routes (/chat, /web/chat.html, /chat/<asset>)
# ---------------------------------------------------------------------------
threading.Thread(target=INGEST.warm, daemon=True).start()      # detector and OCR are loaded while the server starts


@app.get("/chat", response_class=HTMLResponse)
@app.get("/chat.html", response_class=HTMLResponse)
@app.get("/web/chat.html", response_class=HTMLResponse)
def chat_page():
    html_path = os.path.join(WEB_DIR, "chat.html")
    return HTMLResponse(open(html_path, "r", encoding="utf-8").read(), headers={"Cache-Control": "no-cache"})


@app.get("/chat/{asset_name}")
@app.get("/web/chat/{asset_name}")
def chat_asset(asset_name: str):
    if not re.match(r"^[a-zA-Z0-9_.-]+$", asset_name):
        return Response(status_code=404)
    target = os.path.realpath(os.path.join(CHAT_ASSETS_DIR, asset_name))
    if os.path.commonpath([CHAT_ASSETS_DIR, target]) != CHAT_ASSETS_DIR or not os.path.isfile(target):
        return Response(status_code=404)
    media = {"css": "text/css", "js": "application/javascript", "html": "text/html", "png": "image/png", "svg": "image/svg+xml", "ico": "image/x-icon"}.get(asset_name.rsplit(".", 1)[-1], "text/plain")
    return FileResponse(target, media_type=media, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# API Routes (/api/chat/*)
# ---------------------------------------------------------------------------
@app.post("/api/chat/upload")
async def api_chat_upload(request: Request):
    try:
        sid_in, filename, data, pages = await _parse_upload(request)
        sid = INGEST.clean_id(sid_in, "s")
        import asyncio      # the reading runs in a worker thread, so the server can answer the page's progress requests meanwhile
        doc_view = await asyncio.to_thread(INGEST.ingest_upload, sid, filename, data, pages)
        INGEST.PROGRESS.pop(sid, None)
        slog = load_session_log(sid)
        slog["events"].append({
            "type": "upload",
            "ts": round(time.time(), 2),
            "doc_id": doc_view["doc_id"],
            "display_name": doc_view["display_name"],
            "format": doc_view["format"],
            "size_bytes": doc_view["size_bytes"],
            "understood_summary": doc_view["understood_summary"],
            "unreadable": doc_view["unreadable"],
        })
        save_session_log(sid, slog)
        return JSONResponse({
            "ok": True,
            "session_id": sid,
            "document": doc_view,
            "session_totals": slog["totals"],
        })
    except INGEST.IngestError as e:
        return JSONResponse({"ok": False, "error": e.message}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Upload failed: {type(e).__name__}: {e}"}, status_code=500)


@app.post("/api/chat/pages")
async def api_chat_pages(request: Request):
    try:
        body = await request.json()
        sid = INGEST.clean_id(body.get("session_id"), "s")
        did = INGEST.clean_id(body.get("doc_id"), "d")
        pages = [int(p) for p in (body.get("pages") or [1])]
        doc_view = INGEST.update_selected_pages(sid, did, pages)
        return JSONResponse({"ok": True, "session_id": sid, "document": doc_view})
    except INGEST.IngestError as e:
        return JSONResponse({"ok": False, "error": e.message}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Page update failed: {type(e).__name__}: {e}"}, status_code=400)


@app.get("/api/chat/files")
def api_chat_files(session_id: str = Query("")):
    sid = INGEST.clean_id(session_id, "s")
    manifest = INGEST.load_manifest(sid)
    docs = [INGEST.public_doc_view(d) for d in manifest.get("documents", [])]
    slog = load_session_log(sid)
    return JSONResponse({
        "session_id": sid,
        "documents": docs,
        "session_totals": slog["totals"],
        "log_url": f"/api/chat/session_log?session_id={sid}&download=1",
    })


@app.get("/api/chat/thumb")
def api_chat_thumb(session_id: str, doc_id: str, page: int = 1, kind: str = "thumb"):
    try:
        doc_meta = INGEST.get_doc_meta(session_id, doc_id)
        doc_dir = doc_meta["doc_dir"]
        if kind == "audit":
            cand = os.path.join(doc_dir, "audit.png")
            if os.path.isfile(cand):
                return FileResponse(INGEST.ensure_inside_sessions(cand), media_type="image/png")
            kind = "preview"
        if kind == "plain":        # the drawing itself, untouched and at its own resolution; the page draws marks on top of it
            qp = doc_meta.get("query_path") or ""
            if doc_meta.get("format") == "raster" and os.path.isfile(qp):
                return FileResponse(INGEST.ensure_inside_sessions(qp), media_type="image/jpeg" if qp.lower().endswith((".jpg", ".jpeg")) else "image/png",
                                    headers={"Cache-Control": "private, max-age=86400"})
            kind = "preview"
        pno = max(1, min(int(page), max(1, doc_meta.get("page_count", 1))))
        prefix = "preview" if kind == "preview" else "thumb"
        f = os.path.join(doc_dir, f"{prefix}_p{pno}.png")
        if not os.path.isfile(f):
            f = os.path.join(doc_dir, "thumb_p1.png")
        if os.path.isfile(f):
            return FileResponse(INGEST.ensure_inside_sessions(f), media_type="image/png")
        return Response(status_code=204)
    except INGEST.IngestError as e:
        return JSONResponse({"ok": False, "error": e.message}, status_code=e.status_code)


def _overlay_shapes(st, ids=None, values=None, unit=""):
    """Shapes for the page to draw over the plain drawing, as fractions of the image: boxes for detected symbols, outlines for
    traced rooms. ids=None -> everything that was found (the overview). values -> {id: number} written inside the shape, which
    happens only for an answer that measured something (an area question shows m2 in the asked rooms, a count shows none)."""
    if st is None or st.fmt != "raster": return []
    try: W, H = (st.meta.get("image_size") or Image.open(st.path).size)
    except Exception: return []
    r = max(7, max(W, H) // 110); out = []; want = set(ids) if ids is not None else None
    for e in st.entities:
        if not e.geom or e.kind == "floor" or (want is not None and e.id not in want): continue
        if want is None and e.kind == "room_label": continue
        sh = {"id": e.id, "kind": e.kind}
        if e.get("borderline"): sh["uncertain"] = True
        xs = [q[0] for q in e.geom]; ys = [q[1] for q in e.geom]
        if len(e.geom) >= 3 and not (len(e.geom) == 4 and not (e.kind == "room" and e.get("area_rule"))):
            sh["poly"] = [[round(x / W, 5), round(y / H, 5)] for x, y in e.geom]
        elif len(e.geom) == 1:
            sh["box"] = [round((xs[0] - r) / W, 5), round((ys[0] - r) / H, 5), round((xs[0] + r) / W, 5), round((ys[0] + r) / H, 5)]
        else:
            sh["box"] = [round(min(xs) / W, 5), round(min(ys) / H, 5), round(max(xs) / W, 5), round(max(ys) / H, 5)]
        if e.kind == "room" and e.get("rtype"): sh["rtype"] = e.get("rtype")
        if values and e.id in values: sh["text"] = f"{values[e.id]:g} {unit}".strip()
        out.append(sh)
    return out


@app.get("/api/chat/overlay")
def api_chat_overlay(session_id: str, doc_id: str):
    """everything that was found on a scanned drawing, for the page to draw over the plain image (no painted picture is sent)"""
    try:
        doc_meta = INGEST.get_doc_meta(session_id, doc_id)
        st, _ = ASK.store_for(doc_meta["query_path"])
        return JSONResponse({"ok": True, "shapes": _overlay_shapes(st)})
    except INGEST.IngestError as e:
        return JSONResponse({"ok": False, "error": e.message}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


# ---------------------------------------------------------------------------
# Studio page (/chat/studio.html): one drawing on screen, layers, plan check, room regulations
# ---------------------------------------------------------------------------
REG_DIR = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "regulations"))
DEMO_DIR = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo"))
_STUDIO_LOCK = threading.Lock()


def _room_types(name: str) -> list[str]:
    """room name on the drawing -> the room types used in regulations/regulations_fi_us.json"""
    n = (name or "").lower(); t = ["any room", "any room in a school", "door to any room"]
    if "classroom" in n or "group room" in n: t += ["classroom", "any occupied room"]
    if "kitchen" in n or "servery" in n: t += ["kitchen", "any occupied room"]
    if "toilet" in n or "wc" in n or "changing" in n or "hygiene" in n: t += ["toilet"] + (["accessible toilet"] if "accessible" in n or "hygiene" in n else [])
    if "corridor" in n or "lobby" in n or "landing" in n: t += ["corridor"] + (["entrance", "lobby"] if "lobby" in n else [])
    if "stair" in n: t += ["stair", "exit"]
    if "hall" in n: t += ["hall", "any occupied room", "exit"]
    if any(w in n for w in ("office", "workroom", "reception", "meeting", "library", "room")) and "plant" not in n and "store" not in n: t += ["office", "any work space", "staff room", "any occupied room"]
    return list(dict.fromkeys(t))


@app.post("/api/chat/studio/demo")
async def api_studio_demo(request: Request):
    """open the SAMPLE school as one document with two layers (architecture, fire). Each layer is one of the demo/ drawings,
    ingested like an upload; their stored analysis makes this take a couple of seconds."""
    body = await request.json(); sid = INGEST.clean_id(body.get("session_id"), "s")
    try:
        import asyncio
        side = json.load(open(os.path.join(DEMO_DIR, "school_demo.json")))
        def _open_layers():
            with _STUDIO_LOCK:
                man = INGEST.load_manifest(sid); have = {d.get("display_name"): d for d in man.get("documents", [])}
                out = []
                for h_, f_ in side["files"].items():
                    d = have.get(f_["name"])
                    view = INGEST.public_doc_view(d) if d else INGEST.ingest_upload(sid, f_["name"], open(os.path.join(DEMO_DIR, f_["name"]), "rb").read())
                    out.append({"key": f_["layer"], "label": {"architecture": "Architecture", "fire": "Fire system"}.get(f_["layer"], f_["layer"]), "doc_id": view["doc_id"],
                                "image_url": f"/api/chat/thumb?session_id={sid}&doc_id={view['doc_id']}&kind=plain", "name": f_["name"], "summary": view.get("understood_summary")})
                return out
        layers = await asyncio.to_thread(_open_layers); INGEST.PROGRESS.pop(sid, None)
        layers.sort(key=lambda l: 0 if l["key"] == "architecture" else 1)
        return JSONResponse({"ok": True, "session_id": sid, "title": "Special School, Level 00", "sample": side.get("note"), "layers": layers})
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/chat/studio/progress")
def api_studio_progress(session_id: str):
    """what the server is doing for this session right now (set by bp.ingest.stage); empty when idle"""
    return JSONResponse({"ok": True, **(INGEST.PROGRESS.get(INGEST.clean_id(session_id, "s")) or {})})


@app.get("/api/chat/studio/rooms")
def api_studio_rooms(session_id: str, doc_id: str):
    """everything the page draws for one layer: room outlines with their data, and what was read on this layer
    (fire devices on the fire layout, seats on the architecture plan). On the SAMPLE plan devices and seats come from the
    planted sidecar and are marked as such."""
    try:
        doc_meta = INGEST.get_doc_meta(session_id, doc_id); st, _ = ASK.store_for(doc_meta["query_path"])
        W, H = st.meta.get("image_size") or Image.open(st.path).size
        fr = lambda x, y: [round(x / W, 5), round(y / H, 5)]
        rooms = []
        for e in st.of("room"):
            if not (e.geom and len(e.geom) >= 3): continue
            xs = [q[0] for q in e.geom]; ys = [q[1] for q in e.geom]
            try:
                from shapely.geometry import Polygon
                c = Polygon(e.geom).buffer(0).representative_point(); cx, cy = c.x, c.y
            except Exception: cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            rooms.append({"id": e.id, "name": e.get("name") or e.get("label"), "types": _room_types(e.get("name") or ""), "area": e.get("area"), "perimeter": e.get("perimeter"), "doors": e.get("doors"),
                          "windows": e.get("windows"), "baseboard": e.get("baseboard"), "seats": e.get("seats"), "building": e.get("building"),
                          "poly": [fr(x, y) for x, y in e.geom], "c": fr(cx, cy), "box": [*fr(min(xs), min(ys)), *fr(max(xs), max(ys))]})
        demo = st.meta.get("demo") or {}; devices = []; seats = []
        side_f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "sheet_rooms", os.path.basename(st.path)[:-4], "demo.json")
        if os.path.exists(side_f):
            dm = json.load(open(side_f))
            if dm.get("layer") == "fire":
                for d in dm.get("detectors") or []: devices.append({"kind": d.get("type") + " detector", "room": f"room:{d.get('room')}", "box": [*fr(d["x"] - d["r"], d["y"] - d["r"]), *fr(d["x"] + d["r"], d["y"] + d["r"])]})
                for d in dm.get("other_devices") or []:
                    r_ = 60 if d["kind"] == "panel" else 28
                    devices.append({"kind": {"mcp": "manual call point", "sounder": "alarm sounder", "panel": "control panel"}.get(d["kind"], d["kind"]), "room": f"room:{d.get('room')}", "box": [*fr(d["x"] - r_, d["y"] - (30 if d["kind"] == "panel" else r_)), *fr(d["x"] + r_, d["y"] + (30 if d["kind"] == "panel" else r_))]})
            for sb in dm.get("seat_boxes") or []: seats.append({"room": f"room:{sb['room']}", "box": [*fr(sb["box"][0], sb["box"][1]), *fr(sb["box"][2], sb["box"][3])]})
        return JSONResponse({"ok": True, "size": [W, H], "layer": demo.get("layer"), "sample": demo.get("note"), "rooms": rooms, "devices": devices, "seats": seats})
    except INGEST.IngestError as e:
        return JSONResponse({"ok": False, "error": e.message}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/chat/studio/evac")
def api_studio_evac(session_id: str, doc_id: str, room: str):
    """Shortest way out of a room: through the doors the wall tracer found, to the nearest door that leads outside.
    The route is a graph search over doors (two doors are linked when they open into the same room); its length is the
    sum of straight lines between consecutive doors, so it is a lower bound on the walking distance."""
    try:
        import sqlite3, heapq, math
        from shapely.geometry import Polygon, Point
        from shapely.ops import unary_union
        doc_meta = INGEST.get_doc_meta(session_id, doc_id); st, _ = ASK.store_for(doc_meta["query_path"])
        db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "sheet_rooms", os.path.basename(st.path)[:-4], "sheet.db")
        if not os.path.exists(db): return JSONResponse({"ok": False, "error": "This drawing has no traced rooms and doors, so a way out cannot be worked out."}, status_code=400)
        con = sqlite3.connect(db); mpp = con.execute("select m_per_px from sheet").fetchone()[0]
        W, H = st.meta.get("image_size") or Image.open(st.path).size
        polys = {f"room:{i}": Polygon(json.loads(pg)).buffer(0) for i, pg in con.execute("select id, polygon_px from room")}
        names = {f"room:{i}": n for i, n in con.execute("select id, name from room")}
        if room not in polys: return JSONResponse({"ok": False, "error": "Room not found on this drawing."}, status_code=404)
        doors = [{"mid": ((x0 + x1) / 2, (y0 + y1) / 2), "rooms": [f"room:{i}" for i in json.loads(rs or "[]")], "width": w_, "kind": k_}
                 for k_, w_, x0, y0, x1, y1, rs in con.execute("select kind, width_m, x0_px, y0_px, x1_px, y1_px, rooms from opening") if k_ in ("door", "sliding_door", "open_passage")]
        # a door leads outside when only one room touches it AND it sits on the outline of the building (not on a cupboard or shaft inside)
        wall = 0.45 / mpp if mpp else 40
        hull = unary_union([pg.buffer(wall) for pg in polys.values()])
        rings = [g.exterior for g in (hull.geoms if hasattr(hull, "geoms") else [hull])]
        for d in doors: d["exit"] = len(d["rooms"]) == 1 and min(r.distance(Point(d["mid"])) for r in rings) < 1.4 * wall
        if not any(d["exit"] for d in doors): return JSONResponse({"ok": False, "error": "No door to the outside was found on this drawing."}, status_code=400)
        start = polys[room].representative_point(); S0 = (start.x, start.y)
        by_room = {}
        for i, d in enumerate(doors):
            for r in d["rooms"]: by_room.setdefault(r, []).append(i)
        dist = {}; prev = {}; pq = []
        for i in by_room.get(room, []):
            dist[i] = math.dist(S0, doors[i]["mid"]); prev[i] = None; heapq.heappush(pq, (dist[i], i))
        end = None
        while pq:
            dcur, i = heapq.heappop(pq)
            if dcur > dist.get(i, 1e18): continue
            if doors[i]["exit"]: end = i; break
            for r in doors[i]["rooms"]:
                for j in by_room.get(r, []):
                    nd = dcur + math.dist(doors[i]["mid"], doors[j]["mid"])
                    if nd < dist.get(j, 1e18): dist[j] = nd; prev[j] = i; heapq.heappush(pq, (nd, j))
        if end is None: return JSONResponse({"ok": False, "error": f"No way out was found from {names.get(room) or 'this room'}: its doors do not connect to a door leading outside on the traced plan."}, status_code=400)
        chain = []; i = end
        while i is not None: chain.append(i); i = prev[i]
        chain.reverse(); pts = [S0] + [doors[i]["mid"] for i in chain]
        through = [room]
        for a_, b_ in zip(chain, chain[1:]):
            shared = [r for r in doors[a_]["rooms"] if r in doors[b_]["rooms"]]
            if shared and shared[0] != through[-1]: through.append(shared[0])
        length = dist[end] * mpp if mpp else None
        return JSONResponse({"ok": True, "room": room, "room_name": names.get(room), "path": [[round(x / W, 5), round(y / H, 5)] for x, y in pts], "length_m": round(length, 1) if length else None,
                             "doors": len(chain), "exit_width_m": doors[end]["width"], "through": [names.get(r) or "unnamed room" for r in through],
                             "limit": {"general_m": 45, "one_exit_m": 30, "rule": "Finland, fire safety decree 848/2017, 32 § and Table 10: longest travel distance to the nearest exit, 'other spaces'",
                                       "url": "https://www.finlex.fi/fi/lainsaadanto/saadoskokoelma/2017/848", "note": "Table 10 gives 45 m in general and 30 m where the escape area has a single exit, with footnotes and higher values where a smoke-detection fire alarm or sprinklers are installed."},
                             "method": "Straight lines from the middle of the room to each door on the way. Real walking distance around furniture and corners is longer."})
    except INGEST.IngestError as e:
        return JSONResponse({"ok": False, "error": e.message}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/chat/studio/scene3d")
def api_studio_scene3d(request: Request, session_id: str, doc_id: str):
    """the drawing as a 3D scene for the page (bp/scene3d.py): an IFC model's own geometry, or the traced rooms of a sheet stood
    up as walls with assumed heights. A file with nothing to stand up gets a plain reason instead of an empty scene."""
    try:
        import gzip
        from bp import scene3d as S3
        doc_meta = INGEST.get_doc_meta(session_id, doc_id); st, _ = ASK.store_for(doc_meta["query_path"])
        if st is None: return JSONResponse({"ok": False, "error": "This file could not be read, so there is nothing to show in 3D."}, status_code=400)
        try: sc = S3.scene_for(st, doc_meta["query_path"])
        except S3.NoScene as e: return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        raw = json.dumps({"ok": True, **sc}, separators=(",", ":")).encode()
        if "gzip" in (request.headers.get("accept-encoding") or ""):
            return Response(gzip.compress(raw, 5), media_type="application/json", headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        return Response(raw, media_type="application/json")
    except INGEST.IngestError as e:
        return JSONResponse({"ok": False, "error": e.message}, status_code=e.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/chat/studio/regulations")
def api_studio_regulations(room_name: str = "", country: str = ""):
    """regulation entries that apply to a room of this name (regulations/regulations_fi_us.json), per country"""
    try:
        R = json.load(open(os.path.join(REG_DIR, "regulations_fi_us.json"), encoding="utf-8")); types = set(_room_types(room_name))
        out = [r for r in R if types & set(r.get("rooms") or []) and (not country or r["country"].lower() == country.lower())]
        for r in out: r["source_url"] = f"/api/chat/studio/source?file={os.path.basename(r['local'])}" if r.get("local") else None
        return JSONResponse({"ok": True, "room_name": room_name, "types": sorted(types), "countries": sorted({r["country"] for r in R}), "entries": out})
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/chat/studio/source")
def api_studio_source(file: str):
    """the saved copy of an official text (regulations/sources/), for 'download the regulation'"""
    if not re.match(r"^[a-zA-Z0-9_.-]+$", file): return Response(status_code=404)
    target = os.path.realpath(os.path.join(REG_DIR, "sources", file))
    if os.path.commonpath([os.path.join(REG_DIR, "sources"), target]) != os.path.join(REG_DIR, "sources") or not os.path.isfile(target): return Response(status_code=404)
    media = {"pdf": "application/pdf", "xml": "application/xml", "html": "text/html", "htm": "text/html"}.get(file.rsplit(".", 1)[-1], "application/octet-stream")
    return FileResponse(target, media_type=media, filename=file)


@app.get("/api/chat/inspect")
def api_chat_inspect(session_id: str, doc_id: str):
    try:
        doc_meta = INGEST.get_doc_meta(session_id, doc_id)
        st, _ = ASK.store_for(doc_meta["query_path"])
        if st is None:
            return JSONResponse({"ok": False, "error": "Could not load extracted store."}, status_code=400)
        info = INGEST.build_inspection(st, doc_meta)
        info["ok"] = True
        info["preview_url"] = INGEST.public_doc_view(doc_meta)["preview_url"]
        return JSONResponse(json.loads(json.dumps(info, default=str)))
    except INGEST.IngestError as e:
        return JSONResponse({"ok": False, "error": e.message}, status_code=e.status_code)


@app.post("/api/chat/ask")
async def api_chat_ask(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON request body."}, status_code=400)

    sid = INGEST.clean_id(body.get("session_id"), "s")
    doc_id = body.get("doc_id")
    question = str(body.get("question") or "").strip()
    allow_llm = bool(body.get("allow_llm", True))
    plan_override = body.get("plan_override")

    if not doc_id:
        return JSONResponse({"ok": False, "error": "Please upload or select a document first."}, status_code=400)
    if not question:
        return JSONResponse({"ok": False, "error": "Please enter a question."}, status_code=400)

    try:
        doc_meta = INGEST.get_doc_meta(sid, doc_id)
    except INGEST.IngestError as e:
        return JSONResponse({"ok": False, "error": e.message}, status_code=e.status_code)

    qpath = doc_meta["query_path"]
    st, route_note = ASK.store_for(qpath)

    new_cost_records = []
    t0 = time.time()

    if isinstance(plan_override, dict) and st is not None:
        # Execute validated alternative plan purely in deterministic Python code ($0.00, 0 model calls)
        from bp import llm as LLM
        val_err = LLM.validate(plan_override, st)
        if val_err:
            return JSONResponse({"ok": False, "error": f"Invalid alternative plan: {val_err}"}, status_code=400)
        a_obj = ASK._execute(plan_override, st, question)
        if hasattr(a_obj, "_parts"):
            del a_obj._parts
        a_obj.plan_source = "rules"
        a_obj.trace = [
            {"op": "route", "text": route_note},
            {"op": "plan", "text": "selected alternative reading (executed by code)", "plan": plan_override},
        ] + a_obj.trace
        ans = a_obj.to_dict()
        ans["trust"] = TRUST.assess(st, ans)
        ans["reasoning"] = TRUST.narrate(st, ans)
        ans["ms"] = round((time.time() - t0) * 1000, 1)
        is_override = True
    else:
        is_override = False
        with _ASK_LOCK:
            pos0 = os.path.getsize(COST_LOG_FILE) if os.path.exists(COST_LOG_FILE) else 0
            ans = ASK.ask(qpath, question, allow_llm=allow_llm)
            pos1 = os.path.getsize(COST_LOG_FILE) if os.path.exists(COST_LOG_FILE) else 0
            if pos1 > pos0:
                try:
                    with open(COST_LOG_FILE, "rb") as cf:
                        cf.seek(pos0)
                        chunk = cf.read(pos1 - pos0).decode("utf-8", "replace")
                    for ln in chunk.splitlines():
                        if ln.strip():
                            rec = json.loads(ln)
                            if rec.get("q") == question[:80]:
                                new_cost_records.append(rec)
                except Exception:
                    pass

    slog = load_session_log(sid)
    prev_total = float(slog["totals"].get("usd") or 0.0)
    cost_info = _build_cost_info(question, st, ans, new_cost_records, prev_total, is_override=is_override)
    # Every model call made for this question counts, also a picture of a room sent to a vision model after the rules had
    # already read the question. Shown in euros: an ESTIMATE from the token counts the provider reported and list prices.
    spent = round(sum(_estimate_record_usd(r) for r in new_cost_records), 6)
    if spent > float(cost_info.get("usd") or 0):
        cost_info["usd"] = spent; cost_info["calls"] = len(new_cost_records); cost_info["session_total_usd"] = round(prev_total + spent, 6)
        cost_info["in_tokens"] = sum(int(r.get("in") or 0) for r in new_cost_records); cost_info["out_tokens"] = sum(int(r.get("out") or 0) for r in new_cost_records)
        cost_info["total_tokens"] = cost_info["in_tokens"] + cost_info["out_tokens"]
        if cost_info.get("tier") == "none": cost_info["tier"] = "small"
    cost_info["models"] = sorted({str(r.get("model")) for r in new_cost_records if r.get("model")})
    cost_info["eur"] = round(float(cost_info.get("usd") or 0) * USD_EUR, 6)
    cost_info["session_total_eur"] = round(float(cost_info.get("session_total_usd") or 0) * USD_EUR, 6)
    if cost_info.get("cold_est_usd"): cost_info["cold_est_eur"] = round(cost_info["cold_est_usd"] * USD_EUR, 6)
    cost_info["usd_eur"] = USD_EUR
    cost_info["price_note"] = f"Estimated from the tokens the model reported and list prices, at 1 USD = {USD_EUR} EUR."

    # Update running session totals
    slog["totals"]["questions"] = int(slog["totals"].get("questions") or 0) + 1
    if cost_info["tier"] == "none":
        slog["totals"]["rules_count"] = int(slog["totals"].get("rules_count") or 0) + 1
    elif cost_info["tier"] == "small":
        slog["totals"]["small_model_count"] = int(slog["totals"].get("small_model_count") or 0) + 1
    else:
        slog["totals"]["large_model_count"] = int(slog["totals"].get("large_model_count") or 0) + 1
    slog["totals"]["in_tokens"] = int(slog["totals"].get("in_tokens") or 0) + cost_info["in_tokens"]
    slog["totals"]["out_tokens"] = int(slog["totals"].get("out_tokens") or 0) + cost_info["out_tokens"]
    slog["totals"]["usd"] = cost_info["session_total_usd"]

    alternatives = INGEST.compute_alternatives(st, question, ans)
    evidence_items = _build_evidence_items(st, ans)
    refusal_info = _build_refusal_info(question, st, doc_meta, ans)
    ui_view = _UI_evidence_view(st, ans, evidence_items, alternatives)   # UI kit lane (additive fields only)

    answer_id = f"ans_{uuid.uuid4().hex[:10]}"
    ans_snap_path = INGEST.ensure_inside_sessions(os.path.join(doc_meta["doc_dir"], f"{answer_id}.json"))
    json.dump(
        {"question": question, "answer": ans},
        open(ans_snap_path, "w"),
        default=str,
        ensure_ascii=False,
    )
    evidence_url = f"/api/chat/evidence?session_id={sid}&doc_id={doc_id}&answer_id={answer_id}"
    # The drawing is repainted only from this answer's own evidence. A question that was not answered and located
    # nothing leaves the drawing as it is: the chat text and the picture must never say different things.
    if ans.get("status") != "ok" and not (ans.get("evidence") or []):
        evidence_url = None
    overlay = None
    if st is not None and st.fmt == "raster" and evidence_url:
        # scans: no new picture per answer. The page keeps the one drawing it already loaded and draws these shapes on it.
        evidence_url = f"/api/chat/thumb?session_id={sid}&doc_id={doc_id}&kind=plain"
        try:
            vals = RENDER._trace_vals(ans) if ans.get("status") == "ok" and ans.get("unit") != "count" else {}
            unit = ans.get("unit") if ans.get("unit") not in (None, "count") else ((ans.get("plan") or {}).get("unit") or "")
            overlay = _overlay_shapes(st, ids=ans.get("evidence") or [], values=vals, unit=unit)
            if ans.get("findings"):      # plan check: failing rooms in red with what was measured
                why = {}
                for f_ in ans["findings"]:
                    if f_["status"] == "fail" and f_.get("room"): why.setdefault(f_["room"], []).append(f_["title"].split(": ", 1)[-1])
                for sh in overlay:
                    if sh["id"] in why: sh["kind"] = "fail"; sh["text"] = "; ".join(why[sh["id"]])
        except Exception:
            overlay = []

    # Extract concise single trust reason & action in words (no percentages)
    trust_obj = ans.get("trust") or {"level": "single_source", "headline": "", "checks": []}
    t_level = trust_obj.get("level") or "single_source"
    t_checks = trust_obj.get("checks") or []
    primary_reason = (
        next((c["text"] for c in t_checks if c.get("result") in ("fail", "caution")), None)
        or next((c["text"] for c in t_checks if c.get("result") == "pass"), None)
        or (trust_obj.get("headline") or "").split(": ", 1)[-1]
        or "Nothing else on the drawing confirms or contradicts this."
    )
    action_map = {
        "checked": "This check on the drawing passed. Look at the highlighted evidence if the number surprises you.",
        "single_source": "Compare it with the highlighted evidence before relying on it.",
        "review": "Do not use this value until you have looked at the problem marked on the drawing.",
        "none": "This cannot be computed from this file. See what can be asked instead.",
    }
    label_words = {
        "checked": "Checked",
        "single_source": "Not confirmed",
        "review": "Needs review",
        "none": "Not answered",
    }

    payload = {
        "ok": True,
        "answer_id": answer_id,
        "session_id": sid,
        "doc_id": doc_id,
        "document_name": doc_meta["display_name"],
        "format": st.fmt if st else doc_meta["format"],
        "selected_pages": doc_meta.get("selected_pages", [1]),
        "question": question,
        "status": ans.get("status"),
        "value": ans.get("value"),
        "unit": ans.get("unit"),
        "reason": ans.get("reason"),
        "read_as": TRUST.read_as({**ans["plan"], "attr": ans["plan"].get("attr") or ans["plan"].get("as")} if ans["plan"].get("expr") else ans["plan"]) if ans.get("plan") else None,
        "trust": {
            "level": t_level,
            "label": label_words.get(t_level, t_level),
            "reason": primary_reason,
            "action": action_map.get(t_level, ""),
            "headline": trust_obj.get("headline"),
            "checks": t_checks,
        },
        "reasoning": ans.get("reasoning") or [],
        "trace": ans.get("trace") or [],
        "plan": ans.get("plan"),
        "plan_source": ans.get("plan_source"),
        "fingerprint": ans.get("fingerprint"),
        "ms": ans.get("ms"),
        "evidence_ids": ans.get("evidence") or [],
        "evidence_count": len(ans.get("evidence") or []),
        "evidence_items": evidence_items,
        "evidence_url": evidence_url,
        "has_evidence_image": bool(evidence_url),
        "overlay": overlay,
        "findings": ans.get("findings"),
        "memory": ans.get("memory"),
        "room_marks": ans.get("room_marks"),
        "alternatives": alternatives,
        "evidence_tables": ui_view["tables"],
        "evidence_has_marks": ui_view["has_marks"],
        "read_as_plain": ui_view["reading"],
        "refusal": refusal_info,
        "cost": cost_info,
        "session_totals": slog["totals"],
    }

    # Append turn to session JSON log
    slog["events"].append({
        "type": "ask",
        "ts": round(time.time(), 2),
        "answer_id": answer_id,
        "doc_id": doc_id,
        "document_name": doc_meta["display_name"],
        "question": question,
        "status": payload["status"],
        "value": payload["value"],
        "unit": payload["unit"],
        "trust_level": t_level,
        "trust_reason": primary_reason,
        "plan_source": payload["plan_source"],
        "cost": cost_info,
        "evidence_count": payload["evidence_count"],
        "fingerprint": payload["fingerprint"],
    })
    save_session_log(sid, slog)

    return JSONResponse(json.loads(json.dumps(payload, default=str)))


@app.get("/api/chat/evidence")
def api_chat_evidence(session_id: str, doc_id: str, answer_id: str = "", question: str = ""):
    try:
        doc_meta = INGEST.get_doc_meta(session_id, doc_id)
    except INGEST.IngestError as e:
        return JSONResponse({"ok": False, "error": e.message}, status_code=e.status_code)

    st, _ = ASK.store_for(doc_meta["query_path"])
    ans = None
    if answer_id and INGEST.ID_RX.match(answer_id):
        snap = INGEST.ensure_inside_sessions(os.path.join(doc_meta["doc_dir"], f"{answer_id}.json"))
        if os.path.isfile(snap):
            try:
                ans = json.load(open(snap)).get("answer")
            except Exception:
                ans = None
    if ans is None and question:
        ans = ASK.ask(doc_meta["query_path"], question, allow_llm=True)
    if ans is None:
        return Response(status_code=404)

    png = render_evidence_png(doc_meta, st, ans)
    if not png:
        return Response(status_code=204)
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/chat/session_log")
def api_chat_session_log(session_id: str, download: int = 0):
    sid = INGEST.clean_id(session_id, "s")
    slog = load_session_log(sid)
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="blueprint-chat-{sid}.json"'
    return JSONResponse(slog, headers=headers)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8773"))
    host = os.environ.get("BP_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="warning")
