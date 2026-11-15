/* Essatto chat page: plain JS client, no build step.
   Sections: 1 helpers · 2 words (user vocabulary) · 3 viewer · 4 files · 5 upload · 6 cards · 7 asking
             8 evidence panel · 9 what was read · 10 AI explanation · 11 start-up */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ 1 helpers */
  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const cap = (s) => { s = String(s ?? "").trim(); return s ? s[0].toUpperCase() + s.slice(1) : s; };
  const sentence = (s) => { s = cap(s); return s && !/[.!?]$/.test(s) ? s + "." : s; };

  const ICONS = {
    check: '<circle cx="8" cy="8" r="6.25"/><path d="M5.2 8.3l1.9 1.9 3.7-4.1"/>',
    unconfirmed: '<circle cx="8" cy="8" r="6.25" stroke-dasharray="2.6 2.3"/><path d="M6.5 6.4a1.6 1.6 0 1 1 2.4 1.4c-.6.3-.9.7-.9 1.2"/><circle cx="8" cy="11.2" r=".5" fill="currentColor"/>',
    alert: '<path d="M8 2.3l6 10.7H2L8 2.3z"/><path d="M8 6.5v3"/><circle cx="8" cy="11.2" r=".5" fill="currentColor"/>',
    none: '<circle cx="8" cy="8" r="6.25"/><path d="M3.7 12.3l8.6-8.6"/>',
    upload: '<path d="M8 10.5V2.8M5 5.7l3-3 3 3M2.8 10.3v2.2c0 .5.4.9.9.9h8.6c.5 0 .9-.4.9-.9v-2.2"/>',
    file: '<path d="M4 1.8h5.2l3.4 3.3v8.2c0 .5-.4.9-.9.9H4c-.5 0-.9-.4-.9-.9V2.7c0-.5.4-.9.9-.9z"/><path d="M9 1.9v3.3h3.4"/>',
    plus: '<path d="M8 3.2v9.6M3.2 8h9.6"/>',
    minus: '<path d="M3.2 8h9.6"/>',
    external: '<path d="M9.2 2.8h4v4M13 3L7.6 8.4M11.4 9.4v2.9c0 .5-.4.9-.9.9H3.7c-.5 0-.9-.4-.9-.9V5.5c0-.5.4-.9.9-.9h2.9"/>',
    chevron: '<path d="M6 3.5L10.5 8 6 12.5"/>',
    close: '<path d="M4 4l8 8M12 4l-8 8"/>',
    drawing: '<rect x="2" y="2.5" width="12" height="11" rx="1"/><path d="M2 7h6v6.5M8 10h6"/>',
  };
  const icon = (name) => `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true">${ICONS[name] || ""}</svg>`;

  /* numbers are shown as computed (up to 3 decimals), never rounded further: a quantity surveyor needs the digits */
  function fmtNum(v) {
    if (typeof v !== "number" || !isFinite(v)) return String(v ?? "");
    return (+v.toFixed(3)).toLocaleString("en-US", { maximumFractionDigits: 3 });
  }
  function fmtVal(v) {
    if (v === null || v === undefined) return "";
    if (typeof v === "number") return fmtNum(v);
    if (typeof v === "object") return Object.entries(v).map(([k, x]) => `${k}: ${fmtVal(x)}`).join(", ");
    return String(v);
  }
  function fmtBytes(n) {
    if (!n) return "0 B";
    if (n < 1024) return n + " B";
    if (n < 1048576) return Math.round(n / 1024) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }
  const money = (x) => "$" + Number(x || 0).toFixed(4);

  const STORAGE_KEY = "bp_chat_session_id_v1";
  function makeSessionId() {
    return "s_" + Array.from(crypto.getRandomValues(new Uint8Array(7))).map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  let SESSION_ID = localStorage.getItem(STORAGE_KEY);
  if (!SESSION_ID || !/^[a-zA-Z0-9_-]{4,64}$/.test(SESSION_ID)) {
    SESSION_ID = makeSessionId();
    localStorage.setItem(STORAGE_KEY, SESSION_ID);
  }

  const STATE = {
    documents: [], activeDocId: null, draftPages: new Set(), answers: {}, selectedAnswerId: null, focusId: null,
    inspectCache: {}, zoom: 1, panX: 0, panY: 0, dragging: false, moved: false, dragStartX: 0, dragStartY: 0, viewerKey: null,
  };

  /* ------------------------------------------------------------------ 2 words: the user's vocabulary, never internal names */
  const FORMAT_WORDS = { "pdf-vector": "PDF sheet", raster: "Scanned plan", "dxf-layered": "CAD plan (DXF)", ifc: "Building model (IFC)" };
  const KIND_PLURAL = {
    door: "doors", window: "windows", toilet: "toilets", washbasin: "sinks", shower: "showers", bathtub: "bathtubs", room: "rooms",
    wall: "walls", slab: "slabs", furniture: "furniture items", beam: "beams", column: "columns", stair: "stairs", railing: "railings",
    covering: "coverings", roof: "roofs", member: "structural members", element: "other elements", floor: "floors", curtainwall: "curtain walls",
    schedule_row: "rows", apartment: "apartments", level: "storeys",
  };
  const kindPlural = (k) => KIND_PLURAL[k] || String(k).replace(/_/g, " ") + "s";
  const kindOne = (k) => ({ washbasin: "sink", schedule_row: "row", furniture: "furniture item", level: "storey" }[k] || String(k).replace(/_/g, " "));
  const countOf = (n, k) => `${fmtNum(n)} ${n === 1 ? kindOne(k) : kindPlural(k)}`;
  const TRUST_ICON = { checked: "check", single_source: "unconfirmed", review: "alert", none: "none" };
  const TRUST_WORD = { checked: "Checked", single_source: "Not confirmed", review: "Needs review", none: "Not answered" };
  const METHOD_WORDS = {
    "ruling-line grid": "columns follow the grid lines drawn on the sheet",
    "text gaps": "no grid lines on the sheet: columns separated by text position",
    "callout legend": "read from the legend drawings on the sheet",
  };
  const DETAIL_COLS = [
    ["rtype", "Room type", false], ["apartment", "Apartment", false], ["level", "Storey", false], ["type_name", "Type", false],
    ["area", "Area", true], ["printed", "Printed area", true], ["volume", "Volume", true], ["length", "Length", true],
    ["width", "Width", true], ["height", "Height", true],
  ];

  function docFacts(doc) {
    const c = doc.counts || {};
    if (doc.format === "pdf-vector") return c.tables ? `${c.tables} schedule table${c.tables === 1 ? "" : "s"} · ${c.rows} rows` : "No schedule tables found";
    if (doc.format === "raster") return `${c.rooms || 0} rooms · ${c.symbols || 0} doors, windows and fixtures`;
    if (doc.format === "dxf-layered") return `${c.rooms || 0} rooms · ${fmtNum(c.area_m2 || 0)} m²`;
    if (doc.format === "ifc") return `${c.storeys || 0} storeys · ${fmtNum(c.elements || 0)} elements`;
    return doc.understood_summary || "";
  }
  function docIsUnreadable(doc) {
    const c = doc.counts || {};
    if (doc.format === "pdf-vector") return !c.tables;
    if (doc.format === "dxf-layered") return !c.rooms && !c.symbols;
    return false;
  }
  function docFoundList(doc) {
    const c = doc.counts || {};
    const rows = [];
    if (doc.format === "pdf-vector") (c.table_list || []).forEach((t) => rows.push([t.title, `${t.rows} rows`]));
    if (doc.format === "raster") { rows.push(["Floors", c.floors]); rows.push(["Rooms", c.rooms]); }
    if (doc.format === "dxf-layered") { rows.push(["Rooms", c.rooms]); if (c.apartments) rows.push(["Apartments", c.apartments]); rows.push(["Total room area", `${fmtNum(c.area_m2 || 0)} m²`]); }
    if (doc.format === "ifc") { rows.push(["Storeys", c.storeys]); rows.push(["Rooms (spaces)", c.spaces]); }
    Object.entries(c.by_kind || {}).slice(0, 7).forEach(([k, n]) => rows.push([cap(kindPlural(k)), n]));
    return rows;
  }

  /* ------------------------------------------------------------------ session totals */
  function updateLogLink() {
    $("downloadLogBtn").href = `/api/chat/session_log?session_id=${encodeURIComponent(SESSION_ID)}&download=1`;
  }
  function updateSessionTotals(t) {
    if (!t) return;
    $("topQuestions").textContent = String(t.questions || 0);
    $("topQuestionsWord").textContent = (t.questions || 0) === 1 ? "question" : "questions";
    $("topCost").textContent = money(t.usd);
    $("footCost").textContent = money(t.usd);
    $("footCode").textContent = String(t.rules_count || 0);
    $("footSmall").textContent = String(t.small_model_count || 0);
    $("footLarge").textContent = String(t.large_model_count || 0);
    updateLogLink();
  }

  /* ------------------------------------------------------------------ 3 viewer: drawing with evidence, zoom, pan, focus */
  function setRightTab(tab) {
    const isEv = tab === "evidence";
    $("rTabEvidence").classList.toggle("active", isEv);
    $("rTabInspect").classList.toggle("active", !isEv);
    $("rTabEvidence").setAttribute("aria-selected", String(isEv));
    $("rTabInspect").setAttribute("aria-selected", String(!isEv));
    $("viewEvidenceWrap").hidden = !isEv;
    $("viewInspectWrap").hidden = isEv;
    if (!isEv) loadInspectView(STATE.activeDocId);
    else if (!$("viewerCanvas").hidden) requestAnimationFrame(fitViewerImage);
  }

  function applyViewerTransform(animate) {
    const c = $("viewerCanvas");
    c.classList.toggle("animate", Boolean(animate));
    c.style.transform = `translate(${STATE.panX}px, ${STATE.panY}px) scale(${STATE.zoom})`;
    c.style.setProperty("--zoom", STATE.zoom);
  }
  function fitZoom() {
    const stage = $("viewerStage"), img = $("viewerImg");
    const sw = stage.clientWidth || 480, sh = stage.clientHeight || 360, pad = 16;
    return Math.max(0.05, Math.min((sw - pad * 2) / img.naturalWidth, (sh - pad * 2) / img.naturalHeight, 2.5));
  }
  function fitViewerImage(animate) {
    const stage = $("viewerStage"), img = $("viewerImg");
    if ($("viewerCanvas").hidden || !img.naturalWidth) return;
    STATE.zoom = fitZoom();
    STATE.panX = Math.round((stage.clientWidth - img.naturalWidth * STATE.zoom) / 2);
    STATE.panY = Math.round((stage.clientHeight - img.naturalHeight * STATE.zoom) / 2);
    applyViewerTransform(animate === true);
  }
  function zoomBy(f) {
    const stage = $("viewerStage");
    const mx = stage.clientWidth / 2, my = stage.clientHeight / 2;
    const next = Math.min(12, Math.max(0.05, STATE.zoom * f));
    STATE.panX = mx - ((mx - STATE.panX) / STATE.zoom) * next;
    STATE.panY = my - ((my - STATE.panY) / STATE.zoom) * next;
    STATE.zoom = next;
    applyViewerTransform(true);
  }

  function viewerEmpty(title, text) {
    $("viewerEmpty").innerHTML = `${icon("drawing")}<b>${esc(title)}</b><span>${esc(text)}</span>`;
    $("viewerEmpty").hidden = false;
  }

  /* answer = the answer whose evidence is shown, or null for a plain file preview */
  function setViewerSource(url, captionHtml, answer) {
    $("viewerCaption").innerHTML = captionHtml || "No drawing selected";
    const img = $("viewerImg"), canvas = $("viewerCanvas"), rawBtn = $("openRawImgBtn");
    STATE.focusId = null;
    $("viewerMarks").innerHTML = "";
    $("viewerLegend").hidden = true;
    renderEvidencePanel(answer);
    if (!url) {
      canvas.hidden = true; img.hidden = true; rawBtn.hidden = true;
      viewerEmpty("The drawing appears here", "After each answer, the rows or elements it used are highlighted and numbered.");
      return;
    }
    const key = url + "|" + (answer ? answer.answer_id : "");
    STATE.viewerKey = key;
    rawBtn.hidden = false; rawBtn.href = url;
    if (STATE.loadedUrl === url && img.complete && img.naturalWidth) {
      /* the same drawing is already on screen (scans use ONE plain image for the overview and for every answer):
         nothing is downloaded again, the zoom and position stay, only the shapes drawn on top change */
      $("viewerEmpty").hidden = true; canvas.hidden = false; img.hidden = false;
      buildMarks(answer);
      return;
    }
    canvas.hidden = true; img.hidden = true;
    viewerEmpty(answer ? "Marking the evidence on the drawing" : "Loading the drawing", "One moment.");
    img.onload = () => {
      if (STATE.viewerKey !== key) return;
      $("viewerEmpty").hidden = true;
      canvas.hidden = false; img.hidden = false;
      canvas.style.width = img.naturalWidth + "px";
      canvas.style.height = img.naturalHeight + "px";
      STATE.loadedUrl = url;
      buildMarks(answer);
      fitViewerImage();
    };
    img.onerror = () => {
      if (STATE.viewerKey !== key) return;
      viewerEmpty("The drawing could not be shown", "The list below still names every row or element that was used.");
    };
    img.src = url;
  }

  /* ---- plan check: findings and the clause each one rests on ---- */
  const FIND_WORD = { fail: "Failed", pass: "Passed", not_checked: "Not checked" };
  function findingsHtml(ans) {
    const order = { fail: 0, not_checked: 1, pass: 2 };
    const idx = ans.findings.map((f, i) => i).sort((a, b) => order[ans.findings[a].status] - order[ans.findings[b].status]);
    return `<div class="findings">${idx.map((i) => {
      const f = ans.findings[i];
      const mr = (f.measured || f.required) ? `<div class="finding-mr">${f.measured ? `<span><b>Measured</b> ${esc(f.measured)}</span>` : ""}${f.required ? `<span><b>Required</b> ${esc(f.required)}</span>` : ""}</div>` : "";
      const whatif = (f.status === "fail" && f.per_pupil && f.area) ? `<div class="whatif"><label>Try another number of pupils <input class="whatif-input" type="number" min="1" max="60" value="${esc(String(f.seats))}"></label><span class="whatif-out"></span></div>` : "";
      const clause = f.clause ? `<button type="button" class="clause-btn">${esc(f.clause.jurisdiction)} · ${esc(f.clause.clause.split(",")[0])} · show the paragraph</button>` : "";
      return `<div class="finding is-${esc(f.status)}" data-fi="${i}">
        <div class="finding-main${f.room ? " has-room" : ""}"><span class="finding-chip">${FIND_WORD[f.status] || esc(f.status)}</span><span class="finding-title">${esc(f.title)}</span></div>
        <div class="finding-text">${esc(f.text)}</div>${mr}${whatif}${clause}
      </div>`;
    }).join("")}</div>`;
  }
  function showClause(f) {
    const c = f.clause; if (!c) return;
    let m = $("clauseModal");
    if (!m) { m = el("div", "clause-modal"); m.id = "clauseModal"; document.body.appendChild(m); m.onclick = (e) => { if (e.target === m) m.hidden = true; }; }
    const rows = c.table ? `<div class="clause-table"><div class="clause-sub">${esc(c.table.name)}</div><ul>${c.table.rows.map((r) => `<li>${esc(r.fi)}<br><span class="clause-tr">${esc(r.en)}</span></li>`).join("")}</ul></div>` : "";
    m.innerHTML = `<div class="clause-box" role="dialog" aria-label="Regulation clause">
      <button type="button" class="clause-close" aria-label="Close">×</button>
      <div class="clause-kicker">${esc(c.jurisdiction)} · ${esc(c.status)}</div>
      <h3>${esc(c.title)}</h3>
      <div class="clause-ref">${esc(c.clause)}</div>
      <blockquote>${esc(c.quote)}</blockquote>
      ${c.quote_2 ? `<blockquote>${esc(c.quote_2)}</blockquote>` : ""}
      ${c.quote_translation ? `<p class="clause-tr">${esc(c.quote_translation)}</p>` : ""}
      ${rows}
      <p class="clause-note"><b>How the check uses it.</b> ${esc(c.scope_note || "")}</p>
      <p class="clause-links"><a href="${esc(c.url)}" target="_blank" rel="noopener">Open the official text</a><span>Copy in the project: regulations/${esc(c.local_copy || "")}</span></p>
    </div>`;
    m.hidden = false;
    m.querySelector(".clause-close").onclick = () => { m.hidden = true; };
  }

  /* Shapes sent by the server as fractions of the image: boxes for detected symbols, outlines for traced rooms.
     Drawn here as SVG over the plain drawing, so no painted picture has to be produced or downloaded per answer.
     A value is written inside a shape only when the answer measured something there. */
  const SHAPE_COL = { door: "#d92d20", window: "#1570ef", toilet: "#7a5af8", washbasin: "#0e9384", shower: "#dd2590", bathtub: "#dd2590",
                      shower_or_bath: "#dd2590", room: "#f79009", room_label: "#667085", fail: "#d92d20", fire_detector: "#d92d20" };
  function drawShapes(layer, shapes, opts) {
    if (!shapes || !shapes.length) return;
    const overview = opts && opts.overview;
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 1 1"); svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("class", "shape-layer");
    const img = $("viewerImg"), fs = Math.max(14, Math.round(img.naturalWidth / 230));
    shapes.forEach((sh) => {
      const col = sh.uncertain ? "#d92d20" : (SHAPE_COL[sh.kind] || "#d92d20");
      let el, cx, cy;
      if (sh.poly) {
        el = document.createElementNS(NS, "polygon");
        el.setAttribute("points", sh.poly.map((q) => q[0] + "," + q[1]).join(" "));
        cx = sh.poly.reduce((a, q) => a + q[0], 0) / sh.poly.length; cy = sh.poly.reduce((a, q) => a + q[1], 0) / sh.poly.length;
      } else if (sh.box) {
        el = document.createElementNS(NS, "rect");
        el.setAttribute("x", sh.box[0]); el.setAttribute("y", sh.box[1]);
        el.setAttribute("width", Math.max(0, sh.box[2] - sh.box[0])); el.setAttribute("height", Math.max(0, sh.box[3] - sh.box[1]));
        cx = (sh.box[0] + sh.box[2]) / 2; cy = sh.box[1];
      } else return;
      const isRoom = sh.kind === "room" || sh.kind === "fail";
      el.setAttribute("fill", col); el.setAttribute("fill-opacity", isRoom ? (overview ? 0.10 : 0.22) : (overview ? 0 : 0.18));
      el.setAttribute("stroke", col); el.setAttribute("stroke-width", overview ? 1.5 : 2.5);
      el.setAttribute("vector-effect", "non-scaling-stroke");
      if (sh.uncertain) el.setAttribute("stroke-dasharray", "5 3");
      svg.appendChild(el);
      if (sh.text) {
        const t = document.createElement("div");
        t.className = "shape-text"; t.textContent = sh.text;
        t.style.cssText = `left:${cx * 100}%;top:${cy * 100}%;font-size:${fs}px`;
        layer.appendChild(t);
      }
    });
    layer.insertBefore(svg, layer.firstChild);
  }
  function loadOverview(doc) {
    /* scans: after upload the plain drawing is shown and everything that was found is drawn on it, without numbers */
    if (!doc || !doc.overlay_url) return;
    const key = STATE.viewerKey;
    fetch(doc.overlay_url).then((r) => r.json()).then((d) => {
      if (!d || !d.ok || STATE.viewerKey !== key) return;
      STATE.overviewShapes = STATE.overviewShapes || {}; STATE.overviewShapes[doc.doc_id] = d.shapes;
      const draw = () => { if (STATE.viewerKey === key && !$("viewerCanvas").hidden) { $("viewerMarks").querySelectorAll(".shape-layer,.shape-text").forEach((n) => n.remove()); drawShapes($("viewerMarks"), d.shapes, { overview: true }); } };
      const img = $("viewerImg");
      if (img.complete && img.naturalWidth && !$("viewerCanvas").hidden) draw(); else img.addEventListener("load", draw, { once: true });
    }).catch(() => {});
  }

  function buildMarks(answer) {
    const layer = $("viewerMarks");
    layer.innerHTML = "";
    if (answer && answer.overlay) drawShapes(layer, answer.overlay, {});
    const items = (answer && answer.evidence_items) || [];
    const withBox = items.filter((it) => it.box);
    withBox.forEach((it) => {
      const [x0, y0, x1, y1] = it.box;
      const b = document.createElement("button");
      b.type = "button"; b.className = "hit"; b.dataset.id = it.id;
      b.style.cssText = `left:${x0 * 100}%;top:${y0 * 100}%;width:${(x1 - x0) * 100}%;height:${(y1 - y0) * 100}%`;
      b.title = (it.mark != null ? it.mark + ". " : "") + itemName(it, answer);
      b.setAttribute("aria-label", b.title);
      b.onclick = (ev) => { ev.stopPropagation(); if (!STATE.moved) focusEvidence(answer.answer_id, it.id, { pan: false }); };
      layer.appendChild(b);
    });
    const lg = $("viewerLegend");
    if (answer && answer.status === "ok" && answer.format === "pdf-vector" && (answer.evidence_tables || []).length) {
      lg.innerHTML = `<span><i class="sw sw-row"></i>Row used for the answer</span><span><i class="sw sw-col"></i>Column used</span><span><i class="sw sw-focus"></i>Selected in the list</span>`;
      lg.hidden = false;
    } else if (withBox.length) {
      lg.innerHTML = `<span><i class="sw sw-focus"></i>Selected in the list. Click a mark or a list row.</span>`;
      lg.hidden = false;
    }
  }

  function focusEvidence(answerId, id, opts) {
    const ans = STATE.answers[answerId];
    if (!ans) return;
    if (STATE.selectedAnswerId !== answerId || $("viewEvidenceWrap").hidden) {
      selectAnswerEvidence(answerId, () => focusEvidence(answerId, id, opts));
      return;
    }
    STATE.focusId = id;
    document.querySelectorAll(".ev-row").forEach((el) => {
      const on = el.dataset.id === id && el.dataset.aid === answerId;
      el.classList.toggle("is-focused", on);
      if (on && el.closest("#evidenceListDrawer")) el.scrollIntoView({ block: "nearest" });
    });
    let hit = null;
    document.querySelectorAll("#viewerMarks .hit").forEach((el) => {
      const on = el.dataset.id === id;
      el.classList.toggle("is-focused", on);
      if (on) hit = el;
    });
    const it = (ans.evidence_items || []).find((x) => x.id === id);
    if (!hit || !it || !it.box || (opts && opts.pan === false)) return;
    const stage = $("viewerStage"), img = $("viewerImg");
    const W = img.naturalWidth, H = img.naturalHeight;
    const bw = Math.max(8, (it.box[2] - it.box[0]) * W), bh = Math.max(8, (it.box[3] - it.box[1]) * H);
    const sw = stage.clientWidth, sh = stage.clientHeight;
    const fz = fitZoom();
    /* wide table rows fill the width; small symbols get enough context around them to be recognised */
    const z = Math.max(fz, Math.min((sw * 0.92) / bw, (sh * 0.3) / bh, Math.max(1.2, fz * 1.8), 3));
    STATE.zoom = z;
    STATE.panX = Math.round(sw / 2 - ((it.box[0] + it.box[2]) / 2) * W * z);
    STATE.panY = Math.round(sh / 2 - ((it.box[1] + it.box[3]) / 2) * H * z);
    applyViewerTransform(true);
  }

  function bindViewerEvents() {
    const stage = $("viewerStage");
    stage.addEventListener("wheel", (ev) => {
      if ($("viewerCanvas").hidden) return;
      ev.preventDefault();
      const rect = stage.getBoundingClientRect();
      const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
      const next = Math.min(12, Math.max(0.05, STATE.zoom * (ev.deltaY < 0 ? 1.18 : 1 / 1.18)));
      STATE.panX = mx - ((mx - STATE.panX) / STATE.zoom) * next;
      STATE.panY = my - ((my - STATE.panY) / STATE.zoom) * next;
      STATE.zoom = next;
      applyViewerTransform(false);
    }, { passive: false });
    stage.addEventListener("pointerdown", (ev) => {
      if ($("viewerCanvas").hidden || ev.button !== 0) return;
      STATE.dragging = true; STATE.moved = false;
      STATE.dragStartX = ev.clientX - STATE.panX; STATE.dragStartY = ev.clientY - STATE.panY;
    });
    stage.addEventListener("pointermove", (ev) => {
      if (!STATE.dragging) return;
      const nx = ev.clientX - STATE.dragStartX, ny = ev.clientY - STATE.dragStartY;
      if (!STATE.moved && Math.abs(nx - STATE.panX) + Math.abs(ny - STATE.panY) < 4) return;
      if (!STATE.moved) { STATE.moved = true; stage.classList.add("dragging"); stage.setPointerCapture(ev.pointerId); }
      STATE.panX = nx; STATE.panY = ny;
      applyViewerTransform(false);
    });
    const stop = () => { STATE.dragging = false; stage.classList.remove("dragging"); setTimeout(() => { STATE.moved = false; }, 0); };
    stage.addEventListener("pointerup", stop);
    stage.addEventListener("pointercancel", stop);
    $("zoomInBtn").innerHTML = icon("plus");
    $("zoomOutBtn").innerHTML = icon("minus");
    $("openRawImgBtn").innerHTML = icon("external");
    $("zoomInBtn").onclick = () => zoomBy(1.3);
    $("zoomOutBtn").onclick = () => zoomBy(1 / 1.3);
    $("zoomFitBtn").onclick = () => fitViewerImage(true);
    $("zoom100Btn").onclick = () => zoomBy(1 / STATE.zoom);
    window.addEventListener("resize", () => fitViewerImage());
  }

  /* ------------------------------------------------------------------ 4 files */
  const getActiveDoc = () => STATE.documents.find((d) => d.doc_id === STATE.activeDocId) || null;

  function renderDocList() {
    $("docCount").textContent = String(STATE.documents.length);
    const list = $("docList");
    if (!STATE.documents.length) {
      list.innerHTML = `<div class="doc-empty">No files yet.</div>`;
      $("pagesPanel").hidden = true;
      $("openInspectBtn").hidden = true;
      $("activeDocTitle").textContent = "Questions and answers";
      $("activeDocSummary").textContent = "No file selected";
      $("questionInput").disabled = true;
      $("questionInput").placeholder = "Upload a file first, then ask a question about it";
      $("askBtn").disabled = true;
      return;
    }
    list.innerHTML = STATE.documents.map((d) => `
      <button type="button" class="doc-card ${d.doc_id === STATE.activeDocId ? "active" : ""}" data-doc="${esc(d.doc_id)}" title="${esc(d.display_name)}">
        <img class="doc-thumb" src="${esc(d.thumb_url)}" alt="" loading="lazy">
        <span style="min-width:0">
          <span class="doc-name">${esc(d.display_name)}</span>
          <span class="doc-meta" style="display:block">${esc(docFacts(d))}</span>
        </span>
      </button>`).join("");
    list.querySelectorAll(".doc-card").forEach((el) => { el.onclick = () => selectDocument(el.dataset.doc); });
    renderPagesPicker();
  }

  function renderPagesPicker() {
    const doc = getActiveDoc(), panel = $("pagesPanel");
    if (!doc || doc.kind !== "pdf" || (doc.page_count || 0) <= 1) { panel.hidden = true; return; }
    panel.hidden = false;
    const grid = $("pagesGrid");
    grid.innerHTML = (doc.pages || []).map((p) => `
      <button type="button" class="page-tile ${STATE.draftPages.has(p.page) ? "selected" : ""}" data-pno="${p.page}" aria-pressed="${STATE.draftPages.has(p.page)}">
        <img src="${esc(p.thumb_url)}" alt="" loading="lazy">
        <span class="page-tile-foot"><span>Page ${p.page}${p.sheet_hint ? " · " + esc(p.sheet_hint) : ""}</span><span>${p.kind === "scanned" ? "scan" : "text"}</span></span>
      </button>`).join("");
    grid.querySelectorAll(".page-tile").forEach((tile) => {
      tile.onclick = () => {
        const pno = Number(tile.dataset.pno);
        if (STATE.draftPages.has(pno) && STATE.draftPages.size > 1) STATE.draftPages.delete(pno); else STATE.draftPages.add(pno);
        renderPagesPicker();
        const pObj = (doc.pages || []).find((x) => x.page === pno);
        if (pObj) { setRightTab("evidence"); setViewerSource(pObj.preview_url, `<b>${esc(doc.display_name)}</b> · page ${pno}`, null); }
      };
    });
  }

  async function applySelectedPages() {
    const doc = getActiveDoc();
    if (!doc) return;
    const btn = $("applyPagesBtn");
    btn.disabled = true; btn.textContent = "Reading pages…";
    try {
      const pages = Array.from(STATE.draftPages).sort((a, b) => a - b);
      const r = await fetch("/api/chat/pages", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: SESSION_ID, doc_id: doc.doc_id, pages }),
      });
      const res = await r.json();
      if (!res.ok) throw new Error(res.error || "The pages could not be read.");
      const idx = STATE.documents.findIndex((x) => x.doc_id === doc.doc_id);
      if (idx >= 0) STATE.documents[idx] = res.document;
      delete STATE.inspectCache[doc.doc_id];
      selectDocument(doc.doc_id);
      appendFileCard(res.document, true);
    } catch (err) {
      appendErrorCard("The pages could not be read", err.message, null);
    } finally {
      btn.disabled = false; btn.textContent = "Read selected pages";
    }
  }

  function selectDocument(docId) {
    STATE.activeDocId = docId;
    const doc = getActiveDoc();
    if (!doc) return;
    STATE.draftPages = new Set(doc.selected_pages || [1]);
    STATE.selectedAnswerId = null;
    document.querySelectorAll(".ans-card.selected").forEach((c) => c.classList.remove("selected"));
    renderDocList();
    $("activeDocTitle").textContent = doc.display_name;
    $("activeDocSummary").textContent = `${FORMAT_WORDS[doc.format] || doc.format} · ${docFacts(doc)}`;
    $("openInspectBtn").hidden = false;
    $("questionInput").disabled = false;
    $("askBtn").disabled = false;
    $("questionInput").placeholder = docIsUnreadable(doc) ? "Nothing was read from this file. Choose or upload another file." : "Ask a question about this file";
    setViewerSource(doc.preview_url, `<b>Overview</b> · ${doc.format === "pdf-vector" ? "the sheet as uploaded" : "everything that was read"} · ${esc(doc.display_name)}`, null);
    loadOverview(doc);
    if (!$("viewInspectWrap").hidden) loadInspectView(doc.doc_id);
  }

  /* ------------------------------------------------------------------ 5 upload */
  function feedAppend(el) {
    const es = $("emptyState");
    if (es) es.remove();
    const feed = $("chatFeed");
    feed.appendChild(el);
    feed.scrollTop = feed.scrollHeight;
    return el;
  }
  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  async function uploadFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    $("uploadError").hidden = true;
    document.body.classList.add("is-uploading");
    const origTitle = $("uploadTitle").textContent;
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      $("uploadTitle").textContent = files.length > 1 ? `Reading file ${i + 1} of ${files.length}…` : "Reading the file…";
      const reading = feedAppend(el("div", "card card-pad", `
        <div class="card-title">Reading ${esc(file.name)}</div>
        <div class="progress" role="progressbar" aria-label="Reading the file"></div>
        <div class="hint">Uploading, then finding tables, rooms, doors and fixtures. A scan or a large model can take up to a minute.</div>
        <div class="skel w70"></div><div class="skel w40"></div>`));
      const fd = new FormData();
      fd.append("session_id", SESSION_ID);
      fd.append("file", file, file.name);
      try {
        const resp = await fetch(`/api/chat/upload?session_id=${encodeURIComponent(SESSION_ID)}`, { method: "POST", body: fd });
        let data = null;
        try { data = await resp.json(); } catch (_) { /* not JSON */ }
        if (!resp.ok || !data || !data.ok) {
          reading.replaceWith(buildErrorCard(`${file.name} could not be read`,
            (data && data.error) || `The server returned an error (${resp.status}).`,
            data && data.error ? null : "Accepted files: PDF, PNG or JPG, DXF and IFC, up to 100 MB.", null));
          continue;
        }
        SESSION_ID = data.session_id;
        localStorage.setItem(STORAGE_KEY, SESSION_ID);
        STATE.documents.push(data.document);
        updateSessionTotals(data.session_totals);
        selectDocument(data.document.doc_id);
        reading.replaceWith(buildFileCard(data.document, false));
        $("chatFeed").scrollTop = $("chatFeed").scrollHeight;
      } catch (err) {
        reading.replaceWith(buildErrorCard(`${file.name} could not be uploaded`, "The server could not be reached. Check the connection and try again.", err.message, null));
      }
    }
    $("uploadTitle").textContent = origTitle;
    $("fileInput").value = "";
    document.body.classList.remove("is-uploading");
  }

  /* ------------------------------------------------------------------ 6 cards */
  function renderEmptyState() {
    $("chatFeed").innerHTML = `
      <div class="empty" id="emptyState">
        <h2>Ask a question about a drawing</h2>
        <p>Upload a file. Each answer is computed by code from what was read in the file, and the rows or elements it used are highlighted on the drawing so you can check it.</p>
        <ul class="empty-list">
          <li><b>PDF sheet</b><span>Counts, totals and single values from door, window and finish schedules</span></li>
          <li><b>Scanned plan (PNG, JPG)</b><span>Counts of rooms, doors, windows, toilets and sinks</span></li>
          <li><b>CAD plan (DXF)</b><span>Room and apartment areas, counts of rooms, doors and windows</span></li>
          <li><b>Building model (IFC)</b><span>Element counts, areas and volumes by type and storey</span></li>
        </ul>
        <button type="button" class="btn btn-primary" id="emptyUploadBtn">${icon("upload")}Upload a file</button>
      </div>`;
    $("emptyUploadBtn").onclick = () => $("fileInput").click();
  }

  function chipsHtml(questions, docId) {
    return `<div class="chips">${questions.map((q) =>
      `<button type="button" class="chip js-q" data-doc="${esc(docId)}" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>`;
  }
  function bindChips(root) {
    root.querySelectorAll(".js-q").forEach((btn) => {
      btn.onclick = (ev) => {
        ev.stopPropagation();
        if (btn.dataset.doc && STATE.activeDocId !== btn.dataset.doc) selectDocument(btn.dataset.doc);
        submitQuestion(btn.dataset.q, null);
      };
    });
  }

  function buildFileCard(doc, isPageChange) {
    const unreadable = docIsUnreadable(doc);
    const found = docFoundList(doc);
    const limits = doc.unreadable || [];
    const sugg = doc.suggested_questions || [];
    const card = el("div", "card card-pad file-card");
    card.innerHTML = `
      <div class="file-head">
        <div>
          <div class="file-name">${esc(doc.display_name)}</div>
          <div class="file-kind">${esc(FORMAT_WORDS[doc.format] || doc.format)} · ${fmtBytes(doc.size_bytes)}${isPageChange ? " · page selection changed" : ""}</div>
        </div>
        ${unreadable ? `<span class="tag tag-none">${icon("none")}Nothing to ask about</span>` : `<button type="button" class="btn btn-sm js-inspect">See what was read</button>`}
      </div>
      ${unreadable
        ? `<div class="section-label">Why</div><ul class="limits">${(limits.length ? limits : ["Nothing that can be counted or measured was found in this file."]).map((u) => `<li>${esc(u)}</li>`).join("")}</ul>`
        : `<div class="section-label">Found in this file</div>
           ${found.length ? `<ul class="facts">${found.map(([k, v]) => `<li><span>${esc(k)}</span><span class="n">${esc(typeof v === "number" ? fmtNum(v) : v)}</span></li>`).join("")}</ul>` : `<div>${esc(cap(doc.understood_summary))}</div>`}
           ${limits.length ? `<div class="section-label">Limits of this file</div><ul class="limits">${limits.map((u) => `<li>${esc(sentence(u))}</li>`).join("")}</ul>` : ""}
           ${sugg.length ? `<div class="section-label">Questions you can ask</div>${chipsHtml(sugg.slice(0, 5), doc.doc_id)}` : ""}`}
    `;
    const ib = card.querySelector(".js-inspect");
    if (ib) ib.onclick = () => { if (STATE.activeDocId !== doc.doc_id) selectDocument(doc.doc_id); setRightTab("inspect"); };
    bindChips(card);
    return card;
  }
  const appendFileCard = (doc, isPageChange) => feedAppend(buildFileCard(doc, isPageChange));

  function buildErrorCard(title, message, detail, retry) {
    const card = el("div", "card card-pad errcard");
    card.setAttribute("role", "alert");
    card.innerHTML = `
      <div class="err-head">${icon("alert")}<h3>${esc(title)}</h3></div>
      <p>${esc(message)}</p>
      ${detail ? `<p class="hint">${esc(detail)}</p>` : ""}
      ${retry ? `<div class="row-gap"><button type="button" class="btn js-retry">Try again</button></div>` : ""}`;
    if (retry) card.querySelector(".js-retry").onclick = retry;
    return card;
  }
  const appendErrorCard = (t, m, d, r) => feedAppend(buildErrorCard(t, m, d, r));

  /* ---- the value ---- */
  function isEmptyValue(v) {
    if (v === null || v === undefined || v === "") return true;
    if (Array.isArray(v)) return v.length === 0;
    if (typeof v === "object") return Array.isArray(v.rows) ? v.rows.length === 0 : Object.keys(v).length === 0;
    return false;
  }
  function splitUnit(s) {
    const m = /^(-?\d[\d,]*(?:\.\d+)?)\s*([A-Za-z²³%]{1,6})$/.exec(String(s).trim());
    return m ? { num: m[1], unit: m[2] } : null;
  }
  function valueHtml(ans) {
    const v = ans.value, unit = ans.unit && ans.unit !== "count" ? ans.unit : "";
    if (typeof v === "number") return `<div class="ans-value-row"><span class="ans-value">${esc(fmtNum(v))}</span>${unit ? `<span class="ans-unit">${esc(unit)}</span>` : ""}</div>`;
    if (typeof v === "string") {
      const lines = v.split("\n").map((l) => l.trim()).filter(Boolean);
      if (lines.length > 1 && lines.every((l) => l.includes(": "))) {
        return `<table class="ans-parts">${lines.map((l) => {
          const i = l.indexOf(": "), val = l.slice(i + 2);
          const numeric = /^-?\d[\d,.]*(\s*[A-Za-z²³%]{1,6})?$/.test(val);
          return `<tr><td class="p-label">${esc(l.slice(0, i))}</td><td class="p-val ${numeric ? "" : "is-text"}">${esc(val)}</td></tr>`;
        }).join("")}</table>`;
      }
      const su = splitUnit(v);
      if (su) return `<div class="ans-value-row"><span class="ans-value">${esc(su.num)}</span><span class="ans-unit">${esc(su.unit)}</span></div>`;
      return `<div class="ans-value-row"><span class="ans-value ${v.length <= 28 ? "is-text" : "is-long"}">${esc(v)}</span>${unit ? `<span class="ans-unit">${esc(unit)}</span>` : ""}</div>`;
    }
    if (v && typeof v === "object") {
      let rows = Array.isArray(v.rows) ? v.rows : Object.entries(v).map(([k, x]) => ({ Item: k, Value: x }));
      const cols = Array.from(new Set(rows.flatMap((r) => Object.keys(r)))).filter((c) => c !== "id").slice(0, 6);
      const label = (c) => colLabel(ans, c);
      return `<div class="table-scroll" style="border:1px solid var(--border);border-radius:var(--radius);max-height:260px">
        <table class="grid"><thead><tr>${cols.map((c) => `<th>${esc(label(c))}</th>`).join("")}</tr></thead>
        <tbody>${rows.slice(0, 100).map((r) => `<tr>${cols.map((c) => `<td class="${typeof r[c] === "number" ? "r" : ""}">${esc(fmtVal(r[c]))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
        ${rows.length > 100 ? `<div class="hint">Showing 100 of ${rows.length} lines.</div>` : ""}`;
    }
    return "";
  }
  /* printed column name for an internal key, when the answer came from a schedule table */
  function colLabel(ans, key) {
    for (const t of ans.evidence_tables || []) {
      const h = (t.header || []).find((x) => x.key === key);
      if (h) return h.group ? `${h.group} › ${h.label}` : h.label;
    }
    const d = DETAIL_COLS.find((x) => x[0] === key);
    return d ? d[1] : cap(String(key).replace(/_/g, " "));
  }

  /* ---- evidence items in words ---- */
  function itemName(it, ans) {
    const d = it.details || {};
    if (it.kind === "schedule_row") return schedRowName(it, ans) || it.label;
    if (ans && ans.format === "raster") return cap(it.kind === "room" ? (d.rtype || it.label) : kindOne(it.kind));
    if (ans && ans.format === "ifc") return d.type_name ? `${cap(kindOne(it.kind))} ${d.type_name}` : it.label;
    if (it.kind === "room") return ans && ans.format === "dxf-layered" ? cap(String(it.label || "").replace(/^\S+\s*\(?/, "").replace(/\)$/, "") || d.rtype || "Room") : `Room ${it.label}`;
    if (it.kind === "apartment") return `Apartment ${it.label}`;
    return /[:$]/.test(it.label) ? cap(kindOne(it.kind)) : `${cap(kindOne(it.kind))} ${it.label}`;
  }
  /* a schedule row is named by its own number or mark plus a describing cell, as printed in the row */
  const ID_KEYS = ["door_number", "window_number", "mark", "number", "tag", "room_number", "type_mark", "door_type"];
  const NAME_KEYS = ["room_name", "from_room_name", "name", "type", "description"];
  function schedRowName(it, ans) {
    for (const t of (ans && ans.evidence_tables) || []) {
      const row = (t.rows || []).find((r) => r.id === it.id);
      if (!row) continue;
      const cell = (keys) => { for (const k of keys) { const i = t.header.findIndex((h) => h.key === k); if (i >= 0 && row.cells[i] !== "" && row.cells[i] != null) return String(row.cells[i]); } return ""; };
      const a = cell(ID_KEYS), b = cell(NAME_KEYS);
      return [a, b].filter(Boolean).join(" · ") || String(row.cells.find((c) => c !== "" && c != null) || "");
    }
    return "";
  }
  /* what is written next to the element on the drawing: its mark number, or the room number for rooms */
  function markText(it, ans) {
    if (it.mark != null) return String(it.mark);
    if (ans && ans.format === "dxf-layered" && it.kind === "room") return String(it.label || "").split(" ")[0];
    return "";
  }
  function plainHow(txt) {
    let t = String(txt || "");
    t = t.replace(/^(\w+) box from the trained detector, confidence ([\d.]+) \(class threshold ([\d.]+)\)$/i, "Found by the trained detector, score $2 (counted from $3)");
    t = t.replace(/^Area of the ROOM_OUTLINE polygon \((\d+) vertices\) containing the room number = /i, "Measured from the room outline ($1 corners): ");
    t = t.replace(/^([A-Z_]+) polyline with \d+ vertices = (.+)$/, (_, layer, what) => `${cap(what)} drawn on the ${layer} layer`);
    t = t.replace(/un-numbered outline\(s\)/g, "outlines without a room number").replace(/^row of table '([^']+)': cells taken from the drawn grid lines.*$/i, "Row of “$1”, cells taken from the drawn grid lines");
    return cap(t);
  }

  function itemValue(it, ans) {
    const d = it.details || {};
    if (it.kind === "schedule_row") {
      for (const t of ans.evidence_tables || []) {
        const row = (t.rows || []).find((r) => r.id === it.id);
        if (row) return t.header.map((h, i) => (h.used ? fmtVal(row.cells[i]) || "(empty)" : null)).filter((x) => x !== null).join(" · ");
      }
      return "";
    }
    const attrs = planAttrs(ans);
    for (const a of attrs) if (d[a] !== undefined && typeof d[a] !== "object") return fmtVal(d[a]) + (ans.unit && ans.unit !== "count" && typeof d[a] === "number" ? " " + ans.unit : "");
    if (it.borderline) return "uncertain detection";
    return d.level || (it.kind !== "room" && d.rtype) || "";
  }
  function planAttrs(ans) {
    const p = ans.plan || {};
    return (p.parts || [p]).map((x) => x && x.attr).filter(Boolean);
  }

  function evidenceListHtml(ans) {
    const items = ans.evidence_items || [];
    if (!items.length) return "";
    const MAX = 6;
    const noun = evNoun(items);
    const numbered = items.some((x) => x.mark != null);
    const doubt = doubtIds(ans);
    const shown = items.filter((x) => doubt.has(x.id)).concat(items.filter((x) => !doubt.has(x.id))).slice(0, MAX);
    const rows = shown.map((it) => `
      <li class="ev-row" data-aid="${esc(ans.answer_id)}" data-id="${esc(it.id)}" tabindex="0" role="button" title="Show on the drawing">
        <span class="mark-no">${esc(markText(it, ans))}</span>
        <span class="ev-name">${esc(itemName(it, ans))}${doubt.has(it.id) ? ' <span class="flag">check this one</span>' : ""}</span>
        <span class="ev-val">${esc(itemValue(it, ans))}</span>
      </li>`).join("");
    const total = ans.evidence_count || items.length;
    return `<div class="ev-block">
      <div class="ev-head">
        <span class="section-label">${ans.status === "ok" ? "Used for this answer" : "What was found"}: ${fmtNum(total)} ${total === 1 ? noun[0] : noun[1]}, ${ans.format === "ifc" ? "numbered on the storey plan (one storey is drawn)" : numbered ? "numbered on the drawing" : "highlighted on the drawing"}</span>
        <button type="button" class="link show-ev js-show-ev">Show on drawing</button>
      </div>
      <ul class="ev-list">${rows}</ul>
      ${total > MAX ? `<div class="hint" style="padding:4px 4px 0 36px">and ${fmtNum(total - MAX)} more in the list beside the drawing</div>` : ""}
    </div>`;
  }

  /* elements named by a failed or cautionary check: these are what the user must look at */
  function doubtIds(ans) {
    const out = new Set();
    ((ans.trust && ans.trust.checks) || []).forEach((c) => { if (c.result === "fail" || c.result === "caution") (c.ids || []).forEach((i) => out.add(i)); });
    return out;
  }
  function evNoun(items) {
    const kinds = Array.from(new Set(items.map((x) => x.kind)));
    return kinds.length === 1 ? [kindOne(kinds[0]), kindPlural(kinds[0])] : ["element", "elements"];
  }

  /* ---- steps and checks in plain words ---- */
  const SOURCE_WORDS = [
    [/^vector PDF: text \+ tables$/i, "text and tables of the PDF sheet"],
    [/^raster image: symbols from cached vision detections \+ OCR$/i, "the scanned image: doors, windows and fixtures found by a trained detector, printed text read by OCR"],
    [/^IFC model$/i, "the IFC building model"],
  ];
  function cleanStep(r, ans) {
    let t = String(r.text || "");
    if (r.t === "source") {
      const body = t.replace(/^Source:\s*/, "");
      const m = SOURCE_WORDS.find(([rx]) => rx.test(body));
      return "Source: " + (m ? m[1] : body);
    }
    if (r.t === "read" && /^Question read as:/.test(t)) {
      const rd = (ans.read_as_plain || []).map((x) => (x.label ? x.label + ": " : "") + x.text).join("; ");
      return "Question read as: " + (rd || t.replace(/^Question read as:\s*/, "")) + (ans.plan_source === "rules" ? " (by the built-in rules)" : " (by an AI model)");
    }
    if (r.t === "note") t = (ans.plan_source !== "rules" && (((ans.plan || {}).notes) || []).includes(t) ? "Note written by the AI model: " : "Note: ") + t;
    t = t.replace(/^Found (\d+) in the drawing \('([a-z_]+)' entities\)/, (_, n, k) => `Found ${n} ${n === "1" ? kindOne(k) : kindPlural(k)} in the file`);
    t = t.replace(/\('([a-z_]+)' entities\)/g, (_, k) => `(${kindPlural(k)})`).replace(/\bschedule_rows\b/g, "rows").replace(/\belement\(s\)/g, "elements").replace(/\brow\(s\)/g, "rows");
    for (const tb of ans.evidence_tables || []) for (const h of tb.header || []) {
      if (h.key.includes("_")) t = t.split(h.key).join(`“${h.group ? h.group + " › " : ""}${h.label}”`);
    }
    return cap(t.replace(/^count = /, "Count = "));
  }
  const RES_WORD = { pass: "Passed", fail: "Failed", caution: "Caution", info: "Note" };

  function howHtml(ans) {
    let stepNo = 0;
    const steps = (ans.reasoning || []).map((r) => {
      const note = r.t === "note";
      if (!note) stepNo++;
      return `<li class="${r.t === "result" ? "is-result" : note ? "is-note" : ""}"><span class="step-no">${note ? "" : stepNo + "."}</span><span>${esc(cleanStep(r, ans))}</span></li>`;
    }).join("");
    const checks = ((ans.trust && ans.trust.checks) || []).map((c) =>
      `<li><span class="res res-${esc(c.result)}">${RES_WORD[c.result] || "Note"}</span><span>${esc(sentence(c.text))}</span></li>`).join("");
    const tech = JSON.stringify({ answer_id: ans.answer_id, fingerprint: ans.fingerprint, plan_source: ans.plan_source, plan: ans.plan }, null, 2);
    return `<details class="how">
      <summary>${icon("chevron")}How this was calculated</summary>
      <div class="how-body">
        <ol class="how-steps">${steps}</ol>
        ${checks ? `<div class="section-label">Checks run on this answer</div><ul class="checks">${checks}</ul>` : ""}
        <details class="tech"><summary>Technical record for developers</summary><pre>${esc(tech)}</pre></details>
      </div>
    </details>`;
  }

  function routeHtml(ans) {
    const c = ans.cost;
    if (!c) return "";
    const bits = [];
    if (c.calls > 0) bits.push(`${fmtNum(c.total_tokens || 0)} tokens`, money(c.usd));
    else if (c.cached_plan) bits.push("same question asked before: no new AI call");
    if (ans.ms != null) bits.push(ans.ms < 1000 ? `${Math.max(1, Math.round(ans.ms))} ms` : `${(ans.ms / 1000).toFixed(1)} s`);
    return `<div class="route"><span>${esc(sentence(c.route_line))}</span><span class="route-meta">${esc(bits.join(" · "))}</span></div>`;
  }

  function readAsHtml(ans) {
    const rd = ans.read_as_plain && ans.read_as_plain.length ? ans.read_as_plain : (ans.read_as ? [{ label: null, text: cap(ans.read_as) }] : []);
    if (!rd.length) return "";
    const byModel = ans.plan_source && ans.plan_source !== "rules";
    return `<dl class="read-as">
      <dt>Read as${byModel ? ` <button type="button" class="ai-mark" aria-haspopup="dialog" aria-expanded="false" title="Written by an AI model. Click for details.">AI</button>` : ""}</dt>
      <dd>${rd.map((x) => `<div>${x.label ? `<b>${esc(x.label)}:</b> ` : ""}${esc(x.text)}</div>`).join("")}</dd>
    </dl>`;
  }

  function trustHtml(ans, level) {
    const t = ans.trust || {};
    return `<div class="trust">
      <span class="tag tag-${esc(level)}">${icon(TRUST_ICON[level] || "none")}${esc(TRUST_WORD[level] || t.label || "")}</span>
      <span class="trust-text"><span class="trust-reason">${esc(sentence(t.reason))}</span> ${esc(level !== "review" && !(ans.evidence_count > 0) ? "Nothing matched, so nothing is highlighted on the drawing." : (t.action || ""))}</span>
    </div>`;
  }

  function altsHtml(ans) {
    const alts = ans.alternatives || [];
    if (alts.length < 2) return "";
    return `<div class="alts" role="radiogroup" aria-label="Readings of this question">
      <div class="alts-title">This question can be read in ${alts.length} ways. Choose the one you meant.</div>
      ${alts.map((o, i) => `
        <button type="button" class="alt-btn ${o.active ? "active" : ""}" data-altidx="${i}" role="radio" aria-checked="${o.active ? "true" : "false"}">
          <span class="alt-radio" aria-hidden="true"></span>
          <span class="alt-title">${esc(o.title || o.label)}</span>
          <span class="alt-val">${esc(fmtVal(o.value))}${o.unit && o.unit !== "count" ? " " + esc(o.unit) : ""}</span>
          <span class="alt-note">${o.active ? "Used for this answer" : "Click to recompute with this reading. No AI model is called."}</span>
        </button>`).join("")}
    </div>`;
  }

  function buildAnswerCard(ans) {
    const card = el("article", "card ans-card");
    card.dataset.aid = ans.answer_id;
    const refused = ans.status !== "ok";
    const empty = !refused && isEmptyValue(ans.value);
    const level = refused || empty ? "none" : ((ans.trust && ans.trust.level) || "single_source");
    const doc = STATE.documents.find((d) => d.doc_id === ans.doc_id) || {};

    if (refused || empty) {
      const ref = ans.refusal || {};
      const sugg = (ref.suggested_questions && ref.suggested_questions.length ? ref.suggested_questions : (doc.suggested_questions || [])).slice(0, 4);
      const why = refused
        ? sentence(ref.why_cannot_answer || ans.reason || "The file does not contain what is needed")
        : "Nothing in this file matches the question as it was read.";
      const canAsk = ref.what_can_be_asked || "";
      const limits = (ref.file_limits || doc.unreadable || []).slice(0, 2);
      card.innerHTML = `
        <div class="ans-body refusal">
          <span class="tag tag-none">${icon("none")}Not answered</span>
          <h3>${refused ? "This cannot be answered from this file" : "Nothing was found for this question"}</h3>
          <p class="why">${esc(why)}${empty ? " This is not the same as zero: the information may simply not be on this sheet." : ""}</p>
          ${readAsHtml(ans)}
          ${limits.length ? `<div class="section-label">Limits of this file</div><ul class="limits">${limits.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>` : ""}
          ${canAsk || sugg.length ? `<div class="section-label">What you can ask instead</div>` : ""}
          ${canAsk ? `<p>${esc(canAsk)}</p>` : ""}
          ${sugg.length ? `<div style="margin-top:8px">${chipsHtml(sugg, ans.doc_id)}</div>` : ""}
          ${evidenceListHtml(ans)}
        </div>
        ${howHtml(ans)}${routeHtml(ans)}`;
    } else if (ans.findings && ans.findings.length) {
      /* plan check: the findings are the answer. Each names the room, measured vs required, and the clause it rests on */
      card.classList.add("is-check");
      card.innerHTML = `
        <div class="ans-body">
          <div class="check-head">Plan check</div>
          ${valueHtml(ans)}
          ${findingsHtml(ans)}
        </div>
        ${howHtml(ans)}${routeHtml(ans)}`;
    } else if (level === "review") {
      /* the problem leads; the value is shown smaller and marked as not confirmed */
      card.classList.add("is-review");
      card.innerHTML = `
        <div class="review-banner">${trustHtml(ans, level)}</div>
        <div class="ans-body">
          <div class="unconfirmed-note">Value before review</div>
          ${valueHtml(ans)}
          ${readAsHtml(ans)}${altsHtml(ans)}${evidenceListHtml(ans)}
        </div>
        ${howHtml(ans)}${routeHtml(ans)}`;
    } else {
      card.innerHTML = `
        <div class="ans-body">
          ${valueHtml(ans)}
          ${trustHtml(ans, level)}
          ${readAsHtml(ans)}${altsHtml(ans)}${evidenceListHtml(ans)}
        </div>
        ${howHtml(ans)}${routeHtml(ans)}`;
    }

    card.querySelectorAll(".finding").forEach((row) => {
      const f = ans.findings[Number(row.dataset.fi)];
      const head = row.querySelector(".finding-main");
      if (f.room && head) head.onclick = () => focusEvidence(ans.answer_id, f.room);
      const cb = row.querySelector(".clause-btn");
      if (cb) cb.onclick = (e) => { e.stopPropagation(); showClause(f); };
      const inp = row.querySelector(".whatif-input");
      if (inp) {
        const out = row.querySelector(".whatif-out");
        const upd = () => {
          const n = Math.max(1, Math.floor(Number(inp.value) || 0)), need = f.per_pupil * n, ok = f.area + 1e-9 >= need;
          out.className = "whatif-out " + (ok ? "ok" : "bad");
          out.textContent = ok ? `${n} pupils need ${need.toFixed(1)} m²: this room (${f.area} m²) is large enough` : `${n} pupils need ${need.toFixed(1)} m²: this room has ${f.area} m²`;
        };
        inp.oninput = upd; inp.onclick = (e) => e.stopPropagation(); upd();
      }
    });
    card.addEventListener("click", (ev) => {
      if (ev.target.closest("button, details, summary, a, input, .ev-row, .finding")) return;
      selectAnswerEvidence(ans.answer_id);
    });
    const se = card.querySelector(".js-show-ev");
    if (se) se.onclick = () => selectAnswerEvidence(ans.answer_id);
    card.querySelectorAll(".ev-row").forEach((row) => {
      const go = () => focusEvidence(ans.answer_id, row.dataset.id);
      row.onclick = go;
      row.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } };
    });
    card.querySelectorAll(".alt-btn").forEach((btn) => {
      btn.onclick = () => {
        const opt = ans.alternatives[Number(btn.dataset.altidx)];
        if (!opt || opt.active) return;
        if (STATE.activeDocId !== ans.doc_id) selectDocument(ans.doc_id);
        submitQuestion(ans.question, opt.plan);
      };
    });
    const am = card.querySelector(".ai-mark");
    if (am) am.onclick = (ev) => { ev.stopPropagation(); toggleAiPopover(am, ans); };
    bindChips(card);
    return card;
  }

  function selectAnswerEvidence(answerId, then) {
    const ans = STATE.answers[answerId];
    if (!ans) return;
    const changed = STATE.selectedAnswerId !== answerId;
    STATE.selectedAnswerId = answerId;
    document.querySelectorAll(".ans-card").forEach((c) => c.classList.toggle("selected", c.dataset.aid === answerId));
    document.querySelectorAll(".ev-row.is-focused").forEach((r) => r.classList.remove("is-focused"));
    if ($("viewEvidenceWrap").hidden) setRightTab("evidence");
    if (!changed && !$("viewerCanvas").hidden) { if (then) then(); return; }
    const n = ans.evidence_count || 0;
    const what = ans.status === "ok" ? (isEmptyValue(ans.value) ? "nothing found" : `answer ${shortValue(ans)}`) : "not answered";
    if (!ans.evidence_url) {
      // Not answered and nothing located: the drawing stays exactly as it was; only the caption and the list change.
      $("viewerCaption").innerHTML = `<b>Nothing highlighted</b> · not answered · ${esc(ans.document_name)}`;
      $("viewerMarks").innerHTML = "";          /* the drawing stays; marks of the previous answer must not remain under this one */
      STATE.viewerKey = "none|" + ans.answer_id;
      renderEvidencePanel(ans);
      if (then) then();
      return;
    }
    setViewerSource(ans.evidence_url, `<b>${n ? fmtNum(n) + " highlighted" : "Nothing highlighted"}</b> · ${esc(what)} · ${esc(ans.document_name)}`, ans);
    if (then) {
      const img = $("viewerImg"), prev = img.onload;
      img.onload = () => { prev && prev(); then(); };
    }
  }
  function shortValue(ans) {
    const v = ans.value;
    if (typeof v === "number") return fmtNum(v) + (ans.unit && ans.unit !== "count" ? " " + ans.unit : "");
    if (typeof v === "string" && v.length <= 24 && !v.includes("\n")) return v;
    return "shown in the card";
  }

  /* ------------------------------------------------------------------ 7 asking */
  async function submitQuestion(questionText, planOverride) {
    const doc = getActiveDoc();
    const q = String(questionText ?? $("questionInput").value).trim();
    if (!doc || !q) return;

    const qEl = feedAppend(el("div", "q-line", `
      <span class="q-meta">${esc(doc.display_name)}${planOverride ? " · other reading chosen by you" : ""}</span>
      <span class="q-text">${esc(q)}</span>`));
    const pending = feedAppend(el("div", "card card-pad", `
      <div class="hint">Computing the answer…</div>
      <div class="skel big"></div><div class="skel w70"></div><div class="skel w40"></div>`));
    if (questionText == null) $("questionInput").value = "";
    $("askBtn").disabled = true;

    const fail = (message, detail) => {
      const errCard = buildErrorCard("The answer could not be computed", message, detail, () => {
        qEl.remove();
        errCard.remove();
        submitQuestion(q, planOverride);
      });
      pending.replaceWith(errCard);
      $("chatFeed").scrollTop = $("chatFeed").scrollHeight;
    };
    try {
      const resp = await fetch("/api/chat/ask", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: SESSION_ID, doc_id: doc.doc_id, question: q, allow_llm: $("allowLlmCheck").checked, plan_override: planOverride || undefined }),
      });
      let data = null;
      try { data = await resp.json(); } catch (_) { /* not JSON */ }
      if (!resp.ok || !data || !data.ok) {
        fail((data && data.error) || `The server returned an error (${resp.status}). Nothing was computed, so no value is shown.`,
          "Your question is kept. Try again, or ask it in a simpler way.");
        return;
      }
      STATE.answers[data.answer_id] = data;
      updateSessionTotals(data.session_totals);
      const cardEl = buildAnswerCard(data);
      pending.replaceWith(cardEl);
      const firstDoubt = (data.evidence_items || []).find((it) => doubtIds(data).has(it.id) && it.box);
      selectAnswerEvidence(data.answer_id, firstDoubt ? () => focusEvidence(data.answer_id, firstDoubt.id) : null);
      /* keep the value in view: put the question line at the top of the feed */
      const feed = $("chatFeed");
      feed.scrollTop = Math.max(0, qEl.offsetTop - feed.offsetTop - 12);
    } catch (err) {
      fail("The server could not be reached. Nothing was computed, so no value is shown.", err.message);
    } finally {
      $("askBtn").disabled = !getActiveDoc();
    }
  }

  /* ------------------------------------------------------------------ 8 evidence panel: the compact list beside the drawing */
  function cellHtml(v, h, extraCls) {
    const txt = fmtVal(v);
    const cls = [h.numeric ? "r" : "", h.used ? "is-used" : "", h.derived ? "td-derived" : "", txt.length > 44 ? "wrap" : "", extraCls || ""].filter(Boolean).join(" ");
    return `<td class="${cls}">${txt === "" ? "" : esc(txt)}</td>`;
  }

  /* Schedule table with the header as printed: group cells (Door / Frame) span their columns in a first
     header row; columns the reader added are set apart and labelled as not printed. */
  function scheduleTableHtml(header, rows, opts) {
    const o = opts || {};
    if (o.hideDerived) {
      const keep = header.map((h, i) => (!h.derived || h.used ? i : -1)).filter((i) => i >= 0);
      header = keep.map((i) => header[i]);
      rows = rows.map((r) => ({ ...r, cells: keep.map((i) => r.cells[i]) }));
    }
    header = header.map((h, i) => (i === 0 ? { ...h, first: true } : h));
    const twoRows = header.some((h) => h.group || h.derived);
    const lead = `<th class="th-mark" ${twoRows ? 'rowspan="2"' : ""} title="${o.markTitle || "Row"}">${o.markHead || "Row"}</th>`;
    const th = (h) => `<th class="${[h.numeric ? "r" : "", h.used ? "is-used" : "", h.derived ? "th-derived" : "", h.first ? "col-first" : "", h.label.length > 16 ? "th-wrap" : ""].filter(Boolean).join(" ")}"${h.derived && h.note ? ` title="${esc(h.note)}"` : ""}>${h.used ? `<span class="role">${esc(h.used)}</span>` : ""}${esc(h.label)}</th>`;
    let head;
    if (!twoRows) head = `<tr>${lead}${header.map(th).join("")}</tr>`;
    else {
      let top = "", bottom = "";
      for (let i = 0; i < header.length;) {
        const h = header[i];
        const g = h.derived ? "\u0000derived" : h.group;
        if (!g) { top += th(h).replace("<th ", '<th rowspan="2" '); i++; continue; }
        let j = i;
        while (j < header.length && (header[j].derived ? "\u0000derived" : header[j].group) === g) j++;
        top += h.derived
          ? `<th class="th-group th-derived" colspan="${j - i}" title="These columns are not printed in this table on the sheet.">Added by the reader</th>`
          : `<th class="th-group" colspan="${j - i}">${esc(g)}</th>`;
        for (let k = i; k < j; k++) bottom += th(header[k]);
        i = j;
      }
      head = `<tr>${lead}${top}</tr><tr>${bottom}</tr>`;
    }
    const body = rows.map((r, idx) => `
      <tr ${o.aid ? `class="ev-row" data-aid="${esc(o.aid)}" data-id="${esc(r.id)}" tabindex="0"` : ""}>
        <td class="td-mark ${o.aid && r.mark != null ? "has-mark" : ""}">${o.aid ? (r.mark != null ? r.mark : "") : idx + 1}</td>
        ${header.map((h, i) => cellHtml(r.cells[i], h, h.first ? "col-first" : "")).join("")}
      </tr>`).join("");
    return `<table class="grid sched ${o.filterable ? "js-filterable" : ""}"><thead>${head}</thead><tbody>${body}</tbody></table>`;
  }

  function renderEvidencePanel(ans) {
    const panel = $("evidenceListDrawer");
    const items = (ans && ans.evidence_items) || [];
    if (!ans || !items.length) { panel.hidden = true; panel.innerHTML = ""; return; }
    let html = "";
    const tables = ans.evidence_tables || [];
    if (tables.length) {
      html = tables.map((t) => {
        const used = t.header.filter((h) => h.used).map((h) => `${h.group ? h.group + " › " : ""}${h.label}`);
        return `<div class="panel-head">
            <h3>${esc(t.title)} · ${fmtNum(t.rows.length)} of ${fmtNum(t.total_rows || t.rows.length)} rows ${ans.status === "ok" ? "used" : "found"}</h3>
            <span class="hint">${used.length ? `Column used: <b>${esc(used.join(", "))}</b> · ` : ""}Click a row to find it on the drawing</span>
          </div>
          <div class="table-scroll">${scheduleTableHtml(t.header, t.rows, { aid: ans.answer_id, markHead: "No.", markTitle: "Number on the drawing", hideDerived: true })}</div>`;
      }).join("");
    } else {
      const cols = DETAIL_COLS.filter(([k]) => items.some((it) => it.details && it.details[k] !== undefined && typeof it.details[k] !== "object" && !(k === "area" && typeof it.details[k] !== "number")));
      const showHow = items.some((it) => it.provenance);
      const total = ans.evidence_count || items.length;
      const unit = (k) => (ans.format === "dxf-layered" || ans.format === "ifc") && k === "area" ? " (m²)" : (ans.format === "ifc" && k === "volume" ? " (m³)" : (ans.format === "ifc" && ["length", "width", "height"].includes(k) ? " (m)" : ""));
      html = `<div class="panel-head">
          <h3>${fmtNum(total)} ${evNoun(items)[total === 1 ? 0 : 1]} ${ans.status === "ok" ? "used for this answer" : "found"}</h3>
          <span class="hint">${ans.evidence_has_marks ? "Click a row to find it on the drawing" : "Numbers match the marks on the drawing. One storey is drawn at a time."}</span>
        </div>
        <div class="table-scroll"><table class="grid">
          <thead><tr><th class="th-mark" title="Number on the drawing">No.</th><th>Element</th>${cols.map(([k, l, n]) => `<th class="${n ? "r" : ""}">${l}${unit(k)}</th>`).join("")}${showHow ? "<th>How it was found</th>" : ""}</tr></thead>
          <tbody>${items.map((it) => `
            <tr class="ev-row" data-aid="${esc(ans.answer_id)}" data-id="${esc(it.id)}" tabindex="0">
              <td class="td-mark ${markText(it, ans) ? "has-mark" : ""}">${esc(markText(it, ans))}</td>
              <td>${esc(itemName(it, ans))}${doubtIds(ans).has(it.id) ? ' <span class="flag">check this one</span>' : it.borderline ? ' <span class="hint">· uncertain detection</span>' : ""}</td>
              ${cols.map(([k, , n]) => `<td class="${n ? "r" : ""}">${esc(fmtVal((it.details || {})[k]))}</td>`).join("")}
              ${showHow ? `<td class="wrap">${esc(plainHow(it.provenance))}</td>` : ""}
            </tr>`).join("")}</tbody>
        </table></div>
        ${(ans.evidence_count || 0) > items.length ? `<div class="hint" style="padding:4px 16px">Showing the first ${items.length} of ${fmtNum(ans.evidence_count)}.</div>` : ""}`;
    }
    panel.innerHTML = html;
    panel.hidden = false;
    /* bring the column the answer used into view; the row number and first column stay pinned */
    panel.querySelectorAll(".table-scroll").forEach((sc) => {
      const u = sc.querySelector("th.is-used:not(.col-first)");
      if (u && u.offsetLeft + u.offsetWidth > sc.clientWidth) sc.scrollLeft = u.offsetLeft + u.offsetWidth - sc.clientWidth + 120;
    });
    panel.querySelectorAll(".ev-row").forEach((row) => {
      const go = () => focusEvidence(row.dataset.aid, row.dataset.id);
      row.onclick = go;
      row.onkeydown = (e) => { if (e.key === "Enter") go(); };
    });
  }

  /* ------------------------------------------------------------------ 9 what was read */
  async function loadInspectView(docId) {
    const wrap = $("viewInspectWrap");
    if (!docId) {
      wrap.innerHTML = `<div class="viewer-empty" style="position:static;padding:48px 24px">${icon("file")}<b>Nothing read yet</b><span>Upload a file to see every table, room and element that was read from it.</span></div>`;
      return;
    }
    wrap.innerHTML = `<div class="hint">Loading what was read…</div><div class="skel w70"></div><div class="skel w40"></div>`;
    try {
      let info = STATE.inspectCache[docId];
      if (!info) {
        const r = await fetch(`/api/chat/inspect?session_id=${encodeURIComponent(SESSION_ID)}&doc_id=${encodeURIComponent(docId)}`);
        info = await r.json();
        if (info.ok) STATE.inspectCache[docId] = info;
      }
      if (!info || !info.ok) throw new Error((info && info.error) || "The file could not be opened.");
      if (docId === STATE.activeDocId) renderInspect(wrap, info);
    } catch (err) {
      wrap.innerHTML = "";
      wrap.appendChild(buildErrorCard("What was read could not be shown", err.message, null, () => loadInspectView(docId)));
    }
  }

  function simpleTable(cols, rows, filterable) {
    /* cols: [key, label, numeric] */
    return `<div class="table-scroll"><table class="grid ${filterable ? "js-filterable" : ""}">
      <thead><tr>${cols.map(([, l, n]) => `<th class="${n ? "r" : ""}">${esc(l)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((r) => `<tr>${cols.map(([k, , n]) => {
        const txt = fmtVal(r[k]);
        return `<td class="${n ? "r" : ""} ${txt.length > 44 ? "wrap" : ""}">${esc(txt)}</td>`;
      }).join("")}</tr>`).join("")}</tbody></table></div>`;
  }
  const block = (title, meta, body, note) => `<section class="block">
      <div class="block-head"><h4>${esc(title)}</h4>${meta ? `<span class="hint">${esc(meta)}</span>` : ""}</div>
      ${body}${note ? `<div class="block-note">${esc(note)}</div>` : ""}</section>`;

  function renderInspect(wrap, info) {
    const stats = [];
    if ((info.tables || []).length) { stats.push([info.tables.length, "schedule tables"]); stats.push([info.tables.reduce((s, t) => s + (t.row_count || 0), 0), "rows"]); }
    if ((info.rooms || []).length) stats.push([info.rooms.length, info.format === "raster" ? "rooms with printed labels" : "rooms"]);
    if ((info.apartments || []).length) stats.push([info.apartments.length, "apartments"]);
    if ((info.levels || []).length) stats.push([info.levels.length, "storeys"]);
    Object.entries(info.symbols || {}).filter(([k]) => k !== "schedule_row" && k !== "element").sort((a, b) => b[1] - a[1]).slice(0, 5).forEach(([k, n]) => stats.push([n, kindPlural(k)]));

    const limits = info.unreadable || [];
    let html = `<div class="inspect-head">
        <h3>${esc(info.display_name)}</h3>
        <p>${esc(FORMAT_WORDS[info.format] || info.format)}. Everything below was read from the file and is what answers are computed from.${info.units && info.format !== "pdf-vector" ? " Units: " + esc(info.units) + "." : ""}</p>
      </div>
      ${stats.length ? `<div class="stats">${stats.map(([v, l]) => `<div class="stat"><b>${esc(fmtNum(v))}</b><span>${esc(l)}</span></div>`).join("")}</div>` : ""}
      ${limits.length ? `<div class="section-label" style="margin-top:16px">Limits of this file</div><ul class="limits">${limits.map((u) => `<li>${esc(sentence(u))}</li>`).join("")}</ul>` : ""}
      <input type="search" class="inspect-filter" id="inspectFilterInput" placeholder="Filter rows, for example a door number, a room name or a material" aria-label="Filter rows">`;

    (info.tables || []).forEach((t) => {
      const header = t.header && t.header.length ? t.header : (t.columns || []).map((c) => ({ key: c, label: cap(c.replace(/_/g, " ")), group: null, derived: false, numeric: false }));
      const rows = (t.rows || []).map((r) => ({ id: r._id, cells: header.map((h) => r[h.key]) }));
      const derived = header.filter((h) => h.derived);
      html += block(t.title, `${t.row_count} rows · page ${t.page} · ${METHOD_WORDS[t.method] || t.method || ""}`,
        `<div class="table-scroll">${scheduleTableHtml(header, rows, { filterable: true })}</div>`,
        derived.length ? `Shaded columns (${derived.map((h) => h.label).join(", ")}) are not printed in this table; the reader added them. ${derived.map((h) => h.note).filter((n) => n && !/^Worked out/.test(n)).map((n) => "Source: " + n + ".")[0] || "They are worked out from the other columns."}` : "");
    });

    if ((info.rooms || []).length) {
      const base = info.format === "ifc"
        ? [["level", "Storey"], ["number", "Number"], ["label", "Name"], ["area", "Area (m²)", true], ["volume", "Volume (m³)", true], ["how", "How the area was obtained"]]
        : info.format === "raster"
        ? [["label", "Label"], ["type", "Type"], ["area", "Printed area", true], ["how", "How it was read"]]
        : [["number", "Number"], ["label", "Name"], ["type", "Type"], ["apartment", "Apartment"], ["area", "Area (m²)", true], ["printed", "Area printed on the plan", true], ["how", "How the area was obtained"]];
      const cols = base.filter(([k]) => info.rooms.some((r) => r[k] !== null && r[k] !== undefined && r[k] !== ""));
      const pc = info.printed_check;
      html += block("Rooms", `${info.rooms.length} rooms`, simpleTable(cols, info.rooms, true),
        pc && pc.with_printed ? `${pc.with_printed} of ${pc.rooms} rooms have an area printed on the plan; ${pc.agree} of those agree with the measured area.` : "");
    }
    if ((info.apartments || []).length) {
      const keys = Object.keys(info.apartments[0]).filter((k) => !["id", "flags", "ids"].includes(k) && typeof info.apartments[0][k] !== "object").slice(0, 6);
      html += block("Apartments", `${info.apartments.length}`, simpleTable(keys.map((k) => [k, cap(k.replace(/_/g, " ")), typeof info.apartments[0][k] === "number"]), info.apartments, true));
    }
    if ((info.levels || []).length) {
      html += block("Storeys", `${info.levels.length}`, simpleTable([["level", "Storey"], ["elevation", "Elevation (m)", true], ["spaces", "Rooms", true], ["area", "Room area (m²)", true]], info.levels, false));
    }
    if ((info.room_types || []).length) {
      const keys = Object.keys(info.room_types[0]);
      const lab = { type: "Room type", "by vision model": "Found by the detector", "labels read by OCR": "Printed labels read" };
      html += block("Rooms by type", "", simpleTable(keys.map((k) => [k, lab[k] || cap(k), k !== "type"]), info.room_types, false));
    }
    const syms = Object.entries(info.symbols || {}).filter(([k]) => k !== "schedule_row");
    if (syms.length && !(info.elements_by_kind || []).length) {
      html += block(info.format === "raster" ? "Detected on the plan" : "Counted on the plan", "",
        simpleTable([["k", "Element"], ["n", "Count", true]], syms.map(([k, n]) => ({ k: cap(kindPlural(k)), n })), false));
    }
    if ((info.elements_by_kind || []).length) {
      html += `<section class="block"><div class="block-head"><h4>Elements in the model</h4><span class="hint">Open a line to see examples</span></div>
        ${info.elements_by_kind.map((g) => {
          const sample = g.sample || [];
          const cols = [["label", "Name"], ["type_name", "Type"], ["level", "Storey"], ["number", "Number"], ["area", "Area", true], ["volume", "Volume", true], ["length", "Length", true]]
            .filter(([k]) => sample.some((s) => s[k] !== undefined && s[k] !== null && s[k] !== ""));
          return `<details class="kind-row"><summary><b>${esc(cap(kindPlural(g.kind)))}</b><span class="num">${fmtNum(g.count)}</span></summary>
            ${sample.length && cols.length ? simpleTable(cols, sample, true) + (g.count > sample.length ? `<div class="hint" style="padding:4px 12px">First ${sample.length} of ${fmtNum(g.count)} shown.</div>` : "") : ""}</details>`;
        }).join("")}</section>`;
    }
    if ((info.facts || []).length) {
      html += block("Facts about the model", "", simpleTable([["name", "Fact"], ["value", "Value"]], info.facts.map((f) => ({ name: cap(String(f.name || "").replace(/_/g, " ")), value: f.value })), false));
    }
    if ((info.issues || []).length) {
      const sev = { error: "Problem", warn: "Caution", info: "Note" };
      html += block("Notes on this drawing", "These lower the trust label of answers they touch",
        `<ul class="checks">${info.issues.map((i) => `<li><span class="res res-${i.sev === "error" ? "fail" : i.sev === "warn" ? "caution" : "info"}">${sev[i.sev] || "Note"}</span><span>${esc(sentence(i.text))}</span></li>`).join("")}</ul>`);
    }
    if (!(info.tables || []).length && !(info.rooms || []).length && !syms.length && !(info.elements_by_kind || []).length) {
      html += `<div class="viewer-empty" style="position:static;padding:48px 24px">${icon("none")}<b>Nothing that can be counted or measured was read</b><span>See “Limits of this file” above for the reason.</span></div>`;
    }
    wrap.innerHTML = html;
    wrap.scrollTop = 0;
    const flt = $("inspectFilterInput");
    flt.oninput = () => {
      const needle = flt.value.trim().toLowerCase();
      wrap.querySelectorAll("table.js-filterable tbody tr").forEach((tr) => { tr.hidden = Boolean(needle) && !tr.textContent.toLowerCase().includes(needle); });
    };
  }

  /* ------------------------------------------------------------------ 10 the one AI marker and its explanation */
  let aiPopAnchor = null;
  function closeAiPopover() {
    const pop = $("aiPopover");
    if (pop.hidden) return;
    pop.hidden = true;
    if (aiPopAnchor) { aiPopAnchor.setAttribute("aria-expanded", "false"); aiPopAnchor.focus(); }
    aiPopAnchor = null;
  }
  function toggleAiPopover(anchor, ans) {
    const pop = $("aiPopover");
    if (!pop.hidden && aiPopAnchor === anchor) { closeAiPopover(); return; }
    closeAiPopover();
    const c = ans.cost || {};
    const usage = c.calls > 0
      ? `${fmtNum(c.in_tokens || 0)} tokens in, ${fmtNum(c.out_tokens || 0)} out, about ${money(c.usd)}`
      : c.cached_plan ? `No new call: the same question was read before${c.cold_est_usd ? ` (that call cost about ${money(c.cold_est_usd)})` : ""}` : "No call";
    pop.innerHTML = `
      <button type="button" class="pop-close" aria-label="Close">${icon("close")}</button>
      <h3 id="aiPopTitle">This line was written by an AI model</h3>
      <dl>
        <dt>What it did</dt><dd>Read your question and turned it into the instruction shown in “Read as”. The built-in rules did not recognise the wording.</dd>
        <dt>What it did not do</dt><dd>It did not see the drawing and did not produce the value. Code ran the instruction on the data read from the file.</dd>
        <dt>What to check</dt><dd>That “Read as” says what you meant. The arithmetic is exact; the reading is the part that can be wrong.</dd>
        <dt>Model</dt><dd>${esc(c.model || "planner model")} (${c.tier === "large" ? "large" : "small"})</dd>
        <dt>Use</dt><dd>${esc(usage)}</dd>
      </dl>`;
    pop.hidden = false;
    const r = anchor.getBoundingClientRect();
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    let left = Math.min(Math.max(8, r.left), window.innerWidth - pw - 8);
    let top = r.bottom + 8;
    if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 8);
    pop.style.left = left + "px"; pop.style.top = top + "px";
    aiPopAnchor = anchor;
    anchor.setAttribute("aria-expanded", "true");
    pop.querySelector(".pop-close").onclick = closeAiPopover;
    pop.querySelector(".pop-close").focus();
  }

  /* ------------------------------------------------------------------ 11 start-up */
  async function loadExistingSession() {
    updateLogLink();
    try {
      const r = await fetch(`/api/chat/files?session_id=${encodeURIComponent(SESSION_ID)}`);
      const data = await r.json();
      if (data.session_id) { SESSION_ID = data.session_id; localStorage.setItem(STORAGE_KEY, SESSION_ID); }
      STATE.documents = data.documents || [];
      updateSessionTotals(data.session_totals);
      renderDocList();
      if (STATE.documents.length) {
        const last = STATE.documents[STATE.documents.length - 1];
        selectDocument(last.doc_id);
        appendFileCard(last, false);
      }
    } catch (_) { /* a fresh server has no session yet */ }
  }

  function init() {
    bindViewerEvents();
    renderEmptyState();
    renderDocList();
    setViewerSource(null, null, null);
    $("uploadIcon").innerHTML = icon("upload");

    const uBox = $("uploadBox");
    uBox.onclick = () => $("fileInput").click();
    $("fileInput").onchange = (e) => uploadFiles(e.target.files);
    ["dragover", "dragenter"].forEach((t) => document.addEventListener(t, (e) => { e.preventDefault(); uBox.classList.add("dragover"); }));
    ["dragleave", "drop"].forEach((t) => document.addEventListener(t, (e) => { e.preventDefault(); uBox.classList.remove("dragover"); }));
    document.addEventListener("drop", (e) => { if (e.dataTransfer && e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files); });

    $("applyPagesBtn").onclick = applySelectedPages;
    $("selectAllPagesBtn").onclick = () => {
      const doc = getActiveDoc();
      if (!doc) return;
      STATE.draftPages = new Set((doc.pages || []).map((p) => p.page));
      renderPagesPicker();
    };
    $("openInspectBtn").onclick = () => setRightTab("inspect");
    $("rTabEvidence").onclick = () => setRightTab("evidence");
    $("rTabInspect").onclick = () => setRightTab("inspect");

    $("askForm").onsubmit = (e) => { e.preventDefault(); submitQuestion(null, null); };
    $("questionInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitQuestion(null, null); }
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAiPopover(); });
    document.addEventListener("click", (e) => { if (!e.target.closest("#aiPopover, .ai-mark")) closeAiPopover(); });
    $("chatFeed").addEventListener("scroll", closeAiPopover);

    $("newSessionBtn").onclick = () => {
      SESSION_ID = makeSessionId();
      localStorage.setItem(STORAGE_KEY, SESSION_ID);
      Object.assign(STATE, { documents: [], activeDocId: null, answers: {}, selectedAnswerId: null, inspectCache: {}, focusId: null });
      renderEmptyState();
      updateSessionTotals({ questions: 0, rules_count: 0, small_model_count: 0, large_model_count: 0, usd: 0 });
      setViewerSource(null, null, null);
      renderDocList();
      if (!$("viewInspectWrap").hidden) loadInspectView(null);
    };

    loadExistingSession();
  }

  init();
})();
