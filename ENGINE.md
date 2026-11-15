# bp: deterministic, explainable answers about construction drawings

Every answer is computed by code from entities extracted from the drawing. A language model never produces a number.
Opus is used only to *plan* a question the rules do not understand. Its plan is shown, validated and cached.
That means the same file and question always give the same plan, value and trace.

```
file ──extract──> Store (entities + provenance) ──┐
question ──rules (vocab.py)──> plan (JSON) ───────┼──execute──> value + trace ──render──> overlay
          └─(unknown phrasing)─ Opus planner ─────┘   (plain code)    (every step)
```

## Run

```bash
source ../.venv/bin/activate
BP_ACCESS_CODE=<code> python serve.py  # the web app  -> http://127.0.0.1:8080/?code=<code>
python eval_bp.py swiss_dwellings     # score on the dataset questions (add --nollm for rules only)
python eval_tricky.py [--llm]         # score on the generated tricky questions
python eval_trust.py swiss_dwellings  # is 'checked' right, are wrong answers flagged? -> cache/trust_<src>.json
python determinism.py out.json        # fingerprints, run twice and compare
```

The dataset path comes from `BP_DATA` (defaults to the full dataset). Set `BP_NO_LLM=1` to forbid planner calls.

## On another machine

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
export BP_DATA=/path/to/junction-dataset     # the folder containing cubicasa5k/, swiss_dwellings/, ...
BP_ACCESS_CODE=<code> .venv/bin/python serve.py
```

- `cache/` holds the scores, the cached plans and the stored detections, so replays need no AI.
- Questions that were never planned need the `claude` CLI, logged in. Without it, set `BP_NO_LLM=1` and such questions are refused with a reason.
- Don't copy `.venv`; it is tied to this machine's paths.

## Layers (`bp/`)

| file | role |
|---|---|
| `core.py` | `Entity` (id, kind, attrs, geometry, provenance), `Store`, `Trace`, `Answer` (with a fingerprint) |
| `x_dxf.py` | layered DXF: rooms (polygon area, area printed on the drawing, apartment, room type), doors, windows, fixtures |
| `x_ifc.py` | IFC: spaces, building elements, MEP elements, systems, levels, model facts. Each quantity records its quantity set or geometry rule |
| `x_raster.py` | PNG/JPG: cached vision detections (`cache/detections/`) and OCR labels |
| `x_pdf.py` | vector PDF: schedule tables, with each row as an entity |
| `vocab.py` | **multilingual vocabulary** (kinds, room types, aggregates). Extend here |
| `plan.py` | rule parser: question to plan. It defers to the planner when the question has words outside the rule vocabulary, needs grouping or several parts, or asks about a kind the extractor cannot produce |
| `llm.py` | Opus planner. It returns a plan only, which is validated against the store and cached in `cache/llm_plans.json` |
| `execute.py` | select, filter, aggregate (count / exists / sum / avg / max / min / get / list / group), unit conversion, documented fallbacks |
| `audit.py` | problems in a drawing found without knowing any answer (overlaps, printed vs measured area, untyped rooms, apartment outline checks...) and the per-room table |
| `trust.py` | trust label per answer (`checked` / `single_source` / `review`), its checks, and the plain-language reasoning lines |
| `render.py` | evidence overlays and the audit image (every room coloured by type with number and area) |
| `ask.py` | routing by file content, store cache keyed by a hash of the extractor code, multi-part plans |

## Plan schema

```json
{"op": "sum", "kind": "room", "attr": "area",
 "filters": [{"field": "rtype", "op": "eq", "value": "wc"}, {"field": "apartment", "op": "eq", "value": "B"}],
 "convert": "ft2", "notes": ["'toilet' with an area asked -> the toilet ROOM, not the bowl"]}
```

## Extending

- **New word or language:** add it to the right regex in `vocab.py` (`KINDS`, `RTYPES`, `RTYPE_IN_Q`, `COUNT`, `AREA`…).
- **New interpretation rule:** add it in `plan.py`, and add a `notes` entry so the user sees it.
- **New drawing type:** write `x_<name>.py` that returns a `Store`, declare `meta["supports"]`, and route to it in `ask.pick`.
- **New operation:** add it to `execute.run` and to `llm.OPS`.

## Things the engine says explicitly instead of guessing

- An unlabelled room is typed from the fixtures inside it ("no label; contains toilet + bath/shower").
- "Toilet size" with no WC room uses the room that contains the toilet, and notes when that room is a bathroom.
- "Bedrooms" on plans that only say Zimmer / Pièce / Stanza: those rooms are counted, and the plan says so.
- Lifts count as shafts when a question excludes "shafts and voids".
- Computed room areas are cross-checked against areas printed on the drawing.
- Refusals carry a reason: not in the drawing, extractor cannot produce that kind, no area source, and so on.

## Trust label

Every answer carries `trust = {level, checks, headline}`, computed without the reference answer:

- `checked`: an independent check passed (measured areas equal printed areas; every door has its swing line).
- `single_source`: nothing contradicts the answer, nothing confirms it.
- `review`: a check failed, or `audit.py` found a problem touching the elements or scope of this answer.

Add a check by appending an issue in `audit.py` (with `ids` and a `scope`), then run `eval_trust.py` to see how often
answers carrying it are right. Keep a check as a caution only if it separates right from wrong; otherwise make it `info`.
