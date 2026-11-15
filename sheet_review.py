"""Two-level review of a room database built by sheet_rooms.py.

  python sheet_review.py <sheet.png> <out_dir> [--l1 haiku --l2 gpt-6.1-sol --threads 2]

What is code and what is a model
  code   finds anomalies (two room names inside one region; a room name lying in 'outside'), finds the exact leak:
         the narrowest free passage that still connects the two places (bottleneck search on the wall mask), and
         measures it. Every number stays geometric.
  model  sees a FULL-RESOLUTION crop of that one spot (never a downscaled sheet) with the proposed line drawn in red,
         and picks ONE action from the tool list below. It returns no coordinates and no quantities.
  level 1 = cheap model on every anomaly, 2 threads.  level 2 = stronger model only when level 1 escalates, is unsure,
         or says the line is in the wrong place.

Tool list offered to the model (TOOLS): close_gap, keep_open, wrong_place, escalate.
"""
import os, sys, json, math, argparse, subprocess, sqlite3, time, re
from concurrent.futures import ThreadPoolExecutor
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", str(2 ** 40))
import cv2, numpy as np

TOOLS = [
    {"name": "close_gap", "args": {"kind": ["door", "sliding_door", "window", "glazed_screen", "thin_partition"]},
     "when": "the red line lies where something physical separates the two spaces (a door leaf/opening, sliding door, window, glazed screen, thin partition drawn with thin lines)",
     "effect": "code closes the wall mask along the red line and recomputes rooms; door kinds are deducted from baseboard length, the others are not"},
    {"name": "keep_open", "args": {"kind": ["open_passage", "same_room"]},
     "when": "nothing is drawn across the red line. open_passage: two differently named spaces joined by an opening with no door (the rooms are still split there). same_room: both names belong to one undivided space",
     "effect": "open_passage splits the rooms at the line without a wall; same_room leaves the region as one room"},
    {"name": "wrong_place", "args": {"reason": "text"},
     "when": "there is a real separation between the two spaces but the red line is not on it",
     "effect": "nothing is changed; the case goes to the stronger model and is reported as unresolved if it also cannot place it"},
    {"name": "escalate", "args": {"reason": "text"},
     "when": "the crop is not enough to decide",
     "effect": "the same crop goes to the stronger model"},
]

ap = argparse.ArgumentParser()
ap.add_argument("image"); ap.add_argument("out")
ap.add_argument("--l1", default="haiku"); ap.add_argument("--l2", default="gpt-6.1-sol"); ap.add_argument("--threads", type=int, default=2)
ap.add_argument("--max-gap-m", type=float, default=5.0); ap.add_argument("--no-model", action="store_true")
a = ap.parse_args()
if a.l1 == "auto":      # server: only a Gemini key exists. laptop: command-line logins for Claude and OpenAI.
    _g = bool(os.environ.get("GEMINI_API_KEY") or os.path.exists(os.path.expanduser("~/.config/bp_gemini_key")))
    a.l1, a.l2 = ("gemini-3.8-flash", "gemini-4-carbon") if _g else ("haiku", "gpt-6.1-sol")
S = np.load(os.path.join(a.out, "state.npz")); closed = S["closed"].copy(); lab = S["lab"]; k = float(S["k"]); wall_t = float(S["wall_t"]); mpp = float(S["m_per_px"])
room_cc = set(int(v) for v in S["room_cc"]); ocr = json.load(open(os.path.join(a.out, "ocr.json")))
H, W = closed.shape
if not mpp: sys.exit("no scale: review needs metres per pixel")
RMAX = int(a.max_gap_m / mpp / 2)

# ---- room-name labels: OCR lines grouped into one label when they are stacked
words = [o for o in ocr if o["conf"] >= 0.6 and re.search(r"[A-Za-z]{3}", o["text"])]
words.sort(key=lambda o: (o["y"], o["x"])); labels = []
for o in words:
    for L in labels:
        if abs(L["x"] - o["x"]) < 90 and 0 < o["y"] - L["y1"] < 2.2 * max(o["h"], 12): L["text"] += " " + o["text"]; L["y1"] = o["y"]; L["n"] += 1; break
    else: labels.append({"text": o["text"], "x": o["x"], "y": o["y"], "y1": o["y"], "n": 1})
for L in labels: L["y"] = (L["y"] + L["y1"]) / 2

# ---- buildings = bounding boxes of the wall components that carry rooms
wn, wl, wst, _ = cv2.connectedComponentsWithStats(closed, 8)
bboxes = [wst[i, :4] for i in range(1, wn) if wst[i, 2] * wst[i, 3] * mpp * mpp > 60]
def building_of(x, y):
    for j, (bx, by, bw, bh) in enumerate(bboxes):
        if bx <= x <= bx + bw and by <= y <= by + bh: return j
    return None

def snap(dist, x, y, r=30):
    x, y = int(x), int(y); x0, y0 = max(0, x - r), max(0, y - r); sub = dist[y0:y + r, x0:x + r]
    j = np.unravel_index(np.argmax(sub), sub.shape); return x0 + j[1], y0 + j[0]

def bottleneck(cl, A, B, roi):
    """Narrowest free passage that still connects A to B (B None = to the outside of the roi).
    Returns (q1, q2) wall-to-wall segment across it, or None when they are separated or the passage is wider than the limit."""
    X0, Y0, X1, Y1 = roi; free = (cl[Y0:Y1, X0:X1] == 0).astype(np.uint8)
    dist = cv2.distanceTransform(free, cv2.DIST_L2, 5)
    ax, ay = snap(dist, A[0] - X0, A[1] - Y0)
    if B is not None: bx, by = snap(dist, B[0] - X0, B[1] - Y0)
    def connected(r):
        n, l = cv2.connectedComponents((dist > r).astype(np.uint8), connectivity=8)
        la = l[ay, ax]
        if la == 0: return None, l
        if B is None:
            edge = set(np.unique(np.concatenate([l[0], l[-1], l[:, 0], l[:, -1]]))) - {0}
            return la in edge, l
        return (l[by, bx] == la and la != 0), l
    c0, _ = connected(0.5)
    if os.environ.get("DBG"): print("   dbg A", (ax, ay), round(float(dist[ay, ax]), 1), "B", (bx, by, round(float(dist[by, bx]), 1)) if B is not None else None, "c0", c0, "RMAX", RMAX, "cmax", connected(float(RMAX))[0])
    if not c0: return None
    # the search radius cannot exceed what fits inside either space itself
    top = min(float(RMAX), float(dist[ay, ax]) - 1.0, (float(dist[by, bx]) - 1.0) if B is not None else 1e9)
    if top <= 1.0: return None
    lo, hi = 0.5, top
    if connected(hi)[0]: return "wide" if top >= RMAX - 1 else None      # no neck narrower than the spaces themselves
    while hi - lo > 0.5:
        mid = (lo + hi) / 2; c, _ = connected(mid)
        if c: lo = mid
        else: hi = mid
    _, l_hi = connected(hi); la = l_hi[ay, ax]
    if os.environ.get("DBG"): print("   dbg neck lo/hi", round(lo, 1), round(hi, 1), "top", round(top, 1), "la", la)
    if la == 0: return None
    compA = (l_hi == la).astype(np.uint8)
    other = ((l_hi > 0) & (l_hi != la)).astype(np.uint8)
    if B is not None: other = (l_hi == l_hi[by, bx]).astype(np.uint8) if l_hi[by, bx] else other
    else:
        edge = list(set(np.unique(np.concatenate([l_hi[0], l_hi[-1], l_hi[:, 0], l_hi[:, -1]]))) - {0}); other = np.isin(l_hi, edge).astype(np.uint8)
    if not other.any(): return None
    # grow both sides back through free space only (never through a wall); they meet in the neck
    k3 = np.ones((3, 3), np.uint8); DA, DO = compA.copy(), other.copy()
    for _ in range(int(math.ceil(hi)) + 3): DA = cv2.dilate(DA, k3) & free; DO = cv2.dilate(DO, k3) & free
    ov = (DA & DO).astype(bool) & (dist >= lo - 1.0)
    if os.environ.get("DBG"): print("   dbg overlap px", int(ov.sum()), "other px", int(other.sum()))
    if not ov.any(): return None
    ys, xs = np.nonzero(ov); j = np.argmin(dist[ys, xs]); px, py = int(xs[j]), int(ys[j])
    # wall-to-wall segment through the bridge pixel: nearest wall point, then the opposite side along the same line
    R = int(hi * 3 + 6); wx0, wy0 = max(0, px - R), max(0, py - R); win = free[wy0:py + R, wx0:px + R] == 0
    wy, wx = np.nonzero(win)
    if not len(wx): return None
    vx, vy = wx + wx0 - px, wy + wy0 - py; d2 = vx ** 2 + vy ** 2; j = np.argmin(d2); q1 = (wx[j] + wx0, wy[j] + wy0)
    # the other side of the neck: nearest wall point in the opposite half-plane (within 45 degrees of straight across)
    n1 = math.sqrt(d2[j]) or 1.0; cosang = -(vx * vx[j] + vy * vy[j]) / (np.sqrt(d2) * n1 + 1e-9)
    opp = np.nonzero(cosang > 0.7)[0]
    if not len(opp): return None
    j2 = opp[np.argmin(d2[opp])]; q2 = (wx[j2] + wx0, wy[j2] + wy0)
    return (q1[0] + X0, q1[1] + Y0), (q2[0] + X0, q2[1] + Y0)

# ---- anomalies
border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])))
by_cc = {}
for L in labels:
    x, y = int(L["x"]), int(L["y"])
    if not (0 <= x < W and 0 <= y < H) or building_of(x, y) is None: continue
    cc = int(lab[y, x])
    if cc == 0:                                    # label centre sits on a wall pixel/closure: look around
        v = lab[max(0, y - 6):y + 7, max(0, x - 6):x + 7]; v = v[v > 0]; cc = int(np.bincount(v).argmax()) if len(v) else 0
    L["cc"] = cc; by_cc.setdefault(cc, []).append(L)
anoms = []
for cc, Ls in by_cc.items():
    if cc in room_cc:
        Ls = sorted(Ls, key=lambda L: -L["n"]); base = Ls[0]
        for L in Ls[1:]:
            if math.hypot(L["x"] - base["x"], L["y"] - base["y"]) * mpp > 1.2:
                anoms.append({"type": "two names in one region", "a": base, "b": L, "cc": cc})
    else:
        for L in Ls: anoms.append({"type": "named space is not enclosed", "a": L, "b": None, "cc": cc})

# ---- propose gap lines (iterating: a leak can have several gaps)
work = closed.copy(); props = []
for an in anoms:
    A = (an["a"]["x"], an["a"]["y"]); B = (an["b"]["x"], an["b"]["y"]) if an["b"] else None
    if B is not None:
        ys, xs = np.nonzero(lab == an["cc"]); roi = (max(0, xs.min() - 20), max(0, ys.min() - 20), min(W, xs.max() + 20), min(H, ys.max() + 20))
    else:
        bx, by, bw, bh = bboxes[building_of(*A)]; roi = (max(0, bx - 150), max(0, by - 150), min(W, bx + bw + 150), min(H, by + bh + 150))
    for it in range(5):
        seg = bottleneck(work, A, B, roi)
        if seg is None: break
        if seg == "wide": an["note"] = f"connected by a passage wider than {a.max_gap_m} m: treated as one space / open to outside"; break
        (x0, y0), (x1, y1) = seg; wpx = math.hypot(x1 - x0, y1 - y0)
        dup = next((p for p in props if math.hypot(p["mid"][0] - (x0 + x1) / 2, p["mid"][1] - (y0 + y1) / 2) < 8), None)
        cv2.line(work, (int(x0), int(y0)), (int(x1), int(y1)), 1, 3)
        if dup: dup["anoms"].append(an); continue
        props.append({"id": len(props) + 1, "seg": [x0 / k, y0 / k, x1 / k, y1 / k], "mid": ((x0 + x1) / 2, (y0 + y1) / 2), "width_m": round(wpx * mpp, 3), "anoms": [an]})

# ---- crops from the ORIGINAL image at full resolution
g0 = cv2.imread(a.image, cv2.IMREAD_COLOR); cdir = os.path.join(a.out, "review"); os.makedirs(cdir, exist_ok=True)
for p in props:
    x0, y0, x1, y1 = p["seg"]; cx, cy = (x0 + x1) / 2, (y0 + y1) / 2; half = int(max(420, 2.2 * math.hypot(x1 - x0, y1 - y0)))
    X0, Y0 = int(max(0, cx - half)), int(max(0, cy - half)); crop = g0[Y0:int(cy + half), X0:int(cx + half)].copy()
    cv2.imwrite(os.path.join(cdir, f"gap{p['id']}_clean.png"), crop)
    cv2.line(crop, (int(x0 - X0), int(y0 - Y0)), (int(x1 - X0), int(y1 - Y0)), (0, 0, 255), 3)
    for an in p["anoms"]:
        for key, col in (("a", (200, 0, 160)), ("b", (0, 140, 0))):
            L = an[key]
            if L: cv2.circle(crop, (int(L["x"] / k - X0), int(L["y"] / k - Y0)), 14, col, 3)
    p["crop"] = os.path.join(cdir, f"gap{p['id']}.png"); p["crop_clean"] = os.path.join(cdir, f"gap{p['id']}_clean.png"); cv2.imwrite(p["crop"], crop)

def prompt_for(p):
    an = p["anoms"][0]
    ctx = (f"The spaces named '{an['a']['text']}' and '{an['b']['text']}' came out as ONE region." if an["b"] else f"The space named '{an['a']['text']}' is not enclosed: it connects to the outside of the building.")
    return (f"You are checking one spot of an architectural floor plan. Thick black strokes are walls. Automatic room tracing found a problem: {ctx}\n"
            f"The narrowest connection is marked by the RED line (it is {p['width_m']} m long in reality). "
            f"Look at image {p['crop']} (annotated, full resolution) and {p['crop_clean']} (same crop without marks).\n"
            f"Choose exactly ONE tool. Tools:\n{json.dumps(TOOLS, indent=1)}\n"
            'Answer with JSON only: {"tool": "...", "kind": "... or null", "reason": "one sentence on what is drawn at the red line", "confidence": "high|low"}')

def parse(txt):
    m = re.search(r"\{[^{}]*\"tool\"[^{}]*\}", txt or "", re.S)
    try: return json.loads(m.group(0)) if m else None
    except Exception: return None

def call_claude(p, model):
    t = time.time()
    try:
        r = subprocess.run(["claude", "-p", "--model", model, "--allowedTools", "Read", "--output-format", "json", prompt_for(p)], capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL)
        d = json.loads(r.stdout); return parse(d.get("result")), {"model": model, "s": round(time.time() - t, 1), "usd": d.get("total_cost_usd")}
    except Exception as e: return None, {"model": model, "s": round(time.time() - t, 1), "error": repr(e)[:200]}

def call_codex(p, model):
    t = time.time()
    try:
        # the prompt goes on stdin: '-i' takes a list of files and would swallow a prompt given as an argument
        r = subprocess.run(["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "-m", model, "-i", p["crop"] + "," + p["crop_clean"]], input=prompt_for(p), capture_output=True, text=True, timeout=300)
        ans = parse(r.stdout); return ans, {"model": model, "s": round(time.time() - t, 1), **({} if ans else {"error": (r.stderr or r.stdout)[-160:]})}
    except Exception as e: return None, {"model": model, "s": round(time.time() - t, 1), "error": repr(e)[:200]}

def _gemini_key():
    kk = os.environ.get("GEMINI_API_KEY")
    if kk: return kk
    try: return open(os.path.expanduser("~/.config/bp_gemini_key")).read().strip()
    except Exception: return None

def call_gemini(p, model):
    import base64, urllib.request
    t = time.time()
    try:
        parts = [{"inline_data": {"mime_type": "image/png", "data": base64.b64encode(open(f, "rb").read()).decode()}} for f in (p["crop"], p["crop_clean"])]
        parts.append({"text": prompt_for(p).replace(f"Look at image {p['crop']} (annotated, full resolution) and {p['crop_clean']} (same crop without marks).", "The first image is the annotated crop at full resolution; the second is the same crop without marks.")})
        body = {"contents": [{"parts": parts}], "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
        req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", data=json.dumps(body).encode(), headers={"content-type": "application/json", "x-goog-api-key": _gemini_key()})
        r = json.load(urllib.request.urlopen(req, timeout=120))
        return parse("".join(x.get("text", "") for x in r["candidates"][0]["content"]["parts"])), {"model": model, "s": round(time.time() - t, 1)}
    except Exception as e: return None, {"model": model, "s": round(time.time() - t, 1), "error": repr(e)[:200]}

# every model answer is stored by (crop pixels, question context, model): the same sheet always gets the same decisions
import hashlib
RCACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "review_answers.json")
def _cached_call(p, model):
    key = hashlib.sha1(open(p["crop"], "rb").read() + model.encode() + str(p["width_m"]).encode()).hexdigest()
    try: cur = json.load(open(RCACHE))
    except Exception: cur = {}
    if key in cur: return cur[key], {"model": model, "s": 0, "cached": True}
    fn = call_gemini if model.startswith("gemini") else call_codex if model.startswith("gpt") else call_claude
    ans, meta = fn(p, model)
    if ans is not None:
        try: cur = json.load(open(RCACHE))
        except Exception: cur = {}
        cur[key] = ans; os.makedirs(os.path.dirname(RCACHE), exist_ok=True); tmp = RCACHE + f".{os.getpid()}.tmp"; json.dump(cur, open(tmp, "w"), indent=1); os.replace(tmp, RCACHE)
    return ans, meta

def review(p):
    ans, meta = _cached_call(p, a.l1); p["l1"] = {"answer": ans, **meta}; final = ans; p["level"] = 1
    if ans is None or ans.get("tool") in ("escalate", "wrong_place") or ans.get("confidence") == "low":
        if a.l2 not in ("none", ""):
            ans2, meta2 = _cached_call(p, a.l2); p["l2"] = {"answer": ans2, **meta2}; p["level"] = 2
            if ans2 is not None: final = ans2
    p["final"] = final; return p

if not a.no_model and props:
    with ThreadPoolExecutor(a.threads) as ex: list(ex.map(review, props))

cf = os.path.join(a.out, "closures.json"); closures = json.load(open(cf)) if os.path.exists(cf) else []; n_before = len(closures)
def axis_aligned(seg):
    ang = abs(math.degrees(math.atan2(seg[3] - seg[1], seg[2] - seg[0]))) % 90
    return min(ang, 90 - ang) <= 12
for p in props:
    f = p.get("final") or {}
    if f.get("tool") == "close_gap" and f.get("kind"): closures.append({"kind": f["kind"], "seg": p["seg"], "by": f"level {p['level']}", "width_m": p["width_m"]}); p["applied"] = "closed"
    elif f.get("tool") == "keep_open" and f.get("kind") == "open_passage":
        # an opening without a door splits two rooms only where it is a real doorway (a straight wall-to-wall line);
        # a slanted line across open floor is not a boundary anyone drew, so the spaces stay one region and keep both names
        if axis_aligned(p["seg"]): closures.append({"kind": "open_passage", "seg": p["seg"], "by": f"level {p['level']}", "width_m": p["width_m"]}); p["applied"] = "split at open passage"
        else: p["applied"] = "left as one open space (no drawn boundary)"
    else: p["applied"] = "nothing changed"
json.dump(closures, open(cf, "w"), indent=1)
# flags for the database: what the review could not settle
con = sqlite3.connect(os.path.join(a.out, "sheet.db")); con.execute("drop table if exists review_flag"); con.execute("create table review_flag(kind text, space_a text, space_b text, note text, room_id int)")
cc_ids = [int(v) for v in S["room_cc"]]
for an in anoms:
    ps = [p for p in props if an in p["anoms"]]
    note = an.get("note") or ("; ".join(f"gap {p['width_m']} m: {(p.get('final') or {}).get('tool')} {(p.get('final') or {}).get('kind') or ''} -> {p.get('applied')}" for p in ps) if ps else f"no connection narrower than {a.max_gap_m} m found: not split")
    con.execute("insert into review_flag values(?,?,?,?,?)", (an["type"], an["a"]["text"], an["b"]["text"] if an["b"] else "outside", note, (cc_ids.index(an["cc"]) + 1) if an["cc"] in cc_ids else None))
con.commit()
rep = {"tools": TOOLS, "anomalies": [{"type": an["type"], "a": an["a"]["text"], "b": an["b"]["text"] if an["b"] else None, "note": an.get("note")} for an in anoms],
       "gaps": [{kk: v for kk, v in p.items() if kk not in ("anoms", "mid")} | {"for": [(an["a"]["text"], an["b"]["text"] if an["b"] else "outside") for an in p["anoms"]]} for p in props]}
json.dump(rep, open(os.path.join(a.out, "review.json"), "w"), indent=1, default=str)
print(f"labels {len(labels)}, anomalies {len(anoms)}, proposed gaps {len(props)}, new closures {len(closures) - n_before} (total {len(closures)})")
for an in anoms: print("  anomaly:", an["type"], "|", an["a"]["text"], "|", an["b"]["text"] if an["b"] else "outside", "|", an.get("note", ""))
for p in props:
    f = p.get("final") or {}; l1 = p.get("l1", {}); l2 = p.get("l2")
    print(f"  gap {p['id']} [{p.get('applied')}]: {p['width_m']} m  ->  level {p.get('level')}: {f.get('tool')} {f.get('kind')} ({f.get('confidence')}) | {str(f.get('reason'))[:110]} | l1 {l1.get('s')}s ${l1.get('usd')}" + (f" | l2 {l2.get('model')} {l2.get('s')}s {l2.get('error', '')}" if l2 else ""))
