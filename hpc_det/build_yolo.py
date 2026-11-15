"""CubiCasa5k -> YOLO detection dataset (labels in F1_original.png pixels, the same image the hackathon ships).
Classes follow the counting conventions that reproduce the dataset's answers exactly:
 door (every Door group), window, toilet (Toilet), sink (Sink, DoubleSink, DoubleSinkRight, RoundSink, SideSink, CornerSink),
 shower (Shower), bathtub (Bathtub, BathtubRound), shower_cab (ShowerCab: drawn but NOT counted as a shower by the dataset),
 sauna_bench, plus room boxes: bedroom, kitchen, bath, sauna, balcony, outdoor_other.
Split: official train.txt for training, val.txt + test.txt held out (never trained on)."""
import os, sys, glob, pickle, collections as C
from concurrent.futures import ProcessPoolExecutor
import cc_svg
ROOT = "cubicasa5k"; OUT = "yolo"
NAMES = ["door", "window", "toilet", "sink", "shower", "bathtub", "shower_cab", "sauna_bench", "bedroom", "kitchen", "bathroom", "wc_room", "sauna_room", "balcony", "floor"]
SINK = {"Sink", "DoubleSink", "DoubleSinkRight", "RoundSink", "SideSink", "CornerSink"}
def cls_of(c, label=None):
    if c.startswith("Door"): return "door"
    if c.startswith("Window"): return "window"
    if c.startswith("FixedFurniture "):
        t = c.split()[-1]
        if t == "Toilet": return "toilet"
        if t in SINK: return "sink"
        if t == "Shower": return "shower"
        if t in ("Bathtub", "BathtubRound"): return "bathtub"
        if t == "ShowerCab": return "shower_cab"
        if t.startswith("SaunaBench"): return "sauna_bench"
        return None
    if c.startswith("Space Bedroom"): return "bedroom"
    if c.startswith("Space Kitchen"): return "kitchen"
    if c.startswith("Space Bath"): return "wc_room" if (label or "").startswith("WC") else "bathroom"
    if c.startswith("Space Outdoor Balcony"): return "balcony"
    return None
def room_items(r):
    """(class, box) for every labelled item; a sauna room = any space whose polygon contains a sauna bench (or Space Sauna)"""
    from shapely.geometry import Polygon, Point
    out = []; benches = [((i["box"][0] + i["box"][2]) / 2, (i["box"][1] + i["box"][3]) / 2) for i in r["items"] if i["cls"].startswith("FixedFurniture SaunaBench")]
    for it in r["items"]:
        k = cls_of(it["cls"], it.get("label"))
        if k: out.append((k, it["box"]))
        if it["cls"].startswith("Space") and len(it["poly"]) >= 3:
            try: pg = Polygon(it["poly"]).buffer(0)
            except Exception: continue
            if it["cls"].startswith("Space Sauna") or any(pg.contains(Point(b)) for b in benches): out.append(("sauna_room", it["box"]))
    for b in r.get("floor_boxes", []): out.append(("floor", b))
    return out
def one(args):
    d, split = args; name = d.strip("/").replace("/", "-")
    try: r = cc_svg.parse(ROOT + "/" + d.strip("/"))
    except Exception as e: return name, None
    W, H = r["size"]; lines = []
    for k, box in room_items(r):
        x0, y0, x1, y1 = box; x0, x1 = max(0, x0), min(W, x1); y0, y1 = max(0, y0), min(H, y1)
        if x1 - x0 < 2 or y1 - y0 < 2: continue
        if k in ("door", "window"):       # thin wall-embedded boxes: pad so the symbol (arc / frame) is inside the box
            pad = 0.15 * max(x1 - x0, y1 - y0); x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
        lines.append(f"{NAMES.index(k)} {(x0+x1)/2/W:.6f} {(y0+y1)/2/H:.6f} {(x1-x0)/W:.6f} {(y1-y0)/H:.6f}")
    os.makedirs(f"{OUT}/images/{split}", exist_ok=True); os.makedirs(f"{OUT}/labels/{split}", exist_ok=True)
    dst = f"{OUT}/images/{split}/{name}.png"
    if not os.path.exists(dst): os.symlink(os.path.abspath(f"{ROOT}/{d.strip('/')}/F1_original.png"), dst)
    open(f"{OUT}/labels/{split}/{name}.txt", "w").write("\n".join(lines))
    return name, r
if __name__ == "__main__":
    jobs = []
    for split, files in (("train", ["train.txt"]), ("val", ["val.txt", "test.txt"])):
        for f in files: jobs += [(l.strip(), split) for l in open(f"{ROOT}/{f}") if l.strip()]
    with ProcessPoolExecutor(48) as ex: R = list(ex.map(one, jobs, chunksize=16))
    pickle.dump({n: r for n, r in R if r}, open("parsed_all.pkl", "wb"))
    open(f"{OUT}/data.yaml", "w").write(f"path: {os.path.abspath(OUT)}\ntrain: images/train\nval: images/val\nnames:\n" + "".join(f"  {i}: {n}\n" for i, n in enumerate(NAMES)))
    c = C.Counter(int(l.split()[0]) for f in glob.glob(f"{OUT}/labels/*/*.txt") for l in open(f) if l.strip())
    print(len(R), "images;", {NAMES[k]: v for k, v in sorted(c.items())})
