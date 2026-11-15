"""Run the trained detector over every CubiCasa image, tune one confidence threshold per class for EXACT counts on
half of the held-out images, and report exact-count accuracy on the other half (never used for training or tuning).
Usage: predict.py <weights> <out_dir>
Writes <out_dir>/<image>.json = {"size":[W,H], "model":..., "thresholds":{cls:t}, "boxes":[[cls, conf, x0,y0,x1,y1], ...]} (all boxes with conf>=0.05)"""
import sys, os, glob, json, pickle, collections as C
from ultralytics import YOLO
from build_yolo import NAMES, room_items
w, out = sys.argv[1], sys.argv[2]; os.makedirs(out, exist_ok=True)
m = YOLO(w); P = pickle.load(open("parsed_all.pkl", "rb"))
res = {}
for split in ("val", "train"):
    fs = sorted(glob.glob(f"yolo/images/{split}/*.png"))
    for i in range(0, len(fs), 16):
        for f, r in zip(fs[i:i + 16], m.predict(fs[i:i + 16], imgsz=1536, conf=0.05, iou=0.6, max_det=800, verbose=False, device=0, half=True)):
            b = r.boxes; H, W = r.orig_shape
            res[os.path.basename(f)[:-4]] = {"split": split, "size": [W, H], "boxes": [[NAMES[int(c)], round(float(p), 3)] + [round(float(v), 1) for v in xy] for c, p, xy in zip(b.cls.tolist(), b.conf.tolist(), b.xyxy.tolist())]}
pickle.dump(res, open(out + "/_all.pkl", "wb"))
val = sorted(n for n, r in res.items() if r["split"] == "val"); tune, test = val[::2], val[1::2]
gt = {n: C.Counter(k for k, _ in room_items(P[n])) for n in val if n in P}
def acc(names, cls, t): return sum(sum(1 for b in res[n]["boxes"] if b[0] == cls and b[1] >= t) == gt[n][cls] for n in names if n in gt) / max(1, sum(n in gt for n in names))
th = {}; rows = []
for cls in NAMES:
    best = max((acc(tune, cls, t / 100), -abs(t - 35), t / 100) for t in range(8, 80, 2)); th[cls] = best[2]
    rows.append((cls, th[cls], round(100 * acc(tune, cls, th[cls]), 1), round(100 * acc(test, cls, th[cls]), 1), round(100 * acc(test, cls, 0.25), 1)))
print("class            thr  tune%  HELD-OUT%  (thr .25)")
for r in rows: print(f"{r[0]:15s} {r[1]:.2f}  {r[2]:5.1f}  {r[3]:5.1f}     {r[4]:5.1f}")
sb = lambda n: sum(1 for b in res[n]["boxes"] if b[0] in ("shower", "bathtub") and b[1] >= th[b[0]]) == gt[n]["shower"] + gt[n]["bathtub"]
print("showers+bathtubs (as asked) held-out:", round(100 * sum(sb(n) for n in test if n in gt) / len(test), 1))
json.dump({"thresholds": th, "rows": rows, "n_tune": len(tune), "n_test": len(test), "weights": w}, open(out + "/_report.json", "w"), indent=1)
for n, r in res.items():
    json.dump({"size": r["size"], "split": r["split"], "model": os.path.basename(os.path.dirname(os.path.dirname(w))), "thresholds": th, "boxes": r["boxes"]}, open(f"{out}/{n}.json", "w"))
