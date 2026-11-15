"""Document ingestion for the client chat app.
Accepts PDF (single or multi-page, vector or scanned), PNG/JPG scans or photos, DXF, and IFC files
into a strictly isolated per-session workspace folder under cache/chat_sessions/<session_id>/.
Never trusts client file names; never reads paths outside SESSIONS_DIR."""
import os, re, io, json, time, uuid, pickle, hashlib, threading, subprocess
from PIL import Image, ImageDraw

from . import ask as ASK, execute as EX, audit as AUDIT, render as RENDER, vocab as VOCAB

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.realpath(os.path.join(HERE, ".."))
CACHE_DIR = os.path.realpath(os.path.join(ROOT_DIR, "cache"))
SESSIONS_DIR = os.path.realpath(os.path.join(CACHE_DIR, "chat_sessions"))
DET_YOLO_DIR = os.path.realpath(os.path.join(CACHE_DIR, "detections_yolo"))
OCR_UPLOAD_CACHE = os.path.realpath(os.path.join(CACHE_DIR, "ocr_chat_uploads.json"))

Image.MAX_IMAGE_PIXELS = 400_000_000      # full-size sheets (A0 at 300 dpi is ~140 MP) are legitimate uploads
PLAN_RENDER_PX = int(os.environ.get("BP_PLAN_RENDER_PX", "20000"))   # long side of a PDF plan page rendered for the detector

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(DET_YOLO_DIR, exist_ok=True)

MAX_UPLOAD_BYTES = int(float(os.environ.get("BP_MAX_UPLOAD_MB", "100")) * 1024 * 1024)
MAX_PDF_THUMBS = 60

ID_RX = re.compile(r"^[a-zA-Z0-9_-]{4,64}$")

# Default tuned thresholds from held-out validation of the trained floor-plan detector (hpc_det/predict.py)
DEFAULT_THRESHOLDS = {
    "door": 0.64, "window": 0.54, "toilet": 0.50, "sink": 0.34, "shower": 0.30,
    "bathtub": 0.76, "shower_cab": 0.36, "sauna_bench": 0.36, "bedroom": 0.56,
    "kitchen": 0.40, "bathroom": 0.36, "wc_room": 0.78, "sauna_room": 0.64,
    "balcony": 0.28, "floor": 0.36,
}
DET_NAMES = [
    "door", "window", "toilet", "sink", "shower", "bathtub", "shower_cab",
    "sauna_bench", "bedroom", "kitchen", "bathroom", "wc_room", "sauna_room",
    "balcony", "floor",
]


class IngestError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Workspace & Path Safety
# ---------------------------------------------------------------------------
def clean_id(val: str | None, prefix: str = "s") -> str:
    if val and isinstance(val, str) and ID_RX.match(val):
        return val
    return f"{prefix}_{uuid.uuid4().hex[:14]}"


def ensure_inside_sessions(path: str) -> str:
    real = os.path.realpath(path)
    if os.path.commonpath([SESSIONS_DIR, real]) != SESSIONS_DIR:
        raise IngestError("Access outside session workspace is forbidden.", 403)
    return real


def session_dir(session_id: str) -> str:
    sid = clean_id(session_id, "s")
    d = ensure_inside_sessions(os.path.join(SESSIONS_DIR, sid))
    os.makedirs(d, exist_ok=True)
    return d


def sanitize_display_name(filename: str | None) -> str:
    raw = os.path.basename(str(filename or "drawing").replace(chr(92), "/"))
    cleaned = "".join(ch for ch in raw if ord(ch) >= 32 and ord(ch) != 127).strip()
    return (cleaned or "drawing")[:120]


PNG_SIG = bytes.fromhex("89504e470d0a1a0a")
JPG_SIG = bytes.fromhex("ffd8ff")


def _dwg2dxf():
    """path of LibreDWG's dwg2dxf if it is installed (BP_DWG2DXF, PATH, or ~/.local/libredwg/bin), else None"""
    import shutil
    for c in (os.environ.get("BP_DWG2DXF"), shutil.which("dwg2dxf"), os.path.expanduser("~/.local/libredwg/bin/dwg2dxf")):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _normalise_upload(data: bytes, kind: str, ext: str, work_dir: str) -> tuple[bytes, str, str | None]:
    """Formats the extractors do not read directly are converted once, losslessly, at upload:
       WEBP / TIFF / BMP / GIF -> PNG at the same pixel size;  SVG -> PNG rendered at high resolution;  DWG -> DXF (LibreDWG).
    Returns (data, extension, note for the user or None)."""
    if kind == "raster" and ext in (".webp", ".tif", ".bmp", ".gif"):
        try:
            im = Image.open(io.BytesIO(data)); im.seek(0); im = im.convert("RGB")
            b = io.BytesIO(); im.save(b, "PNG", compress_level=3)
            return b.getvalue(), ".png", f"{ext[1:].upper()} image converted to PNG at the same size ({im.width} × {im.height} px), no quality loss"
        except Exception as e:
            raise IngestError(f"Could not read this {ext[1:].upper()} image: {e}", 400)
    if kind == "raster" and ext == ".svg":
        try:
            import pymupdf
            doc = pymupdf.open(stream=data, filetype="svg"); pg = doc[0]
            z = max(1.0, min(PLAN_RENDER_PX / max(pg.rect.width, pg.rect.height, 1), 12.0))
            if max(pg.rect.width, pg.rect.height) * z > 9000: z = 9000 / max(pg.rect.width, pg.rect.height)
            pm = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
            im = Image.frombytes("RGB", (pm.width, pm.height), pm.samples); b = io.BytesIO(); im.save(b, "PNG", compress_level=3)
            return b.getvalue(), ".png", f"SVG drawing rendered to an image of {pm.width} × {pm.height} px and read like a scan"
        except Exception as e:
            raise IngestError(f"Could not render this SVG: {type(e).__name__}: {e}", 400)
    if kind == "dxf" and ext == ".dwg":
        exe = _dwg2dxf(); src = os.path.join(work_dir, "_in.dwg"); dst = os.path.join(work_dir, "_in.dxf")
        open(src, "wb").write(data)
        try:
            r = subprocess.run([exe, "-y", "-o", dst, src], capture_output=True, text=True, timeout=300)
        except Exception as e:
            raise IngestError(f"DWG conversion failed: {type(e).__name__}: {e}", 400)
        if not os.path.exists(dst) or os.path.getsize(dst) < 200:
            raise IngestError("DWG conversion produced no drawing (LibreDWG: " + (r.stderr or r.stdout or "no message").strip()[-200:] + ")", 400)
        out = open(dst, "rb").read()
        for f in (src, dst):
            try: os.remove(f)
            except OSError: pass
        return out, ".dxf", "DWG converted to DXF with LibreDWG"
    return data, ext, None


def _render_dxf_png(dxf_path: str, png_path: str, long_px: int = 9000) -> tuple[int, int]:
    """Draw the model space of a DXF in black on white, fitted to an A1 page at ~270 dpi."""
    try:
        import ezdxf
        from ezdxf import recover
        from ezdxf.addons.drawing import Frontend, RenderContext, layout, config
        from ezdxf.addons.drawing.pymupdf import PyMuPdfBackend
        try: doc = ezdxf.readfile(dxf_path)
        except Exception: doc, _aud = recover.readfile(dxf_path)
        msp = doc.modelspace()
        if len(msp) == 0: raise IngestError("This CAD file has an empty model space: there is nothing to draw.", 400)
        backend = PyMuPdfBackend()
        cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE, color_policy=config.ColorPolicy.BLACK)
        Frontend(RenderContext(doc), backend, config=cfg).draw_layout(msp)
        page = layout.Page(841, 594, layout.Units.mm, margins=layout.Margins.all(10))
        png = backend.get_pixmap_bytes(page, fmt="png", settings=layout.Settings(fit_page=True), dpi=round(long_px / 841 * 25.4))
        open(png_path, "wb").write(png)
        with Image.open(png_path) as im: return im.size
    except IngestError: raise
    except Exception as e:
        raise IngestError(f"Could not draw this CAD file: {type(e).__name__}: {str(e)[:160]}", 400)


def sniff_format(data: bytes, display_name: str) -> tuple[str, str]:
    """Inspect magic bytes / header structure. Returns (kind, safe_3_or_4_char_ext) or raises IngestError.
    Note: JPEG always uses '.jpg' (3-char ext) so bp.x_raster's name[:-4] stem slicing is exact."""
    if not data:
        raise IngestError("The uploaded file is empty (0 bytes).", 400)
    if len(data) > MAX_UPLOAD_BYTES:
        mb = len(data) / (1024 * 1024)
        lim = MAX_UPLOAD_BYTES / (1024 * 1024)
        raise IngestError(f"File is {mb:.1f} MB, which exceeds the {lim:.0f} MB upload limit.", 413)

    ext_hint = os.path.splitext(display_name.lower())[1]
    head = data[:8192]
    head_large = data[:262144]

    if data.startswith(b"%PDF-"):
        return "pdf", ".pdf"
    if data.startswith(PNG_SIG):
        return "raster", ".png"
    if data.startswith(JPG_SIG):
        return "raster", ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "raster", ".webp"                      # converted to PNG by _normalise_upload
    if data[:4] in (b"II*\x00", b"MM\x00*") or data.startswith(b"BM") or data.startswith(b"GIF8"):
        return "raster", {b"II*\x00": ".tif", b"MM\x00*": ".tif"}.get(data[:4], ".bmp" if data.startswith(b"BM") else ".gif")
    if (b"<svg" in head_large.lower()) and (ext_hint == ".svg" or head.lstrip().startswith((b"<?xml", b"<svg", b"<!DOCTYPE svg", b"<!--"))):
        return "raster", ".svg"                       # rendered to PNG by _normalise_upload
    if data.startswith(b"AC10"):
        if not _dwg2dxf():
            raise IngestError(
                "DWG files need the LibreDWG converter (dwg2dxf), which is not installed on this server. Export the sheet as DXF, PDF, IFC or an image.",
                415,
            )
        return "dxf", ".dwg"                          # converted to DXF by _normalise_upload
    if b"ISO-10303-21" in head and (b"IFC" in head.upper() or ext_hint == ".ifc"):
        return "ifc", ".ifc"
    if data.startswith(b"AutoCAD Binary DXF") or (
        b"SECTION" in head and (b"ENTITIES" in head_large or b"HEADER" in head)
    ):
        return "dxf", ".dxf"

    # Check if PIL can open other common mobile photo formats? Only PNG/JPG are officially supported; reject others clearly.
    supported = "PDF, image (PNG, JPG, WEBP, TIFF, BMP, SVG), DXF, DWG, or IFC"
    if ext_hint in (".rvt", ".skp", ".zip", ".docx", ".xlsx"):
        raise IngestError(
            f"Unsupported file format '{ext_hint}'. Please upload a {supported}.",
            415,
        )
    raise IngestError(
        f"Could not recognise file content as a supported drawing format. Supported formats: {supported}.",
        415,
    )


# ---------------------------------------------------------------------------
# Live Raster Detector (YOLO26m subprocess) + RapidOCR
# ---------------------------------------------------------------------------
_OCR_LOCK = threading.Lock()
_OCR_ENG = None
_OCR_LOADED = False


def _ensure_upload_ocr_loaded():
    global _OCR_LOADED
    from . import x_raster as XR
    with _OCR_LOCK:
        if XR._ocr is None:
            try:
                XR._ocr = json.load(open(XR.OCR))
            except Exception:
                XR._ocr = {}
        if not _OCR_LOADED:
            if os.path.exists(OCR_UPLOAD_CACHE):
                try:
                    XR._ocr.update(json.load(open(OCR_UPLOAD_CACHE)))
                except Exception:
                    pass
            _OCR_LOADED = True


def _cores() -> int:
    try: return max(1, len(os.sched_getaffinity(0)))
    except Exception: return max(1, os.cpu_count() or 1)


def _new_ocr_engine(RapidOCR, threads: int | None = None):
    """RapidOCR with its thread pool capped to the cores this process is allowed to use (a 4-core job on a 64-core
    node otherwise starts ~100 threads that fight each other)."""
    try:
        n = threads or max(1, min(16, _cores()))
        return RapidOCR(intra_op_num_threads=n, inter_op_num_threads=1)
    except Exception:
        return RapidOCR()


# A big sheet is read as overlapping tiles. One engine reads them one after another and uses a handful of cores however many
# the machine has; so several engines read tiles at the same time, each with its own share of the cores. The tiles and the
# order in which their text is merged are unchanged, so the result is the same as reading them in sequence.
_OCR_POOL: list = []
OCR_PER_ENGINE = int(os.environ.get("BP_OCR_THREADS_PER_ENGINE", "5"))


def _ocr_pool(RapidOCR, want: int) -> list:
    n = max(1, min(want, int(os.environ.get("BP_OCR_ENGINES", "0")) or max(1, _cores() // OCR_PER_ENGINE), 8))
    with _OCR_LOCK:
        while len(_OCR_POOL) < n: _OCR_POOL.append(_new_ocr_engine(RapidOCR, max(2, min(8, _cores() // n))))
        return _OCR_POOL[:n]


def _run_ocr_on_image(img_path: str) -> list[dict]:
    global _OCR_ENG
    _ensure_upload_ocr_loaded()
    from . import x_raster as XR
    bname = os.path.basename(img_path)
    with _OCR_LOCK:
        if bname in (XR._ocr or {}):
            return XR._ocr[bname].get("boxes", [])
    try:
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
        with _OCR_LOCK:
            if _OCR_ENG is None:
                _OCR_ENG = _new_ocr_engine(RapidOCR)
            eng = _OCR_ENG
        im = Image.open(img_path).convert("RGB")
        w, h = im.size
        s = 1.0
        if max(w, h) < 1400:
            s = 1400 / max(w, h)
        elif max(w, h) > 2600:
            # big sheet: read at the scale the detector chose (plus a margin for small lettering), in overlapping tiles;
            # squeezing a whole A0/A1 sheet into 2600 px makes the room names unreadable
            ds = None
            try:
                ds = (json.load(open(os.path.join(DET_YOLO_DIR, bname[:-4] + ".json"))).get("tiling") or {}).get("used_scale")
            except Exception:
                ds = None
            s = max(2600 / max(w, h), min(1.0, (ds or 0) * 1.3))
        if s != 1.0:
            im = im.resize((int(w * s), int(h * s)), Image.LANCZOS)
        arr = np.array(im); hh, ww = arr.shape[:2]; TS, OVL = 2400, 240
        def _g(n):
            xs = list(range(0, max(n - TS, 0) + 1, TS - OVL))
            if xs[-1] + TS < n: xs.append(n - TS)
            return xs
        boxes = []
        tiles = [(tx, ty) for ty in _g(hh) for tx in _g(ww)]
        if len(tiles) > 1:
            from concurrent.futures import ThreadPoolExecutor
            import queue as _q
            pool = _ocr_pool(RapidOCR, len(tiles)); free = _q.Queue()
            for e_ in pool: free.put(e_)
            def _read(t_):
                e_ = free.get()
                try: return e_(arr[t_[1]:t_[1] + TS, t_[0]:t_[0] + TS])[0]
                finally: free.put(e_)
            with ThreadPoolExecutor(len(pool)) as ex: results = list(ex.map(_read, tiles))
        else:
            results = [eng(arr[t_[1]:t_[1] + TS, t_[0]:t_[0] + TS])[0] for t_ in tiles]
        for (tx, ty), res in zip(tiles, results):
            if True:
                for b, t, c in (res or []):
                    q = [[round((float(x) + tx) / s, 1), round((float(y) + ty) / s, 1)] for x, y in b]
                    cx = sum(p[0] for p in q) / 4; cy = sum(p[1] for p in q) / 4
                    if any(o["text"] == t and abs(sum(p[0] for p in o["box"]) / 4 - cx) < 20 / s and abs(sum(p[1] for p in o["box"]) / 4 - cy) < 20 / s for o in boxes):
                        continue          # same text read again in the overlap of two tiles
                    boxes.append({"text": t, "conf": float(c), "box": q})
        entry = {"size": [w, h], "scale": s, "boxes": boxes}
        with _OCR_LOCK:
            XR._ocr[bname] = entry
            saved = {}
            if os.path.exists(OCR_UPLOAD_CACHE):
                try:
                    saved = json.load(open(OCR_UPLOAD_CACHE))
                except Exception:
                    saved = {}
            saved[bname] = entry
            tmp = f"{OCR_UPLOAD_CACHE}.{os.getpid()}.tmp"
            json.dump(saved, open(tmp, "w"))
            os.replace(tmp, OCR_UPLOAD_CACHE)
        return boxes
    except Exception:
        return []


_DET_LOCK = threading.Lock()
_DET_PROC = None


def _fix_weights() -> str:
    """optional fixture specialist (toilet, sink, shower, bathtub at 2048 px); empty string when not installed"""
    f = os.environ.get("BP_DET_FIX_WEIGHTS") or ""
    return f if f and os.path.exists(f) else ""


def _detector_paths() -> tuple[str | None, str | None, dict]:
    det_py = os.environ.get("BP_DET_PYTHON") or "/data/hackathon/env/det/bin/python"
    weights = os.environ.get("BP_DET_WEIGHTS") or "/data/hackathon/cc5k/runs/detect/runs/m1536/weights/best.pt"
    rep_path = os.environ.get("BP_DET_REPORT") or "/data/hackathon/cc5k/det_out/_report.json"
    th = dict(DEFAULT_THRESHOLDS)
    if os.path.exists(rep_path):
        try:
            th.update(json.load(open(rep_path)).get("thresholds", {}))
        except Exception:
            pass
    if not (os.path.exists(det_py) and os.path.exists(weights)):
        return None, None, th
    return det_py, weights, th


_DET_WORKER_CODE = """
# Detector worker. Large sheets are NOT squeezed into one 1536 px pass (thin lines vanish and symbols become a few
# pixels wide). Pass 1 runs on the whole sheet; the median door size it finds tells how much smaller the symbols are
# than on the training plans (median door 97 px at 1536 px input). The sheet is then rescaled so doors are ~DOOR_PX
# and read in overlapping 1536 px tiles; boxes cut by an inner tile edge are dropped (the neighbour tile has them whole).
import sys, json, os
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", str(2 ** 40))
import numpy as np, torch, torchvision, cv2
from ultralytics import YOLO

weights = sys.argv[1]
TH = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
fix_weights = sys.argv[4] if len(sys.argv) > 4 else ""
FIX_SZ = 2048; FIX_CLS = ("toilet", "sink", "shower", "bathtub")
gpu = torch.cuda.is_available()
dev = 0 if gpu else "cpu"
if not gpu:
    try: torch.set_num_threads(max(1, min(16, len(os.sched_getaffinity(0)))))
    except Exception: pass
T, OV, DOOR_PX, DOOR_MIN = 1536, 384, 80.0, 55.0
MAX_TILES = int(os.environ.get("BP_DET_MAX_TILES", "96" if gpu else "40"))
model = YOLO(weights)
names = [model.names[i] for i in range(len(model.names))]        # class names come from the weights file, not from a fixed list
fix = YOLO(fix_weights) if fix_weights and os.path.exists(fix_weights) else None    # optional fixture specialist (toilet, sink, shower, bathtub)
from concurrent.futures import ThreadPoolExecutor
POOL = ThreadPoolExecutor(2)
try:        # first call of each model pays for start-up (CUDA, layer fusing): pay it when the server starts, not on the first upload
    _blank = np.full((T, T, 3), 255, np.uint8)
    model.predict([_blank], imgsz=T, conf=0.05, verbose=False, device=dev)
    if fix is not None and gpu: fix.predict([_blank], imgsz=FIX_SZ, conf=0.05, verbose=False, device=dev)
except Exception: pass
print(json.dumps({"ready": True, "device": str(dev)}), flush=True)

def grid(n):
    xs = list(range(0, max(n - T, 0) + 1, T - OV))
    if xs[-1] + T < n: xs.append(n - T)
    return xs

def n_tiles(w, h): return len(grid(w)) * len(grid(h))

def run(im0, scale, mdl=None, imgsz=None):
    mdl = mdl or model; imgsz = imgsz or T; nm = [mdl.names[i] for i in range(len(mdl.names))]
    H0, W0 = im0.shape[:2]
    w, h = max(1, round(W0 * scale)), max(1, round(H0 * scale))
    im = cv2.resize(im0, (w, h), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC) if (w, h) != (W0, H0) else im0
    xs, ys = grid(w), grid(h)
    tiles = [(x, y, im[y:min(y + T, h), x:min(x + T, w)]) for y in ys for x in xs]
    B, S, C = [], [], []
    bs = 16 if gpu else 1
    for i in range(0, len(tiles), bs):
        rs = mdl.predict([t[2] for t in tiles[i:i + bs]], imgsz=imgsz, conf=0.05, iou=0.6, max_det=800, verbose=False, device=dev)
        for (x, y, tim), r in zip(tiles[i:i + bs], rs):
            if len(r.boxes) == 0: continue
            b = r.boxes.xyxy.cpu().numpy().astype(float); th, tw = tim.shape[:2]
            keep = np.ones(len(b), bool)
            if len(tiles) > 1:      # cut by an inner tile edge -> the overlapping neighbour sees it whole
                keep &= ~((b[:, 0] < 3) & (x > 0)) & ~((b[:, 1] < 3) & (y > 0))
                keep &= ~((b[:, 2] > tw - 3) & (x + tw < w)) & ~((b[:, 3] > th - 3) & (y + th < h))
            b[:, [0, 2]] += x; b[:, [1, 3]] += y
            B.append(b[keep]); S.append(r.boxes.conf.cpu().numpy()[keep]); C.append(r.boxes.cls.cpu().numpy()[keep])
    if not B or not sum(len(b) for b in B): return [], len(tiles)
    B, S, C = np.concatenate(B), np.concatenate(S), np.concatenate(C)
    if len(tiles) > 1:
        k = torchvision.ops.batched_nms(torch.tensor(B, dtype=torch.float32), torch.tensor(S, dtype=torch.float32), torch.tensor(C).long(), 0.4).numpy()
        B, S, C = B[k], S[k], C[k]
    return [(nm[int(c)], float(s), [float(v) / scale for v in b]) for c, s, b in zip(C, S, B) if 0 <= int(c) < len(nm)], len(tiles)

def fuse_fixtures(dets, im0, scale, W, H):
    # second opinion on fixtures from the specialist, fused box by box (weighted boxes fusion, the setting that was evaluated)
    if fix is None: return dets, None
    try:
        from ensemble_boxes import weighted_boxes_fusion
        sp, nt = run(im0, scale, fix, FIX_SZ)
        A = [d for d in dets if d[0] in FIX_CLS]; Bx = [d for d in sp if d[0] in FIX_CLS]; keep = [d for d in dets if d[0] not in FIX_CLS]
        cl = sorted({d[0] for d in A + Bx})
        if not cl: return dets, {"tiles": nt, "fused": 0}
        def pack(L): return ([[max(0, b[0] / W), max(0, b[1] / H), min(1, b[2] / W), min(1, b[3] / H)] for _, _, b in L] or np.zeros((0, 4)), [s for _, s, _ in L], [cl.index(n) for n, _, _ in L])
        (b1, s1, l1), (b2, s2, l2) = pack(A), pack(Bx)
        Bf, Sf, Lf = weighted_boxes_fusion([b1, b2], [s1, s2], [l1, l2], iou_thr=0.55, skip_box_thr=0.05, conf_type="avg")
        fused = [(cl[int(l)], float(s), [float(b[0] * W), float(b[1] * H), float(b[2] * W), float(b[3] * H)]) for b, s, l in zip(Bf, Sf, Lf)]
        return keep + fused, {"tiles": nt, "fused": len(fused)}
    except Exception as e:
        return dets, {"error": str(e)[:120]}

def door_px(dets, scale):
    d = [max(b[2] - b[0], b[3] - b[1]) * scale for c, s, b in dets if c == "door" and s >= 0.5]
    return (float(np.median(d)) if len(d) >= 3 else None), len(d)

def fit(W, H, s, floor):
    while n_tiles(round(W * s), round(H * s)) > MAX_TILES and s > floor * 1.15: s *= 0.92
    return s

def kept(dets):
    return [(c, s, b) for c, s, b in dets if s >= TH.get(c, 0.25)]

def crosscheck(da, db, sa, sb):
    # Second reading of the same sheet at another working scale. A counted symbol is 'confirmed' when the other
    # reading has a counted symbol of the same class on the same spot (box centre inside the other box).
    A, Bm = kept(da), kept(db); out = {}
    for cls in sorted({n for n, _, _ in A} | {n for n, _, _ in Bm}):
        a = [b for n, _, b in A if n == cls]; bb = [b for n, _, b in Bm if n == cls]; used = set(); both = 0; only = []
        for box in a:
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            j = next((j for j, q in enumerate(bb) if j not in used and q[0] <= cx <= q[2] and q[1] <= cy <= q[3]), None)
            if j is None:
                j = next((j for j, q in enumerate(bb) if j not in used and box[0] <= (q[0] + q[2]) / 2 <= box[2] and box[1] <= (q[1] + q[3]) / 2 <= box[3]), None)
            if j is None: only.append([round(v, 1) for v in box])
            else: used.add(j); both += 1
        out[cls] = {"used": len(a), "second": len(bb), "confirmed": both, "only_used": only,
                    "only_second": [[round(v, 1) for v in q] for j, q in enumerate(bb) if j not in used]}
    return {"scale_used": round(sa, 4), "scale_second": round(sb, 4), "classes": out}

def detect(path):
    im0 = cv2.imread(path, cv2.IMREAD_COLOR)
    if im0 is None: raise ValueError("image could not be decoded")
    H, W = im0.shape[:2]; L = max(W, H)
    s1 = min(1.0, T / L)
    dets, nt = run(im0, s1); info = {"passes": [{"scale": round(s1, 4), "tiles": nt}]}; used = s1; cc = None; early_fx = None
    if L > T:
        med, nd = door_px(dets, s1)
        info["pass1_median_door_px"] = med
        if med is None: s2 = min(1.0, 3072 / L)                 # too few doors to measure: try a 2x closer look
        elif med < DOOR_MIN: s2 = min(1.0, s1 * DOOR_PX / med)
        else: s2 = None
        if s2 and s2 > s1 * 1.15:
            s2 = fit(W, H, s2, s1)
            d2, nt2 = run(im0, s2); info["passes"].append({"scale": round(s2, 4), "tiles": nt2})
            m2, nd2 = door_px(d2, s2)
            if med is None and m2 is not None and m2 < DOOR_MIN and s2 < 1.0:     # second look measured the doors: correct once more
                s3 = fit(W, H, min(1.0, s2 * DOOR_PX / m2), s2)
                if s3 > s2 * 1.15:
                    d2, nt2 = run(im0, s3); info["passes"].append({"scale": round(s3, 4), "tiles": nt2}); m2, nd2 = door_px(d2, s3); s2 = s3
            # keep the closer look unless it found nothing where the whole-sheet pass found symbols
            if d2 and (nd2 >= nd or med is None):
                dets = d2; used = s2; info["median_door_px"] = m2
                sb = s2 * 0.72                                   # independent second reading: doors ~58 px instead of ~80
                if sb > s1 * 1.15:
                    # the cross-check reading (main model) and the fixture specialist (another model) do not depend on each
                    # other: on a GPU they run at the same time
                    fut = POOL.submit(fuse_fixtures, dets, im0, used, W, H) if gpu and fix is not None else None
                    db, ntb = run(im0, sb); info["passes"].append({"scale": round(sb, 4), "tiles": ntb, "role": "cross-check"})
                    cc = crosscheck(dets, db, s2, sb)
                    if fut is not None: early_fx = fut.result()
    info["used_scale"] = round(used, 4)
    dets, fx = early_fx if early_fx is not None else fuse_fixtures(dets, im0, used, W, H)
    if fx is not None: info["fixture_specialist"] = fx
    boxes = [[c, round(s, 3)] + [round(v, 1) for v in b] for c, s, b in dets]
    return {"ok": True, "size": [W, H], "boxes": boxes, "tiling": info, "crosscheck": cc}

for line in sys.stdin:
    p = line.strip()
    if not p:
        continue
    try:
        print(json.dumps(detect(p)), flush=True)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}), flush=True)
"""


def _run_yolo_detector(img_path: str) -> tuple[bool, str | None]:
    """Run trained floor-plan detector on img_path and write cache/detections_yolo/<stem>.json
    so bp.x_raster.extract() picks it up natively with confidence thresholds & borderline flags."""
    global _DET_PROC
    stem = os.path.basename(img_path)[:-4]
    out_json = os.path.join(DET_YOLO_DIR, stem + ".json")
    if os.path.exists(out_json):
        return True, None

    det_py, weights, thresholds = _detector_paths()
    if not det_py or not weights:
        return False, "local symbol detector weights are not installed on this host"

    with _DET_LOCK:
        if os.path.exists(out_json):
            return True, None
        for attempt in range(2):
            try:
                if _DET_PROC is None or _DET_PROC.poll() is not None:
                    _DET_PROC = subprocess.Popen(
                        [det_py, "-u", "-c", _DET_WORKER_CODE, weights, json.dumps(DET_NAMES), json.dumps(thresholds), _fix_weights()],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                    )
                    ready = _DET_PROC.stdout.readline()
                    if not ready or '"ready"' not in ready:
                        raise RuntimeError("detector worker did not start")
                _DET_PROC.stdin.write(os.path.abspath(img_path) + chr(10))
                _DET_PROC.stdin.flush()
                resp_line = _DET_PROC.stdout.readline()
                if not resp_line:
                    raise RuntimeError("detector worker closed pipe")
                resp = json.loads(resp_line)
                if not resp.get("ok"):
                    return False, resp.get("error", "detector failed")
                payload = {
                    "size": resp["size"],
                    "split": "upload",
                    "model": os.environ.get("BP_DET_NAME") or ("yolo26m-m1536" if not _fix_weights() else os.path.basename(weights)[:-3] + " + " + os.path.basename(_fix_weights())[:-3]),
                    "thresholds": thresholds,
                    "boxes": resp["boxes"],
                    "tiling": resp.get("tiling"),
                    "crosscheck": resp.get("crosscheck"),
                }
                tmp = f"{out_json}.{os.getpid()}.tmp"
                json.dump(payload, open(tmp, "w"))
                os.replace(tmp, out_json)
                return True, None
            except Exception as e:
                if _DET_PROC is not None:
                    try:
                        _DET_PROC.kill()
                    except Exception:
                        pass
                    _DET_PROC = None
                if attempt == 1:
                    return False, f"detector failed: {e}"
    return False, "detector unavailable"


# What the server is doing for a session right now, read by the page's loading bar (GET /api/chat/studio/progress).
PROGRESS: dict = {}
_MANIFEST_LOCK = threading.Lock()
_CUR = threading.local()


def stage(text: str, pct: int):
    sid = getattr(_CUR, "sid", None); now = time.time()
    tl = getattr(_CUR, "timeline", None)
    if tl is not None: tl.append((text, now))
    if sid: PROGRESS[sid] = {"text": text, "pct": int(pct), "t": round(now, 2)}


def _timings() -> list:
    """seconds spent in each step of the current upload: [[step, seconds], ...]"""
    tl = list(getattr(_CUR, "timeline", None) or []); now = time.time()
    return [[t, round((tl[i + 1][1] if i + 1 < len(tl) else now) - t0, 2)] for i, (t, t0) in enumerate(tl)]


def _prepare_raster(img_path: str) -> dict:
    """detector first (the OCR working scale and the door/window boxes come from it); then OCR and wall tracing run at
    the same time, because tracing needs no text. The tracer runs inside the server process (no second decode of a
    100+ megapixel image by a child process, no start-up cost)."""
    if _demo_sidecar(img_path):        # one of the SAMPLE files of demo/: its analysis is already stored, nothing is recomputed
        stage("Loading the stored analysis of this sample drawing", 60)
        return {"ocr_count": 0, "det_ok": True, "det_err": None, "room_geometry": "sample"}
    stage("Finding doors, windows and fixtures", 15)
    det_ok, det_err = _run_yolo_detector(img_path)
    holder = _trace_async(img_path) if det_ok else None
    stage("Reading the text" + (" and tracing the walls" if holder and holder.get("thread") else ""), 40)
    ocr_boxes = _run_ocr_on_image(img_path)
    if holder: stage("Building rooms from the walls", 70)
    rooms = _finish_sheet_rooms(img_path, holder, ocr_boxes) if holder else None
    stage("Preparing the drawing", 90)
    return {"ocr_count": len(ocr_boxes), "det_ok": det_ok, "det_err": det_err, "room_geometry": rooms}


SHEET_ROOMS_DIR = os.path.realpath(os.path.join(CACHE_DIR, "sheet_rooms"))
SHEET_ROOMS_MIN_PX = int(os.environ.get("BP_SHEET_ROOMS_MIN_PX", "3000"))
SHEET_WORK = 0.5


DEMO_DIR = os.path.realpath(os.path.join(ROOT_DIR, "demo"))


def _demo_sidecar(img_path: str):
    """The sample plans of demo/ (made by demo/make_school_demo.py) are recognised by their exact content. Their room
    database and detections are copied into the caches, together with the planted data (fire symbols, seats), which
    bp/x_raster.py labels as sample data. Returns the file's entry or None."""
    try:
        side = json.load(open(os.path.join(DEMO_DIR, "school_demo.json")))
        entry = (side.get("files") or {}).get(hashlib.sha1(open(img_path, "rb").read()).hexdigest())
        if not entry: return None
        import shutil
        stem = os.path.basename(img_path)[:-4]; out = os.path.join(SHEET_ROOMS_DIR, stem); os.makedirs(out, exist_ok=True)
        shutil.copy(os.path.join(DEMO_DIR, side["detections"]), os.path.join(DET_YOLO_DIR, stem + ".json"))
        shutil.copy(os.path.join(DEMO_DIR, side["sheet_db"]), os.path.join(out, "sheet.db"))
        json.dump({**{k_: v_ for k_, v_ in side.items() if k_ != "files"}, "layer": entry.get("layer"), "file": entry.get("name")}, open(os.path.join(out, "demo.json"), "w"))
        return entry
    except Exception:
        return None


def _trace_async(img_path: str):
    """start phase A of sheet_rooms (walls, openings, enclosed regions) in a thread; returns a holder or None for small images"""
    try:
        with Image.open(img_path) as im:
            w, h = im.size
        if max(w, h) < SHEET_ROOMS_MIN_PX:
            return None
        stem = os.path.basename(img_path)[:-4]
        out = os.path.join(SHEET_ROOMS_DIR, stem)
        if os.path.exists(os.path.join(out, "sheet.db")):
            return {"cached": True}
        import sys as _sys
        if ROOT_DIR not in _sys.path: _sys.path.insert(0, ROOT_DIR)
        import sheet_rooms as SR
        holder = {"out": out, "stem": stem, "SR": SR}
        def work():
            try: holder["A"] = SR.trace(img_path, os.path.join(DET_YOLO_DIR, stem + ".json"), SHEET_WORK)
            except Exception as e: holder["error"] = f"{type(e).__name__}: {e}"
        t = threading.Thread(target=work, daemon=True); t.start(); holder["thread"] = t
        return holder
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _finish_sheet_rooms(img_path: str, holder: dict, ocr_boxes: list[dict]):
    """phase B: scale, room outlines, names, quick flags, database; then the model review in the background"""
    if holder.get("cached"): return "cached"
    if holder.get("thread"): holder["thread"].join(timeout=600)
    if holder.get("error") or "A" not in holder: return "failed: " + str(holder.get("error") or "tracing did not finish")
    try:
        k = SHEET_WORK; out = holder["out"]; ocr = []
        for b in ocr_boxes:
            q = b["box"]
            ocr.append({"text": b["text"].strip(), "conf": b["conf"], "x": sum(pt[0] for pt in q) / 4 * k, "y": sum(pt[1] for pt in q) / 4 * k, "h": abs(q[2][1] - q[0][1]) * k})
        os.makedirs(out, exist_ok=True); json.dump(ocr, open(os.path.join(out, "ocr_in.json"), "w"))
        summary = holder["SR"].finish(holder.pop("A"), ocr, out)
        open(os.path.join(out, "log.txt"), "w").write(json.dumps(summary))
        rounds = 0 if os.environ.get("BP_NO_LLM") else int(os.environ.get("BP_SHEET_REVIEW_ROUNDS", "3"))
        if rounds > 0:
            import sys as _sys
            base = [_sys.executable, os.path.join(ROOT_DIR, "sheet_rooms.py"), img_path, os.path.join(DET_YOLO_DIR, holder["stem"] + ".json"), out, "--work", str(k), "--ocr-cache", os.path.join(out, "ocr_in.json")]
            threading.Thread(target=_review_later, args=(img_path, out, base, rounds), daemon=True).start()
        return "ok"
    except Exception as e:
        return f"failed: {type(e).__name__}: {e}"


def warm():
    """load the detector worker and the OCR engine when the server starts, so the first upload does not pay for it"""
    try:
        det_py, weights, thresholds = _detector_paths()
        global _DET_PROC, _OCR_ENG
        if det_py and weights:
            with _DET_LOCK:
                if _DET_PROC is None or _DET_PROC.poll() is not None:
                    _DET_PROC = subprocess.Popen([det_py, "-u", "-c", _DET_WORKER_CODE, weights, json.dumps(DET_NAMES), json.dumps(thresholds), _fix_weights()],
                                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                    _DET_PROC.stdout.readline()
        from rapidocr_onnxruntime import RapidOCR
        with _OCR_LOCK:
            if _OCR_ENG is None: _OCR_ENG = _new_ocr_engine(RapidOCR)
        import sys as _sys
        if ROOT_DIR not in _sys.path: _sys.path.insert(0, ROOT_DIR)
        import sheet_rooms  # noqa: F401  (cv2 and numpy loaded once)
    except Exception:
        pass


def _review_later(img_path: str, out: str, base: list, rounds: int):
    """Model review of the spots where tracing looks wrong: a model sees a full-resolution crop of each spot and picks a
    tool (close_gap / keep_open / wrong_place / escalate); code re-traces. Answers are cached, so a sheet gets the same
    decisions every time."""
    import sys as _sys
    log = ""; changed = False
    try:
        for _round in range(rounds):
            r2 = subprocess.run([_sys.executable, os.path.join(ROOT_DIR, "sheet_review.py"), img_path, out, "--l1", "auto"], capture_output=True, text=True, timeout=600)
            log += f"== round {_round + 1}\n" + r2.stdout[-2500:] + r2.stderr[-800:]
            if r2.returncode != 0 or "new closures 0" in r2.stdout: break
            subprocess.run(base + ["--closures", os.path.join(out, "closures.json")], capture_output=True, text=True, timeout=600); changed = True
        r2 = subprocess.run([_sys.executable, os.path.join(ROOT_DIR, "sheet_review.py"), img_path, out, "--no-model"], capture_output=True, text=True, timeout=300)
        log += "== flags after review\n" + r2.stdout[-3000:]
        if changed: os.utime(img_path, None)          # invalidates the cached store for this image
    except Exception as e:
        log += f"\nreview stopped: {type(e).__name__}: {e}"
    try: open(os.path.join(out, "log_review.txt"), "a").write("\n" + log + "\n== review finished\n")
    except Exception: pass


# ---------------------------------------------------------------------------
# Thumbnails & IFC 2D Plan Preview
# ---------------------------------------------------------------------------
def _save_resized_png(im: Image.Image, dst: str, max_px: int):
    w, h = im.size
    if max(w, h) > max_px:
        k = max_px / max(w, h)
        im = im.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
    im.save(dst, "PNG", **({"compress_level": 3} if im.width * im.height > 8_000_000 else {"optimize": True}))


def render_ifc_plan_png(ifc_path: str, st, evidence_ids=None, size: int = 1200) -> bytes:
    """Fast 2D plan/elevation projection for IFC models (wall axes + element placements) so IFC uploads
    and IFC answers always have a visual drawing overview alongside the element schedule."""
    ev_set = set(evidence_ids or [])
    pts_bg = []      # (kind, [(x,y), ...])
    pts_ev = []      # (id, kind, label, [(x,y), ...])
    try:
        import ifcopenshell
        import ifcopenshell.util.placement as UP
        import ifcopenshell.util.unit as UU
        m = ifcopenshell.open(ifc_path)
        ls = UU.calculate_unit_scale(m)

        # Determine target storey(s) if evidence is concentrated on one or a few storeys
        ev_ents = [e for e in st.entities if e.id in ev_set]
        ev_levels = {e.get("level") for e in ev_ents if e.get("level")}

        def world_xy(prod):
            if not getattr(prod, "ObjectPlacement", None):
                return None
            mat = UP.get_local_placement(prod.ObjectPlacement)
            ox, oy = float(mat[0][3]) * ls, float(mat[1][3]) * ls
            ux, uy = float(mat[0][0]), float(mat[1][0])
            vx, vy = float(mat[0][1]), float(mat[1][1])
            # Try to read 2D Axis polyline if present (e.g. IfcWall)
            rep = getattr(prod, "Representation", None)
            if rep:
                for r in rep.Representations or []:
                    if getattr(r, "RepresentationIdentifier", "") == "Axis":
                        for it in r.Items or []:
                            if it.is_a("IfcPolyline") and len(it.Points) >= 2:
                                seg = []
                                for pt in it.Points:
                                    c = pt.Coordinates
                                    px = ox + (c[0] * ux + (c[1] if len(c) > 1 else 0.0) * vx) * ls
                                    py = oy + (c[0] * uy + (c[1] if len(c) > 1 else 0.0) * vy) * ls
                                    seg.append((px, py))
                                return seg
            return [(ox, oy)]

        # Background walls / columns / doors / windows (capped for speed on huge models)
        walls = m.by_type("IfcWall")
        if len(walls) > 700:
            step = max(1, len(walls) // 700)
            walls = walls[::step]
        for w in walls:
            try:
                g = world_xy(w)
                if g:
                    pts_bg.append(("wall", g))
            except Exception:
                pass
        for cls, knd in (("IfcColumn", "column"), ("IfcDoor", "door"), ("IfcWindow", "window")):
            els = m.by_type(cls)
            if len(els) > 300:
                els = els[:: max(1, len(els) // 300)]
            for el in els:
                if el.GlobalId in ev_set:
                    continue
                try:
                    g = world_xy(el)
                    if g:
                        pts_bg.append((knd, g))
                except Exception:
                    pass

        for e in ev_ents:
            guid = e.get("guid") or e.id
            try:
                prod = m.by_guid(guid)
                g = world_xy(prod)
                if g:
                    pts_ev.append((e.id, e.kind, e.get("label") or e.get("name") or e.id, g))
            except Exception:
                pass
    except Exception:
        pass

    all_xy = [p for _, g in pts_bg for p in g] + [p for *_, g in pts_ev for p in g]
    im = Image.new("RGB", (size, int(size * 0.72)), (250, 251, 253))
    d = ImageDraw.Draw(im, "RGBA")
    f_title = RENDER.BIG(16)
    f_sub = RENDER._font(13)
    f_num = RENDER.BIG(13)

    hdr_h = 54
    d.rectangle([0, 0, im.width, hdr_h], fill=(238, 242, 248, 255))
    schema = st.meta.get("schema") or "IFC"
    title = f"BIM 2D Plan Projection ({schema}) — {len(st.entities):,} extracted entities"
    if ev_set:
        title = f"BIM Evidence View ({schema}) — {len(pts_ev)} geometric / {len(ev_set)} matched element(s)"
    d.text((16, 10), title, fill=(21, 24, 29), font=f_title)
    levels_txt = ", ".join(st.summary().get("levels", [])[:8]) or "no named storeys"
    d.text((16, 32), f"Storeys: {levels_txt}", fill=(102, 112, 133), font=f_sub)

    if all_xy:
        xs = [p[0] for p in all_xy]
        ys = [p[1] for p in all_xy]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        span_x = max(x1 - x0, 1.0)
        span_y = max(y1 - y0, 1.0)
        pad_px = 46
        avail_w = im.width - 2 * pad_px
        avail_h = im.height - hdr_h - 2 * pad_px - 28
        sc = min(avail_w / span_x, avail_h / span_y)
        ox_px = pad_px + (avail_w - span_x * sc) / 2
        oy_px = hdr_h + pad_px + (avail_h - span_y * sc) / 2
        T = lambda x, y: (ox_px + (x - x0) * sc, im.height - 28 - (oy_px - hdr_h + (y - y0) * sc))

        for knd, g in pts_bg:
            if len(g) >= 2:
                d.line([T(x, y) for x, y in g], fill=(165, 174, 188, 210), width=3 if knd == "wall" else 2)
            else:
                cx, cy = T(*g[0])
                r = 3 if knd == "column" else 2
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(180, 188, 200, 190))

        for idx, (eid, knd, lab, g) in enumerate(pts_ev, 1):
            col = RENDER.COL.get(knd, (36, 87, 214))
            if len(g) >= 2:
                pts_t = [T(x, y) for x, y in g]
                d.line(pts_t, fill=col + (255,), width=5)
                cx = sum(p[0] for p in pts_t) / len(pts_t)
                cy = sum(p[1] for p in pts_t) / len(pts_t)
            else:
                cx, cy = T(*g[0])
                d.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], fill=col + (90,), outline=col + (255,), width=3)
            if len(pts_ev) <= 80:
                d.text((cx + 9, cy - 8), str(idx), fill=col + (255,), font=f_num, stroke_width=3, stroke_fill=(255, 255, 255))

        d.text(
            (16, im.height - 22),
            f"Plan extent: {span_x:.1f} m × {span_y:.1f} m  |  Grey = context walls/openings, Coloured/numbered = matched IFC elements",
            fill=(102, 112, 133),
            font=f_sub,
        )
    else:
        # Non-geometric IFC summary card (e.g. model facts, systems, or placement-free elements)
        y = hdr_h + 24
        d.text((24, y), "Matched BIM records (non-geometric / model metadata):", fill=(36, 87, 214), font=f_title)
        y += 30
        shown = [e for e in st.entities if e.id in ev_set][:18] if ev_set else st.entities[:18]
        for idx, e in enumerate(shown, 1):
            txt = f"{idx}. [{e.kind}] {e.get('name') or e.get('label') or e.id}"
            for k in ("value", "type_name", "level", "area", "volume", "length", "members"):
                if e.get(k) is not None:
                    txt += f"  ·  {k}={e.get(k)}"
            d.text((28, y), txt[:115], fill=(35, 40, 48), font=f_sub)
            y += 24
            if y > im.height - 40:
                break

    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Multi-page PDF Store Builder (supports arbitrary page selection > 3 pages & scanned PDF pages)
# ---------------------------------------------------------------------------
def _render_plan_page(pg, dst_png: str):
    """Render a PDF page for the symbol detector. Long side up to PLAN_RENDER_PX and at most 400 dpi: enough for the
    detector to choose its own working scale; the old 2600 px cap made symbols on large sheets a few pixels wide."""
    import pymupdf
    z = min(400 / 72, PLAN_RENDER_PX / max(pg.rect.width, pg.rect.height, 1))
    pm = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
    Image.frombytes("RGB", (pm.width, pm.height), pm.samples).save(dst_png, "PNG")


def _build_pdf_selection(doc_dir: str, doc_id: str, pdf_path: str, pages_meta: list[dict], selected_pages: list[int]) -> tuple[str, str, dict]:
    """Prepare the queryable file & Store for the selected 1-based page numbers of a PDF.
    Returns (query_path, active_mode, extra_info)."""
    import pymupdf
    from . import x_pdf as XPDF

    valid = sorted({p for p in selected_pages if 1 <= p <= len(pages_meta)})
    if not valid:
        valid = [1]

    sel_meta = [pages_meta[p - 1] for p in valid]
    # If every selected page is a scanned image (no vector words), treat the first selected page as a scanned raster plan!
    if all(m["kind"] == "scanned" for m in sel_meta):
        pno = valid[0]
        scan_png = os.path.join(doc_dir, f"{doc_id}_p{pno}.png")
        if not os.path.exists(scan_png):
            src_doc = pymupdf.open(pdf_path)
            pg = src_doc[pno - 1]
            pg.remove_rotation()
            _render_plan_page(pg, scan_png)
        prep = _prepare_raster(scan_png)
        return scan_png, "pdf-scanned", {"active_pages": [pno], "ignored_scanned_pages": valid[1:], **prep}

    # Vector pages that carry no schedule table are drawings (plans). The schedule reader has nothing to read there, so
    # the page is rendered at high resolution and read like a scan: symbols by the detector, text by OCR.
    try:
        probe = pymupdf.open(pdf_path); n_rows = 0
        for pno in valid[:3]:
            one = os.path.join(doc_dir, f"_tmp_p{pno}.pdf")
            if not os.path.exists(one):
                od = pymupdf.open(); od.insert_pdf(probe, from_page=pno - 1, to_page=pno - 1); od.save(one); od.close()
            n_rows += len(XPDF.extract(one).entities)
        if n_rows == 0:
            pno = valid[0]
            scan_png = os.path.join(doc_dir, f"{doc_id}_p{pno}.png")
            if not os.path.exists(scan_png):
                pg = probe[pno - 1]; pg.remove_rotation(); _render_plan_page(pg, scan_png)
            prep = _prepare_raster(scan_png)
            return scan_png, "pdf-plan", {"active_pages": [pno], "ignored_scanned_pages": valid[1:], "rendered_from_vector": True, **prep}
    except Exception:
        pass

    # Vector (or mixed) PDF pages:
    # If all pages of a <=3-page PDF are selected, we can query pdf_path directly!
    if valid == list(range(1, len(pages_meta) + 1)) and len(pages_meta) <= 3:
        return pdf_path, "pdf-vector", {"active_pages": valid, "page_map": {p: p for p in valid}}

    # Otherwise build a subset PDF containing `valid` pages, extract each page individually so >3 pages work,
    # and write the combined Store into cache/stores/<key>.pkl so bp.ask.store_for(sel_pdf) uses all selected pages!
    tag = hashlib.sha1(",".join(map(str, valid)).encode()).hexdigest()[:8]
    sel_pdf = os.path.join(doc_dir, f"{doc_id}_sel_{tag}.pdf")
    page_map = {idx + 1: orig_p for idx, orig_p in enumerate(valid)}  # subset 1-based page -> original 1-based page

    if not os.path.exists(sel_pdf):
        src_doc = pymupdf.open(pdf_path)
        out_doc = pymupdf.open()
        for orig_p in valid:
            out_doc.insert_pdf(src_doc, from_page=orig_p - 1, to_page=orig_p - 1)
        out_doc.save(sel_pdf)
        out_doc.close()

    # Extract each selected page one-by-one so bp.x_pdf's 3-page cap never drops page 4+
    combined = None
    ent_seq = 0
    src_doc = pymupdf.open(pdf_path)
    for sub_idx, orig_p in enumerate(valid, 1):
        tmp_one = os.path.join(doc_dir, f"_tmp_p{orig_p}.pdf")
        if not os.path.exists(tmp_one):
            one_doc = pymupdf.open()
            one_doc.insert_pdf(src_doc, from_page=orig_p - 1, to_page=orig_p - 1)
            one_doc.save(tmp_one)
            one_doc.close()
        sub_st = XPDF.extract(tmp_one)
        if combined is None:
            from .core import Store
            combined = Store(sel_pdf, "pdf-vector")
            combined.meta["supports"] = ["schedule_row"]
            combined.meta["extractor"] = "x_pdf"
            combined.meta["units"] = sub_st.meta.get("units")
            combined.meta["tables"] = []
        combined.meta["warnings"].extend(sub_st.meta.get("warnings", []))
        for t in sub_st.meta.get("tables", []):
            tc = dict(t)
            tc["page"] = sub_idx
            tc["orig_page"] = orig_p
            combined.meta["tables"].append(tc)
        for e in sub_st.entities:
            ent_seq += 1
            e.id = f"row:{sub_idx}:{ent_seq}"
            e.attrs["page"] = orig_p
            e.src["page"] = sub_idx
            e.src["orig_page"] = orig_p
            combined.add(e)

    # Pre-seed bp.ask's store cache for sel_pdf so bp.ask.ask(sel_pdf, q) uses `combined` seamlessly
    key = hashlib.sha1(
        f"pdf:{os.path.abspath(sel_pdf)}:{os.path.getmtime(sel_pdf)}:{ASK._code_version('pdf')}".encode()
    ).hexdigest()[:16]
    pk = os.path.join(ASK.CACHE, key + ".pkl")
    pickle.dump(combined, open(pk, "wb"))
    ASK._MEM[key] = combined
    return sel_pdf, "pdf-vector", {"active_pages": valid, "page_map": page_map}


# ---------------------------------------------------------------------------
# Summaries, Unreadable Caveats & Grounded Suggested Questions
# ---------------------------------------------------------------------------
def describe_store(st, meta_doc: dict) -> tuple[str, dict, list[str]]:
    """Build:
      - understood_summary: concise 1-line description ("vector PDF, 1 page, 2 schedule tables found", ...)
      - counts: structured summary numbers
      - unreadable: explicit list of what could NOT be read or has caveats"""
    unreadable = []
    kinds = st.kinds()

    if st.fmt == "pdf-vector":
        pages = meta_doc.get("pages", [])
        n_pages = len(pages)
        sel = meta_doc.get("selected_pages") or list(range(1, n_pages + 1))
        tables = st.meta.get("tables") or []
        n_rows = kinds.get("schedule_row", 0)
        p_word = f"{n_pages} page" + ("s" if n_pages != 1 else "")
        if sel != list(range(1, n_pages + 1)):
            p_word += f" (selected: {', '.join(map(str, sel))})"
        t_word = f"{len(tables)} schedule table" + ("s" if len(tables) != 1 else "") + " found"
        if tables:
            t_names = ", ".join(f"{t['title']} ({t['rows']} rows)" for t in tables[:4])
            summary = f"PDF sheet, {p_word}, {t_word} ({n_rows} rows in total: {t_names})"
        else:
            summary = f"PDF sheet, {p_word}, no schedule tables found"

        scanned_pages = [p["page"] for p in pages if p["kind"] == "scanned" and p["page"] in sel]
        if scanned_pages:
            unreadable.append(
                f"Page(s) {', '.join(map(str, scanned_pages))} are scanned images without selectable text. Select one scanned page on its own to have its doors, windows and fixtures detected."
            )
        pages_with_tables = {t.get("orig_page") or t.get("page") for t in tables}
        empty_vec = [p["page"] for p in pages if p["page"] in sel and p["kind"] == "vector" and p["page"] not in pages_with_tables]
        if empty_vec:
            unreadable.append(
                f"No schedule table was found on page(s) {', '.join(map(str, empty_vec))}. Drawn plans inside a PDF are not measured: upload the DXF or IFC version of the plan to get room areas and counts."
            )
        elif tables:
            unreadable.append(
                "Drawn details and plans on this sheet are not measured. Answers come only from the schedule tables listed above."
            )
        gap_tables = [t["title"] for t in tables if t.get("method") == "text gaps"]
        if gap_tables:
            unreadable.append(
                f"Table(s) {', '.join(gap_tables)} have no drawn grid lines, so columns were separated by the position of the text. Check the columns in 'What was read' before relying on them."
            )
        for w in st.meta.get("warnings", []):
            unreadable.append(str(w))
        return summary, {"pages": n_pages, "tables": len(tables), "rows": n_rows,
                         "table_list": [{"title": t["title"], "rows": t["rows"]} for t in tables]}, unreadable

    if st.fmt == "raster":
        n_floors = kinds.get("floor", 0) or 1
        n_rooms = kinds.get("room", 0)
        n_labels = kinds.get("room_label", 0)
        sym_kinds = {k: v for k, v in kinds.items() if k not in ("room", "room_label", "floor")}
        n_syms = sum(sym_kinds.values())
        src_prefix = {"pdf-scanned": "scanned PDF page", "pdf-plan": "PDF drawing page (rendered and read like a scan)"}.get(meta_doc.get("active_mode"), "scanned plan")
        rg0 = st.meta.get("room_geometry")
        first = (f"{rg0['buildings']} separate building" + ("s" if rg0["buildings"] != 1 else "")) if rg0 else (f"{n_floors} separate plan" + ("s" if n_floors != 1 else ""))
        parts = [src_prefix, first, f"{n_rooms} room" + ("s" if n_rooms != 1 else "") + " read"]
        if n_syms:
            sym_detail = ", ".join(f"{v} {k}{'s' if v != 1 else ''}" for k, v in sorted(sym_kinds.items()))
            parts.append(f"{n_syms} symbols detected ({sym_detail})")
        if n_labels:
            parts.append(f"{n_labels} printed area label" + ("s" if n_labels != 1 else ""))
        summary = ", ".join(parts)

        if meta_doc.get("ignored_scanned_pages"):
            unreadable.append(
                f"Only scanned page {meta_doc['selected_pages'][0]} is being read. Choose another page in the file list to read page(s) {', '.join(map(str, meta_doc['ignored_scanned_pages']))}."
            )
        if meta_doc.get("det_err"):
            unreadable.append(f"Doors, windows and fixtures could not be detected on this scan ({meta_doc['det_err']}). Only printed room labels were read.")
        rg = st.meta.get("room_geometry")
        if rg:
            unreadable.append(
                f"Room outlines were traced from the walls: {rg['rooms']} rooms in {rg['buildings']} building(s), {rg['area_m2']:g} m² in total. "
                f"Scale: {rg['scale_source']} ({rg['mm_per_px']:.2f} mm per pixel). Areas are wall face to wall face."
                + (f" {rg['flags']} spot(s) could not be settled and are listed as review flags." if rg.get("flags") else "")
            )
        elif n_labels == 0:
            unreadable.append(
                "No scale and no printed room areas were found on this scan. Counts of rooms, doors, windows and fixtures work; areas and lengths cannot be computed."
            )
        else:
            unreadable.append(
                f"The scale of this scan is unknown. Areas are available only for the {n_labels} room(s) whose area is printed on the plan."
            )
        tl = st.meta.get("tiling") or {}
        if len(tl.get("passes") or []) > 1:
            unreadable.append(
                f"Large sheet: read in overlapping tiles at {tl.get('used_scale')} of full size (chosen from the door size found in a first pass), not squeezed into one image."
            )
        ccm = (st.meta.get("crosscheck") or {}).get("keys") or {}
        dis = [f"{k}: {v['used']} vs {v['second']} ({v['confirmed']} on the same spots)" for k, v in sorted(ccm.items()) if not (v["used"] == v["second"] == v["confirmed"])]
        if ccm:
            unreadable.append(
                "A second reading at another scale " + ("agrees on every symbol class." if not dis else "disagrees on " + "; ".join(dis) + ". Those counts are labelled 'Needs review'.")
            )
        bl = st.meta.get("borderline") or {}
        n_bl = sum(len(v) for v in bl.values())
        if n_bl:
            unreadable.append(
                f"{n_bl} detected item(s) are uncertain. Answers that depend on them are labelled 'Needs review'."
            )
        return summary, {"floors": n_floors, "rooms": n_rooms, "symbols": n_syms, "printed_labels": n_labels, "by_kind": dict(sorted(sym_kinds.items()))}, unreadable

    if st.fmt == "dxf-layered":
        n_rooms = kinds.get("room", 0)
        n_apts = kinds.get("apartment", 0)
        tot_area = round(sum(r.get("area") or 0 for r in st.of("room")), 1)
        sym_kinds = {k: v for k, v in kinds.items() if k not in ("room", "apartment")}
        sym_str = ", ".join(f"{v} {k}{'s' if v != 1 else ''}" for k, v in sorted(sym_kinds.items()))
        summary = f"CAD plan (DXF), {n_rooms} room{'s' if n_rooms != 1 else ''} read ({tot_area:g} m²)"
        if n_apts:
            summary += f", {n_apts} apartment{'s' if n_apts != 1 else ''}"
        if sym_str:
            summary += f", {sym_str}"
        if n_rooms == 0 and not sym_kinds:
            try:
                import ezdxf
                layers = sorted({e.dxf.layer for e in ezdxf.readfile(st.path).modelspace()})[:15]
            except Exception:
                layers = []
            unreadable.append(
                "No rooms, doors, windows or fixtures were recognised. This reader expects the layers ROOM_OUTLINE, ROOM_TEXT, DOOR, WINDOW and FIXTURE. "
                + (f"Layers present in this file: {', '.join(layers)}." if layers else "")
            )
        for iss in AUDIT.audit(st):
            if iss["sev"] in ("error", "warn"):
                unreadable.append(iss["text"])
        return summary, {"rooms": n_rooms, "apartments": n_apts, "area_m2": tot_area, "symbols": sum(sym_kinds.values()), "by_kind": dict(sorted(sym_kinds.items()))}, unreadable[:12]

    if st.fmt == "ifc":
        schema = st.meta.get("schema") or "IFC"
        n_lvls = kinds.get("level", 0)
        n_rooms = kinds.get("room", 0)
        phys = {k: v for k, v in kinds.items() if k not in ("level", "fact", "system", "room")}
        top_phys = ", ".join(f"{v:,} {k}{'s' if v != 1 else ''}" for k, v in sorted(phys.items(), key=lambda kv: -kv[1])[:5])
        parts = [f"Building model ({schema})", f"{n_lvls} storey{'s' if n_lvls != 1 else ''}"]
        if n_rooms:
            parts.append(f"{n_rooms} space{'s' if n_rooms != 1 else ''}")
        if top_phys:
            parts.append(top_phys)
        summary = ", ".join(parts)
        for iss in AUDIT.audit(st):
            if iss["sev"] in ("error", "warn"):
                unreadable.append(iss["text"])
        if not n_rooms:
            unreadable.append("This model contains no rooms (spaces), so room areas cannot be answered.")
        return summary, {"storeys": n_lvls, "spaces": n_rooms, "elements": sum(phys.values()),
                         "by_kind": dict(sorted(phys.items(), key=lambda kv: -kv[1]))}, unreadable[:12]

    return f"{st.fmt} drawing", {"entities": len(st.entities)}, list(st.meta.get("warnings", []))


def suggest_questions(st, meta_doc: dict | None = None) -> list[str]:
    if st is not None and st.fmt == "raster" and st.meta.get("room_geometry"):
        rest = _suggest_questions_inner(st, meta_doc)
        return ["Check this plan against the regulations"] + [q for q in rest if q][:5]
    return _suggest_questions_inner(st, meta_doc)


def _suggest_questions_inner(st, meta_doc: dict | None = None) -> list[str]:
    """Generate 5-7 grounded questions directly from the entities, tables, rooms, and kinds extracted from `st`."""
    qs = []
    kinds = st.kinds()

    if st.fmt == "pdf-vector":
        tables = st.meta.get("tables") or []
        rows = st.of("schedule_row")
        for t in tables:
            tname = t["title"]
            cols = t.get("columns") or []
            trows = [r for r in rows if r.get("table") == tname]
            if not trows:
                continue
            is_door = "door_number" in cols or "door" in tname.lower()
            is_room = "room_number" in cols and ("finish" in tname.lower() or "area" in cols)
            if is_door:
                qs.append("How many doors are listed in the door schedule?")
                # Check if sibling matl / matl_2 columns exist with codes
                if "matl_2" in cols:
                    vals = [str(r.get("matl_2") or "") for r in trows if r.get("matl_2")]
                    al_codes = sorted({m for v in vals for m in re.findall(r"[A-Z]{2,4}-\d+", v)})
                    if len(al_codes) >= 2:
                        qs.append(f"How many doors in the door schedule have frame material {al_codes[0]} or {al_codes[1]}?")
                    elif al_codes:
                        qs.append(f"How many doors in the door schedule have frame material {al_codes[0]}?")
                elif "door_type" in cols:
                    dt = next((r.get("door_type") for r in trows if r.get("door_type")), None)
                    if dt:
                        qs.append(f"How many doors in the door schedule are door type {dt}?")
                if "width" in cols and "door_number" in cols:
                    sample_r = next((r for r in trows if r.get("door_number") and r.get("width")), None)
                    if sample_r:
                        qs.append(f"What is the width of door {sample_r.get('door_number')}?")
            elif is_room:
                qs.append(f"How many rooms are listed in the {tname.lower()}?")
                if "area" in cols:
                    with_area = [r for r in trows if r.get("area") and re.search(r"\d", str(r.get("area"))) and r.get("room_number")]
                    if with_area:
                        pick_r = with_area[min(len(with_area) // 2, len(with_area) - 1)]
                        rn = pick_r.get("room_number")
                        rnm = pick_r.get("room_name")
                        qs.append(
                            f"What is the area of room {rn}" + (f" ({rnm})" if rnm else "") + f" in the {tname.lower()}?"
                        )
                        qs.append(f"What is the total area of all rooms in the {tname.lower()}?")
            else:
                qs.append(f"How many rows are in the {tname.lower()}?")
                if len(cols) >= 2 and trows[0].get(cols[0]):
                    qs.append(f"What is the {cols[1].replace('_', ' ')} for {trows[0].get(cols[0])} in the {tname.lower()}?")
        return list(dict.fromkeys(qs))[:7]

    if st.fmt == "raster":
        for sym, label in (("toilet", "toilets"), ("door", "doors"), ("window", "windows"), ("washbasin", "sinks")):
            if sym in kinds or sym in (st.meta.get("supports") or []):
                qs.append(f"How many {label} are on this floor plan?")
        rtypes = sorted({r.get("rtype") for r in st.of("room") if r.get("rtype") and r.get("rtype") != "outdoor"})
        for rt in rtypes[:2]:
            qs.append(f"How many {rt}s are on this floor plan?")
        if "shower" in kinds or "bathtub" in kinds or "shower_or_bath" in kinds:
            qs.append("How many showers and bathtubs are on this floor plan?")
        return list(dict.fromkeys(qs))[:6]

    if st.fmt == "dxf-layered":
        if kinds.get("room"):
            qs.append("What is the total floor area of all indoor rooms (excluding balconies)?")
            qs.append("Which room has the largest area?")
        apts = sorted({a.get("name") for a in st.of("apartment") if a.get("name")})
        if apts:
            qs.append(f"What is the total area of the rooms in apartment {apts[0]}?")
        rtypes = {r.get("rtype") for r in st.of("room") if r.get("rtype")}
        if "bathroom" in rtypes:
            qs.append("What is the total area of the bathrooms?")
        for sym in ("door", "window", "toilet"):
            if kinds.get(sym):
                qs.append(f"How many {sym}s are on this floor?")
        return list(dict.fromkeys(qs))[:6]

    if st.fmt == "ifc":
        phys = [(k, v) for k, v in kinds.items() if k not in ("level", "fact", "system", "room", "element")]
        phys.sort(key=lambda kv: -kv[1])
        for k, _ in phys[:2]:
            qs.append(f"How many {k}s are in this model?")
            qs.append(f"What {k} types are used, with their quantities?")
        if kinds.get("wall") and any(w.get("volume") for w in st.of("wall")):
            qs.append("What is the total volume of all walls?")
        if kinds.get("room") and any(r.get("area") for r in st.of("room")):
            qs.append("What is the total floor area of all spaces?")
        if kinds.get("level", 0) >= 2 and phys:
            qs.append(f"How many {phys[0][0]}s are on each level?")
        if kinds.get("fact"):
            qs.append("What authoring software and IFC schema were used for this model?")
        return list(dict.fromkeys(qs))[:6]

    return qs[:6]


# ---------------------------------------------------------------------------
# Ambiguity Detection -> Deterministic One-Tap Alternative Readings
# ---------------------------------------------------------------------------
def _column_context_label(col: str, cols: list[str]) -> str:
    """Human-readable disambiguation label for a schedule column using its position and neighbours
    (e.g. 'matl (Door group, next to door_type)' vs 'matl_2 (Frame group, next to frame_type)')."""
    if col not in cols:
        return col
    idx = cols.index(col)
    left = cols[idx - 1] if idx > 0 else None
    right = cols[idx + 1] if idx + 1 < len(cols) else None
    hints = []
    for nb in (left, right):
        if not nb:
            continue
        if nb.startswith("door_"):
            hints.append(f"near {nb} (Door)")
        elif nb.startswith("frame_"):
            hints.append(f"near {nb} (Frame)")
        elif nb.startswith("room_"):
            hints.append(f"near {nb} (Room)")
        elif nb.startswith("wall_"):
            hints.append(f"near {nb} (Wall)")
    hint_txt = hints[0] if hints else f"column #{idx + 1}"
    return f"{col} ({hint_txt})"


def _sibling_columns(col: str, cols: list[str]) -> list[str]:
    base = re.sub(r"_\d+$", "", col)
    sibs = [c for c in cols if c != col and re.sub(r"_\d+$", "", c) == base]
    if sibs:
        return sibs
    # Semantic suffix siblings (e.g. door_type <-> frame_type)
    if "_" in col:
        suffix = col.split("_", 1)[1]
        sibs = [c for c in cols if c != col and c.endswith("_" + suffix)]
    return sibs[:3]


def compute_alternatives(st, question: str, ans: dict) -> list[dict]:
    """Check if the executed plan touches an ambiguous column or scope in `st`.
    Every alternative is computed deterministically via bp.execute.run(alt_plan, st) — never guessed."""
    if not st or ans.get("status") != "ok":
        return []
    plan = ans.get("plan")
    if not isinstance(plan, dict) or plan.get("parts"):
        return []

    options = []

    # 1. Schedule table parallel/duplicate columns (e.g. Door 'matl' vs Frame 'matl_2' on A-801)
    if plan.get("kind") == "schedule_row":
        tables = st.meta.get("tables") or []
        tbl_filter = next(
            (f.get("value") for f in (plan.get("filters") or []) if f.get("field") == "table"),
            None,
        )
        cand_tables = [t for t in tables if not tbl_filter or str(t["title"]).lower() == str(tbl_filter).lower()] or tables
        cols = cand_tables[0].get("columns", []) if cand_tables else []
        if cols:
            # Check filter fields for sibling columns
            for fi, flt in enumerate(plan.get("filters") or []):
                fld = str(flt.get("field") or "")
                sibs = _sibling_columns(fld, cols)
                if not sibs:
                    continue
                chosen_lbl = f"Filter on {_column_context_label(fld, cols)}"
                alts = []
                for sc in sibs:
                    alt_plan = json.loads(json.dumps(plan))
                    alt_plan["filters"][fi]["field"] = sc
                    alt_plan["notes"] = [f"Alternative reading: filter on column '{sc}' instead of '{fld}'."]
                    res = EX.run(alt_plan, st)
                    if res.status == "ok" and (res.value != ans.get("value") or res.evidence != ans.get("evidence")):
                        alts.append({
                            "label": f"Filter on {_column_context_label(sc, cols)}",
                            "value": res.value,
                            "unit": res.unit,
                            "plan": alt_plan,
                            "active": False,
                        })
                if alts:
                    options = [
                        {
                            "label": chosen_lbl,
                            "value": ans.get("value"),
                            "unit": ans.get("unit"),
                            "plan": plan,
                            "active": True,
                        },
                        *alts,
                    ]
                    return options

            # Check target `attr` for sibling columns (e.g. reading `matl` vs `matl_2` for a specific door)
            attr = str(plan.get("attr") or "")
            sibs = _sibling_columns(attr, cols) if attr else []
            if sibs:
                chosen_lbl = f"Read {_column_context_label(attr, cols)}"
                alts = []
                for sc in sibs:
                    alt_plan = json.loads(json.dumps(plan))
                    alt_plan["attr"] = sc
                    alt_plan["notes"] = [f"Alternative reading: read column '{sc}' instead of '{attr}'."]
                    res = EX.run(alt_plan, st)
                    if res.status == "ok" and res.value != ans.get("value"):
                        alts.append({
                            "label": f"Read {_column_context_label(sc, cols)}",
                            "value": res.value,
                            "unit": res.unit,
                            "plan": alt_plan,
                            "active": False,
                        })
                if alts:
                    return [
                        {
                            "label": chosen_lbl,
                            "value": ans.get("value"),
                            "unit": ans.get("unit"),
                            "plan": plan,
                            "active": True,
                        },
                        *alts,
                    ]

    # 2. Indoor rooms vs All rooms (including balconies/outdoor) on floor plans when outdoor spaces exist
    if plan.get("kind") == "room" and plan.get("op") in ("sum", "count"):
        has_outdoor = any(r.get("rtype") == "outdoor" for r in st.of("room"))
        flts = plan.get("filters") or []
        specific_room = any(f.get("field") in ("number", "name", "label") for f in flts)
        rtype_flts = [f for f in flts if f.get("field") == "rtype"]
        if has_outdoor and not specific_room:
            if not rtype_flts:
                alt_plan = json.loads(json.dumps(plan))
                alt_plan.setdefault("filters", []).append({"field": "rtype", "op": "nin", "value": ["outdoor"]})
                alt_plan["notes"] = ["Excluding outdoor spaces (balconies / terraces / loggias)."]
                res = EX.run(alt_plan, st)
                if res.status == "ok" and res.value != ans.get("value"):
                    return [
                        {"label": "All rooms (including balconies/outdoor)", "value": ans.get("value"), "unit": ans.get("unit"), "plan": plan, "active": True},
                        {"label": "Indoor rooms only (excluding balconies)", "value": res.value, "unit": res.unit, "plan": alt_plan, "active": False},
                    ]
            elif len(rtype_flts) == 1 and rtype_flts[0].get("op") == "nin" and "outdoor" in (rtype_flts[0].get("value") or []):
                alt_plan = json.loads(json.dumps(plan))
                alt_plan["filters"] = [f for f in alt_plan["filters"] if f.get("field") != "rtype"]
                alt_plan["notes"] = ["Including all numbered spaces (indoor + outdoor balconies/terraces)."]
                res = EX.run(alt_plan, st)
                if res.status == "ok" and res.value != ans.get("value"):
                    return [
                        {"label": "Indoor rooms (excluding outdoor)", "value": ans.get("value"), "unit": ans.get("unit"), "plan": plan, "active": True},
                        {"label": "Include outdoor / balconies", "value": res.value, "unit": res.unit, "plan": alt_plan, "active": False},
                    ]

    return options


# ---------------------------------------------------------------------------
# Printed table headers (UI kit lane): what the sheet actually prints above each column
# ---------------------------------------------------------------------------
_HDR_CACHE = {}
_NUM_RX = re.compile(r"""^\s*(?:-?[\d][\d,]*(?:\.\d+)?\s*(?:SF|SQ\.?\s*FT\.?|m2|m²|m3|m³|mm|cm|m|ft|in)?|\d+'\s*-\s*\d+(?:\s+\d+/\d+)?"|\d+(?:\s+\d+/\d+)?"|\d+/\d+")\s*$""", re.I)
_ID_COL_RX = re.compile(r"(^|_)(number|no|mark|id|set|group|tag|code|room|level|type)(_|$)", re.I)


def _pretty_col(key: str) -> str:
    s = str(key or "").replace("_", " ").strip()
    return s[:1].upper() + s[1:] if s else s


def _col_is_numeric(key: str, values: list) -> bool:
    vals = [str(v).strip() for v in values if v not in (None, "") and str(v).strip() not in ("-", "")]
    if not vals:
        return False
    hits = sum(1 for v in vals if _NUM_RX.match(v))
    if hits < 0.8 * len(vals):
        return False
    plain_ints = all(re.fullmatch(r"\d+", v) for v in vals)
    if plain_ints and (_ID_COL_RX.search(key or "") or any(len(v) > 1 and v.startswith("0") for v in vals)):
        return False    # room numbers, marks and hardware sets are identifiers, not quantities
    return True


def _h_segments(pg, x_lo, x_hi, y_lo, y_hi):
    """Horizontal ruling segments on a page inside a window, merged per y: [(y, [(x0, x1), ...]), ...]."""
    raw = {}
    for dr in pg.get_drawings():
        for it in dr["items"]:
            if it[0] == "l":
                segs = [(it[1].x, it[1].y, it[2].x, it[2].y)]
            elif it[0] == "re":
                r = it[1]
                segs = [(r.x0, r.y0, r.x1, r.y0), (r.x0, r.y1, r.x1, r.y1)]
            else:
                continue
            for ax, ay, bx, by in segs:
                if abs(ay - by) > 0.8 or abs(ax - bx) < 6:
                    continue
                if not (y_lo <= ay <= y_hi) or max(ax, bx) < x_lo - 2 or min(ax, bx) > x_hi + 2:
                    continue
                raw.setdefault(round(ay, 1), []).append((max(min(ax, bx), x_lo), min(max(ax, bx), x_hi)))
    merged = []
    for y in sorted(raw):
        if merged and y - merged[-1][0] <= 1.5:
            merged[-1][1].extend(raw[y])
        else:
            merged.append([y, list(raw[y])])
    out = []
    for y, segs in merged:
        runs = []
        for a, b in sorted(segs):
            if runs and a - runs[-1][1] <= 2.5:
                runs[-1][1] = max(runs[-1][1], b)
            else:
                runs.append([a, b])
        out.append((y, [(a, b) for a, b in runs]))
    return out


def _join_words(ws) -> str:
    lines = []
    for w in sorted(ws, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        cy = (w[1] + w[3]) / 2
        if lines and abs(cy - lines[-1][0]) <= 4:
            lines[-1][1].append(w)
        else:
            lines.append([cy, [w]])
    return " ".join(" ".join(x[4] for x in sorted(ln, key=lambda x: x[0])) for _, ln in lines).strip()


def table_header_layout(st, t: dict) -> list[dict]:
    """One entry per column of a schedule table, in sheet order:
      key      internal column name used by the query code
      label    header text as printed on the sheet (falls back to a tidied key)
      group    printed group header above it (e.g. 'Door' / 'Frame'), or None
      derived  True when the column is not printed in this table but was added by the reader
      numeric  True when the cells are quantities (right-align them)
    Grouped headers are read from the drawn header grid: a ruling line that spans only some columns
    marks a group cell; the words above it are the group name."""
    ck = (st.path, t.get("page"), t.get("title"))
    if ck in _HDR_CACHE:
        return _HDR_CACHE[ck]
    cols = list(t.get("columns") or [])
    rows = [r for r in st.of("schedule_row") if r.get("table") == t.get("title")]
    out = [{"key": c, "label": _pretty_col(c), "group": None, "derived": False,
            "numeric": _col_is_numeric(c, [r.get(c) for r in rows])} for c in cols]
    cx = list((rows[0].src.get("cols_x") if rows else None) or [])
    if len(cx) >= 3:
        for i in range(len(cx) - 1, len(cols)):
            out[i]["derived"] = True
            rules = {str(r.get(cols[i] + "_rule")) for r in rows if r.get(cols[i] + "_rule")}
            out[i]["note"] = rules.pop() if len(rules) == 1 else "Worked out by the reader from other columns; not printed in this table."
        try:
            _read_printed_header(st, t, rows, cx, out)
        except Exception:
            pass    # keep tidied keys: never let a header problem hide the table
    _HDR_CACHE[ck] = out
    return out


def _read_printed_header(st, t, rows, cx, out):
    if not str(st.path).lower().endswith(".pdf") or any(r.src.get("coords") != "unrotated" for r in rows[:3]):
        return
    import pymupdf
    doc = pymupdf.open(st.path)
    pg = doc[int(t.get("page") or rows[0].src.get("page") or 1) - 1]
    pg.remove_rotation()
    x_lo, x_hi = cx[0], cx[-1]
    width = max(x_hi - x_lo, 1.0)
    rows_top = min(r.src["bbox"][1] for r in rows if r.src.get("bbox"))
    lines = _h_segments(pg, x_lo, x_hi, rows_top - 110, rows_top + 1.5)
    full = sorted(y for y, runs in lines if any(b - a >= 0.97 * width for a, b in runs))
    words = [w for w in pg.get_text("words") if x_lo - 2 <= (w[0] + w[2]) / 2 <= x_hi + 2 and rows_top - 110 <= (w[1] + w[3]) / 2 < rows_top]
    band = None
    for y_a, y_b in reversed(list(zip(full, full[1:]))):
        if y_b - y_a >= 6 and any(y_a < (w[1] + w[3]) / 2 < y_b for w in words):
            band = (y_a, y_b)
            break
    if band is None:
        return
    in_band = [w for w in words if band[0] < (w[1] + w[3]) / 2 < band[1]]
    n_printed = len(cx) - 1
    col_of = lambda x: next((i for i in range(n_printed) if cx[i] - 0.5 <= x <= cx[i + 1] + 0.5), None)
    # group cells (e.g. "Door" over Width / Height / Door Type / Matl): a column whose own header cell has a
    # ruling line above it inside the header band sits under a group cell; neighbouring columns share the
    # group cell unless a vertical ruling separates them in the group row.
    top_of = {}
    for i in range(n_printed):
        a, b = cx[i], cx[i + 1]
        ys = [y for y, runs in lines if band[0] + 2 < y < band[1] - 2
              and any(min(rb, b) - max(ra, a) >= 0.8 * (b - a) and rb - ra < 0.97 * width for ra, rb in runs)]
        if ys:
            top_of[i] = max(ys)
    v_cuts = set()
    for dr in pg.get_drawings():
        for it in dr["items"]:
            if it[0] == "l":
                segs = [(it[1].x, it[1].y, it[2].x, it[2].y)]
            elif it[0] == "re":
                r = it[1]
                segs = [(r.x0, r.y0, r.x0, r.y1), (r.x1, r.y0, r.x1, r.y1)]
            else:
                continue
            for ax, ay, bx, by in segs:
                if abs(ax - bx) > 0.8:
                    continue
                for k in range(1, n_printed):
                    if abs(ax - cx[k]) <= 1.5 and k in top_of and k - 1 in top_of:
                        y_g = min(top_of[k], top_of[k - 1])
                        if min(max(ay, by), y_g) - max(min(ay, by), band[0]) >= 0.5 * (y_g - band[0]):
                            v_cuts.add(k)
    grouped = {}
    i = 0
    while i < n_printed:
        if i not in top_of:
            i += 1
            continue
        j = i
        while j + 1 < n_printed and j + 1 in top_of and abs(top_of[j + 1] - top_of[i]) <= 1.5 and (j + 1) not in v_cuts:
            j += 1
        y_g = top_of[i]
        g_label = _join_words([w for w in in_band if (w[1] + w[3]) / 2 < y_g and cx[i] - 1 <= (w[0] + w[2]) / 2 <= cx[j + 1] + 1])
        if g_label:
            for k in range(i, j + 1):
                grouped[k] = (y_g, g_label)
        i = j + 1
    for i in range(min(n_printed, len(out))):
        ws = [w for w in in_band if col_of((w[0] + w[2]) / 2) == i]
        if i in grouped:
            ws = [w for w in ws if (w[1] + w[3]) / 2 > grouped[i][0]]
        label = _join_words(ws)
        if label:
            out[i]["label"] = label
            if i in grouped:
                out[i]["group"] = grouped[i][1]


def column_labels(st, table_title: str | None = None) -> dict:
    """{column key: {'label', 'group', 'table'}} for the named schedule table (or the first that has the key)."""
    res = {}
    for t in (st.meta.get("tables") or []) if st is not None else []:
        if table_title and str(t.get("title")).lower() != str(table_title).lower():
            continue
        for h in table_header_layout(st, t):
            res.setdefault(h["key"], {"label": h["label"], "group": h.get("group"), "table": t.get("title")})
    return res



# ---------------------------------------------------------------------------
# Inspection ("What did you read from this file")
# ---------------------------------------------------------------------------
def build_inspection(st, doc_meta: dict) -> dict:
    aud = AUDIT.table(st)
    tables_grid = []
    if st.fmt == "pdf-vector":
        rows = st.of("schedule_row")
        for t in st.meta.get("tables") or []:
            cols = t.get("columns") or []
            trows = [
                {"_id": r.id, "_page": r.get("page"), **{c: r.get(c, "") for c in cols}}
                for r in rows
                if r.get("table") == t["title"]
            ]
            tables_grid.append({
                "title": t["title"],
                "page": t.get("orig_page") or t.get("page"),
                "method": t.get("method"),
                "columns": cols,
                "header": table_header_layout(st, t),   # printed header (label, group, derived, numeric) per column
                "row_count": len(trows),
                "rows": trows[:500],
            })

    elements_sample = []
    if st.fmt == "ifc":
        for k, cnt in sorted(st.kinds().items(), key=lambda kv: -kv[1]):
            if k in ("fact", "level"):
                continue
            sample = [e.brief() for e in st.of(k)[:25]]
            elements_sample.append({"kind": k, "count": cnt, "sample": sample})

    facts = [{"name": e.get("name"), "value": e.get("value")} for e in st.of("fact")]

    return {
        "doc_id": doc_meta["doc_id"],
        "display_name": doc_meta["display_name"],
        "format": st.fmt,
        "understood_summary": doc_meta["understood_summary"],
        "unreadable": doc_meta["unreadable"],
        "units": st.meta.get("units"),
        "scale": st.meta.get("scale"),
        "tables": tables_grid,
        "rooms": aud.get("rooms", [])[:400],
        "room_types": aud.get("room_types", []),
        "apartments": aud.get("apartments", []),
        "levels": aud.get("levels", []),
        "symbols": aud.get("symbols", {}),
        "elements_by_kind": elements_sample,
        "facts": facts,
        "issues": aud.get("issues", []),
        "printed_check": aud.get("printed_check"),
    }


# ---------------------------------------------------------------------------
# Main Ingestion & Session Manifest Persistence
# ---------------------------------------------------------------------------
def _manifest_path(session_id: str) -> str:
    return ensure_inside_sessions(os.path.join(session_dir(session_id), "manifest.json"))


def load_manifest(session_id: str) -> dict:
    mp = _manifest_path(session_id)
    if os.path.exists(mp):
        try:
            return json.load(open(mp))
        except Exception:
            pass
    return {"session_id": clean_id(session_id, "s"), "created_at": time.time(), "documents": []}


def save_manifest(session_id: str, manifest: dict):
    mp = _manifest_path(session_id)
    tmp = f"{mp}.{os.getpid()}.tmp"
    json.dump(manifest, open(tmp, "w"), indent=1, ensure_ascii=False)
    os.replace(tmp, mp)


def get_doc_meta(session_id: str, doc_id: str) -> dict:
    did = clean_id(doc_id, "d")
    m = load_manifest(session_id)
    doc = next((d for d in m.get("documents", []) if d["doc_id"] == did), None)
    if not doc:
        raise IngestError(f"Document '{did}' was not found in session '{session_id}'.", 404)
    ensure_inside_sessions(doc["saved_path"])
    ensure_inside_sessions(doc["query_path"])
    return doc


def ingest_upload(session_id: str, filename: str | None, data: bytes, selected_pages: list[int] | None = None) -> dict:
    """Validate, store in per-session workspace, extract entities/thumbnails, and return structured ingestion summary."""
    sid = clean_id(session_id, "s")
    _CUR.sid = sid; _CUR.timeline = []; stage("Reading the file", 5)
    sdir = session_dir(sid)
    display_name = sanitize_display_name(filename)
    kind, safe_ext = sniff_format(data, display_name)
    if kind != "raster": stage({"pdf": "Reading the pages and tables", "dxf": "Reading the CAD drawing", "ifc": "Reading the building model"}.get(kind, "Reading the drawing"), 30)

    doc_id = f"doc_{uuid.uuid4().hex[:10]}"
    doc_dir = ensure_inside_sessions(os.path.join(sdir, doc_id))
    os.makedirs(doc_dir, exist_ok=True)

    data, safe_ext, convert_note = _normalise_upload(data, kind, safe_ext, doc_dir)
    # IMPORTANT: for raster uploads, save directly with `<doc_id>.png` or `<doc_id>.jpg` so bp.x_raster's basename stem is unique
    saved_path = ensure_inside_sessions(os.path.join(doc_dir, f"{doc_id}{safe_ext}"))
    with open(saved_path, "wb") as fh:
        fh.write(data)

    pages_meta = []
    extra = {}
    query_path = saved_path
    active_mode = kind

    if kind == "pdf":
        import pymupdf
        try:
            pdf_doc = pymupdf.open(saved_path)
        except Exception as e:
            raise IngestError(f"Could not open PDF file: {e}", 400)
        n_pages = len(pdf_doc)
        if n_pages == 0:
            raise IngestError("This PDF has 0 pages.", 400)

        # The pictures of the pages (preview and thumbnail) are drawn by a pool of workers, each with its own handle on the
        # file, while this thread goes on to read the tables or the plan. They are collected before the upload returns.
        from concurrent.futures import ThreadPoolExecutor
        def _page_pictures(idx):
            d_ = pymupdf.open(saved_path)
            try:
                pg_ = d_[idx]; pg_.remove_rotation()
                # page preview at print quality (300 dpi, long side up to 9000 px) so zooming in the viewer shows the real line work;
                # long documents fall back to 150 dpi per page to keep the upload quick
                z_prev = min((300 if n_pages <= 6 else 150) / 72, 9000 / max(pg_.rect.width, pg_.rect.height, 1))
                pm = pg_.get_pixmap(matrix=pymupdf.Matrix(z_prev, z_prev))
                im = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
                im.save(os.path.join(doc_dir, f"preview_p{idx + 1}.png"), "PNG", compress_level=3 if im.width * im.height > 4_000_000 else 6)
                _save_resized_png(im, os.path.join(doc_dir, f"thumb_p{idx + 1}.png"), 340)
            finally: d_.close()
        pic_pool = ThreadPoolExecutor(max(1, min(8, _cores() // 2)))
        pic_jobs = [pic_pool.submit(_page_pictures, idx) for idx in range(min(n_pages, MAX_PDF_THUMBS))]
        for idx in range(min(n_pages, MAX_PDF_THUMBS)):
            pg = pdf_doc[idx]
            pg.remove_rotation()
            words = pg.get_text("words")
            drawings = pg.get_drawings()
            imgs = pg.get_images()
            p_kind = "vector" if (len(words) >= 8 or len(drawings) >= 20) else ("scanned" if imgs else "vector")

            # Find sheet number / schedule title hint
            txt_sample = " ".join(w[4] for w in words[:120])
            m_sh = re.search(r"\b([A-Z]{1,2}-\d{3}[A-Z]?)\b", txt_sample)
            pages_meta.append({
                "page": idx + 1,
                "width_pt": round(pg.rect.width, 1),
                "height_pt": round(pg.rect.height, 1),
                "words": len(words),
                "drawings": len(drawings),
                "kind": p_kind,
                "sheet_hint": m_sh.group(1) if m_sh else None,
            })
        pdf_doc.close()

        default_sel = selected_pages if selected_pages else (list(range(1, n_pages + 1)) if n_pages <= 5 else [1, 2, 3])
        query_path, active_mode, extra = _build_pdf_selection(doc_dir, doc_id, saved_path, pages_meta, default_sel)
        selected_pages = extra.get("active_pages", default_sel)
        for j_ in pic_jobs: j_.result()
        pic_pool.shutdown()

    elif kind == "raster":
        try:
            im = Image.open(saved_path).convert("RGB")
        except Exception as e:
            raise IngestError(f"Corrupt or unreadable image file: {e}", 400)
        _save_resized_png(im, os.path.join(doc_dir, "thumb_p1.png"), 340)
        extra = _prepare_raster(saved_path)
        pages_meta = [{"page": 1, "width_px": im.width, "height_px": im.height, "kind": "scanned"}]
        selected_pages = [1]

    elif kind == "dxf":
        head_dxf = data[:400000]
        if b"ROOM_OUTLINE" in head_dxf or b"ROOM_TEXT" in head_dxf:
            pages_meta = [{"page": 1, "kind": "cad-dxf"}]
            selected_pages = [1]
        else:
            # CAD file without the room-layer convention the DXF reader needs: draw it and read the drawing like a scan
            png_path = ensure_inside_sessions(os.path.join(doc_dir, f"{doc_id}.png"))
            w_px, h_px = _render_dxf_png(saved_path, png_path)
            im = Image.open(png_path).convert("RGB")
            _save_resized_png(im, os.path.join(doc_dir, "thumb_p1.png"), 340)
            _save_resized_png(im, os.path.join(doc_dir, "preview_p1.png"), RENDER.RASTER_MAX)
            extra = _prepare_raster(png_path)
            query_path = png_path; active_mode = "cad-rendered"; kind = "raster"
            pages_meta = [{"page": 1, "width_px": w_px, "height_px": h_px, "kind": "cad-rendered"}]
            selected_pages = [1]
            convert_note = ((convert_note + ". ") if convert_note else "") + (
                "This CAD file does not use the room-layer convention the CAD reader needs, so it was drawn to an image and read like a scan. "
                "The drawing's real-world scale is not carried over yet: counts work, areas and lengths do not.")

    elif kind == "ifc":
        pages_meta = [{"page": 1, "kind": "bim-ifc"}]
        selected_pages = [1]

    # Run bp.ask.store_for(query_path) to validate & populate Store
    try:
        st, route_note = ASK.store_for(query_path)
    except Exception as e:
        raise IngestError(f"Failed to parse drawing structure: {type(e).__name__}: {e}", 400)
    if st is None:
        raise IngestError(f"Unsupported drawing structure: {route_note}", 400)

    # Generate audit / overview preview for DXF, Raster, and IFC
    audit_png_path = os.path.join(doc_dir, "audit.png")
    if st.fmt == "raster":
        pass        # the page shows the drawing itself and draws what was found on top of it (see /api/chat/overlay)
    elif st.fmt in ("dxf-layered",):
        png_bytes = RENDER.audit_image(st, AUDIT.audit(st), size=1400)
        if png_bytes:
            open(audit_png_path, "wb").write(png_bytes)
            if kind == "dxf":
                im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
                _save_resized_png(im, os.path.join(doc_dir, "thumb_p1.png"), 340)
                im.save(os.path.join(doc_dir, "preview_p1.png"), "PNG")
    elif st.fmt == "ifc":
        png_bytes = render_ifc_plan_png(saved_path, st, size=1100)
        open(audit_png_path, "wb").write(png_bytes)
        im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        _save_resized_png(im, os.path.join(doc_dir, "thumb_p1.png"), 340)
        im.save(os.path.join(doc_dir, "preview_p1.png"), "PNG")

    doc_meta = {
        "doc_id": doc_id,
        "session_id": sid,
        "display_name": display_name,
        "size_bytes": len(data),
        "kind": kind,
        "format": st.fmt,
        "active_mode": active_mode,
        "saved_path": saved_path,
        "query_path": query_path,
        "doc_dir": doc_dir,
        "pages": pages_meta,
        "page_count": len(pages_meta),
        "selected_pages": selected_pages,
        "page_map": extra.get("page_map", {1: 1}),
        "ignored_scanned_pages": extra.get("ignored_scanned_pages", []),
        "det_err": extra.get("det_err"),
        "uploaded_at": round(time.time(), 2),
    }
    stage("Describing what was read", 95)
    understood_summary, counts, unreadable = describe_store(st, doc_meta)
    if convert_note: unreadable = [convert_note + "."] + list(unreadable)
    doc_meta["understood_summary"] = understood_summary
    doc_meta["counts"] = counts
    doc_meta["unreadable"] = unreadable
    doc_meta["suggested_questions"] = suggest_questions(st, doc_meta)

    doc_meta["timings"] = _timings()
    with _MANIFEST_LOCK:        # several files of one session can be read at the same time; the list of documents is updated one at a time
        manifest = load_manifest(sid)
        manifest["documents"].append(doc_meta)
        save_manifest(sid, manifest)
    return public_doc_view(doc_meta)


def update_selected_pages(session_id: str, doc_id: str, selected_pages: list[int]) -> dict:
    """Re-select pages on an uploaded multi-page PDF and refresh its Store, summary, and suggested questions."""
    sid = clean_id(session_id, "s")
    manifest = load_manifest(sid)
    doc_meta = next((d for d in manifest.get("documents", []) if d["doc_id"] == doc_id), None)
    if not doc_meta:
        raise IngestError("Document not found in session.", 404)
    if doc_meta["kind"] != "pdf":
        return public_doc_view(doc_meta)

    ensure_inside_sessions(doc_meta["saved_path"])
    query_path, active_mode, extra = _build_pdf_selection(
        doc_meta["doc_dir"], doc_id, doc_meta["saved_path"], doc_meta["pages"], selected_pages
    )
    st, _ = ASK.store_for(query_path)
    if st.fmt == "raster":
        png_bytes = RENDER.audit_image(st, AUDIT.audit(st), size=1400)
        if png_bytes:
            open(os.path.join(doc_meta["doc_dir"], "audit.png"), "wb").write(png_bytes)

    doc_meta["query_path"] = query_path
    doc_meta["active_mode"] = active_mode
    doc_meta["format"] = st.fmt
    doc_meta["selected_pages"] = extra.get("active_pages", selected_pages)
    doc_meta["page_map"] = extra.get("page_map", {1: 1})
    doc_meta["ignored_scanned_pages"] = extra.get("ignored_scanned_pages", [])
    doc_meta["det_err"] = extra.get("det_err")
    understood_summary, counts, unreadable = describe_store(st, doc_meta)
    doc_meta["understood_summary"] = understood_summary
    doc_meta["counts"] = counts
    doc_meta["unreadable"] = unreadable
    doc_meta["suggested_questions"] = suggest_questions(st, doc_meta)
    save_manifest(sid, manifest)
    return public_doc_view(doc_meta)


def public_doc_view(doc_meta: dict) -> dict:
    """Safe client-facing metadata (never exposes server filesystem paths)."""
    sid = doc_meta["session_id"]
    did = doc_meta["doc_id"]
    pages_out = []
    for p in doc_meta.get("pages", []):
        pno = p["page"]
        pages_out.append({
            **p,
            "selected": pno in (doc_meta.get("selected_pages") or [1]),
            "thumb_url": f"/api/chat/thumb?session_id={sid}&doc_id={did}&page={pno}&kind=thumb",
            "preview_url": f"/api/chat/thumb?session_id={sid}&doc_id={did}&page={pno}&kind=preview",
        })
    has_audit = os.path.exists(os.path.join(doc_meta["doc_dir"], "audit.png"))
    return {
        "doc_id": did,
        "session_id": sid,
        "display_name": doc_meta["display_name"],
        "size_bytes": doc_meta["size_bytes"],
        "kind": doc_meta["kind"],
        "format": doc_meta["format"],
        "active_mode": doc_meta.get("active_mode"),
        "timings": doc_meta.get("timings"),
        "page_count": doc_meta["page_count"],
        "selected_pages": doc_meta.get("selected_pages", [1]),
        "pages": pages_out,
        "thumb_url": f"/api/chat/thumb?session_id={sid}&doc_id={did}&page={doc_meta.get('selected_pages', [1])[0]}&kind=thumb",
        "overlay_url": f"/api/chat/overlay?session_id={sid}&doc_id={did}" if doc_meta.get("format") == "raster" else None,
        "preview_url": (
            f"/api/chat/thumb?session_id={sid}&doc_id={did}&kind=plain"
            if doc_meta.get("format") == "raster"
            else f"/api/chat/thumb?session_id={sid}&doc_id={did}&kind=audit"
            if has_audit
            else f"/api/chat/thumb?session_id={sid}&doc_id={did}&page={doc_meta.get('selected_pages', [1])[0]}&kind=preview"
        ),
        "understood_summary": doc_meta["understood_summary"],
        "counts": doc_meta["counts"],
        "unreadable": doc_meta["unreadable"],
        "suggested_questions": doc_meta["suggested_questions"],
        "uploaded_at": doc_meta["uploaded_at"],
    }
