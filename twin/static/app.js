// Urban Mobility Digital Twin: current state of the zone and "what if" scenarios.

const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 1 });

const TRAFFIC = { bins: [50, 150, 300, 600], colors: ["#f2d98c", "#eeb05a", "#e07b39", "#c24a2a", "#7d1d2a"],
    labels: ["< 50", "50–150", "150–300", "300–600", "> 600"], unit: "vehicles / hour" };
const CO2 = { bins: [0.5, 2, 5, 15], colors: ["#cfe6e0", "#8cc5b8", "#4c9a8f", "#256a6a", "#103c46"],
    labels: ["< 0.5", "0.5–2", "2–5", "5–15", "> 15"], unit: "kg CO₂ / hour" };
const METRICS = [
    ["trips", "Trips in the hour", 0],
    ["mean_trip_minutes", "Mean trip (min)", 2],
    ["mean_delay_minutes", "Mean delay (min)", 2],
    ["total_delay_hours", "Total delay (h)", 1],
    ["vehicle_km", "Vehicle-km", 0],
    ["co2_kg", "CO₂ (kg)", 0],
];

let map, zone, state, edgeLayer, detectorLayer, legend;
let layer = "traffic";
let delta = null;
const closed = new Map(); // edge id -> name
const lines = new Map(); // edge id -> polyline

function notify(message) {
    $("notification-message").textContent = message;
    $("notification").classList.remove("is-hidden");
}
$("notification-close").onclick = () => $("notification").classList.add("is-hidden");

async function api(path, options) {
    const response = await fetch(path, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(body.error || `Error ${response.status}`), { status: response.status });
    return body;
}

const bin = (scale, v) => { let i = 0; while (i < scale.bins.length && v >= scale.bins[i]) i++; return i; };
const streetName = (e) => e.name || "Unnamed street";

function style(id) {
    if (closed.has(id)) return { color: "#111", weight: 4, opacity: 1, dashArray: "6 5" };
    if (layer === "change" && delta) {
        const d = delta[id] || 0;
        if (Math.abs(d) < 10) return { color: "#c9cfcb", weight: 1, opacity: 0.7, dashArray: null };
        return { color: d > 0 ? "#b8323a" : "#2a78d6", weight: Math.min(1.5 + Math.abs(d) / 60, 9), opacity: 0.9, dashArray: null };
    }
    if (layer === "fit") return { color: "#c9cfcb", weight: 1, opacity: 0.8, dashArray: null };
    const scale = layer === "co2" ? CO2 : TRAFFIC;
    const values = layer === "co2" ? state.co2 : state.flows;
    const v = values[id] || 0;
    if (!v) return { color: "#d9ddda", weight: 1, opacity: 0.6, dashArray: null };
    const i = bin(scale, v);
    return { color: scale.colors[i], weight: 1.5 + i * 1.1, opacity: 0.95, dashArray: null };
}

function restyle() {
    for (const [id, line] of lines) line.setStyle(style(id));
    if (layer === "fit") detectorLayer.addTo(map); else detectorLayer.remove();
    drawLegend();
}

function drawLegend() {
    const div = legend.getContainer();
    if (layer === "fit") {
        const ok = state.detectors.filter((d) => d.ok).length;
        div.innerHTML = `<strong>Detectors: simulated vs counted</strong>
            <div><span class="swatch" style="background:#2f7d4f"></span>Within GEH 5 (${ok})</div>
            <div><span class="swatch" style="background:#b8323a"></span>Off (${state.detectors.length - ok})</div>`;
    } else if (layer === "change") {
        div.innerHTML = `<strong>Change in traffic</strong>
            <div><span class="swatch" style="background:#b8323a"></span>More vehicles</div>
            <div><span class="swatch" style="background:#2a78d6"></span>Fewer vehicles</div>
            <div><span class="swatch" style="background:#111"></span>Closed</div>`;
    } else {
        const s = layer === "co2" ? CO2 : TRAFFIC;
        div.innerHTML = `<strong>${s.unit}</strong>` +
            s.labels.map((l, i) => `<div><span class="swatch" style="background:${s.colors[i]}"></span>${l}</div>`).join("");
    }
}

function setLayer(name) {
    layer = name;
    document.querySelectorAll("#layers .button").forEach((b) => {
        const on = b.dataset.layer === name;
        b.classList.toggle("is-selected", on);
        b.classList.toggle("is-dark", on);
    });
    restyle();
}

function renderClosed() {
    const box = $("closed");
    box.innerHTML = "";
    for (const [id, name] of closed) {
        const tag = document.createElement("span");
        tag.className = "tag is-dark";
        tag.textContent = name;
        const x = document.createElement("button");
        x.className = "delete is-small";
        x.type = "button";
        x.setAttribute("aria-label", `Open ${name}`);
        x.onclick = () => toggleClosed(id);
        tag.append(x);
        box.append(tag);
    }
    $("closed-empty").classList.toggle("is-hidden", closed.size > 0);
}

function toggleClosed(id) {
    const e = zone.byId[id];
    if (closed.has(id)) closed.delete(id); else closed.set(id, streetName(e));
    lines.get(id).setStyle(style(id));
    renderClosed();
}

function metricsList(m) {
    return METRICS.map(([k, label, d]) => `<dt>${label}</dt><dd>${m[k] == null ? "–" : fmt.format(+m[k].toFixed(d))}</dd>`).join("");
}

function renderState() {
    const when = new Date(state.observed_at);
    const ok = state.fit.detectors_ok;
    $("fit-badge").textContent = ok == null ? "" : `${Math.round(ok * 100)} % of detectors reproduced`;
    $("fit-badge").className = `tag ${ok >= 0.7 ? "is-success" : "is-warning"} is-light`;
    $("data-summary").textContent = `Counts of ${when.toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" })} ` +
        `from ${state.detectors.length} detectors, read through the data space.`;
    $("metrics-current").innerHTML = metricsList(state.metrics);
    detectorLayer.clearLayers();
    for (const d of state.detectors) {
        L.circleMarker([d.lat, d.lon], { radius: 5, color: "#fff", weight: 1, fillOpacity: 1,
            fillColor: d.ok ? "#2f7d4f" : "#b8323a" })
            .bindTooltip(`Detector ${d.id}<br>counted ${d.observed} veh/h<br>simulated ${d.simulated} veh/h`)
            .addTo(detectorLayer);
    }
    restyle();
}

function renderResult(job) {
    $("result").classList.remove("is-hidden");
    $("result-state").textContent = { queued: "waiting…", running: "simulating…", done: "", failed: "failed" }[job.state];
    if (job.state === "failed") { $("comparison").innerHTML = ""; $("result-note").textContent = job.error; return; }
    if (job.state !== "done") return;
    const r = job.result;
    $("comparison").innerHTML = METRICS.map(([k, label, d]) => {
        const a = r.baseline[k], b = r.scenario[k];
        const pct = a ? ((b - a) / a) * 100 : 0;
        const cls = Math.abs(pct) < 1 ? "" : pct > 0 ? "up" : "down";
        return `<tr><td>${label}</td><td>${fmt.format(+a.toFixed(d))}</td><td>${fmt.format(+b.toFixed(d))}</td>
            <td class="${k === "trips" || k === "vehicle_km" ? "" : cls}">${pct > 0 ? "+" : ""}${fmt.format(pct)} %</td></tr>`;
    }).join("");
    const s = r.scenario;
    const parts = [`${job.closed.length ? `${fmt.format(s.rerouted)} vehicles re-routed` : ""}`,
        s.trips_impossible ? `${fmt.format(s.trips_impossible)} trips impossible (they start or end on a closed street)` : "",
        job.demand !== 1 ? `demand at ${Math.round(job.demand * 100)} %` : ""].filter(Boolean);
    $("result-note").textContent = parts.join(" · ");
    delta = r.delta;
    $("change-button").disabled = false;
    setLayer("change");
}

async function poll(id) {
    for (;;) {
        const job = await api(`api/scenarios/${id}`);
        renderResult(job);
        if (job.state === "done" || job.state === "failed") return;
        await new Promise((ok) => setTimeout(ok, 2000));
    }
}

$("demand").oninput = () => { $("demand-value").textContent = `${$("demand").value} %`; };
document.querySelectorAll("#layers .button").forEach((b) => { b.onclick = () => setLayer(b.dataset.layer); });

$("scenario").onsubmit = async (event) => {
    event.preventDefault();
    const button = $("simulate");
    button.classList.add("is-loading");
    try {
        const job = await api("api/scenarios", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ closed: [...closed.keys()], demand: $("demand").value / 100 }) });
        renderResult(job);
        await poll(job.id);
    } catch (err) {
        notify(err.message);
    } finally {
        button.classList.remove("is-loading");
    }
};

async function loadState() {
    for (;;) {
        try {
            return await api("api/state");
        } catch (err) {
            if (err.status !== 503) throw err;
            $("loading-message").textContent = "The twin is calibrating with the latest counts. This takes about a minute…";
            await new Promise((ok) => setTimeout(ok, 5000));
        }
    }
}

async function start() {
    try {
        zone = await api("api/zone");
        zone.byId = Object.fromEntries(zone.edges.map((e) => [e.id, e]));
        state = await loadState();
    } catch (err) {
        $("loading-message").textContent = `The twin could not load: ${err.message}`;
        return;
    }
    $("loading").classList.add("is-hidden");
    $("dashboard").classList.remove("is-hidden");
    $("zone-name").textContent = `${zone.name}, València`;

    map = L.map("map");
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19, opacity: 0.55,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    const [lon0, lat0, lon1, lat1] = zone.bbox;
    map.fitBounds([[lat0, lon0], [lat1, lon1]]);
    L.rectangle([[lat0, lon0], [lat1, lon1]], { color: "#52514e", weight: 1, fill: false, dashArray: "4 4", interactive: false }).addTo(map);

    edgeLayer = L.layerGroup().addTo(map);
    detectorLayer = L.layerGroup();
    for (const e of zone.edges) {
        const line = L.polyline(e.c, style(e.id)).addTo(edgeLayer);
        line.bindTooltip(() => {
            const f = state.flows[e.id] || 0, d = delta && layer === "change" ? delta[e.id] || 0 : null;
            return `${streetName(e)}<br>${fmt.format(f)} veh/h now` + (d ? `<br>${d > 0 ? "+" : ""}${d} in the scenario` : "") +
                `<br><span class="has-text-grey">Click to ${closed.has(e.id) ? "open" : "close"}</span>`;
        }, { sticky: true });
        line.on("click", () => toggleClosed(e.id));
        lines.set(e.id, line);
    }
    legend = L.control({ position: "bottomleft" });
    legend.onAdd = () => L.DomUtil.create("div", "legend");
    legend.addTo(map);
    renderState();
    setInterval(async () => {
        try { state = await api("api/state"); renderState(); } catch { /* keep the last state */ }
    }, 5 * 60 * 1000);
}

start();
