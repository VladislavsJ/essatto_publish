"""Build the SAMPLE files for the regulation-check demo from the school plan that the app already processed.

  python demo/make_school_demo.py <school_plan.png> <sheet.db of that plan> <detections.json of that plan>

Writes into demo/:
  school_plan_demo.png        the plan with one classroom re-furnished for 30 pupils (15 double desks, 30 chairs)
  school_fire_plan_demo.png   the same plan as a fire detection layout: a detector symbol in every room EXCEPT the kitchen
  school_demo.json            what was planted: seats per room, detector positions, which room has none
  school_sheet.db / school_detections.json   the room database and detections of the plan (so the demo loads at once)

Everything planted here is a sample with a deliberate error. The room outlines, areas and names are the real ones the
tracer computed from the drawing; only the furniture of one room and the fire symbols are added."""
import os, sys, json, sqlite3, shutil, hashlib
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", str(2 ** 40))
import cv2, numpy as np
from shapely.geometry import Polygon, Point, box
from shapely.ops import polylabel

HERE = os.path.dirname(os.path.abspath(__file__))
img_path, db_path, det_path = sys.argv[1:4]
CLASSROOM_ID = int(os.environ.get("DEMO_CLASSROOM", "22")); SEATS = 30
RED = (40, 40, 215); GREY = (165, 165, 165)

im = cv2.imread(img_path, cv2.IMREAD_COLOR); H, W = im.shape[:2]
con = sqlite3.connect(db_path)
mpp = con.execute("select m_per_px from sheet").fetchone()[0]; ppm = 1.0 / mpp
rooms = [{"id": r[0], "building": r[1], "name": r[2], "area": r[3], "poly": json.loads(r[4])} for r in con.execute("select id, building, name, area_m2, polygon_px from room")]
by_id = {r["id"]: r for r in rooms}

# ---- 1. one classroom furnished for 30 pupils
room = by_id[CLASSROOM_ID]; P = Polygon(room["poly"]).buffer(0); inner = P.buffer(-0.30 * ppm)
mask = np.zeros((H, W), np.uint8); cv2.fillPoly(mask, [np.array(room["poly"], np.int32)], 1)
gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
furn = (mask > 0) & (gray > 110) & (gray < 245)                       # the light-grey furniture lines; walls and the room name are black
furn = cv2.dilate(furn.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
furn &= (mask > 0) & (gray > 60)
im[furn] = (255, 255, 255)
dw, dh, cr = 1.20 * ppm, 0.55 * ppm, 0.21 * ppm                        # double desk 1.2 x 0.55 m, chair radius 0.21 m
x0, y0, x1, y1 = inner.bounds
best = None
for gapx in (0.25, 0.20, 0.15, 0.10):
    for gapy in (0.75, 0.65, 0.55):
        pts = []
        y = y0 + 0.2 * ppm
        while y + dh + 2 * cr < y1:
            x = x0 + 0.1 * ppm
            while x + dw < x1:
                cell = box(x, y, x + dw, y + dh + 2 * cr + 4)
                if inner.contains(cell): pts.append((x, y))
                x += dw + gapx * ppm
            y += dh + 2 * cr + gapy * ppm
        if len(pts) >= SEATS // 2 and (best is None or len(pts) < len(best)): best = pts
if best is None: sys.exit("could not fit 15 double desks into the room")
# keep 15 desks, dropping those that would cover the room name (centre of the room) last
cx, cy = P.representative_point().x, P.representative_point().y
best.sort(key=lambda q: (round(q[1]), q[0])); desks = best[:SEATS // 2]
for x, y in desks:
    cv2.rectangle(im, (int(x), int(y)), (int(x + dw), int(y + dh)), GREY, 3)
    for k in (0.27, 0.73):
        c = (int(x + dw * k), int(y + dh + 2)); cv2.ellipse(im, c, (int(cr), int(cr)), 0, 0, 180, GREY, 3); cv2.line(im, (c[0] - int(cr), c[1]), (c[0] + int(cr), c[1]), GREY, 3)
seat_boxes = [{"room": CLASSROOM_ID, "box": [int(x + dw * k_ - cr), int(y + dh), int(x + dw * k_ + cr), int(y + dh + cr + 4)]} for x, y in desks for k_ in (0.27, 0.73)]
cv2.imwrite(os.path.join(HERE, "school_plan_demo.png"), im, [cv2.IMWRITE_PNG_COMPRESSION, 6])

# ---- 2. fire detection and alarm layout, drawn the way such layouts are drawn:
#   devices with the usual symbols (S smoke, H heat, call point at exits, sounder, control panel),
#   wired as loops from the panel with cable runs shown, zone labels and a legend.
#   Every room gets a detector EXCEPT the kitchen: that is the planted error, and the loop cable passes it by.
del gray, mask, furn
fire = im; detectors = []; missing = []; F = cv2.FONT_HERSHEY_SIMPLEX      # the plan image is already saved; draw the fire layer on it in place
DARK = (30, 30, 170); R = int(0.24 * ppm)
ops = [{"kind": k_, "seg": (x0_, y0_, x1_, y1_), "rooms": json.loads(rs or "[]")} for k_, x0_, y0_, x1_, y1_, rs in con.execute("select kind, x0_px, y0_px, x1_px, y1_px, rooms from opening")]

def main_poly(r):
    Pg = Polygon(r["poly"]).buffer(0)
    return max(Pg.geoms, key=lambda g: g.area) if Pg.geom_type != "Polygon" else Pg

def spot(Pg, rr, prefer=((0.25, -0.28), (-0.25, -0.28), (0.25, 0.28), (-0.25, 0.28), (0, -0.3), (0, 0))):
    c = polylabel(Pg, tolerance=2.0); bx0, by0, bx1, by1 = Pg.bounds
    cand = [Point(c.x + dx * (bx1 - bx0), c.y + dy * (by1 - by0)) for dx, dy in prefer]
    return next((q for q in cand if Pg.buffer(-rr * 1.3).contains(q)), c)

def sym_detector(img, x, y, rr, letter):
    cv2.circle(img, (x, y), rr, (255, 255, 255), -1); cv2.circle(img, (x, y), rr, RED, max(3, rr // 6))
    sc = rr / 24; (tw, th), _ = cv2.getTextSize(letter, F, sc, max(2, rr // 6))
    cv2.putText(img, letter, (x - tw // 2, y + th // 2), F, sc, RED, max(2, rr // 6), cv2.LINE_AA)

def sym_callpoint(img, x, y, rr):                 # manual call point: red square, white field, black dot
    cv2.rectangle(img, (x - rr, y - rr), (x + rr, y + rr), RED, -1); cv2.rectangle(img, (x - rr // 2, y - rr // 2), (x + rr // 2, y + rr // 2), (255, 255, 255), -1)
    cv2.circle(img, (x, y), max(3, rr // 5), (0, 0, 0), -1)

def sym_sounder(img, x, y, rr):                   # sounder: circle with a horn
    cv2.circle(img, (x, y), rr, (255, 255, 255), -1); cv2.circle(img, (x, y), rr, RED, max(3, rr // 6))
    pts = np.array([[x - rr // 2, y - rr // 4], [x - rr // 8, y - rr // 4], [x + rr // 2, y - rr // 2 - 2], [x + rr // 2, y + rr // 2 + 2], [x - rr // 8, y + rr // 4], [x - rr // 2, y + rr // 4]], np.int32)
    cv2.fillPoly(img, [pts], RED)

def sym_panel(img, x, y, w_, h_):                 # fire alarm control panel
    cv2.rectangle(img, (x - w_ // 2, y - h_ // 2), (x + w_ // 2, y + h_ // 2), (255, 255, 255), -1); cv2.rectangle(img, (x - w_ // 2, y - h_ // 2), (x + w_ // 2, y + h_ // 2), RED, 6)
    (tw, th), _ = cv2.getTextSize("FACP", F, 1.5, 4); cv2.putText(img, "FACP", (x - tw // 2, y + th // 2), F, 1.5, RED, 4, cv2.LINE_AA)

devices = []                                      # (building, x, y, kind, room)
for r in rooms:
    name = (r["name"] or "").lower(); Pg = main_poly(r)
    if Pg.is_empty: continue
    if "kitchen" in name: missing.append(r["id"]); continue
    small = Pg.area * mpp * mpp <= 4; rr = int(R * 0.7) if small else R
    pt = spot(Pg, rr); x, y = int(pt.x), int(pt.y)
    letter = "H" if ("plant" in name or "laundry" in name) else "S"      # heat detectors where steam or dust would trip a smoke head
    devices.append({"b": r["building"], "x": x, "y": y, "kind": "det", "letter": letter, "rr": rr, "room": r["id"]})
    detectors.append({"room": r["id"], "type": "heat" if letter == "H" else "smoke", "x": x, "y": y, "r": rr})
    if any(w_ in name for w_ in ("corridor", "hall", "lobby")) and not small:          # sounders in circulation and assembly spaces
        q = spot(Pg, R, prefer=((-0.3, 0.3), (0.3, 0.3), (-0.3, -0.3), (0.3, -0.3)))
        if abs(q.x - x) + abs(q.y - y) > 4 * R: devices.append({"b": r["building"], "x": int(q.x), "y": int(q.y), "kind": "sounder", "room": r["id"]})
# manual call points beside the final exits (doors with a room on one side only)
for o in ops:
    if o["kind"] != "door" or len(o["rooms"]) != 1 or o["rooms"][0] not in by_id: continue
    r = by_id[o["rooms"][0]]; Pg = main_poly(r); x0_, y0_, x1_, y1_ = o["seg"]; mx, my = (x0_ + x1_) / 2, (y0_ + y1_) / 2
    c = Pg.representative_point(); horiz = abs(x1_ - x0_) >= abs(y1_ - y0_); d = 0.55 * ppm; side = 0.75 * ppm + abs(x1_ - x0_ if horiz else y1_ - y0_) / 2
    cand = [(mx + side, my + (d if c.y > my else -d)), (mx - side, my + (d if c.y > my else -d))] if horiz else [(mx + (d if c.x > mx else -d), my + side), (mx + (d if c.x > mx else -d), my - side)]
    q = next((q for q in cand if Pg.buffer(-R * 0.9).contains(Point(q))), None)
    if q and "kitchen" not in (r["name"] or "").lower(): devices.append({"b": r["building"], "x": int(q[0]), "y": int(q[1]), "kind": "mcp", "room": r["id"]})

# control panel at the main entrance (the room named Reception / Entrance Lobby), one loop per building
pr = next((r for r in rooms if "reception" in (r["name"] or "").lower() or "entrance" in (r["name"] or "").lower()), rooms[0])
pp = spot(main_poly(pr), R * 2, prefer=((0.0, 0.32), (0.3, 0.3), (-0.3, 0.3), (0, 0))); panel = (int(pp.x), int(pp.y))

def route(devs, start):
    """order the devices into one short cable run starting at the panel: nearest neighbour, then 2-opt"""
    left = list(devs); path = []; cur = start
    while left:
        j = min(range(len(left)), key=lambda i: abs(left[i]["x"] - cur[0]) + abs(left[i]["y"] - cur[1])); d_ = left.pop(j); path.append(d_); cur = (d_["x"], d_["y"])
    pts = [start] + [(d_["x"], d_["y"]) for d_ in path]
    def L(a_, b_): return abs(a_[0] - b_[0]) + abs(a_[1] - b_[1])
    improved = True; rounds = 0
    while improved and rounds < 30:
        improved = False; rounds += 1
        for i in range(1, len(pts) - 2):
            for j in range(i + 1, len(pts) - 1):
                if L(pts[i - 1], pts[i]) + L(pts[j], pts[j + 1]) > L(pts[i - 1], pts[j]) + L(pts[i], pts[j + 1]) + 1:
                    pts[i:j + 1] = reversed(pts[i:j + 1]); path[i - 1:j] = reversed(path[i - 1:j]); improved = True
    return pts

loops = []
for bi, b in enumerate(sorted({d_["b"] for d_ in devices}), 1):
    devs = [d_ for d_ in devices if d_["b"] == b]
    pts = route(devs, panel); loops.append((bi, pts))
    for i in range(len(pts) - 1):                 # cable run: horizontal then vertical leg, drawn under the symbols
        (xa, ya), (xb, yb) = pts[i], pts[i + 1]
        cv2.line(fire, (xa, ya), (xb, ya), DARK, 4, cv2.LINE_AA); cv2.line(fire, (xb, ya), (xb, yb), DARK, 4, cv2.LINE_AA)
        if i % 9 == 4 and abs(xb - xa) > 260:
            tx = (xa + xb) // 2 - 40; cv2.rectangle(fire, (tx - 8, ya - 44), (tx + 96, ya - 6), (255, 255, 255), -1); cv2.putText(fire, f"L{bi}", (tx, ya - 12), F, 1.1, DARK, 3, cv2.LINE_AA)
for d_ in devices:
    if d_["kind"] == "det": sym_detector(fire, d_["x"], d_["y"], d_["rr"], d_["letter"])
    elif d_["kind"] == "mcp": sym_callpoint(fire, d_["x"], d_["y"], int(R * 0.8))
    else: sym_sounder(fire, d_["x"], d_["y"], R)
sym_panel(fire, panel[0], panel[1], int(1.1 * ppm), int(0.5 * ppm))
# zone labels above each building
for bi, b in enumerate(sorted({r["building"] for r in rooms}), 1):
    xs_ = [p_[0] for r in rooms if r["building"] == b for p_ in r["poly"]]; ys_ = [p_[1] for r in rooms if r["building"] == b for p_ in r["poly"]]
    cv2.putText(fire, f"ZONE {bi}  /  LOOP {bi}", (int(min(xs_)), int(min(ys_)) - 230), F, 2.6, DARK, 7, cv2.LINE_AA)
# legend in the empty upper right of the sheet
lx, ly = int(W * 0.60), int(H * 0.06); LW, LH = 4700, 2050
cv2.rectangle(fire, (lx, ly), (lx + LW, ly + LH), (255, 255, 255), -1); cv2.rectangle(fire, (lx, ly), (lx + LW, ly + LH), (0, 0, 0), 5)
cv2.putText(fire, "FIRE DETECTION AND ALARM LAYOUT", (lx + 90, ly + 190), F, 4.6, (0, 0, 0), 11, cv2.LINE_AA)
cv2.putText(fire, "Level 00   -   addressable system, 2 loops", (lx + 90, ly + 350), F, 3.0, (0, 0, 0), 7, cv2.LINE_AA)
cv2.line(fire, (lx + 60, ly + 420), (lx + LW - 60, ly + 420), (0, 0, 0), 4)
rows_ = [("det_s", "Automatic smoke detector"), ("det_h", "Automatic heat detector"), ("mcp", "Manual call point"), ("sounder", "Alarm sounder"), ("panel", "Fire alarm control panel"), ("cable", "Loop cable, fire resistant")]
for i, (k_, txt) in enumerate(rows_):
    yy = ly + 570 + i * 215; xx = lx + 230
    if k_ == "det_s": sym_detector(fire, xx, yy, 70, "S")
    elif k_ == "det_h": sym_detector(fire, xx, yy, 70, "H")
    elif k_ == "mcp": sym_callpoint(fire, xx, yy, 60)
    elif k_ == "sounder": sym_sounder(fire, xx, yy, 70)
    elif k_ == "panel": sym_panel(fire, xx, yy, 250, 120)
    else: cv2.line(fire, (xx - 120, yy), (xx + 120, yy), DARK, 8, cv2.LINE_AA)
    cv2.putText(fire, txt, (lx + 470, yy + 40), F, 3.3, (0, 0, 0), 7, cv2.LINE_AA)
cv2.putText(fire, "SAMPLE DRAWING FOR DEMONSTRATION - contains a planted error", (lx + 90, ly + LH - 80), F, 2.9, RED, 7, cv2.LINE_AA)
cv2.imwrite(os.path.join(HERE, "school_fire_plan_demo.png"), fire, [cv2.IMWRITE_PNG_COMPRESSION, 6])
other_devices = [{"kind": d_["kind"], "x": d_["x"], "y": d_["y"], "room": d_["room"]} for d_ in devices if d_["kind"] != "det"] + [{"kind": "panel", "x": panel[0], "y": panel[1], "room": pr["id"]}]

# ---- 3. what was planted + the cached analysis, keyed by file content so the app recognises these two files
def sha(p): return hashlib.sha1(open(p, "rb").read()).hexdigest()
shutil.copy(db_path, os.path.join(HERE, "school_sheet.db")); shutil.copy(det_path, os.path.join(HERE, "school_detections.json"))
side = {"note": "SAMPLE data planted for the regulation-check demo. Room outlines, areas and names are computed from the drawing; the furniture of one classroom and all fire symbols were added by demo/make_school_demo.py.",
        "sheet_db": "school_sheet.db", "detections": "school_detections.json", "building_use": "school", "pupils_total": 120,
        "files": {sha(os.path.join(HERE, "school_plan_demo.png")): {"name": "school_plan_demo.png", "layer": "architecture"},
                  sha(os.path.join(HERE, "school_fire_plan_demo.png")): {"name": "school_fire_plan_demo.png", "layer": "fire"}},
        "seats": {str(CLASSROOM_ID): SEATS}, "detectors": detectors, "rooms_without_detector": missing, "other_devices": other_devices, "seat_boxes": seat_boxes}
json.dump(side, open(os.path.join(HERE, "school_demo.json"), "w"), indent=1)
print(f"classroom {CLASSROOM_ID} '{room['name']}' {room['area']} m2: {len(desks)} double desks = {len(desks) * 2} seats")
print(f"devices: {sum(d_['kind'] == 'mcp' for d_ in devices)} call points, {sum(d_['kind'] == 'sounder' for d_ in devices)} sounders, panel in '{pr['name']}'")
print(f"detectors placed in {len(detectors)} rooms; none in rooms {missing} ({[by_id[i]['name'] for i in missing]})")
cl = [int(v) for v in Polygon(room['poly']).bounds]; cv2.imwrite(os.path.join(HERE, "_preview_classroom.jpg"), fire[cl[1] - 250:cl[3] + 250, cl[0] - 250:cl[2] + 250])
kb = [int(v) for v in Polygon(by_id[missing[0]]['poly']).bounds]; cv2.imwrite(os.path.join(HERE, "_preview_kitchen.jpg"), cv2.resize(fire[kb[1] - 900:kb[3] + 500, kb[0] - 2600:kb[2] + 500], None, fx=0.45, fy=0.45))
cv2.imwrite(os.path.join(HERE, "_preview_sheet.jpg"), cv2.resize(fire, (2000, int(2000 * H / W)), interpolation=cv2.INTER_AREA))
