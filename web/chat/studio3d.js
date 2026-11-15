/* 3D view of the studio page. Draws the scene built by bp/scene3d.py with three.js (r160, MIT, vendored next to this file).
   Ported from the team's viewer (stoajunction web/app.js) and cut down to what this page needs:
     open(scene, hooks)   build the scene and show it
     paint(state)         colour rooms by the plan check, the selection and the last answer; pins on failed rooms
     focus(id), reset(), close()
   The page stays the owner of all state: this file only draws and reports clicks back through the hooks. */
import * as THREE from "three";
import { OrbitControls } from "/chat/OrbitControls.js";

const INK = 0x1a211c;
const LOOK = {
  wall: { c: 0xf4f4f1, edge: 0.38 }, floor: { c: 0xfbfbf9, edge: 0.16 }, slab: { c: 0xe3e6e2, edge: 0.2 }, glass: { c: 0x9cc9e6, o: 0.38 },
  window: { c: 0x9cc9e6, o: 0.45, edge: 0.25 }, door: { c: 0xc9a27a, edge: 0.3 }, roof: { c: 0xb9bfb9, o: 0.16 }, covering: { c: 0xd9ddd9, o: 0.12 },
  space: { c: 0x7fb0a0, o: 0.0 }, column: { c: 0xc9cdc9, edge: 0.3 }, beam: { c: 0xc9cdc9, edge: 0.2 }, stair: { c: 0xd8d2c4, edge: 0.3 },
  railing: { c: 0x9aa29b }, furniture: { c: 0xd3d7d3, edge: 0.18 }, fixture: { c: 0xdfe7ea, edge: 0.2 }, mep: { c: 0x7fb0a0 }, other: { c: 0xcfd4cf, edge: 0.15 },
  device: { c: 0x163028 }, device2: { c: 0xc46a00 }, seat: { c: 0xe0a45c, edge: 0.3 },
};
const PAINT = { fail: 0xe2574c, pass: 0xbfe3cd, not_checked: 0xe6e8e4, answer: 0xf7a23b, picked: 0x7fa596, selected: 0xcfe0d6 };

let host, pinsEl, renderer, scene, camera, controls, group, raf = 0, dirty = true, open_ = false;
let meshes = [], byId = new Map(), rooms = [], pins = [], hooks = {}, data = null, extraGroup = null, mats = {};
const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();

function mat(cat) {
  if (mats[cat]) return mats[cat];
  const L = LOOK[cat] || LOOK.other, tr = L.o != null && L.o < 1;
  return (mats[cat] = new THREE.MeshLambertMaterial({ color: L.c, transparent: tr, opacity: tr ? L.o : 1, depthWrite: !tr, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 }));
}
const flat = (c, o) => new THREE.MeshLambertMaterial({ color: c, transparent: o != null, opacity: o == null ? 1 : o, depthWrite: o == null, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
const HI = {}; const hi = (k) => HI[k] || (HI[k] = flat(PAINT[k]));
let dimMat = null;

function geomOf(e) {
  if (e.kind === "prism") {
    const sh = new THREE.Shape(e.outer.map((p) => new THREE.Vector2(p[0], p[1])));
    (e.holes || []).forEach((h) => sh.holes.push(new THREE.Path(h.map((p) => new THREE.Vector2(p[0], p[1])))));
    const g = new THREE.ExtrudeGeometry(sh, { depth: e.h, bevelEnabled: false });
    g.rotateX(Math.PI / 2); g.translate(0, e.y0 + e.h, 0);      /* the shape's y becomes the scene's z; the extrusion becomes height */
    return g;
  }
  if (e.kind === "mesh") {
    const g = new THREE.BufferGeometry(); g.setAttribute("position", new THREE.Float32BufferAttribute(e.verts, 3)); g.setIndex(e.faces); g.computeVertexNormals(); return g;
  }
  if (e.kind === "cyl") { const g = new THREE.CylinderGeometry(e.r, e.r, e.h, 20); g.translate(e.pos[0], e.pos[1], e.pos[2]); return g; }
  const g = new THREE.BoxGeometry(e.size[0], e.size[1], e.size[2]); g.translate(e.pos[0], e.pos[1], e.pos[2]); return g;
}

function init(el) {
  host = el; pinsEl = el.querySelector(".v3-pins");
  const canvas = el.querySelector("canvas");
  try { renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false }); }
  catch (e) { throw new Error("The 3D view could not start in this browser (WebGL is not available)."); }
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1)); renderer.outputColorSpace = THREE.SRGBColorSpace;
  scene = new THREE.Scene(); scene.background = new THREE.Color(0xffffff);
  camera = new THREE.PerspectiveCamera(36, 1, 0.1, 4000);
  controls = new OrbitControls(camera, canvas); controls.enableDamping = true; controls.dampingFactor = 0.12; controls.maxPolarAngle = Math.PI / 2 - 0.02; controls.screenSpacePanning = false;
  controls.addEventListener("change", () => { dirty = true; });
  scene.add(new THREE.HemisphereLight(0xffffff, 0xdfe5e0, 1.9));
  const sun = new THREE.DirectionalLight(0xffffff, 1.5); sun.position.set(0.6, 1, 0.45); scene.add(sun);
  const back = new THREE.DirectionalLight(0xffffff, 0.5); back.position.set(-0.7, 0.5, -0.6); scene.add(back);
  window.addEventListener("resize", size);
  let down = null;
  canvas.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY, b: e.button }; });
  canvas.addEventListener("pointerup", (e) => {
    if (!down || Math.abs(e.clientX - down.x) + Math.abs(e.clientY - down.y) > 5) { down = null; return; }
    const b = down.b; down = null; const hit = pick(e.clientX, e.clientY);
    if (b === 2) { if (hit && hit.room && hooks.onMenu) hooks.onMenu(hit.id, e.clientX, e.clientY); return; }
    if (b !== 0) return;
    if (!hit) { if (hooks.onNothing) hooks.onNothing(); return; }
    if (hit.room) { if (hooks.onRoom) hooks.onRoom(hit.id, e); } else if (hooks.onElement) hooks.onElement(hit);
  });
}
function size() {
  if (!open_) return; const w = host.clientWidth, h = host.clientHeight;
  renderer.setSize(w, h, false); camera.aspect = w / Math.max(1, h); camera.updateProjectionMatrix(); dirty = true;
}
function pick(x, y) {
  const r = renderer.domElement.getBoundingClientRect(); ptr.set(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1); ray.setFromCamera(ptr, camera);
  /* a traced sheet: a click means the room under the pointer, whatever wall stands in front of it. A model: the element in front. */
  const pool = rooms.length ? meshes.filter((m) => m.userData.cat === "floor") : meshes.filter((m) => m.visible && m.userData.cat !== "space" && m.material !== dimMat);
  const h = ray.intersectObjects(pool, false)[0]; if (!h) return null;
  return { ...h.object.userData.el, room: h.object.userData.cat === "floor" };
}
function loop() {
  raf = requestAnimationFrame(loop);
  if (controls.update() || dirty) { dirty = false; renderer.render(scene, camera); placePins(); }
}
const v3 = new THREE.Vector3();
function placePins() {
  const w = host.clientWidth, h = host.clientHeight;
  pins.forEach((p) => { v3.copy(p.pos).project(camera); const off = v3.z > 1 || v3.z < -1; p.el.style.display = off ? "none" : ""; p.el.style.left = ((v3.x + 1) / 2) * w + "px"; p.el.style.top = ((1 - v3.y) / 2) * h + "px"; });
}

function clear() {
  if (group) { group.traverse((o) => { if (o.geometry) o.geometry.dispose(); }); scene.remove(group); }
  group = null; extraGroup = null; meshes = []; byId = new Map(); rooms = []; pins = []; if (pinsEl) pinsEl.innerHTML = "";
}
function build(sc) {
  clear(); data = sc; group = new THREE.Group(); scene.add(group); rooms = sc.rooms || [];
  const edgeMat = {};
  for (const e of sc.elements) {
    let g; try { g = geomOf(e); } catch (err) { continue; }
    const L = LOOK[e.cat] || LOOK.other, m = new THREE.Mesh(g, mat(e.cat));
    m.userData = { cat: e.cat, el: { id: e.id, name: e.name, cat: e.cat, ifc_class: e.ifc_class, level: e.level, area: e.area }, base: mat(e.cat) };
    if (e.cat === "space") m.visible = false;
    if (L.edge && (g.index ? g.index.count : g.attributes.position.count) < 9000) {
      const ln = new THREE.LineSegments(new THREE.EdgesGeometry(g, 28), edgeMat[L.edge] || (edgeMat[L.edge] = new THREE.LineBasicMaterial({ color: INK, transparent: true, opacity: L.edge })));
      ln.raycast = () => {}; m.add(ln);
    }
    group.add(m); meshes.push(m);
    if (e.id) { if (!byId.has(e.id)) byId.set(e.id, []); byId.get(e.id).push(m); }
  }
  extraGroup = new THREE.Group(); group.add(extraGroup);
}
function reset() {
  const s = (data && data.size) || [20, 3, 20], span = Math.max(s[0], s[2], s[1] * 1.6), d = (span * 0.9) / Math.tan((camera.fov * Math.PI) / 360) / Math.max(0.75, Math.min(1.6, camera.aspect));
  controls.target.set(0, 0, span * 0.04); camera.position.set(d * 0.36, d * 0.68, d * 0.66); camera.near = Math.max(0.05, span / 800); camera.far = span * 40; camera.updateProjectionMatrix();
  controls.minDistance = 1.5; controls.maxDistance = span * 6; controls.update(); dirty = true;
}
function focus(id) {
  const ms = byId.get(id); if (!ms) return;
  const box = new THREE.Box3(); ms.forEach((m) => box.expandByObject(m)); const c = box.getCenter(new THREE.Vector3()), sz = box.getSize(new THREE.Vector3());
  const dir = camera.position.clone().sub(controls.target).normalize(); if (dir.y < 0.45) { dir.y = 0.6; dir.normalize(); }
  const d = Math.max(14, Math.max(sz.x, sz.z, sz.y) * 3.2);
  controls.target.copy(c); camera.position.copy(c).addScaledVector(dir, d); controls.update(); dirty = true;
}

/* state: { marks: {roomId: "fail" | "pass" | "not_checked"}, selected, picked: Set, answer: Set, pins: [{id, text, n}], path: [[fx, fy]..] } */
function paint(st) {
  if (!open_ || !data) return; st = st || {};
  const marks = st.marks || {}, picked = st.picked || new Set(), ans = st.answer || new Set(), model = !rooms.length, dimOthers = model && ans.size > 0;
  if (dimOthers && !dimMat) dimMat = flat(0xd9ddd9, 0.07);
  meshes.forEach((m) => {
    const u = m.userData, id = u.el.id; let mm = u.base, show = u.cat !== "space";
    if (u.cat === "floor") { mm = ans.has(id) ? hi("answer") : marks[id] === "fail" ? hi("fail") : picked.has(id) ? hi("picked") : id === st.selected ? hi("selected") : marks[id] ? hi(marks[id]) : u.base; }
    else if (model) { if (id && ans.has(id)) { mm = hi("answer"); show = true; } else if (id && id === st.selected) mm = hi("picked"); else if (dimOthers) mm = dimMat; }
    m.material = mm; m.visible = show; m.children.forEach((c) => { c.visible = mm !== dimMat; });
  });
  /* pins on failed rooms: the same red mark as on the sheet, standing above the room */
  pinsEl.innerHTML = ""; pins = [];
  (st.pins || []).forEach((p) => {
    const r = rooms.find((x) => x.id === p.id); if (!r) return;
    const b = document.createElement("button"); b.type = "button"; b.className = "pin"; b.innerHTML = `!${p.n > 1 ? `<span class="n">${p.n}</span>` : ""}<span class="pin-label"></span>`; b.querySelector(".pin-label").textContent = p.text || "";
    b.onclick = (e) => { e.stopPropagation(); if (hooks.onPin) hooks.onPin(p.id); };
    pinsEl.appendChild(b); pins.push({ el: b, pos: new THREE.Vector3(r.c[0], (data.size[1] || 2.7) + 0.9, r.c[1]) });
  });
  /* way out: the path found on the sheet, laid on the floor */
  while (extraGroup.children.length) { const c = extraGroup.children.pop(); c.geometry.dispose(); }
  if (st.path && data.frame && data.origin) {
    const P = st.path.map((q) => new THREE.Vector3(q[0] * data.frame[0] - data.origin[0], 0.18, q[1] * data.frame[1] - data.origin[1])), gm = flat(0x0a8f4d);
    for (let i = 0; i + 1 < P.length; i++) {
      const a = P[i], b = P[i + 1], len = a.distanceTo(b); if (len < 0.05) continue;
      const g = new THREE.BoxGeometry(len, 0.14, 0.34), m = new THREE.Mesh(g, gm); m.position.copy(a).add(b).multiplyScalar(0.5); m.rotation.y = -Math.atan2(b.z - a.z, b.x - a.x); m.raycast = () => {}; extraGroup.add(m);
    }
    P.forEach((p, i) => { const g = new THREE.CylinderGeometry(i === P.length - 1 ? 0.5 : 0.28, i === P.length - 1 ? 0.5 : 0.28, 0.2, 20), m = new THREE.Mesh(g, gm); m.position.copy(p); m.raycast = () => {}; extraGroup.add(m); });
  }
  dirty = true;
}

window.Studio3D = {
  open(el, sc, hk) {
    if (!renderer) init(el);
    hooks = hk || {}; host.hidden = false; open_ = true; build(sc); size(); reset();
    cancelAnimationFrame(raf); loop();
    return { elements: meshes.length };
  },
  close() { open_ = false; cancelAnimationFrame(raf); if (host) host.hidden = true; clear(); data = null; },
  paint, focus, reset,
  get isOpen() { return open_; },
};
window.dispatchEvent(new Event("studio3d-ready"));
