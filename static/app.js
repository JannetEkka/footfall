"use strict";

const $ = (s) => document.querySelector(s);
const el = (tag, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const k of kids) n.append(k);
  return n;
};

let map, layer;
function initMap() {
  if (typeof L === "undefined") return;
  map = L.map("map", { scrollWheelZoom: false }).setView([51.5, -0.12], 3);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap contributors",
  }).addTo(map);
  layer = L.layerGroup().addTo(map);
}

function log(html, cls = "") {
  const li = el("li", { className: cls });
  li.innerHTML = html;
  $("#log").append(li);
  li.scrollIntoView({ block: "nearest" });
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function health() {
  try {
    const h = await (await fetch("/api/health")).json();
    const bits = [];
    if (!h.qloo_key) bits.push("Qloo key not set on the server");
    if (!h.llm_key) bits.push("LLM key not set on the server");
    $("#health").textContent = bits.length ? bits.join(" · ") : `Agent: ${h.llm}`;
  } catch {
    $("#health").textContent = "Server not reachable.";
  }
}

function onLocated(ev) {
  if (!map) return;
  layer.clearLayers();
  map.setView([ev.lat, ev.lon], 15);
  L.circleMarker([ev.lat, ev.lon], { radius: 9, color: "#c2512c", weight: 3, fillOpacity: 0.9 })
    .bindTooltip("Your shop").addTo(layer);
}

function plotItems(items, color) {
  if (!map) return;
  for (const it of items) {
    if (it.lat == null) continue;
    L.circleMarker([it.lat, it.lon], { radius: 5, color, weight: 1, fillOpacity: 0.6 })
      .bindTooltip(esc(it.name)).addTo(layer);
  }
}

function onQloo(ev) {
  const why = ev.why ? ` <span class="why">${esc(ev.why)}</span>` : "";
  if (ev.error) {
    log(`<span class="ref">${esc(ev.ref || "—")}</span>${esc(ev.title)}: ${esc(ev.error)}${why}`, "err");
    return;
  }
  const names = ev.items.slice(0, 4).map((i) => esc(i.name)).join(", ");
  log(`<span class="ref">${esc(ev.ref)}</span><b>${esc(ev.title)}</b>: ${ev.count} result${ev.count === 1 ? "" : "s"}${names ? ` (${names}${ev.count > 4 ? ", …" : ""})` : ""}${why}`);
}

function chips(ul, tags) {
  ul.replaceChildren(...tags.map((t) => el("li", { textContent: t.name, title: t.id })));
}

function renderPlan(ev) {
  const { plan, ledger, stats } = ev;
  $("#plan-card").hidden = false;
  $("#summary").textContent = plan.summary || "";
  const byRef = Object.fromEntries(ledger.map((c) => [c.ref, c]));
  const box = $("#suggestions");
  box.replaceChildren();
  for (const s of plan.suggestions) {
    const card = el("article", { className: "sugg" });
    card.append(el("div", { className: "area", textContent: s.area || "" }));
    card.append(el("h3", { textContent: s.title || "" }));
    card.append(el("p", { textContent: s.what || "" }));
    if (s.items?.length) {
      const ol = el("ol");
      for (const it of s.items) ol.append(el("li", { textContent: it.name, title: `${it.id} · ${it.call} #${it.rank}` }));
      card.append(ol);
    }
    card.append(el("p", { className: "why", textContent: s.why || "" }));
    const evd = el("div", { className: "ev" });
    const refs = [...new Set(s.evidence.map((e) => e.call))];
    for (const r of refs) {
      const c = byRef[r];
      const b = el("button", { type: "button", textContent: `${r} · ${c ? c.purpose : ""}` });
      b.addEventListener("click", () => showCall(r));
      evd.append(b);
    }
    card.append(el("div", { className: "verified", textContent: `✓ ${s.evidence.length} Qloo item${s.evidence.length === 1 ? "" : "s"} behind this` }));
    card.append(evd);
    if (s.checker_notes?.length) card.append(el("div", { className: "notes", textContent: s.checker_notes.join(" · ") }));
    box.append(card);
  }
  $("#dropped-n").textContent = plan.dropped.length;
  $("#dropped-box").hidden = !plan.dropped.length;
  $("#dropped").replaceChildren(...plan.dropped.map((d) => el("li", { textContent: `${d.title || "(untitled)"}: ${d.reason}` })));
  $("#calls-n").textContent = ledger.length;
  $("#ledger").replaceChildren(
    ...ledger.map((c) => {
      const d = el("div", { className: "call", id: `call-${c.ref}` });
      d.innerHTML = `<b>${esc(c.ref)}</b> ${esc(c.endpoint)}: ${esc(c.purpose)}${c.probe ? " (probe)" : ""}<br><code>${esc(JSON.stringify(c.params))}</code><br>${c.error ? `<span class="err">${esc(c.error)}</span>` : c.items.slice(0, 12).map((i) => esc(i.name)).join(" · ")}`;
      return d;
    })
  );
  $("#stats").textContent = `${stats.qloo_calls} Qloo calls (${stats.agent_calls} chosen by the agent) · ${stats.llm}`;
  $("#plan-card").scrollIntoView({ behavior: "smooth" });
}

function showCall(ref) {
  const d = $(`#call-${ref}`);
  if (!d) return;
  d.closest("details").open = true;
  document.querySelectorAll(".call.hl").forEach((n) => n.classList.remove("hl"));
  d.classList.add("hl");
  d.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function run(e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = Object.fromEntries(fd.entries());
  body.radius_m = Number(body.radius_m);
  $("#go").disabled = true;
  $("#log").replaceChildren();
  $("#plan-card").hidden = true;
  $("#gap-card").hidden = true;
  try {
    const r = await fetch("/api/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      log(esc(j.error || j.detail?.[0]?.msg || `Server error ${r.status}`), "err");
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
  } finally {
    $("#go").disabled = false;
  }
}

function handle(ev) {
  switch (ev.type) {
    case "step": log(`<b>${esc(ev.text)}</b>`); break;
    case "located": onLocated(ev); log(`Found: ${esc(ev.label)}`); break;
    case "qloo":
      onQloo(ev);
      if (ev.title === "Places around the shop") plotItems(ev.items, "#2f6b4f");
      break;
    case "gap":
      $("#gap-card").hidden = false;
      chips($("#gap-missing"), ev.block_not_shop);
      chips($("#gap-shared"), ev.shared);
      break;
    case "plan": renderPlan(ev); log("<b>Done.</b>"); break;
    case "error": log(esc(ev.text), "err"); break;
  }
}

initMap();
health();
$("#shop-form").addEventListener("submit", run);
