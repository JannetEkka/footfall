"use strict";

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const el = (tag, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of kids) if (k != null) n.append(k);
  return n;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (id) => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("aria-hidden", "true");
  const u = document.createElementNS("http://www.w3.org/2000/svg", "use");
  u.setAttribute("href", `#i-${id}`);
  s.append(u);
  return s;
};
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch { return false; } },
};

/* --- theme ------------------------------------------------------------- */
function isDark() {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
}
$("#theme").addEventListener("click", () => {
  const next = isDark() ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("footfall-theme", next); } catch {}
  setTiles();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", setTiles);

/* --- nav, reveals, hero walkers --------------------------------------- */
const nav = $("#nav");
new IntersectionObserver(([e]) => nav.classList.toggle("solid", !e.isIntersecting), { rootMargin: "-64px 0px 0px 0px" })
  .observe($(".hero"));

const revealer = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); revealer.unobserve(e.target); }
}, { threshold: 0.12 });
$$(".reveal").forEach((n) => revealer.observe(n));

function walkers() {
  // Footfall, literally: people walking the streets drawn over the hero photo.
  if (reduceMotion) return;
  const NS = "http://www.w3.org/2000/svg";
  const g = $(".walkers .feet");
  const paths = ["st1", "st2", "st3", "st4", "st5"];
  for (let i = 0; i < 26; i++) {
    const p = paths[i % paths.length];
    const dur = 16 + Math.random() * 22;
    const back = Math.random() < 0.5;
    const grp = document.createElementNS(NS, "g");
    const halo = document.createElementNS(NS, "circle");
    halo.setAttribute("r", "9");
    halo.setAttribute("fill", "url(#glow)");
    halo.setAttribute("opacity", "0.55");
    const core = document.createElementNS(NS, "circle");
    core.setAttribute("r", (1.6 + Math.random() * 1.4).toFixed(1));
    core.setAttribute("class", "core");
    grp.append(halo, core);
    const m = document.createElementNS(NS, "animateMotion");
    m.setAttribute("dur", `${dur.toFixed(1)}s`);
    m.setAttribute("begin", `-${(Math.random() * dur).toFixed(1)}s`);
    m.setAttribute("repeatCount", "indefinite");
    if (back) { m.setAttribute("keyPoints", "1;0"); m.setAttribute("keyTimes", "0;1"); m.setAttribute("calcMode", "linear"); }
    const mp = document.createElementNS(NS, "mpath");
    mp.setAttribute("href", `#${p}`);
    m.append(mp);
    grp.append(m);
    g.append(grp);
  }
}
walkers();

/* --- toasts ------------------------------------------------------------ */
function toast(text, kind = "") {
  const t = el("div", { className: `toast ${kind}`, textContent: text });
  $("#toasts").append(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 320); }, kind === "err" ? 6000 : 3000);
}

/* --- tabs -------------------------------------------------------------- */
const tabs = $$('[role="tab"]');
function selectTab(i, focus = false) {
  tabs.forEach((t, j) => {
    const on = i === j;
    t.setAttribute("aria-selected", on);
    t.tabIndex = on ? 0 : -1;
    $(`#${t.getAttribute("aria-controls")}`).hidden = !on;
  });
  $(".tabs").dataset.active = i;
  if (focus) tabs[i].focus();
}
tabs.forEach((t, i) => {
  t.addEventListener("click", () => selectTab(i));
  t.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") selectTab((i + 1) % tabs.length, true);
  });
});

/* --- map --------------------------------------------------------------- */
let map, tiles, layer, heat;
function initMap() {
  if (typeof L === "undefined") return;
  map = L.map("map", { scrollWheelZoom: false, zoomControl: true }).setView([30, 10], 2);
  layer = L.layerGroup().addTo(map);
  heat = L.layerGroup().addTo(map);
  setTiles();
}
function setTiles() {
  // OpenStreetMap's own tiles, tinted to the page's palette in CSS (.map-light / .map-dark).
  if (!map) return;
  $("#map").classList.toggle("map-dark", isDark());
  $("#map").classList.toggle("map-light", !isDark());
  if (tiles) return;
  tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
}
function onLocated(ev) {
  if (!map) return;
  layer.clearLayers();
  heat.clearLayers();
  const pin = L.divIcon({ className: "", html: '<div class="shop-pin"></div>', iconSize: [18, 18], iconAnchor: [9, 9] });
  L.marker([ev.lat, ev.lon], { icon: pin, keyboard: false }).bindTooltip("The shop").addTo(layer);
  if (reduceMotion) map.setView([ev.lat, ev.lon], 15);
  else map.flyTo([ev.lat, ev.lon], 15, { duration: 1.6 });
}
function plotPlaces(items) {
  if (!map) return;
  const pts = [];
  items.forEach((it, i) => {
    if (it.lat == null) return;
    pts.push([it.lat, it.lon]);
    setTimeout(() => {
      L.circleMarker([it.lat, it.lon], { radius: 6, color: "#2f6b4f", weight: 2, fillColor: "#7cc39f", fillOpacity: 0.85 })
        .bindTooltip(`${esc(it.name)}${it.distance_m != null ? ` · ${it.distance_m} m` : ""}`).addTo(layer);
    }, reduceMotion ? 0 : 120 * i);
  });
  if (pts.length > 1) setTimeout(() => map.flyToBounds(pts, { padding: [28, 28], maxZoom: 16, duration: 1.2 }), reduceMotion ? 0 : 1700);
}
function plotHeat(items) {
  if (!map) return;
  const top = Math.max(...items.map((c) => c.affinity || 0), 0.001);
  for (const c of items) {
    if (c.lat == null) continue;
    L.circle([c.lat, c.lon], { radius: 140, stroke: false, fillColor: "#e9a14b", fillOpacity: 0.12 + 0.35 * ((c.affinity || 0) / top) })
      .bindTooltip("Where this taste is strongest").addTo(heat);
  }
}

/* --- progress ---------------------------------------------------------- */
const STAGES = ["locate", "survey", "investigate", "write", "check"];
let progress = 0;
function setStage(stage) {
  const i = STAGES.indexOf(stage);
  if (i < 0) return;
  $$("#stepper li").forEach((li, j) => {
    li.classList.toggle("done", j < i);
    li.classList.toggle("active", j === i);
  });
  bump([8, 22, 58, 86, 95][i]);
}
function bump(to) { progress = Math.max(progress, Math.min(to, 100)); $("#bar").style.width = `${progress}%`; }
function resetProgress() {
  progress = 0;
  $("#bar").style.width = "0";
  $$("#stepper li").forEach((li) => li.classList.remove("done", "active"));
}
function finishProgress() {
  bump(100);
  $$("#stepper li").forEach((li) => { li.classList.remove("active"); li.classList.add("done"); });
}

/* --- log --------------------------------------------------------------- */
function log(html, cls = "") {
  $(".log .empty")?.remove();
  const li = el("li", { className: cls });
  li.innerHTML = html;
  $("#log").append(li);
  $("#log").scrollTop = $("#log").scrollHeight;
}
function onQloo(ev) {
  const why = ev.why ? ` <span class="why">${esc(ev.why)}</span>` : "";
  if (ev.error) {
    log(`<span class="ref">${esc(ev.ref || "—")}</span>${esc(ev.title)}: ${esc(ev.error)}${why}`, "err");
    return;
  }
  const names = ev.items.slice(0, 4).map((i) => esc(i.name)).join(", ");
  const nf = ev.not_found ? ` <span class="why">Qloo didn't recognise ${ev.not_found} of the places sent as signal; they're replaced for the next calls.</span>` : "";
  log(`<span class="ref">${esc(ev.ref)}</span><b>${esc(ev.title)}</b>: ${ev.count} result${ev.count === 1 ? "" : "s"}${names ? ` (${names}${ev.count > 4 ? ", …" : ""})` : ""}${why}${nf}`, ev.by === "agent" ? "agent" : "");
}

/* --- health and demo shops -------------------------------------------- */
const MODEL_NAMES = {
  "nvidia/Nemotron-3_5-Lightning": "Nemotron 3.5 Lightning",
  "nvidia/nemotron-3-super-120b-a12b": "Nemotron 3 Super",
  "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": "Nemotron 3 Nano",
  "nvidia/Nemotron-3-Ultra-550b-a55b": "Nemotron 3 Ultra",
};
const modelName = (id) => MODEL_NAMES[id] || id.replace(/^nvidia\//, "");
const KIND_ICON = { Bookshop: "book", Café: "cup", Cafe: "cup", "Record shop": "music", Bar: "cup" };
let presets = [];
async function health() {
  const st = $("#health");
  try {
    const h = await (await fetch("/api/health")).json();
    const bits = [];
    const replay = h.qloo === "replay";
    if (!h.qloo_key && !replay) bits.push("Qloo key not set on the server");
    if (!h.llm_key) bits.push("LLM key not set on the server");
    if (h.budget && h.budget.ok === false) bits.push("spend ledger unavailable");
    st.className = `status ${bits.length ? "bad" : "ok"}`;
    const models = [...new Set([h.models?.tools, h.models?.write].filter(Boolean))].map(modelName).join(" + ");
    $(".txt", st).textContent = bits.length ? bits.join(" · ") : `Agent ready · ${models || h.llm} on Nebius Token Factory`;
    presets = h.presets;
    renderPresets(replay);
    if (replay) {
      $("#replay-note").hidden = false;
      $("#replay-note").textContent = "Qloo's hackathon API key has expired, so your own address can't be read now. The demo shops still work: they replay the Qloo answers saved while the key worked, and Nemotron runs live.";
      $("#go").disabled = true;
      $("#live-badge").hidden = false;
      $("#live-badge").textContent = "Replaying saved Qloo answers";
    }
    deepLink();
  } catch {
    st.className = "status bad";
    $(".txt", st).textContent = "Server not reachable. On the free plan it can take a minute to wake up; try again shortly.";
    $("#presets").replaceChildren();
  }
}
function renderPresets(replay) {
  $("#presets").replaceChildren(
    ...presets.map((p) => {
      const [kind, place] = p.title.split(" · ");
      const b = el("button", { type: "button", className: "preset" });
      b.dataset.id = p.id;
      b.append(
        el("span", { className: "ico" }, icon(KIND_ICON[kind] || "store")),
        el("span", {}, el("b", { textContent: place || p.title }), el("small", { textContent: `${kind}${replay ? " · saved Qloo answers" : ""}` })),
        el("span", { className: "go", textContent: "→" })
      );
      b.addEventListener("click", () => runPlan({ preset: p.id }, b));
      return b;
    })
  );
}
function deepLink() {
  const m = location.hash.match(/^#demo=([\w-]+)/);
  if (!m) return;
  const b = $(`.preset[data-id="${CSS.escape(m[1])}"]`);
  if (!b) return;
  selectTab(0);
  $("#try").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
  b.focus({ preventScroll: true });
  b.classList.add("running");
  setTimeout(() => b.classList.remove("running"), 1600);
}

/* --- running a plan ---------------------------------------------------- */
let current = null;
function run(e) {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target).entries());
  body.radius_m = Number(body.radius_m);
  runPlan(body);
}
async function runPlan(body, presetBtn) {
  const controls = [$("#go"), ...$$(".preset")];
  const goWasDisabled = $("#go").disabled;
  controls.forEach((b) => (b.disabled = true));
  presetBtn?.classList.add("running");
  $("#log").replaceChildren();
  $("#plan-card").hidden = true;
  $("#gap-card").hidden = true;
  resetProgress();
  current = { body, shop: body.preset ? null : { name: body.name, address: body.address }, preset: body.preset || null, when: new Date().toISOString() };
  if (innerWidth < 880) $(".live").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  try {
    const r = await fetch("/api/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      const msg = j.error || j.detail?.[0]?.msg || `Server error ${r.status}`;
      log(esc(msg), "err");
      toast(msg, "err");
      resetProgress();
      return;
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i);
        buf = buf.slice(i + 2);
        if (chunk.startsWith("data: ")) handle(JSON.parse(chunk.slice(6)));
      }
    }
  } catch (err) {
    log(esc(`Connection lost: ${err.message}`), "err");
    toast("Connection lost. Please try again.", "err");
  } finally {
    controls.forEach((b) => (b.disabled = false));
    $("#go").disabled = goWasDisabled;
    presetBtn?.classList.remove("running");
    $("#drafting").hidden = true;
  }
}

function handle(ev) {
  switch (ev.type) {
    case "mode": {
      current.shop = ev.shop;
      const f = $("#shop-form");
      for (const k of ["name", "address", "kind", "sells"]) if (f.elements[k]) f.elements[k].value = ev.shop[k] ?? "";
      const when = ev.recorded ? ` saved on ${esc(ev.recorded)}` : "";
      log(ev.replay
        ? `Demo shop: <b>${esc(ev.shop.name)}</b>. Replaying ${ev.saved} Qloo answers${when}; Nemotron runs live.`
        : `Demo shop: <b>${esc(ev.shop.name)}</b>. ${ev.saved} Qloo answers already saved${when}; anything new is fetched live.`, "mode");
      break;
    }
    case "step":
      setStage(ev.stage);
      log(`<b>${esc(ev.text)}</b>`, "is-step");
      if (ev.stage === "write") { $("#plan-card").hidden = false; $("#drafting").hidden = false; resetPlan(); }
      break;
    case "located":
      current.located = ev;
      onLocated(ev);
      log(`Found: ${esc(ev.label)}`);
      break;
    case "qloo":
      onQloo(ev);
      if (progress < 86) bump(progress + (ev.by === "agent" ? 3 : 2.5));
      if (ev.title === "Places around the shop") plotPlaces(ev.items);
      if (ev.title === "heatmap" && !ev.error) plotHeat(ev.items);
      break;
    case "taste": current.taste = ev; onTaste(ev); break;
    case "plan":
      current.plan = ev;
      finishProgress();
      renderPlan(ev);
      log("<b>Done.</b> Every suggestion below cites Qloo results.", "done");
      saveRecent();
      break;
    case "error": log(esc(ev.text), "err"); toast(ev.text, "err"); break;
  }
}

/* --- taste ------------------------------------------------------------- */
function chips(ul, tags, cls = () => "") {
  ul.replaceChildren(...tags.map((t, i) => {
    const li = el("li", { textContent: t.name, title: t.id, className: cls(t) });
    li.style.setProperty("--i", i);
    return li;
  }));
}
function onTaste(ev) {
  $("#gap-card").hidden = false;
  $("#gap-note").textContent = ev.on_qloo
    ? "Tags Qloo links to the places around you. Highlighted: the block likes it, and Qloo doesn't link it to your shop yet. Faded: you already share it."
    : "Tags Qloo links to the places around you. Qloo doesn't list your shop, so there's nothing to compare against: every tag here is open to you.";
  $("#gap-domains").replaceChildren(
    ...ev.domains.map((d) => {
      const missing = new Set((d.missing || []).map((t) => t.id));
      const shared = new Set((d.shared || []).map((t) => t.id));
      const ul = el("ul", { className: "chips" });
      chips(ul, d.block, (t) => (missing.has(t.id) ? "gap-missing" : shared.has(t.id) ? "gap-shared" : ""));
      const box = el("div", {}, el("h3", { textContent: d.label }), ul);
      if (!d.block.length) box.append(el("p", { className: "muted", textContent: "Qloo has no tags here for this block." }));
      else if (ev.on_qloo && d.missing == null) box.append(el("p", { className: "muted", textContent: "Qloo has no tags here for your shop, so no comparison." }));
      return box;
    })
  );
  if (ev.on_qloo) {
    const legend = el("p", { className: "legend" });
    legend.innerHTML = '<span><i class="m"></i>the street likes it, your shop doesn\'t show it yet</span><span><i class="s"></i>already shared</span>';
    $("#gap-domains").append(legend);
  }
  $("#distinctive-box").hidden = !ev.distinctive.length;
  chips($("#distinctive"), ev.distinctive);
}

/* --- the plan ---------------------------------------------------------- */
const AREA_ICON = { playlist: "music", stock: "bag", menu: "cup", event: "cal" };
function resetPlan() {
  $("#summary").textContent = "";
  $("#kpis").replaceChildren();
  $("#suggestions").replaceChildren();
  $("#stats").textContent = "";
}
function countUp(node, to, fmt = (v) => Math.round(v)) {
  if (reduceMotion) { node.textContent = fmt(to); return; }
  const t0 = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - t0) / 900);
    node.textContent = fmt(to * (1 - (1 - k) ** 3));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function renderPlan(ev) {
  const { plan, ledger, stats } = ev;
  $("#plan-card").hidden = false;
  $("#drafting").hidden = true;
  $("#summary").textContent = plan.summary || "";

  const kpis = [
    [plan.suggestions.length, "suggestions, all checked"],
    [stats.qloo_calls, "Qloo calls"],
    [stats.agent_calls, "chosen by the agent"],
    [plan.dropped.length, "cut by the checker"],
  ];
  $("#kpis").replaceChildren(...kpis.map(([n, label], i) => {
    const b = el("b", { textContent: "0" });
    const k = el("div", { className: "kpi" }, b, el("span", { textContent: label }));
    k.style.setProperty("--i", i);
    countUp(b, n);
    return k;
  }));

  const byRef = Object.fromEntries(ledger.map((c) => [c.ref, c]));
  $("#suggestions").replaceChildren(...plan.suggestions.map((s, i) => {
    const card = el("article", { className: "sugg" });
    card.style.setProperty("--i", i);
    card.append(el("div", { className: "area" }, icon(AREA_ICON[s.area] || "spark"), s.area || ""));
    card.append(el("h3", { textContent: s.title || "" }));
    card.append(el("p", { textContent: s.what || "" }));
    if (s.items?.length) {
      const ol = el("ol");
      for (const it of s.items) {
        if (it.kind === "heatmap") {
          const cell = byRef[it.call]?.items.find((c) => c.id === it.id);
          const b = el("button", { type: "button", className: "linkish", textContent: "Hotspot near the shop" });
          b.title = `Qloo heatmap cell ${it.name} (${it.call})`;
          if (cell?.lat != null) b.addEventListener("click", () => showSpot(cell));
          ol.append(el("li", {}, b));
        } else {
          ol.append(el("li", { textContent: it.name, title: `${it.id} · ${it.call} #${it.rank}` }));
        }
      }
      card.append(ol);
    }
    card.append(el("p", { className: "why", textContent: s.why || "" }));
    const foot = el("div", { className: "sugg-foot" });
    foot.append(el("div", { className: "verified" }, icon("check"), `${s.evidence.length} Qloo item${s.evidence.length === 1 ? "" : "s"} behind this`));
    const evd = el("div", { className: "ev" });
    for (const r of [...new Set(s.evidence.map((e) => e.call))]) {
      const c = byRef[r];
      const b = el("button", { type: "button", textContent: `${r} · ${c ? c.purpose : ""}` });
      b.addEventListener("click", () => showCall(r));
      evd.append(b);
    }
    foot.append(evd);
    if (s.checker_notes?.length) foot.append(el("div", { className: "notes", textContent: s.checker_notes.join(" · ") }));
    card.append(foot);
    return card;
  }));

  $("#dropped-n").textContent = plan.dropped.length;
  $("#dropped-box").hidden = !plan.dropped.length;
  $("#dropped").replaceChildren(...plan.dropped.map((d) => el("li", { textContent: `${d.title || "(untitled)"}: ${d.reason}` })));
  $("#calls-n").textContent = ledger.length;
  $("#ledger").replaceChildren(...ledger.map((c) => {
    const d = el("div", { className: "call", id: `call-${c.ref}` });
    d.innerHTML = `<b>${esc(c.ref)}</b> ${esc(c.endpoint)}: ${esc(c.purpose)}${c.probe ? " (probe)" : ""}<br><code>${esc(JSON.stringify(c.params))}</code><br>${c.error ? `<span class="err">${esc(c.error)}</span>` : c.items.slice(0, 12).map((i) => esc(i.name)).join(" · ")}`;
    return d;
  }));
  const cost = stats.llm_cost_usd != null ? ` · LLM cost $${stats.llm_cost_usd.toFixed(4)}` : "";
  const saved = stats.qloo_saved ? ` (${stats.qloo_saved} replayed from saved answers)` : "";
  const llm = Object.keys(MODEL_NAMES).reduce((s, id) => s.replaceAll(id, MODEL_NAMES[id]), stats.llm || "");
  $("#stats").textContent = `${stats.qloo_calls} Qloo calls${saved} · ${llm}${cost}`;
  $("#plan-card").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
}
function showSpot(cell) {
  if (!map) return;
  $("#map").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
  L.circle([cell.lat, cell.lon], { radius: 160, color: "#c2512c", weight: 2, fillOpacity: 0.15 }).addTo(heat);
  map.flyTo([cell.lat, cell.lon], 16, { duration: reduceMotion ? 0 : 1.2 });
}
function showCall(ref) {
  const d = $(`#call-${ref}`);
  if (!d) return;
  $("#ledger-box").open = true;
  $$(".call.hl").forEach((n) => n.classList.remove("hl"));
  d.classList.add("hl");
  d.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
}

/* --- export, share ----------------------------------------------------- */
function planMarkdown() {
  const p = current?.plan?.plan;
  if (!p) return "";
  const shop = current.shop || {};
  const lines = [`# Footfall plan: ${shop.name || "your shop"}`, shop.address || "", `_${new Date(current.when).toLocaleDateString()}_`, "", p.summary || ""];
  for (const s of p.suggestions) {
    lines.push("", `## ${(s.area || "").toUpperCase()}: ${s.title}`, s.what || "");
    for (const it of s.items || []) lines.push(`- ${it.kind === "heatmap" ? "Hotspot near the shop (see the map)" : it.name}`);
    if (s.why) lines.push("", `Why: ${s.why}`);
  }
  lines.push("", "---", "Taste data from Qloo. Agent: NVIDIA Nemotron on Nebius Token Factory. Every item was checked against Qloo's own responses. Made with Footfall.");
  return lines.join("\n");
}
async function copyText(text, ok) {
  try { await navigator.clipboard.writeText(text); toast(ok); } catch { toast("Couldn't reach the clipboard in this browser.", "err"); }
}
$("#copy-plan").addEventListener("click", () => copyText(planMarkdown(), "Plan copied."));
$("#dl-plan").addEventListener("click", () => {
  const md = planMarkdown();
  if (!md) return;
  const name = (current.shop?.name || "shop").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const a = el("a", { href: URL.createObjectURL(new Blob([md], { type: "text/markdown" })), download: `footfall-${name}.md` });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  toast("Plan downloaded.");
});
$("#print-plan").addEventListener("click", () => window.print());
$("#share-plan").addEventListener("click", async () => {
  const url = current?.preset ? `${location.origin}/#demo=${current.preset}` : location.origin;
  const data = { title: "Footfall", text: `What the street around ${current?.shop?.name || "a shop"} likes, from Footfall.`, url };
  if (navigator.share) { try { await navigator.share(data); return; } catch { /* closed or unsupported */ } }
  copyText(url, current?.preset ? "Link to this demo shop copied." : "Link copied.");
});

/* --- recent plans (this browser only) --------------------------------- */
const RECENT = "footfall-recent";
function saveRecent() {
  if (!current?.plan) return;
  const entry = { when: current.when, preset: current.preset, shop: current.shop, located: current.located, taste: current.taste, plan: current.plan };
  const list = [entry, ...store.get(RECENT, []).filter((r) => r.when !== entry.when)].slice(0, 5);
  while (list.length && !store.set(RECENT, list)) list.pop(); // browser storage full: keep fewer
  renderRecent();
}
function renderRecent() {
  const list = store.get(RECENT, []);
  $("#recent").hidden = !list.length;
  $("#recent-list").replaceChildren(...list.map((r) => {
    const b = el("button", { type: "button" },
      el("span", { textContent: r.shop?.name || "A shop" }),
      el("time", { dateTime: r.when, textContent: new Date(r.when).toLocaleDateString(undefined, { day: "numeric", month: "short" }) }));
    b.addEventListener("click", () => openRecent(r));
    return el("li", {}, b);
  }));
}
function openRecent(r) {
  current = { ...r };
  $("#log").replaceChildren();
  log(`Opened your saved plan for <b>${esc(r.shop?.name || "a shop")}</b> from ${esc(new Date(r.when).toLocaleString())}. Nothing was re-run.`, "mode");
  if (r.located) onLocated(r.located);
  if (r.taste) onTaste(r.taste);
  finishProgress();
  renderPlan(r.plan);
}

/* --- start ------------------------------------------------------------- */
initMap();
health();
renderRecent();
$("#shop-form").addEventListener("submit", run);
addEventListener("hashchange", deepLink);
