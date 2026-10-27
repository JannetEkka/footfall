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
    const replay = h.qloo === "replay";
    if (!h.qloo_key && !replay) bits.push("Qloo key not set on the server");
    if (!h.llm_key) bits.push("LLM key not set on the server");
    if (h.budget && h.budget.ok === false) bits.push("spend ledger unavailable");
    $("#health").textContent = bits.length ? bits.join(" · ") : `Agent: ${h.llm}`;
    $("#presets").replaceChildren(
      ...h.presets.map((p) => {
        const b = el("button", { type: "button", textContent: p.title });
        b.addEventListener("click", () => runPlan({ preset: p.id }));
        return b;
      })
    );
    if (replay) {
      $("#replay-note").hidden = false;
      $("#replay-note").textContent = "Qloo's hackathon API key has expired, so your own address can't be read now. The demo shops still work: they replay the Qloo answers saved while the key worked, and Nemotron runs live.";
      $("#go").disabled = true;
    }
  } catch {
    $("#health").textContent = "Server not reachable.";
  }
}

function onMode(ev) {
  const f = $("#shop-form");
  for (const k of ["name", "address", "kind", "sells"]) if (f.elements[k]) f.elements[k].value = ev.shop[k] ?? "";
  const when = ev.recorded ? ` saved on ${esc(ev.recorded)}` : "";
  log(ev.replay
    ? `Demo shop. Replaying ${ev.saved} Qloo answers${when}; Nemotron runs live.`
    : `Demo shop. ${ev.saved} Qloo answers already saved${when}; anything new is fetched live.`, "mode");
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
  const nf = ev.not_found ? ` <span class="why">Qloo didn't recognise ${ev.not_found} of the places sent as signal; they're replaced for the next calls.</span>` : "";
  log(`<span class="ref">${esc(ev.ref)}</span><b>${esc(ev.title)}</b>: ${ev.count} result${ev.count === 1 ? "" : "s"}${names ? ` (${names}${ev.count > 4 ? ", …" : ""})` : ""}${why}${nf}`);
}

function chips(ul, tags, cls = () => "") {
  ul.replaceChildren(...tags.map((t) => el("li", { textContent: t.name, title: t.id, className: cls(t) })));
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
  $("#distinctive-box").hidden = !ev.distinctive.length;
  chips($("#distinctive"), ev.distinctive);
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
  const cost = stats.llm_cost_usd != null ? ` · LLM cost $${stats.llm_cost_usd.toFixed(4)}` : "";
  const saved = stats.qloo_saved ? ` (${stats.qloo_saved} replayed from saved answers)` : "";
  $("#stats").textContent = `${stats.qloo_calls} Qloo calls${saved} (${stats.agent_calls} chosen by the agent) · ${stats.llm}${cost}`;
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

function run(e) {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target).entries());
  body.radius_m = Number(body.radius_m);
  runPlan(body);
}

async function runPlan(body) {
  const buttons = [$("#go"), ...document.querySelectorAll("#presets button")];
  const goWasDisabled = $("#go").disabled;
  buttons.forEach((b) => (b.disabled = true));
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
    buttons.forEach((b) => (b.disabled = false));
    $("#go").disabled = goWasDisabled;
  }
}

function handle(ev) {
  switch (ev.type) {
    case "mode": onMode(ev); break;
    case "step": log(`<b>${esc(ev.text)}</b>`); break;
    case "located": onLocated(ev); log(`Found: ${esc(ev.label)}`); break;
    case "qloo":
      onQloo(ev);
      if (ev.title === "Places around the shop") plotItems(ev.items, "#2f6b4f");
      break;
    case "taste": onTaste(ev); break;
    case "plan": renderPlan(ev); log("<b>Done.</b>"); break;
    case "error": log(esc(ev.text), "err"); break;
  }
}

initMap();
health();
$("#shop-form").addEventListener("submit", run);
