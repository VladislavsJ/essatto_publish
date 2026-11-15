/* Studio page: one drawing on screen, layers, plan check with pins, click to explain, right-click for regulations.
   Talks to the same server as the chat page (/api/chat/...). No drawing is repainted by the server: the page loads the
   plain image of each layer once and draws rooms, marks and boxes itself. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const NS = "http://www.w3.org/2000/svg";
  const SID = (() => { let s = sessionStorage.getItem("studio_sid"); if (!s) { s = "s_" + Math.random().toString(16).slice(2, 16); sessionStorage.setItem("studio_sid", s); } return s; })();
  const S = { layers: [], active: 0, zoom: 1, panX: 0, panY: 0, W: 0, H: 0, showRead: false, selected: null };
  /* which findings belong to which layer: the fire layout shows fire rules, the architecture plan shows the rest */
  const onLayer = (f, key) => (key === "fire") === String(f.check || "").startsWith("fire_");

  // ---------- busy / errors
  /* one loader. busy(text) = spinner; busy(text, true) = bar that follows the server's own steps for this session */
  let pollT = null;
  function busy(text, withBar) {
    $("busyText").textContent = text; $("busy").hidden = false; $("busyBarWrap").hidden = !withBar; $("busySpin").hidden = !!withBar; $("busyBar").style.width = withBar ? "4%" : "0";
    clearInterval(pollT); pollT = null;
    if (withBar) pollT = setInterval(async () => { try { const r = await fetch(`/api/chat/studio/progress?session_id=${SID}`); const d = await r.json(); if (d.text) { $("busyText").textContent = d.text; $("busyBar").style.width = Math.max(4, d.pct || 0) + "%"; } } catch (e) {} }, 450);
  }
  function idle() { clearInterval(pollT); pollT = null; $("busyBar").style.width = "100%"; $("busy").hidden = true; }
  async function api(url, opts) {
    const r = await fetch(url, opts); let d = null;
    try { d = await r.json(); } catch (e) { throw new Error("The server did not answer (" + r.status + ")"); }
    if (!r.ok || d.ok === false) throw new Error(d.error || ("Request failed (" + r.status + ")"));
    return d;
  }

  // ---------- view: pan and zoom
  function apply() {
    $("sheet").style.transform = `translate(${S.panX}px, ${S.panY}px) scale(${S.zoom})`;
    $("sheetPins").style.setProperty("--inv", String(1 / S.zoom));
    document.querySelectorAll("#sheetPins > *").forEach((el) => { el.style.transform = `translate(-50%, -50%) scale(${1 / S.zoom})`; });
  }
  function fit() {
    const st = $("stage"), z = Math.min(st.clientWidth / S.W, (st.clientHeight - 90) / S.H) * 0.96;
    S.zoom = z; S.panX = (st.clientWidth - S.W * z) / 2; S.panY = (st.clientHeight - 70 - S.H * z) / 2 + 8; apply();
  }
  function zoomAt(mx, my, f) {
    const z = Math.min(8, Math.max(0.02, S.zoom * f));
    S.panX = mx - ((mx - S.panX) / S.zoom) * z; S.panY = my - ((my - S.panY) / S.zoom) * z; S.zoom = z; apply();
  }
  function focusBox(b, pad) {
    if (S.is3d) { if (S.selected) window.Studio3D.focus(S.selected); return; }
    const st = $("stage"), w = (b[2] - b[0]) * S.W, h = (b[3] - b[1]) * S.H, cardW = $("card").hidden ? 0 : 420;
    const z = Math.min(1.1, (st.clientWidth - cardW) / (w * (pad || 3.2)), (st.clientHeight - 120) / (h * (pad || 3.2)));
    S.zoom = z; S.panX = (st.clientWidth - cardW) / 2 - ((b[0] + b[2]) / 2) * S.W * z; S.panY = (st.clientHeight - 80) / 2 - ((b[1] + b[3]) / 2) * S.H * z;
    apply();      /* no animated transform: the sheet can be over 100 megapixels and an animated scale makes browsers drop frames */
  }
  (function wire() {
    const st = $("stage"); let drag = null, moved = false;
    st.addEventListener("wheel", (e) => { if ($("sheet").hidden) return; e.preventDefault(); zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.18 : 1 / 1.18); }, { passive: false });
    st.addEventListener("pointerdown", (e) => { if (e.button !== 0 || $("sheet").hidden || e.target.closest(".pin")) return; drag = { x: e.clientX, y: e.clientY, px: S.panX, py: S.panY }; moved = false; });
    window.addEventListener("pointermove", (e) => { if (!drag) return; const dx = e.clientX - drag.x, dy = e.clientY - drag.y; if (Math.abs(dx) + Math.abs(dy) > 4) { moved = true; st.classList.add("dragging"); } S.panX = drag.px + dx; S.panY = drag.py + dy; apply(); });
    window.addEventListener("pointerup", () => { drag = null; st.classList.remove("dragging"); setTimeout(() => { moved = false; }, 0); });
    S.wasDrag = () => moved;
    window.addEventListener("resize", () => { if (!$("sheet").hidden) apply(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeMenu(); $("card").hidden = true; $("note").hidden = true; select(null); } });
    document.addEventListener("click", (e) => { if (!e.target.closest("#menu")) closeMenu(); });
  })();

  // ---------- layers
  const layer = () => S.layers[S.active];
  function renderLayerPill() {
    const p = $("layerPill");
    if (S.layers.length < 2) { p.hidden = true; return; }
    p.hidden = false;
    /* several uploaded files: a list, one row per file, with what kind of file it is and the result of its check */
    const kindOf = (l) => ({ raster: "drawing", ifc: "IFC model", "dxf-layered": "CAD", "pdf-vector": "PDF tables" }[l.format] || (l.format || ""));
    const dot = (l) => (l.check ? `<i class="dot ${l.check.findings.some((f) => f.status === "fail") ? "fail" : "pass"}"></i>` : l.checkError ? `<i class="dot nc"></i>` : `<i class="dot none"></i>`);
    p.classList.toggle("filelist", !!S.files);
    p.innerHTML = S.files
      ? `<span class="k">Files · ${S.layers.length}</span>` + S.layers.map((l, i) => `<button type="button" class="${i === S.active ? "active" : ""}" data-i="${i}" title="${esc(l.name || l.label)}">${dot(l)}<span class="fn">${esc(l.name || l.label)}</span><span class="fk">${esc(kindOf(l))}</span></button>`).join("")
      : `<span class="k">Layers</span>` + S.layers.map((l, i) => `<button type="button" class="${i === S.active ? "active" : ""}" data-i="${i}">${esc(l.label)}</button>`).join("");
    $("allBtn").hidden = !(S.files && S.layers.length > 1); if ($("allBtn").hidden && S.askAll) { S.askAll = false; $("allBtn").setAttribute("aria-pressed", "false"); $("askInput").placeholder = "Ask about this drawing"; }
    p.querySelectorAll("button").forEach((b) => { b.onclick = () => setLayer(Number(b.dataset.i)); });
  }
  async function setLayer(i) {
    S.active = i; const l = layer(); renderLayerPill(); select(null); if (S.files) $("titleText").textContent = l.name || l.label; $("card").hidden = true; $("note").hidden = true; $("view3d").hidden = true;
    const img = $("sheetImg"), first = !S.W;
    if (!l.img) { busy("Loading the " + l.label.toLowerCase() + " layer"); l.img = await new Promise((ok, no) => { const im = new Image(); im.onload = () => ok(im); im.onerror = () => no(new Error("The drawing could not be loaded")); im.src = l.image_url; }); idle(); }
    img.src = l.img.src; S.W = l.img.naturalWidth; S.H = l.img.naturalHeight;
    $("sheet").style.width = S.W + "px"; $("sheet").style.height = S.H + "px"; $("sheet").hidden = false; $("start").hidden = true; $("dock").hidden = false;
    if (first) fit();
    $("readBtn").hidden = !!l.simple; renderPagePill();
    if (!l.simple && !l.data) { l.data = await api(`/api/chat/studio/rooms?session_id=${SID}&doc_id=${l.doc_id}`); }
    if (S.showRead && !l.simple && !l.overlay) { try { l.overlay = (await api(`/api/chat/overlay?session_id=${SID}&doc_id=${l.doc_id}`)).shapes || []; } catch (e) { l.overlay = []; } }
    draw();
    if (S.is3d) { await show3d(); return; }
    /* after a check, a layer's alert must be on screen when the layer is opened */
    const pinsEl = [...document.querySelectorAll("#sheetPins .pin")];
    if (pinsEl.length && !pinsEl.some((el) => { const r = el.getBoundingClientRect(); return r.left > 0 && r.top > 60 && r.right < window.innerWidth && r.bottom < window.innerHeight - 80; })) fit();
  }

  async function showImage(url, keepView) {
    const im = await new Promise((ok, no) => { const x = new Image(); x.onload = () => ok(x); x.onerror = () => no(new Error("The picture could not be loaded")); x.src = url; });
    const same = keepView && im.naturalWidth === S.W && im.naturalHeight === S.H;
    $("sheetImg").src = im.src; S.W = im.naturalWidth; S.H = im.naturalHeight; $("sheet").style.width = S.W + "px"; $("sheet").style.height = S.H + "px";
    if (!same) fit(); else apply();
  }
  function renderPagePill() {
    const l = layer(), p = $("pagePill"); p.style.top = (S.layers.length > 1 && !$("layerPill").hidden ? Math.round($("layerPill").getBoundingClientRect().bottom) + 8 : 14) + "px";
    if (!l || !l.simple || !l.pages || l.pages.length < 2) { p.hidden = true; return; }
    p.hidden = false;
    p.innerHTML = `<button type="button" id="pgPrev" ${l.page <= 0 ? "disabled" : ""}>‹</button><span class="k" style="padding:0 6px">Page ${l.page + 1} of ${l.pages.length}</span><button type="button" id="pgNext" ${l.page >= l.pages.length - 1 ? "disabled" : ""}>›</button>`;
    const go = async (d) => { l.page = Math.max(0, Math.min(l.pages.length - 1, l.page + d)); renderPagePill(); busy("Loading page " + (l.page + 1)); try { await showImage(l.pages[l.page].preview_url, false); } catch (e) {} idle(); };
    $("pgPrev").onclick = () => go(-1); $("pgNext").onclick = () => go(1);
  }

  // ---------- drawing the overlay
  function draw() {
    const l = layer(), svg = $("sheetSvg"), pins = $("sheetPins"); svg.innerHTML = ""; pins.innerHTML = "";
    if (!l || !l.data) return;
    const res = l.check, marks = {};
    if (res) {
      res.findings.forEach((f, i) => { if (f.room && onLayer(f, l.key)) { const cur = marks[f.room]; if (!cur || f.status === "fail" || (cur.status === "not_checked" && f.status === "pass")) { if (!(cur && cur.status === "fail")) marks[f.room] = { status: f.status, list: [] }; } marks[f.room].list.push(i); } });
      if (l.key === "fire") Object.entries(res.room_marks || {}).forEach(([rid, m]) => { if (m.status === "pass" && !marks[rid]) marks[rid] = { status: "pass", list: [] }; });
    }
    l.marks = marks;
    l.data.rooms.forEach((r) => {
      const el = document.createElementNS(NS, "polygon");
      el.setAttribute("points", r.poly.map((q) => q[0] + "," + q[1]).join(" "));
      const m = marks[r.id], ans = l.answer && l.answer.ids.has(r.id);
      el.setAttribute("class", "room" + (m ? " is-" + m.status : "") + (ans ? " is-answer" : "") + (S.selected === r.id ? " is-selected" : ""));
      el.dataset.id = r.id;
      el.addEventListener("click", (e) => { if (S.wasDrag()) return; e.stopPropagation(); if (e.shiftKey || e.ctrlKey || e.metaKey) togglePick(r.id); else openRoom(r); });
      if (l.sel && l.sel.has(r.id)) el.classList.add("is-picked");
      el.addEventListener("contextmenu", (e) => { e.preventDefault(); e.stopPropagation(); openMenu(r, e.clientX, e.clientY); });
      svg.appendChild(el);
    });
    if (S.showRead) {
      svg.querySelectorAll(".room").forEach((el) => el.classList.add("is-read"));
      (l.overlay || []).forEach((sh) => {
        if (sh.kind === "room" || sh.kind === "fire_detector") return;
        const col = readColor(sh.kind); let el;
        if (sh.box) { el = document.createElementNS(NS, "rect"); el.setAttribute("x", sh.box[0]); el.setAttribute("y", sh.box[1]); el.setAttribute("width", Math.max(0, sh.box[2] - sh.box[0])); el.setAttribute("height", Math.max(0, sh.box[3] - sh.box[1])); }
        else if (sh.poly) { el = document.createElementNS(NS, "polygon"); el.setAttribute("points", sh.poly.map((q) => q[0] + "," + q[1]).join(" ")); }
        if (!el) return;
        el.setAttribute("class", "rd" + (sh.uncertain ? " uncertain" : "")); el.setAttribute("fill", col); el.setAttribute("stroke", col); svg.appendChild(el);
      });
    }
    renderLegend();
    if (S.showRead || res) {
      (l.data.devices || []).forEach((d) => box(svg, d.box, "read-box"));
      (l.key === "architecture" ? (l.data.seats || []) : []).forEach((d) => box(svg, d.box, "read-box seat"));
    }
    if (l.answer) (l.answer.shapes || []).forEach((sh) => { if (sh.box) box(svg, sh.box, "ans-shape"); });
    if (l.evac) {
      const pl = document.createElementNS(NS, "polyline"); pl.setAttribute("points", l.evac.path.map((q) => q[0] + "," + q[1]).join(" ")); pl.setAttribute("class", "evac"); svg.appendChild(pl);
      const a = l.evac.path[0], z = l.evac.path[l.evac.path.length - 1];
      [[a, "evac-dot start", ""], [z, "evac-dot exit", "EXIT"]].forEach(([q, cls, txt]) => { const el = document.createElement("div"); el.className = cls; el.style.left = q[0] * 100 + "%"; el.style.top = q[1] * 100 + "%"; el.textContent = txt; pins.appendChild(el); });
      if (l.evac.length_m != null) { const m = l.evac.path[Math.floor(l.evac.path.length / 2)]; tag(pins, m, l.evac.length_m + " m to the exit"); }
    }
    // pins on failed rooms, tags for values
    l.data.rooms.forEach((r) => {
      const m = marks[r.id];
      if (m && m.status === "fail") {
        const b = document.createElement("button"); b.type = "button"; b.className = "pin"; b.style.left = r.c[0] * 100 + "%"; b.style.top = r.c[1] * 100 + "%";
        const f0 = res.findings[m.list[0]];
        b.innerHTML = `!${m.list.length > 1 ? `<span class="n">${m.list.length}</span>` : ""}<span class="pin-label">${esc(f0.title.split(": ").pop())}</span>`;
        b.title = f0.title; b.onclick = (e) => { e.stopPropagation(); openFinding(r, m.list); };
        pins.appendChild(b);
      }
      if (l.key === "architecture" && r.seats && (S.showRead || res)) tag(pins, [r.c[0], r.box[1] + (r.box[3] - r.box[1]) * 0.12], `${r.seats} seats`);
      if (l.answer && l.answer.text[r.id]) tag(pins, r.c, l.answer.text[r.id]);
    });
    apply(); renderResultPill(); renderSel(); paint3d();
  }
  /* "What was read": one colour per kind of thing, the same in the drawing and in the legend */
  const READ_COL = { door: "#d97706", window: "#2563eb", toilet: "#0f766e", sink: "#0f766e", shower: "#0f766e", bathtub: "#0f766e", shower_cab: "#0f766e", sauna_bench: "#9a3412", room_label: "#7c3aed" };
  const readColor = (k) => READ_COL[k] || "#64748b";
  const plural = (k, n) => { const w = String(k).replace(/_/g, " "); return n === 1 ? w : /(s|x|ch|sh)$/.test(w) ? w + "es" : w + "s"; };
  function renderLegend() {
    const l = layer(), g = $("legend");
    if (!S.showRead || !l || !l.data || S.is3d) { g.hidden = true; return; }
    const n = {}; (l.overlay || []).forEach((sh) => { if (sh.kind !== "room" && sh.kind !== "fire_detector") n[sh.kind] = (n[sh.kind] || 0) + 1; });
    const dev = {}; (l.data.devices || []).forEach((d) => { dev[d.kind] = (dev[d.kind] || 0) + 1; });
    const seats = l.key === "architecture" ? (l.data.seats || []).length : 0, unsure = (l.overlay || []).filter((sh) => sh.uncertain).length;
    const row = (col, name, k) => `<div><i style="border-color:${col};background:${col}29"></i>${esc(name)}<b>${k}</b></div>`;
    g.innerHTML = `<h4>Read on this layer</h4>` + row("rgba(22,48,40,.7)", plural("room", l.data.rooms.length), l.data.rooms.length)
      + Object.entries(n).sort((a, b) => b[1] - a[1]).map(([k, v]) => row(readColor(k), plural(k === "room_label" ? "text label" : k, v), v)).join("")
      + Object.entries(dev).map(([k, v]) => row("#163028", plural(k, v), v)).join("") + (seats ? row("#c46a00", plural("seat", seats), seats) : "")
      + (unsure ? `<p>Dashed outline: ${unsure} the detector was not sure about.</p>` : "") + (l.data.sample && (seats || Object.keys(dev).length) ? `<p>Sample plan: fire devices and seats were planted.</p>` : "")
      + (!l.data.rooms.length && !Object.keys(n).length ? `<p>Nothing was recognised on this drawing.</p>` : "");
    g.hidden = false;
  }
  function box(svg, b, cls) { const el = document.createElementNS(NS, "rect"); el.setAttribute("x", b[0]); el.setAttribute("y", b[1]); el.setAttribute("width", Math.max(0, b[2] - b[0])); el.setAttribute("height", Math.max(0, b[3] - b[1])); el.setAttribute("class", cls); svg.appendChild(el); }
  function tag(pins, c, text) { const t = document.createElement("div"); t.className = "tag"; t.style.left = c[0] * 100 + "%"; t.style.top = c[1] * 100 + "%"; t.textContent = text; pins.appendChild(t); }
  /* rooms picked by the user (per layer): shift-click a room, or use Select in its card */
  const picked = () => { const l = layer(); if (!l.sel) l.sel = new Set(); return l.sel; };
  function togglePick(id) { const p = picked(); if (p.has(id)) p.delete(id); else p.add(id); document.querySelectorAll("#sheetSvg .room").forEach((el) => el.classList.toggle("is-picked", p.has(el.dataset.id))); renderSel(); paint3d(); }
  function selRooms() { const l = layer(), p = picked(); return l.data ? l.data.rooms.filter((r) => p.has(r.id)) : []; }
  const sum = (rs, k) => rs.reduce((a, r) => a + (typeof r[k] === "number" ? r[k] : 0), 0);
  function renderSel() {
    const bar = $("selbar"), rs = layer() && layer().data ? selRooms() : [];
    if (!rs.length) { bar.hidden = true; return; }
    bar.hidden = false;
    bar.innerHTML = `<b>${sum(rs, "area").toFixed(2)} m²</b><span>${rs.length} room${rs.length === 1 ? "" : "s"} selected</span><button type="button" id="selList">Details</button><button type="button" id="selClear">Clear</button>`;
    $("selClear").onclick = () => { picked().clear(); document.querySelectorAll("#sheetSvg .room.is-picked").forEach((el) => el.classList.remove("is-picked")); renderSel(); paint3d(); };
    $("selList").onclick = () => openSelection();
  }
  function roomsTable(rs, cols) {
    return `<table class="rtable"><thead><tr><th>Room</th>${cols.map((c) => `<th class="n">${c[1]}</th>`).join("")}</tr></thead><tbody>${rs.map((r) => `<tr class="go" data-id="${esc(r.id)}"><td>${esc(r.name || "Unnamed room")}</td>${cols.map((c) => `<td class="n">${r[c[0]] != null ? esc(typeof r[c[0]] === "number" ? (+r[c[0]].toFixed(2)) : r[c[0]]) : ""}</td>`).join("")}</tr>`).join("")}</tbody>
      <tfoot><tr><td>Total, ${rs.length} rooms</td>${cols.map((c) => `<td class="n">${c[2] === false ? "" : +sum(rs, c[0]).toFixed(2)}</td>`).join("")}</tr></tfoot></table>`;
  }
  function wireRows(root) { root.querySelectorAll("tr.go").forEach((tr) => { tr.onclick = () => { const r = layer().data.rooms.find((x) => x.id === tr.dataset.id); if (r) { select(r.id); focusBox(r.box); } }; }); }
  function openSelection() {
    const rs = selRooms(); if (!rs.length) return;
    const c = card(`<div class="kick">Selected rooms</div><h2>${sum(rs, "area").toFixed(2)} m² in ${rs.length} room${rs.length === 1 ? "" : "s"}</h2>
      <div class="kv"><b>Perimeter</b><span>${sum(rs, "perimeter").toFixed(2)} m</span><b>Baseboard</b><span>${sum(rs, "baseboard").toFixed(2)} m</span><b>Doors</b><span>${sum(rs, "doors")}</span></div>
      ${roomsTable(rs, [["area", "Area m²"], ["perimeter", "Perimeter m"], ["baseboard", "Baseboard m"]])}<p class="tr" style="margin-top:8px">Areas and lengths are added up from the traced room outlines. Shift-click a room to add or remove it.</p>`);
    wireRows(c);
  }
  function select(id) { S.selected = id; document.querySelectorAll("#sheetSvg .room").forEach((el) => el.classList.toggle("is-selected", el.dataset.id === id)); paint3d(); }

  // ---------- 3D view (studio3d.js draws; this page keeps the state)
  /* what the 3D view shows is the same state as the sheet: check marks, selection, the last answer, the way out */
  function paint3d() {
    if (!S.is3d || !window.Studio3D || !window.Studio3D.isOpen) return;
    const l = layer(), marks = {}, pins = [];
    Object.entries(l.marks || {}).forEach(([id, m]) => { marks[id] = m.status; if (m.status === "fail" && l.check) { const f0 = l.check.findings[m.list[0]]; pins.push({ id, n: m.list.length, text: f0 ? f0.title.split(": ").pop() : "" }); } });
    window.Studio3D.paint({ marks, pins, selected: S.selected, picked: l.sel || new Set(), answer: (l.answer && l.answer.ids) || l.answer3d || new Set(), path: l.evac ? l.evac.path : null });
  }
  async function show3d() {
    const l = layer(); if (!l) return;
    try {
      if (!window.Studio3D) throw new Error("The 3D view is still loading. Try again in a moment.");
      if (!l.scene) { busy("Building the 3D model"); l.scene = await api(`/api/chat/studio/scene3d?session_id=${SID}&doc_id=${l.doc_id}`); idle(); }
      const room = (id) => (l.data ? l.data.rooms.find((x) => x.id === id) : null);
      window.Studio3D.open($("view3d"), l.scene, {
        onRoom: (id, e) => { const r = room(id); if (!r) return; if (e.shiftKey || e.ctrlKey || e.metaKey) togglePick(id); else openRoom(r); },
        onMenu: (id, x, y) => { const r = room(id); if (r) openMenu(r, x, y); },
        onPin: (id) => { const r = room(id), m = (l.marks || {})[id]; if (r && m) openFinding(r, m.list); },
        onElement: (el) => { select(el.id); card(`<div class="kick">${esc(el.ifc_class || el.cat || "Element")}</div><h2>${esc(el.name || "Unnamed element")}</h2><div class="kv">${el.level ? `<b>Level</b><span>${esc(el.level)}</span>` : ""}${el.id ? `<b>Id</b><span>${esc(el.id)}</span>` : ""}</div><p class="tr">Ask about it in the box below; the elements of an answer are highlighted here.</p>`); },
        onNothing: () => { select(null); },
      });
      S.is3d = true; $("sheet").hidden = true; $("d3Btn").setAttribute("aria-pressed", "true"); $("readBtn").hidden = true;
      $("v3Note").innerHTML = `<b>3D from the ${esc(l.scene.source || "drawing")}.</b> ${esc(l.scene.note || "")}`;
      paint3d();
    } catch (e) { idle(); hide3d(); note(`<div class="v bad">No 3D view</div><div class="t">${esc(e.message)}</div>`); }
  }
  function hide3d() {
    S.is3d = false; if (window.Studio3D) window.Studio3D.close(); $("view3d").hidden = true; $("d3Btn").setAttribute("aria-pressed", "false");
    const l = layer(); if (l && !$("dock").hidden) { $("sheet").hidden = false; $("readBtn").hidden = !!l.simple; apply(); }
  }
  function renderResultPill() {
    const l = layer(), p = $("resultPill");
    if (!l || !l.check) { p.hidden = true; return; }
    const fs = l.check.findings.filter((f) => onLayer(f, l.key)), n = (s) => fs.filter((f) => f.status === s).length;
    const passRooms = Object.values(l.marks || {}).filter((m) => m.status === "pass").length;
    p.hidden = false;
    p.innerHTML = `<span class="res fail"><i></i>${n("fail")} failed</span><span class="res pass"><i></i>${l.key === "fire" ? passRooms + " rooms pass" : n("pass") + " passed"}</span><span class="res nc"><i></i>${n("not_checked")} not checked</span>`;
    p.style.cursor = "pointer"; p.onclick = () => openSummary();
  }

  // ---------- plan check
  async function analyse() {
    const btn = $("analyseBtn"); btn.disabled = true; busy("Checking the plan against the rules");
    try {
      await Promise.all(S.layers.map(async (l) => {
        l.checkError = null;
        let a; try { a = await api("/api/chat/ask", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ session_id: SID, doc_id: l.doc_id, question: "Check this plan against the regulations", allow_llm: false }) }); }
        catch (e) { if (!S.files) throw e; l.checkError = e.message; return; }
        if (!a.findings && S.files) { l.check = null; l.checkError = l.simple ? "Not a floor plan image: there are no traced rooms to check. Questions about it still work." : ((a.refusal && a.refusal.why) || a.reason || "This drawing has no traced rooms to check"); return; }
        if (!a.findings) throw new Error(l.simple ? "The plan check works on floor plan images, where rooms are traced from the walls. This file is opened in simple mode: ask questions about it below." : ((a.refusal && a.refusal.why) || a.reason || "This drawing has no traced rooms to check"));
        l.check = { findings: a.findings, room_marks: a.room_marks || {}, cost: a.cost }; l.answer = null;
      }));
      renderLayerPill(); draw(); if (S.files && S.layers.length > 1) openFilesSummary(); else if (layer().check) openSummary(); else note(`<div class="v bad">Not checked</div><div class="t">${esc(layer().checkError || "")}</div>`);
    } catch (e) { note(`<div class="v bad">Not checked</div><div class="t">${esc(e.message)}</div>`); }
    idle(); btn.disabled = false;
  }
  /* A finding, kept short: what is wrong (large), and the one sentence of the rule it rests on. Numbers, the what-if, the
     second quote, the table, the scope note and the links sit under "Details". folded=true puts the whole finding under one line. */
  function findingHtml(f, idx, folded) {
    const c = f.clause, cls = f.status === "fail" ? "fail" : f.status === "pass" ? "pass" : "";
    const word = { fail: "Failed", pass: "Passed", not_checked: "Not checked" }[f.status] || f.status;
    const whatif = f.status === "fail" && f.per_pupil && f.area ? `<div class="whatif" data-per="${f.per_pupil}" data-area="${f.area}"><label>Try another number of pupils <input type="number" min="1" max="60" value="${esc(f.seats)}"></label><span class="out"></span></div>` : "";
    const table = c && c.table ? `<p class="tr"><b>${esc(c.table.name)}</b></p><p class="tr">${c.table.rows.map((r) => esc(r.en)).join("<br>")}</p>` : "";
    const cite = c ? `<blockquote class="cite">${esc(c.quote)}${c.quote_translation ? `<span class="trn">${esc(c.quote_translation)}</span>` : ""}<cite>${esc(c.title)}, ${esc(c.clause)}</cite></blockquote>` : "";
    const more = (f.measured || f.required || whatif || c) ? `<details class="more"><summary>Details</summary>
        ${f.measured || f.required ? `<div class="kv">${f.measured ? `<b>Measured</b><span>${esc(f.measured)}</span>` : ""}${f.required ? `<b>Required</b><span>${esc(f.required)}</span>` : ""}</div>` : ""}
        ${whatif}
        ${c ? `${c.quote_2 ? `<blockquote>${esc(c.quote_2)}</blockquote>` : ""}${table}<p class="tr">${esc(c.scope_note || "")}</p>
          <div class="links"><a href="${esc(c.url)}" target="_blank" rel="noopener">Open the official text</a>${c.local_copy ? `<a href="/api/chat/studio/source?file=${encodeURIComponent(c.local_copy.split("/").pop())}" target="_blank" rel="noopener">Download the saved copy</a>` : ""}</div>` : ""}</details>` : "";
    const head = `<div class="kick ${cls}">${word}${c ? " · " + esc(c.jurisdiction) + " · " + esc(c.status) : ""}</div>`;
    if (folded) return `<details class="more other"><summary><span class="s ${esc(f.status)}"></span>${word}: ${esc(f.title)}</summary>${head}<p>${esc(f.text)}</p>${cite}${more}</details>`;
    return `${head}<h2>${esc(f.title)}</h2><p class="why">${esc(f.text)}</p>${cite}${more}`;
  }
  function wireWhatIf(root) {
    root.querySelectorAll(".whatif").forEach((w) => {
      const inp = w.querySelector("input"), out = w.querySelector(".out"), per = Number(w.dataset.per), area = Number(w.dataset.area);
      const upd = () => { const n = Math.max(1, Math.floor(Number(inp.value) || 0)), need = per * n, ok = area + 1e-9 >= need; out.className = "out " + (ok ? "ok" : "no"); out.textContent = ok ? `${n} pupils need ${need.toFixed(1)} m²: the room is large enough` : `${n} pupils need ${need.toFixed(1)} m²: the room has ${area} m²`; };
      inp.oninput = upd; upd();
    });
  }
  function card(html) { const c = $("card"); c.innerHTML = `<button type="button" class="x" aria-label="Close">×</button>` + html; c.hidden = false; c.querySelector(".x").onclick = () => { c.hidden = true; select(null); const l = layer(); if (l && l.evac) { l.evac = null; draw(); } }; wireWhatIf(c); return c; }
  function openFinding(room, list) {
    const l = layer(); select(room.id);
    /* the failed finding is shown in full; anything else found for the same room is one folded line each */
    const fs = list.map((i) => l.check.findings[i]), main = fs.find((f) => f.status === "fail") || fs[0], rest = fs.filter((f) => f !== main);
    const cc = card(findingHtml(main) + rest.map((f) => findingHtml(f, 0, true)).join("") + `<details class="more other"><summary>Room and selection</summary>${pickBtnHtml(room)}${l.data.sample ? `<p class="sample">Sample plan: the fire symbols and the seats of one classroom were planted for this demonstration. Room outlines and areas are computed from the drawing.</p>` : ""}</details>`);
    const pb = cc.querySelector("#cPick"); if (pb) pb.onclick = () => { togglePick(room.id); openFinding(room, list); }; const sb = cc.querySelector("#cSel"); if (sb) sb.onclick = () => openSelection();
    focusBox(room.box);
  }
  function openSummary() {
    const l = layer(); if (!l.check) return;
    const fs = l.check.findings.map((f, i) => [f, i]).filter(([f]) => onLayer(f, l.key)), order = { fail: 0, not_checked: 1, pass: 2 };
    fs.sort((a, b) => order[a[0].status] - order[b[0].status]);
    const passRooms = Object.values(l.marks || {}).filter((m) => m.status === "pass").length;
    const c = card(`<div class="kick">Plan check · ${esc(l.label)} layer</div><h2>${fs.filter(([f]) => f.status === "fail").length} failed${l.key === "fire" ? `, ${passRooms} rooms pass` : ""}</h2>
      <div class="flist">${fs.map(([f, i]) => `<button type="button" data-i="${i}"><span class="s ${esc(f.status)}"></span>${esc(f.title)}</button>`).join("")}</div>
      ${S.files && S.layers.length > 1 ? `<div class="links"><a href="#" id="cAllFiles">All ${S.layers.length} files</a></div>` : S.layers.length === 2 ? `<p class="tr" style="margin-top:8px">Other findings are on the ${esc(S.layers[1 - S.active].label)} layer.</p>` : ""}${costHtml(l.check)}`);
    const af = c.querySelector("#cAllFiles"); if (af) af.onclick = (e) => { e.preventDefault(); openFilesSummary(); };
    c.querySelectorAll(".flist button").forEach((b) => { b.onclick = () => { const f = l.check.findings[Number(b.dataset.i)], r = f.room && l.data.rooms.find((x) => x.id === f.room); if (r) openFinding(r, [Number(b.dataset.i)]); else card(findingHtml(f)); }; });
  }

  /* plan check over several files: one line per file, click to open that file's findings */
  function openFilesSummary() {
    const n = (l, s) => l.check.findings.filter((f) => f.status === s).length, failed = S.layers.filter((l) => l.check && n(l, "fail")).length, done = S.layers.filter((l) => l.check).length;
    const c = card(`<div class="kick">Plan check · ${S.layers.length} files</div><h2>${failed} of ${done} checked file${done === 1 ? "" : "s"} ${failed === 1 ? "has" : "have"} failed findings</h2>
      <div class="flist">${S.layers.map((l, i) => `<button type="button" data-i="${i}"><span class="s ${l.check ? (n(l, "fail") ? "fail" : "pass") : "not_checked"}"></span><b>${esc(l.name || l.label)}</b><br><span class="tr">${l.check ? `${n(l, "fail")} failed, ${n(l, "pass")} passed, ${n(l, "not_checked")} not checked` : esc(l.checkError || "Not checked")}</span></button>`).join("")}</div>
      ${costHtml({ cost: { eur: 0, session_total_eur: (S.layers.map((l) => l.check && l.check.cost).filter(Boolean).pop() || {}).session_total_eur } })}`);
    c.querySelectorAll(".flist button").forEach((b) => { b.onclick = async () => { const i = Number(b.dataset.i); await setLayer(i); if (S.layers[i].check) openSummary(); else note(`<div class="q">${esc(S.layers[i].name || "")}</div><div class="t">${esc(S.layers[i].checkError || "Not checked")}</div>`); }; });
  }
  /* the same question to every open file, answers side by side */
  async function askAll(q) {
    busy(`Asking ${S.layers.length} files`);
    const rs = await Promise.all(S.layers.map((l) => api("/api/chat/ask", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ session_id: SID, doc_id: l.doc_id, question: q, allow_llm: true }) }).then((a) => ({ l, a }), (e) => ({ l, error: e.message }))));
    const val = (a) => (a.status === "ok" ? (typeof a.value === "object" ? "see the file" : String(a.value) + (a.unit && a.unit !== "count" ? " " + a.unit : "")) : null);
    const nums = rs.filter((r) => r.a && r.a.status === "ok" && typeof r.a.value === "number"), units = new Set(nums.map((r) => r.a.unit || ""));
    /* how the per-file answers combine depends on what was asked: counts and sums add up; "largest" / "smallest" picks the
       winning file; an average cannot be combined from per-file averages, so none is shown */
    const ops = new Set(nums.map((r) => (r.a.plan && (r.a.plan.op || (r.a.plan.recipe ? "sum" : ""))) || "")), op = ops.size === 1 ? [...ops][0] : "";
    const same = nums.length > 1 && units.size === 1, best = same && (op === "max" || op === "min") ? nums.reduce((b, r) => ((op === "max" ? r.a.value > b.a.value : r.a.value < b.a.value) ? r : b)) : null;
    const total = same && (op === "count" || op === "sum") ? +nums.reduce((s, r) => s + r.a.value, 0).toFixed(3) : null, unit = [...units][0], spent = rs.reduce((s, r) => s + ((r.a && r.a.cost && r.a.cost.eur) || 0), 0);
    const sess = Math.max(0, ...rs.map((r) => (r.a && r.a.cost && r.a.cost.session_total_eur) || 0));
    note(`<div class="q">${esc(q)} · ${S.layers.length} files</div>${total != null ? `<div class="v">${total}${unit && unit !== "count" ? " " + esc(unit) : ""} in total</div><div class="t">Sum of the ${nums.length} files that gave a number${nums.length < rs.length ? "; " + (rs.length - nums.length) + " did not" : ""}.</div>` : ""}${best ? `<div class="v">${best.a.value}${unit && unit !== "count" ? " " + esc(unit) : ""}</div><div class="t">The ${op === "max" ? "largest" : "smallest"} of the ${nums.length} files: <b>${esc(best.l.name || best.l.label)}</b>${best.a.evidence_names && best.a.evidence_names[0] ? ", " + esc(best.a.evidence_names[0]) : ""}.</div>` : ""}
      <table class="rtable"><thead><tr><th>File</th><th class="n">Answer</th></tr></thead><tbody>${rs.map((r, i) => `<tr class="gofile${best && r === best ? " win" : ""}" data-i="${i}"><td>${esc(r.l.name || r.l.label)}</td><td class="n${r.a && val(r.a) != null ? "" : " bad"}">${esc(r.error ? "Not answered: " + r.error : val(r.a) != null ? val(r.a) : "Not answered: " + ((r.a.refusal && r.a.refusal.why) || r.a.reason || "not in this file"))}</td></tr>`).join("")}</tbody></table>
      <div class="cost"><b>${eur(spent)}</b><span>${spent ? "model calls for this question, all files" : "computed by code, no AI model"}</span><span class="tot">session ${eur(sess)}</span></div>`);
    $("note").querySelectorAll("tr.gofile").forEach((tr) => { tr.style.cursor = "pointer"; tr.onclick = () => setLayer(Number(tr.dataset.i)); });
    idle();
  }

  // ---------- a room: click and right-click
  function roomDataHtml(r) {
    const rows = [["Area", r.area != null ? r.area + " m²" : null], ["Perimeter", r.perimeter != null ? r.perimeter + " m" : null], ["Baseboard", r.baseboard != null ? r.baseboard + " m" : null], ["Doors", r.doors], ["Windows", r.windows], ["Seats", r.seats], ["Building", r.building]].filter((x) => x[1] != null);
    return `<div class="kick">Room</div><h2>${esc(r.name || "Unnamed room")}</h2><div class="kv">${rows.map(([k, v]) => `<b>${k}</b><span>${esc(v)}</span>`).join("")}</div>`;
  }
  function openRoom(r) {
    const l = layer(), m = (l.marks || {})[r.id]; select(r.id);
    if (m && m.list.length) return openFinding(r, m.list);
    const st = m ? `<p><span class="kick pass">Passed</span> ${l.key === "fire" ? "This room has a fire detector." : ""}</p>` : (l.check ? `<p class="tr">No rule was checked for this room on this layer.</p>` : "");
    const c = card(roomDataHtml(r) + st + pickBtnHtml(r) + `<div class="links"><a href="#" id="cReg">Regulations for this room</a></div>`);
    c.querySelector("#cReg").onclick = (e) => { e.preventDefault(); openRegs(r, "Finland"); }; wirePick(c, r);
  }
  function pickBtnHtml(r) { const on = picked().has(r.id); return `<div><button type="button" class="btn${on ? " alt" : ""}" id="cPick">${on ? "Remove from selection" : "Select this room"}</button>${picked().size ? `<button type="button" class="btn alt" id="cSel">Show the selection</button>` : ""}</div>`; }
  function wirePick(c, r) { const b = c.querySelector("#cPick"); if (b) b.onclick = () => { togglePick(r.id); openRoom(r); }; const s2 = c.querySelector("#cSel"); if (s2) s2.onclick = () => openSelection(); }
  /* way out of a room to the nearest door that leads outside, drawn on the sheet */
  async function openEvac(r) {
    const l = layer(); select(r.id); busy("Finding the way out");
    try {
      const d = await api(`/api/chat/studio/evac?session_id=${SID}&doc_id=${l.doc_id}&room=${encodeURIComponent(r.id)}`);
      l.evac = d; draw(); idle();
      const over = d.length_m != null && d.length_m > d.limit.general_m, mid = d.length_m != null && d.length_m > d.limit.one_exit_m && !over;
      card(`<div class="kick ${over ? "fail" : "pass"}">Evacuation path</div><h2>${esc(d.room_name || "Room")}: ${d.length_m != null ? d.length_m + " m" : "length unknown"} to the nearest exit</h2>
        <div class="kv"><b>Through</b><span>${esc(d.through.join(" → "))} → outside</span><b>Doors on the way</b><span>${d.doors}</span><b>Exit door width</b><span>${d.exit_width_m != null ? d.exit_width_m + " m" : ""}</span></div>
        <p>${over ? "This is longer than the 45 m the decree allows in general." : mid ? "This is within the general 45 m, but over the 30 m that applies where an area has only one exit." : "This is within the 30 m that applies even where an area has only one exit."}</p>
        <p class="tr">${esc(d.method)}</p><div class="sep"></div><div class="kick">The rule</div><p class="tr">${esc(d.limit.rule)}</p><p class="tr">${esc(d.limit.note)}</p>
        <div class="links"><a href="${esc(d.limit.url)}" target="_blank" rel="noopener">Open the official text</a><a href="/api/chat/studio/source?file=fi_848_2017.xml" target="_blank" rel="noopener">Download the saved copy</a></div>`);
      const xs = d.path.map((q) => q[0]), ys = d.path.map((q) => q[1]); focusBox([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)], 1.6);
    } catch (e) { idle(); l.evac = null; draw(); card(`<div class="kick">Evacuation path</div><h2>${esc(r.name || "Room")}</h2><p>${esc(e.message)}</p>`); }
  }
  function closeMenu() { $("menu").hidden = true; }
  function openMenu(r, x, y) {
    const m = $("menu"); select(r.id);
    m.innerHTML = `<div class="h">${esc(r.name || "Unnamed room")}</div><div class="hs">${r.area != null ? r.area + " m²" : ""}</div>
      <button type="button" data-a="fi">Regulations for this room: Finland</button><button type="button" data-a="us">Regulations for this room: USA</button>
      <button type="button" data-a="evac">Evacuation path</button><button type="button" data-a="pick">${picked().has(r.id) ? "Remove from selection" : "Select this room"}</button>
      <button type="button" data-a="data">Room data</button><button type="button" data-a="check">What the check found here</button>`;
    m.hidden = false; const w = m.offsetWidth, h = m.offsetHeight;
    m.style.left = Math.min(x, window.innerWidth - w - 8) + "px"; m.style.top = Math.min(y, window.innerHeight - h - 8) + "px";
    m.querySelectorAll("button").forEach((b) => { b.onclick = () => { closeMenu(); const a = b.dataset.a; if (a === "fi") openRegs(r, "Finland"); else if (a === "us") openRegs(r, "USA"); else if (a === "evac") openEvac(r); else if (a === "pick") togglePick(r.id); else if (a === "data") card(roomDataHtml(r)); else openRoom(r); }; });
  }
  async function openRegs(r, country) {
    select(r.id); card(`<div class="kick">Regulations</div><h2>${esc(r.name || "Room")}</h2><p class="tr">Loading</p>`);
    try {
      const d = await api(`/api/chat/studio/regulations?room_name=${encodeURIComponent(r.name || "")}&country=${encodeURIComponent(country)}`);
      const c = card(`<div class="kick">Regulations for this kind of room</div><h2>${esc(r.name || "Room")}</h2>
        <div class="tabs">${d.countries.map((k) => `<button type="button" class="${k === country ? "active" : ""}" data-k="${esc(k)}">${esc(k)}</button>`).join("")}</div>
        ${d.entries.length ? d.entries.map((e) => `<div class="reg"><h3>${esc(e.topic.charAt(0).toUpperCase() + e.topic.slice(1))}</h3><div class="meta">${esc(e.doc)} · ${esc(e.clause)} · ${esc(e.status)}</div>
          ${e.value ? `<div class="val">${esc(e.value)}</div>` : ""}${e.quote ? `<blockquote>${esc(e.quote)}</blockquote>` : ""}${e.en ? `<p class="tr">${esc(e.en)}</p>` : ""}
          ${e.limit ? `<p class="tr">${esc(e.limit)}</p>` : ""}${String(e.checked).startsWith("NOT") ? `<p class="warn">Not checked against the official text.</p>` : ""}
          <div class="links"><a href="${esc(e.url)}" target="_blank" rel="noopener">Open the official text</a>${e.source_url ? `<a href="${esc(e.source_url)}" target="_blank" rel="noopener">Download the saved copy</a>` : ""}</div></div>`).join("") : `<p>No regulation is recorded for this kind of room in ${esc(country)}.</p>`}
        <p class="sample">Collected for this concept. Not a complete list and not legal advice.</p>`);
      c.querySelectorAll(".tabs button").forEach((b) => { b.onclick = () => openRegs(r, b.dataset.k); });
    } catch (e) { card(`<h2>Regulations</h2><p>${esc(e.message)}</p>`); }
  }

  // ---------- ask
  /* how the answer was worked out, from what the server recorded: the route, the reading of the question, the steps */
  function thinkingHtml(a) {
    const steps = (a.trace || []).filter((t) => t && t.text).slice(0, 14).map((t) => `<li>${esc(t.text)}</li>`).join("");
    const why = (a.reasoning || []).filter((x) => x && x.text).slice(0, 8).map((x) => `<li>${esc(x.text)}</li>`).join("");
    const m = a.memory;
    return `<div class="think" hidden>
      <h4>Route</h4><div>${esc((a.cost && a.cost.route_line) || (a.plan_source === "rules" ? "Computed by code, no AI model" : a.plan_source || ""))}</div>
      ${a.read_as ? `<h4>The question was read as</h4><div>${esc(a.read_as)}</div>` : ""}
      ${m ? `<h4>Similar solved question</h4><div>${esc(m.pattern)} (${Math.round((m.prob || 0) * 100)}%)</div>` : ""}
      ${steps ? `<h4>Steps</h4><ol>${steps}</ol>` : ""}${why ? `<h4>Checks</h4><ol>${why}</ol>` : ""}
      ${a.plan ? `<h4>Query run on the drawing's data</h4><code>${esc(JSON.stringify(Object.fromEntries(Object.entries(a.plan).filter(([k]) => k !== "notes"))))}</code>` : ""}</div>`;
  }
  /* what this answer cost in model calls, in euros. Code-only answers cost nothing and say so. */
  const eur = (v) => "€" + Number(v || 0).toFixed(7);
  function costHtml(a) {
    const c = a.cost; if (!c) return "";
    if (c.unpriced && c.unpriced.length) return `<div class="cost"><b>price not set</b><span>${esc(c.unpriced.join(" + "))}, ${Number(c.total_tokens || 0).toLocaleString("en")} tokens; add its price to models.json</span></div>`;
    const how = c.eur ? `${esc((c.models && c.models.length ? c.models.join(" + ") : c.model) || "AI model")}, ${Number(c.total_tokens || 0).toLocaleString("en")} tokens`
      : c.cached_plan ? `reading reused from an earlier identical question${c.cold_est_eur ? " (first time " + eur(c.cold_est_eur) + ")" : ""}` : "computed by code, no AI model";
    return `<div class="cost" title="${esc(c.price_note || "")}"><b>${eur(c.eur)}</b><span>${how}</span><span class="tot">session ${eur(c.session_total_eur)}</span></div>`;
  }
  function wireThinking(n) { const m = n.querySelector(".main"), t = n.querySelector(".think"), h = n.querySelector(".hint"); if (m && t) m.onclick = () => { t.hidden = !t.hidden; if (h) h.textContent = t.hidden ? "Click to see how this was worked out" : "Click to hide"; }; }
  function note(html) { const n = $("note"); n.innerHTML = `<button type="button" class="x" aria-label="Close">×</button>` + html; n.hidden = false; n.querySelector(".x").onclick = () => { n.hidden = true; }; }
  async function ask(q) {
    const l = layer(); if (!l || !q.trim()) return;
    if (S.askAll && S.layers.length > 1) return askAll(q);
    busy("Working out the answer");
    try {
      const a = await api("/api/chat/ask", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ session_id: SID, doc_id: l.doc_id, question: q, allow_llm: true }) });
      if (a.findings) { S.layers.forEach((x) => { if (x === l) x.check = { findings: a.findings, room_marks: a.room_marks || {}, cost: a.cost }; }); l.answer = null; draw(); openSummary(); idle(); return; }
      if (l.simple) {
        l.answer3d = new Set(a.status === "ok" ? (a.evidence_ids || []) : []); paint3d();
        let val0 = a.status === "ok" ? (typeof a.value === "object" ? "" : String(a.value) + (a.unit && a.unit !== "count" ? " " + a.unit : "")) : "Not answered";
        let rows = "";
        if (a.status === "ok" && a.value && typeof a.value === "object") {
          if (Array.isArray(a.value.rows)) {
            /* a list of things: a table, one row each. Internal ids are left out, and so is a column that only repeats another one */
            const R = a.value.rows, keys = [...new Set(R.flatMap((r) => Object.keys(r)))].filter((k) => !["id", "guid", "step_id"].includes(k));
            const same = (x, y) => R.every((r) => String(r[x] == null ? "" : r[x]) === String(r[y] == null ? "" : r[y]));
            const cols = keys.filter((k, i) => !keys.slice(0, i).some((p) => same(p, k)) && R.some((r) => r[k] != null && r[k] !== ""));
            const head = (k) => { const w = String(k).replace(/_/g, " "); return w.charAt(0).toUpperCase() + w.slice(1); };
            const cell = (v) => (v == null ? "" : typeof v === "number" ? String(+v.toFixed(3)) : typeof v === "object" ? JSON.stringify(v) : String(v));
            const noun = String((a.plan && a.plan.kind) || "item").replace(/_/g, " ");
            val0 = `${R.length} ${noun}${R.length === 1 ? "" : (/(s|x|ch|sh)$/.test(noun) ? "es" : "s")}`;
            rows = `<table class="rtable"><thead><tr>${cols.map((k) => `<th class="${R.every((r) => typeof r[k] === "number" || r[k] == null) ? "n" : ""}">${esc(head(k))}</th>`).join("")}</tr></thead><tbody>${R.slice(0, 200).map((r) => `<tr>${cols.map((k) => `<td class="${typeof r[k] === "number" ? "n" : ""}">${esc(cell(r[k]))}</td>`).join("")}</tr>`).join("")}</tbody></table>${R.length > 200 ? `<div class="t">Showing the first 200 of ${R.length}.</div>` : ""}`;
          } else {
            rows = `<table class="rtable"><tbody>${Object.entries(a.value).slice(0, 80).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="n">${esc(typeof v === "number" ? +v.toFixed(3) : typeof v === "object" ? JSON.stringify(v) : v)}</td></tr>`).join("")}</tbody></table>`;
          }
        }
        note(`<div class="main"><div class="q">${esc(q)}</div>${val0 ? `<div class="v ${a.status === "ok" ? "" : "bad"}">${esc(val0)}</div>` : ""}${rows}<div class="t">${esc(a.status === "ok" ? (((a.trust || {}).label ? a.trust.label + ". " : "") + ((a.cost && a.cost.route_line) ? a.cost.route_line + "." : "")) : ((a.refusal && a.refusal.why) || a.reason || "This cannot be answered from this file."))}</div>${costHtml(a)}<div class="hint">Click to see how this was worked out</div></div>${thinkingHtml(a)}`);
        wireThinking($("note"));
        if (a.evidence_url && !S.is3d) { try { await showImage(a.evidence_url, true); } catch (e) {} }
        idle(); return;
      }
      const ids = new Set(a.evidence_ids || []), text = {}, shapes = [];
      (a.overlay || []).forEach((sh) => { if (sh.text) text[sh.id] = sh.text; if (sh.box && !String(sh.id).startsWith("room:")) shapes.push(sh); });
      l.answer = { ids, text, shapes }; draw();
      const val = a.status === "ok" ? (typeof a.value === "object" ? "See the highlighted items" : String(a.value) + (a.unit && a.unit !== "count" ? " " + a.unit : "")) : "Not answered";
      const tr = a.trust || {};
      /* more than one room in the answer: list them, each with the value that was measured for it */
      const hit = l.data.rooms.filter((r) => ids.has(r.id)); let table = "";
      if (hit.length > 1) {
        const hasVal = hit.some((r) => text[r.id]);
        table = `<table class="rtable"><thead><tr><th>Room</th><th class="n">${hasVal ? "Value" : "Area m²"}</th></tr></thead><tbody>${hit.slice(0, 80).map((r) => `<tr class="go" data-id="${esc(r.id)}"><td>${esc(r.name || "Unnamed room")}</td><td class="n">${esc(hasVal ? (text[r.id] || "") : (r.area != null ? r.area : ""))}</td></tr>`).join("")}</tbody></table>`;
      }
      note(`<div class="main"><div class="q">${esc(q)}</div><div class="v ${a.status === "ok" ? "" : "bad"}">${esc(val)}</div><div class="t">${esc(a.status === "ok" ? ((tr.label ? tr.label + ". " : "") + (hit.length > 1 ? hit.length + " rooms. " : "")) : (a.refusal && a.refusal.why) || a.reason || "This cannot be answered from this drawing.")}</div>${costHtml(a)}<div class="hint">Click to see how this was worked out</div></div>${thinkingHtml(a)}${table}`);
      wireThinking($("note")); wireRows($("note"));
      const first = hit[0]; if (first && ids.size <= 3) { if (S.is3d) window.Studio3D.focus(first.id); else focusBox(first.box, 4); } else if (S.is3d && hit.length) window.Studio3D.reset();
    } catch (e) { note(`<div class="v bad">Not answered</div><div class="t">${esc(e.message)}</div>`); }
    idle();
  }

  // ---------- opening a document
  async function openSample() {
    busy("Opening the school plan", true); $("startNote").textContent = "";
    try {
      const d = await api("/api/chat/studio/demo", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ session_id: SID }) });
      if (S.is3d) hide3d(); S.layers = d.layers; S.files = false; S.W = 0; $("titleText").textContent = d.title; idle(); await setLayer(0);
      /* load the second layer in the background so the switch is instant */
      S.layers.slice(1).forEach((l) => { const im = new Image(); im.onload = () => { l.img = im; }; im.src = l.image_url; api(`/api/chat/studio/rooms?session_id=${SID}&doc_id=${l.doc_id}`).then((x) => { l.data = x; }).catch(() => {}); });
    } catch (e) { idle(); $("startNote").textContent = e.message; }
  }
  /* Open one or several files. Each file becomes a tab in the top-left pill; Analyse and "All files" questions run over all of
     them. append=true (the Add file button) adds to what is open instead of replacing it. */
  const shortName = (n) => { const b = String(n || "file").replace(/\.[a-z0-9]+$/i, ""); return b.length > 22 ? b.slice(0, 20) + "…" : b; };
  function docLayer(doc, i) {
    /* images get the full treatment (rooms, marks drawn here). Tables, CAD files and models have no room layer on an image yet:
       they open in a simple mode, where the server paints the evidence of each answer onto the page or plan. */
    return { key: "file" + i, label: shortName(doc.display_name), doc_id: doc.doc_id, image_url: doc.preview_url, name: doc.display_name, simple: doc.format !== "raster", format: doc.format,
             summary: doc.understood_summary, pages: (doc.pages || []).filter((p) => p.preview_url), page: 0, suggested: doc.suggested_questions || [] };
  }
  async function upload(files, append) {
    files = [...files]; if (!files.length) return;
    const many = files.length > 1, keep = append && S.layers.length && !$("dock").hidden ? S.layers : [];
    busy(many ? `Reading ${files.length} files` : "Uploading " + files[0].name, true); $("startNote").textContent = "";
    /* a single image is already in the browser: put it on screen now, and let the server's reading arrive on top of it */
    let local = null;
    if (!many && !keep.length && /^image\/(png|jpeg|webp)$/.test(files[0].type)) {
      const im = new Image(); im.src = URL.createObjectURL(files[0]);
      im.decode().then(() => { if ($("busy").hidden || local === false) return; local = im; if (S.is3d) hide3d(); S.layers = []; $("sheetSvg").innerHTML = ""; $("sheetPins").innerHTML = ""; $("sheetImg").src = im.src; S.W = im.naturalWidth; S.H = im.naturalHeight;
        $("sheet").style.width = S.W + "px"; $("sheet").style.height = S.H + "px"; $("sheet").hidden = false; $("start").hidden = true; $("layerPill").hidden = true; $("pagePill").hidden = true; $("resultPill").hidden = true; $("card").hidden = true; $("note").hidden = true; fit(); }).catch(() => {});
    }
    /* the files are sent at the same time; a file that cannot be read is reported and does not stop the others */
    const res = await Promise.all(files.map((f) => api(`/api/chat/upload?session_id=${SID}&filename=${encodeURIComponent(f.name)}`, { method: "POST", headers: { "content-type": f.type || "application/octet-stream" }, body: f })
      .then((d) => ({ doc: d.document }), (e) => ({ error: e.message, name: f.name }))));
    const ok = res.filter((r) => r.doc), bad = res.filter((r) => r.error);
    if (!ok.length) { local = false; idle(); const msg = bad.map((b) => (many ? b.name + ": " : "") + b.error).join(" · "); if (keep.length) note(`<div class="v bad">Not opened</div><div class="t">${esc(msg)}</div>`); else { $("startNote").textContent = msg; $("sheet").hidden = true; $("dock").hidden = true; $("start").hidden = false; } return; }
    const fresh = ok.map((r, i) => docLayer(r.doc, keep.length + i));
    if (!many && !keep.length && !fresh[0].simple && local && local.naturalWidth) fresh[0].img = local;      /* same picture: no second download */
    const shown = !many && !keep.length && !fresh[0].simple && local && S.W === local.naturalWidth; local = false;
    if (S.is3d) hide3d();
    S.layers = keep.concat(fresh); S.files = !S.layers.some((l) => l.key === "architecture" || l.key === "fire") || S.layers.length > 2; if (!shown) S.W = 0;
    idle(); await setLayer(keep.length);
    ok.forEach((r) => { if (r.doc.timings) console.info(r.doc.display_name, "reading steps (s):", r.doc.timings.map((t) => t[0] + " " + t[1]).join(" | ")); });
    const first = ok[0].doc;
    if (S.layers.length > 1) note(`<div class="q">${S.layers.length} files open</div><div class="t">${ok.map((r) => `<b>${esc(r.doc.display_name)}</b>: ${esc(r.doc.understood_summary || "")}`).join("<br>")}</div>${bad.length ? `<div class="t bad">${bad.map((b) => esc(b.name + ": " + b.error)).join("<br>")}</div>` : ""}<div class="t">Switch files with the tabs at the top left. Analyse checks all of them; turn on "All files" to ask every file the same question.</div>`);
    else if (fresh[0].simple) note(`<div class="q">${esc(first.display_name)}</div><div class="t">${esc(first.understood_summary || "")}</div><div class="t">Ask a question below. Room clicks, layers and the plan check work on floor plan images.</div>`);
  }

  $("openSampleBtn").onclick = openSample;
  $("fileInput").onchange = (e) => { const fs = [...e.target.files], add = S.addMode; S.addMode = false; e.target.value = ""; if (fs.length) upload(fs, add); };
  /* drop files anywhere on the page */
  window.addEventListener("dragover", (e) => { e.preventDefault(); });
  window.addEventListener("drop", (e) => { e.preventDefault(); const fs = [...((e.dataTransfer && e.dataTransfer.files) || [])]; if (fs.length) upload(fs, !$("dock").hidden); });
  $("allBtn").onclick = () => { S.askAll = !S.askAll; $("allBtn").setAttribute("aria-pressed", String(S.askAll)); $("askInput").placeholder = S.askAll ? "Ask all " + S.layers.length + " files" : "Ask about this drawing"; };
  $("analyseBtn").onclick = analyse;
  $("fitBtn").onclick = () => { if (S.is3d) window.Studio3D.reset(); else fit(); };
  $("d3Btn").onclick = () => { if (S.is3d) hide3d(); else show3d(); };
  /* the first press fetches everything the server found on this layer (rooms, doors, windows, fixtures, text labels) */
  $("readBtn").onclick = async () => {
    S.showRead = !S.showRead; $("readBtn").setAttribute("aria-pressed", String(S.showRead)); const l = layer();
    if (S.showRead && l && !l.simple && !l.overlay) { busy("Loading what was read"); try { l.overlay = (await api(`/api/chat/overlay?session_id=${SID}&doc_id=${l.doc_id}`)).shapes || []; } catch (e) { l.overlay = []; } idle(); }
    draw();
  };
  $("addBtn").onclick = () => { S.addMode = true; $("fileInput").click(); };
  $("askForm").onsubmit = (e) => { e.preventDefault(); const v = $("askInput").value; $("askInput").value = ""; ask(v); };
  $("titleBtn").onclick = () => { $("legend").hidden = true; $("pagePill").hidden = true; if (S.is3d) hide3d(); $("sheet").hidden = true; $("dock").hidden = true; $("layerPill").hidden = true; $("resultPill").hidden = true; $("card").hidden = true; $("note").hidden = true; $("start").hidden = false; };
  $("stage").addEventListener("contextmenu", (e) => { if (!$("sheet").hidden) e.preventDefault(); });
  if (new URLSearchParams(location.search).get("sample") === "1") openSample();
})();
