/**
 * ClinSight demo UI
 *
 * Flow: collect form → POST /api/v1/query → render by visualization.type
 *   - Chart.js: time_series, bar/pie/histogram/scatter/grouped/stacked
 *   - SVG: network_graph (bipartite layout)
 * Click a mark to focus that datum's deep citations (nct_id + excerpt).
 */

// --- Example chips: one-click demos for graders ---
const EXAMPLES = [
  {
    name: "Trials over time",
    blurb: "Pembrolizumab · yearly trend",
    request: {
      query: "How has the number of trials for this drug changed over time?",
      drug_name: "Pembrolizumab",
    },
  },
  {
    name: "Phase distribution",
    blurb: "NSCLC · by phase",
    request: {
      query: "Show me the distribution of trials by phase for this condition",
      condition: "Non-small Cell Lung Cancer",
    },
  },
  {
    name: "Status breakdown",
    blurb: "COVID-19 · share of statuses",
    request: {
      query: "What proportion of COVID-19 trials are in each status?",
      condition: "COVID-19",
    },
  },
  {
    name: "Sponsor landscape",
    blurb: "Diabetes · top sponsors",
    request: {
      query: "Which sponsors have the most clinical trials for diabetes?",
      condition: "Diabetes",
    },
  },
  {
    name: "Drug A vs Drug B",
    blurb: "Phases · Pembrolizumab vs Nivolumab",
    request: {
      query: "Compare phases for Pembrolizumab vs Nivolumab",
    },
  },
  {
    name: "Drug–sponsor network",
    blurb: "Diabetes · relationships",
    request: {
      query: "Show a network of relationships between drugs and sponsors for diabetes trials",
      condition: "Diabetes",
    },
  },
];
// --- Form field ids mirrored into the POST /api/v1/query body ---
const FIELDS = [
  "query",
  "drug_name",
  "condition",
  "trial_phase",
  "sponsor",
  "country",
  "status",
  "start_year",
  "end_year",
  "max_studies",
];

// --- Chart color palette (clinical green family) ---
const PALETTE = ["#0f6b5c", "#1f4e79", "#b45309", "#5b4b8a", "#0e7490", "#3f6212", "#9f1239", "#365314"];

let chartInstance = null;
let latestVizData = [];

function $(id) {
  return document.getElementById(id);
}

function setVisible(el, visible) {
  el.classList.toggle("hidden", !visible);
}

// --- Populate the form from an example (or clear) ---
function fillForm(request) {
  for (const key of FIELDS) {
    const el = $(key);
    if (!el) continue;
    el.value = request[key] ?? "";
  }
  const panel = $("filters-panel");
  if (panel) {
    const hasFilters = FIELDS.some((k) => k !== "query" && request[k]);
    panel.open = Boolean(hasFilters);
  }
}

// --- Read form → JSON body for POST /api/v1/query ---
function readPayload() {
  const payload = {};
  for (const key of FIELDS) {
    const el = $(key);
    if (!el) continue;
    const raw = el.value.trim();
    if (!raw) continue;
    if (key === "start_year" || key === "end_year" || key === "max_studies") {
      payload[key] = Number(raw);
    } else {
      payload[key] = raw;
    }
  }
  return payload;
}

function renderExamples() {
  const list = $("example-list");
  list.innerHTML = "";
  for (const ex of EXAMPLES) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "example-btn";
    btn.innerHTML = `<strong>${ex.name}</strong><span>${ex.blurb || ""}</span>`;
    btn.addEventListener("click", () => {
      fillForm(ex.request);
      runQuery();
    });
    list.appendChild(btn);
  }
}

function showState({ empty = false, loading = false, error = null, viz = false }) {
  setVisible($("empty-state"), empty);
  setVisible($("loading-state"), loading);
  setVisible($("error-state"), Boolean(error));
  setVisible($("viz-panel"), viz);
  if (error) {
    $("error-state").textContent = error;
  }
}

function fieldFromEncoding(encoding, channel, fallback) {
  const mapped = encoding?.[channel]?.field;
  return mapped || fallback;
}

function pointValue(point, field, fallbackKeys) {
  if (field && point[field] != null) return point[field];
  for (const key of fallbackKeys) {
    if (point[key] != null) return point[key];
  }
  return null;
}

function colorFor(index) {
  return PALETTE[index % PALETTE.length];
}

function truncateLabel(text, max = 28) {
  const s = String(text || "");
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

function parseEntity(raw) {
  const text = String(raw || "");
  const m = text.match(/^(Drug|Sponsor|Condition|Site|Investigator):\s*(.+)$/i);
  if (m) {
    return { type: m[1], name: m[2].trim(), full: text };
  }
  return { type: "Entity", name: text, full: text };
}

// --- Pie helper: collapse tiny slices into "Other" ---
function preparePieSlices(viz, encoding) {
  const xField = fieldFromEncoding(encoding, "x", "label");
  const yField = fieldFromEncoding(encoding, "y", "value");
  const rows = viz.data
    .map((p) => ({
      label: String(pointValue(p, xField, ["label", "x", "status", "phase"]) ?? "Unknown"),
      value: Number(pointValue(p, yField, ["value", "y", "trial_count"]) ?? 0),
      point: p,
    }))
    .filter((r) => r.value > 0)
    .sort((a, b) => b.value - a.value);

  const total = rows.reduce((sum, r) => sum + r.value, 0) || 1;
  const major = [];
  let otherValue = 0;
  let otherPoints = [];
  for (const row of rows) {
    const share = row.value / total;
    if (share < 0.03 && rows.length > 5) {
      otherValue += row.value;
      otherPoints.push(row.point);
    } else {
      major.push(row);
    }
  }
  if (otherValue > 0) {
    major.push({
      label: "Other",
      value: otherValue,
      point: { citations: otherPoints.flatMap((p) => p.citations || []).slice(0, 5) },
    });
  }
  return { rows: major, total: major.reduce((s, r) => s + r.value, 0) || 1 };
}

// --- Chart.js configs per viz type ---
function buildPieConfig(viz, encoding) {
  const { rows, total } = preparePieSlices(viz, encoding);
  const labels = rows.map((r) => r.label);
  const values = rows.map((r) => r.value);

  return {
    type: "doughnut",
    data: {
      labels,
      datasets: [
        {
          data: values,
          backgroundColor: labels.map((_, i) => colorFor(i)),
          borderColor: "#fff",
          borderWidth: 2,
          hoverOffset: 6,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "58%",
      layout: { padding: { top: 8, right: 8, bottom: 8, left: 8 } },
      animation: { duration: 650, easing: "easeOutQuart" },
      plugins: {
        legend: {
          position: window.innerWidth < 720 ? "bottom" : "right",
          align: "center",
          labels: {
            boxWidth: 12,
            boxHeight: 12,
            padding: 14,
            font: { size: 12, family: "'IBM Plex Sans', sans-serif" },
            generateLabels(chart) {
              const data = chart.data;
              const ds = data.datasets[0];
              return data.labels.map((label, i) => {
                const value = Number(ds.data[i] || 0);
                const pct = ((value / total) * 100).toFixed(1);
                return {
                  text: `${label}  ${value.toLocaleString()} (${pct}%)`,
                  fillStyle: Array.isArray(ds.backgroundColor) ? ds.backgroundColor[i] : ds.backgroundColor,
                  strokeStyle: "#fff",
                  lineWidth: 1,
                  hidden: false,
                  index: i,
                };
              });
            },
          },
        },
        tooltip: {
          callbacks: {
            label(ctx) {
              const value = Number(ctx.raw || 0);
              const pct = ((value / total) * 100).toFixed(1);
              return ` ${ctx.label}: ${value.toLocaleString()} trials (${pct}%)`;
            },
          },
        },
      },
    },
  };
}

function buildGroupedConfig(viz, encoding) {
  const xField = fieldFromEncoding(encoding, "x", "phase");
  const yField = fieldFromEncoding(encoding, "y", "trial_count");
  const seriesField = fieldFromEncoding(encoding, "color", "status") || "series";
  const categories = [...new Set(viz.data.map((p) => String(pointValue(p, xField, ["label", "x"]) ?? "")))];
  const seriesNames = [...new Set(viz.data.map((p) => String(pointValue(p, seriesField, ["series", "status", "drug"]) ?? "Series")))];
  const datasets = seriesNames.map((name, idx) => ({
    label: name,
    data: categories.map((cat) => {
      const match = viz.data.find(
        (p) =>
          String(pointValue(p, xField, ["label", "x"]) ?? "") === cat &&
          String(pointValue(p, seriesField, ["series", "status", "drug"]) ?? "Series") === name,
      );
      return match ? Number(pointValue(match, yField, ["value", "y"]) ?? 0) : 0;
    }),
    backgroundColor: colorFor(idx),
    borderRadius: 4,
    stack: viz.type === "stacked_bar_chart" ? "stack" : undefined,
  }));

  return {
    type: "bar",
    data: { labels: categories, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 650, easing: "easeOutQuart" },
      plugins: { legend: { display: true, position: "bottom" } },
      scales: {
        x: { stacked: viz.type === "stacked_bar_chart", grid: { display: false }, title: { display: true, text: xField } },
        y: { stacked: viz.type === "stacked_bar_chart", beginAtZero: true, title: { display: true, text: yField } },
      },
    },
  };
}

function buildScatterConfig(viz, encoding) {
  const xField = fieldFromEncoding(encoding, "x", "year");
  const yField = fieldFromEncoding(encoding, "y", "enrollment");
  const seriesField = fieldFromEncoding(encoding, "color", "phase");
  const groups = new Map();
  for (const p of viz.data) {
    const series = String(pointValue(p, seriesField, ["series", "phase"]) ?? "All");
    if (!groups.has(series)) groups.set(series, []);
    groups.get(series).push({
      x: Number(pointValue(p, xField, ["x", "year"]) ?? 0),
      y: Number(pointValue(p, yField, ["y", "enrollment", "value"]) ?? 0),
      label: p.label,
    });
  }
  const datasets = [...groups.entries()].map(([name, points], idx) => ({
    label: name,
    data: points,
    backgroundColor: colorFor(idx),
    borderColor: colorFor(idx),
    pointRadius: 5,
  }));
  return {
    type: "scatter",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 650 },
      plugins: {
        legend: { display: true, position: "bottom" },
        tooltip: {
          callbacks: {
            label(ctx) {
              const raw = ctx.raw || {};
              return `${raw.label || ctx.dataset.label}: (${raw.x}, ${raw.y})`;
            },
          },
        },
      },
      scales: {
        x: { title: { display: true, text: xField }, ticks: { precision: 0 } },
        y: { beginAtZero: true, title: { display: true, text: yField } },
      },
    },
  };
}

// --- Pick Chart.js config from visualization.type ---
function buildChartConfig(response) {
  const { visualization: viz } = response;
  const encoding = viz.encoding || {};
  const type = viz.type;

  if (type === "grouped_bar_chart" || type === "stacked_bar_chart") {
    return buildGroupedConfig(viz, encoding);
  }
  if (type === "scatter_plot") {
    return buildScatterConfig(viz, encoding);
  }
  if (type === "pie_chart") {
    return buildPieConfig(viz, encoding);
  }

  const xField = fieldFromEncoding(encoding, "x", "label");
  const yField = fieldFromEncoding(encoding, "y", "value");
  const labels = viz.data.map((p) => String(pointValue(p, xField, ["label", "x", "enrollment_bin"]) ?? ""));
  const values = viz.data.map((p) => Number(pointValue(p, yField, ["value", "y", "trial_count"]) ?? 0));
  const isTime = type === "time_series";
  const isHist = type === "histogram";
  const categoricalField = ["country", "drug", "sponsor", "condition", "phase", "status"].includes(
    String(xField || "").toLowerCase(),
  );
  // Prefer horizontal bars so every category name stays readable (no alternating x ticks).
  const horizontal = !isTime && !isHist && (categoricalField || labels.length >= 5);

  return {
    type: isTime ? "line" : "bar",
    data: {
      labels,
      datasets: [
        {
          label: yField || "value",
          data: values,
          backgroundColor: isTime
            ? "rgba(15, 107, 92, 0.18)"
            : labels.map((_, i) => colorFor(i)),
          borderColor: isTime ? PALETTE[0] : labels.map((_, i) => colorFor(i)),
          borderWidth: isTime ? 2.5 : 0,
          fill: isTime,
          tension: 0.25,
          pointRadius: isTime ? 3 : 0,
          borderRadius: isTime ? 0 : isHist ? 0 : 5,
          barPercentage: isHist ? 1.0 : 0.8,
          categoryPercentage: isHist ? 1.0 : 0.78,
        },
      ],
    },
    options: {
      indexAxis: horizontal ? "y" : "x",
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 650, easing: "easeOutQuart" },
      layout: {
        padding: horizontal
          ? { left: 8, right: 16, top: 6, bottom: 6 }
          : { left: 4, right: 8, top: 4, bottom: 8 },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title(items) {
              const idx = items[0]?.dataIndex;
              return labels[idx] ?? "";
            },
            afterBody(items) {
              const idx = items[0]?.dataIndex;
              const point = viz.data[idx];
              const n = point?.citations?.length || 0;
              return n ? `${n} citation(s)` : "";
            },
          },
        },
      },
      scales: horizontal
        ? {
            x: {
              beginAtZero: true,
              title: { display: true, text: yField || "count" },
              ticks: { precision: 0 },
              grid: { color: "rgba(20, 32, 28, 0.06)" },
            },
            y: {
              title: { display: false },
              offset: true,
              ticks: {
                autoSkip: false,
                padding: 6,
                font: { size: 12, family: "'IBM Plex Sans', sans-serif" },
              },
              grid: { display: false },
              afterFit(scale) {
                // Reserve room for full country / category names.
                const longest = labels.reduce((n, l) => Math.max(n, String(l).length), 0);
                scale.width = Math.max(scale.width, Math.min(220, 8 * longest + 16));
              },
            },
          }
        : {
            x: {
              title: { display: true, text: xField || "category" },
              ticks: {
                autoSkip: false,
                maxRotation: isTime ? 45 : 0,
                minRotation: isTime ? 0 : 0,
                font: { size: 11 },
              },
              grid: { display: false },
            },
            y: {
              beginAtZero: true,
              title: { display: true, text: yField || "count" },
              ticks: { precision: 0 },
              grid: { color: "rgba(20, 32, 28, 0.06)" },
            },
          },
    },
  };
}

function svgEl(name, attrs = {}) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [k, v] of Object.entries(attrs)) {
    el.setAttribute(k, String(v));
  }
  return el;
}

// --- SVG bipartite network: edges = relationships, click → citations ---
function renderNetwork(viz) {
  const svg = $("network");
  const canvas = $("chart");
  const caption = $("network-caption");
  const wrap = $("chart-wrap");
  setVisible(canvas, false);
  setVisible(svg, true);
  setVisible(caption, true);
  wrap.classList.add("is-network");
  wrap.classList.remove("is-pie", "is-hbar");
  wrap.style.height = "";
  svg.innerHTML = "";

  // Keep the densest relationships only so labels stay readable.
  const ranked = [...viz.data]
    .map((p) => ({
      source: p.source || String(p.x || ""),
      target: p.target || String(p.y || ""),
      weight: Number(p.edge_weight ?? p.value ?? 1),
      label: p.label,
    }))
    .filter((e) => e.source && e.target && e.weight > 0)
    .sort((a, b) => b.weight - a.weight)
    .slice(0, 20);

  const leftMap = new Map();
  const rightMap = new Map();
  for (const e of ranked) {
    const s = parseEntity(e.source);
    const t = parseEntity(e.target);
    if (!leftMap.has(e.source)) leftMap.set(e.source, { id: e.source, ...s, weight: 0 });
    if (!rightMap.has(e.target)) rightMap.set(e.target, { id: e.target, ...t, weight: 0 });
    leftMap.get(e.source).weight += e.weight;
    rightMap.get(e.target).weight += e.weight;
  }

  const leftNodes = [...leftMap.values()].sort((a, b) => b.weight - a.weight);
  const rightNodes = [...rightMap.values()].sort((a, b) => b.weight - a.weight);

  const width = Math.max(svg.clientWidth || wrap.clientWidth || 640, 520);
  const height = Math.max(svg.clientHeight || 480, Math.max(leftNodes.length, rightNodes.length) * 36 + 72);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.style.height = `${height}px`;

  const leftX = 190;
  const rightX = width - 190;
  const topPad = 42;
  const bottomPad = 24;

  const placeColumn = (nodes, x) => {
    const span = Math.max(height - topPad - bottomPad, 1);
    nodes.forEach((n, i) => {
      n.x = x;
      n.y = topPad + ((i + 0.5) / nodes.length) * span;
    });
  };
  placeColumn(leftNodes, leftX);
  placeColumn(rightNodes, rightX);

  const leftTitle = leftNodes[0]?.type ? `${leftNodes[0].type}s` : "Sources";
  const rightTitle = rightNodes[0]?.type ? `${rightNodes[0].type}s` : "Targets";
  svg.appendChild(svgEl("text", { class: "col-title", x: leftX, y: 22, "text-anchor": "end" })).textContent = leftTitle;
  svg.appendChild(svgEl("text", { class: "col-title", x: rightX, y: 22, "text-anchor": "start" })).textContent = rightTitle;

  const maxW = Math.max(...ranked.map((e) => e.weight), 1);
  const leftIndex = new Map(leftNodes.map((n) => [n.id, n]));
  const rightIndex = new Map(rightNodes.map((n) => [n.id, n]));

  const edgeSelector = (source, target) => {
    const key = `${source}||${target}`;
    return [...svg.querySelectorAll("path[data-edge]")].filter(
      (el) => el.getAttribute("data-edge") === key,
    );
  };

  const clearEdgeSelection = () => {
    svg.querySelectorAll(".edge.is-selected, .edge-hit.is-selected").forEach((el) => {
      el.classList.remove("is-selected");
    });
  };

  const focusEdgeCitations = (source, target) => {
    clearEdgeSelection();
    const point = (viz.data || []).find((p) => p.source === source && p.target === target);
    edgeSelector(source, target).forEach((el) => el.classList.add("is-selected"));
    renderCitations(viz.data || [], point || { source, target, label: `${source} → ${target}` });
    $("citations-block").open = true;
    $("citations-block").scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  const focusNodeCitations = (nodeId) => {
    clearEdgeSelection();
    const related = (viz.data || []).filter((p) => p.source === nodeId || p.target === nodeId);
    related.forEach((p) => {
      edgeSelector(p.source, p.target).forEach((el) => el.classList.add("is-selected"));
    });
    // Render only this node's edges in the citations panel.
    const list = $("citations-list");
    list.innerHTML = "";
    let shownCites = 0;
    for (const point of related) {
      const cites = point.citations || [];
      if (!cites.length) continue;
      const group = document.createElement("div");
      group.className = "cite-group";
      const heading = document.createElement("div");
      heading.className = "cite-group-head";
      const bucket = point.label || `${point.source} → ${point.target}`;
      const contributing = point.contributing_count ?? point.value ?? cites.length;
      heading.innerHTML = `<strong>${bucket}</strong><span>${cites.length} cited · ${contributing} contributing</span>`;
      group.appendChild(heading);
      for (const cite of cites) {
        shownCites += 1;
        const div = document.createElement("div");
        div.className = "cite";
        div.innerHTML = `
          <a href="${cite.url || `https://clinicaltrials.gov/study/${cite.nct_id}`}" target="_blank" rel="noopener noreferrer">${cite.nct_id}</a>
          <p>${cite.excerpt || ""}</p>
        `;
        group.appendChild(div);
      }
      list.appendChild(group);
    }
    const summary = $("citations-summary");
    if (summary) {
      summary.textContent = `Showing citations for “${parseEntity(nodeId).name}” (${related.length} edges · ${shownCites} excerpts). Click empty chart area to show all.`;
    }
    $("citations-block").open = true;
    if (!shownCites) {
      list.innerHTML = "<p class='notes'>No citations returned for this node.</p>";
    }
    $("citations-block").scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  svg.addEventListener("click", (ev) => {
    if (ev.target === svg) {
      clearEdgeSelection();
      renderCitations(viz.data || []);
    }
  });

  for (const e of ranked) {
    const s = leftIndex.get(e.source);
    const t = rightIndex.get(e.target);
    if (!s || !t) continue;
    const midX = (s.x + t.x) / 2;
    const d = `M ${s.x} ${s.y} C ${midX} ${s.y}, ${midX} ${t.y}, ${t.x} ${t.y}`;
    const edgeKey = `${e.source}||${e.target}`;
    const point = (viz.data || []).find((p) => p.source === e.source && p.target === e.target);
    const citeN = point?.citations?.length || 0;

    // Wide invisible stroke so thin edges are easy to click.
    const hit = svgEl("path", {
      class: "edge-hit",
      d,
      "data-edge": edgeKey,
      "stroke-width": "14",
    });
    hit.style.cursor = "pointer";
    hit.addEventListener("click", (ev) => {
      ev.stopPropagation();
      focusEdgeCitations(e.source, e.target);
    });

    const path = svgEl("path", {
      class: `edge${e.weight >= maxW * 0.75 ? " is-strong" : ""}`,
      d,
      "data-edge": edgeKey,
      "stroke-width": (1.4 + (4.5 * e.weight) / maxW).toFixed(2),
    });
    path.style.pointerEvents = "none";
    const tip = `${parseEntity(e.source).name} → ${parseEntity(e.target).name}: ${e.weight} trials` +
      (citeN ? ` · ${citeN} citation(s) — click to view` : " — click for details");
    hit.appendChild(svgEl("title")).textContent = tip;
    svg.appendChild(hit);
    svg.appendChild(path);
  }

  const drawNode = (n, side) => {
    const g = svgEl("g");
    g.style.cursor = "pointer";
    const r = 5.5 + Math.min(7, Math.sqrt(n.weight) * 1.4);
    const color =
      n.type === "Drug" ? PALETTE[0] :
      n.type === "Condition" ? PALETTE[2] :
      n.type === "Site" ? PALETTE[4] :
      n.type === "Investigator" ? PALETTE[3] :
      PALETTE[1];
    g.appendChild(svgEl("circle", { cx: n.x, cy: n.y, r, fill: color }));
    const label = svgEl("text", {
      class: "node-label",
      x: side === "left" ? n.x - 12 : n.x + 12,
      y: n.y + 4,
      "text-anchor": side === "left" ? "end" : "start",
    });
    label.textContent = truncateLabel(n.name, side === "left" ? 26 : 28);
    label.appendChild(svgEl("title")).textContent = `${n.full} (${n.weight} linked trials) — click for NCT citations`;
    g.appendChild(label);
    g.addEventListener("click", (ev) => {
      ev.stopPropagation();
      focusNodeCitations(n.id);
    });
    svg.appendChild(g);
  };

  leftNodes.forEach((n) => drawNode(n, "left"));
  rightNodes.forEach((n) => drawNode(n, "right"));

  caption.textContent =
    `Showing top ${ranked.length} relationships by shared trial count. ` +
    `Line thickness = linked trials. Click a line or node to see NCT citations below.`;
}

// --- Meta chips under the chart (source, grouping, truncation, filters) ---
function renderMeta(meta) {
  const row = $("meta-row");
  row.innerHTML = "";
  const chips = [];
  if (meta.source) chips.push(`source: ${meta.source}`);
  if (meta.grouping) chips.push(`grouping: ${meta.grouping}`);
  if (meta.time_granularity) chips.push(`granularity: ${meta.time_granularity}`);
  if (meta.units) chips.push(`units: ${meta.units}`);
  if (meta.total_records != null) chips.push(`used: ${meta.total_records.toLocaleString()}`);
  if (meta.total_available != null) chips.push(`available: ${meta.total_available.toLocaleString()}`);
  if (meta.truncated) chips.push("truncated fetch");
  if (meta.filters && Object.keys(meta.filters).length) {
    for (const [k, v] of Object.entries(meta.filters)) {
      chips.push(`${k}: ${v}`);
    }
  }
  for (const text of chips) {
    const span = document.createElement("span");
    span.className = "chip";
    span.textContent = text;
    row.appendChild(span);
  }

  const notes = $("viz-notes");
  if (meta.notes) {
    notes.textContent = meta.notes;
    notes.classList.remove("hidden");
  } else {
    notes.textContent = "";
    notes.classList.add("hidden");
  }
}

// --- Deep citations panel (all marks, or focused mark after click) ---
function renderCitations(data, focusPoint = null) {
  const list = $("citations-list");
  list.innerHTML = "";
  let shownPoints = 0;
  let shownCites = 0;

  const points = focusPoint
    ? data.filter((p) => {
        if (focusPoint.source || focusPoint.target) {
          return p.source === focusPoint.source && p.target === focusPoint.target;
        }
        if (focusPoint.series || focusPoint.status) {
          return (
            String(p.label ?? p.x ?? "") === String(focusPoint.label ?? focusPoint.x ?? "") &&
            String(p.series ?? p.status ?? "") === String(focusPoint.series ?? focusPoint.status ?? "")
          );
        }
        return String(p.label ?? p.x ?? "") === String(focusPoint.label ?? focusPoint.x ?? focusPoint);
      })
    : data;

  for (const point of points) {
    const cites = point.citations || [];
    if (!cites.length) continue;
    shownPoints += 1;

    const group = document.createElement("div");
    group.className = "cite-group";

    const heading = document.createElement("div");
    heading.className = "cite-group-head";
    const bucket =
      point.label ||
      (point.source && point.target ? `${point.source} → ${point.target}` : null) ||
      [point.x, point.series ?? point.status].filter(Boolean).join(" · ") ||
      "Datum";
    const contributing = point.contributing_count ?? point.value ?? cites.length;
    heading.innerHTML = `<strong>${bucket}</strong><span>${cites.length} cited · ${contributing} contributing</span>`;
    group.appendChild(heading);

    for (const cite of cites) {
      shownCites += 1;
      const div = document.createElement("div");
      div.className = "cite";
      div.innerHTML = `
        <a href="${cite.url || `https://clinicaltrials.gov/study/${cite.nct_id}`}" target="_blank" rel="noopener noreferrer">${cite.nct_id}</a>
        <p>${cite.excerpt || ""}</p>
      `;
      group.appendChild(div);
    }
    list.appendChild(group);
  }

  const summary = $("citations-summary");
  if (summary) {
    const focusName = focusPoint
      ? (focusPoint.label ?? focusPoint.x ?? "selection")
      : null;
    summary.textContent = focusName
      ? `Showing citations for “${focusName}” (${shownCites} excerpts). Click empty chart area to show all.`
      : `${shownPoints} data points · ${shownCites} citation excerpts from ClinicalTrials.gov`;
  }

  $("citations-block").open = shownCites > 0;
  if (!shownCites) {
    list.innerHTML = "<p class='notes'>No citations returned for this response.</p>";
  }
}

// --- Chart.js click → focus citations for that bar/slice ---
function attachCitationClick(chart, viz) {
  chart.options.onClick = (_event, elements) => {
    if (!elements.length) {
      renderCitations(viz.data || []);
      return;
    }
    const el = elements[0];
    const idx = el.index;
    const datasetIndex = el.datasetIndex;
    let point = viz.data[idx];

    // Grouped/stacked charts: map category + series back to the datum.
    if (viz.type === "grouped_bar_chart" || viz.type === "stacked_bar_chart") {
      const category = chart.data.labels?.[idx];
      const series = chart.data.datasets?.[datasetIndex]?.label;
      point = (viz.data || []).find((p) => {
        const x = String(p.phase ?? p.x ?? p.label ?? "");
        const s = String(p.status ?? p.drug ?? p.series ?? "");
        return x === String(category) && s === String(series);
      });
    }

    if (!point) {
      renderCitations(viz.data || []);
      return;
    }
    renderCitations(viz.data || [], point);
    $("citations-block").open = true;
    $("citations-block").scrollIntoView({ behavior: "smooth", block: "nearest" });
  };
}

function destroyChart() {
  if (chartInstance) {
    chartInstance.destroy();
    chartInstance = null;
  }
  const canvas = $("chart");
  if (canvas) {
    const ctx = canvas.getContext("2d");
    if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
    // Reset canvas size so Chart.js does not inherit stale layout from prior chart type.
    canvas.width = canvas.width;
  }
}

// --- Orchestrate: title + meta + citations + chart or network ---
function renderResponse(response) {
  const viz = response.visualization;
  const wrap = $("chart-wrap");
  latestVizData = viz.data || [];
  $("viz-title").textContent = viz.title || "Visualization";
  $("viz-type").textContent = (viz.type || "chart").replaceAll("_", " ");
  renderMeta(response.meta || {});
  renderCitations(latestVizData);
  $("raw-json").textContent = JSON.stringify(response, null, 2);

  destroyChart();

  wrap.classList.remove("is-pie", "is-network", "is-hbar");
  wrap.style.height = "";

  if (viz.type === "network_graph") {
    setVisible($("chart"), false);
    renderNetwork(viz);
  } else {
    setVisible($("network"), false);
    setVisible($("network-caption"), false);
    setVisible($("chart"), true);
    $("network").innerHTML = "";
    if (viz.type === "pie_chart") wrap.classList.add("is-pie");
    const config = buildChartConfig(response);
    // Avoid animation carry-over when switching filter results (e.g. USA → China).
    if (config?.options) {
      config.options.animation = false;
    }
    if (config?.options?.indexAxis === "y") {
      wrap.classList.add("is-hbar");
      const n = (viz.data || []).length || 8;
      wrap.style.height = `${Math.min(720, Math.max(420, n * 34 + 48))}px`;
    } else {
      wrap.style.height = "";
    }
    chartInstance = new Chart($("chart"), config);
    attachCitationClick(chartInstance, viz);
  }
  showState({ viz: true });
}

// --- Submit button: call API, render or show error ---
async function runQuery() {
  const payload = readPayload();
  if (!payload.query) {
    showState({ error: "Query is required." });
    return;
  }

  const btn = $("run-btn");
  btn.disabled = true;
  showState({ loading: true });

  try {
    const res = await fetch("/api/v1/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = body.detail;
      const formatMsg = (d) => {
        if (typeof d === "string") return d;
        const raw = d.msg || JSON.stringify(d);
        return String(raw).replace(/^Value error,\s*/i, "");
      };
      const message = typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map(formatMsg).join("; ")
          : `Request failed (${res.status})`;
      throw new Error(message);
    }
    renderResponse(body);
  } catch (err) {
    showState({ error: err.message || "Something went wrong." });
  } finally {
    btn.disabled = false;
  }
}

function clearForm() {
  for (const key of FIELDS) {
    const el = $(key);
    if (el) el.value = "";
  }
  destroyChart();
  latestVizData = [];
  $("network").innerHTML = "";
  setVisible($("network"), false);
  setVisible($("network-caption"), false);
  setVisible($("chart"), true);
  $("chart-wrap").classList.remove("is-pie", "is-network", "is-hbar");
  $("chart-wrap").style.height = "";
  showState({ empty: true });
}

async function copyRawJson() {
  const btn = $("copy-json-btn");
  const text = $("raw-json")?.textContent || "";
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
  }
  const prev = btn.textContent;
  btn.textContent = "Copied";
  btn.classList.add("is-copied");
  setTimeout(() => {
    btn.textContent = prev;
    btn.classList.remove("is-copied");
  }, 1400);
}

document.addEventListener("DOMContentLoaded", () => {
  // Wire example chips, form submit, clear, and copy-JSON once the DOM is ready.
  renderExamples();
  $("query-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runQuery();
  });
  $("clear-btn").addEventListener("click", clearForm);
  $("copy-json-btn").addEventListener("click", copyRawJson);
  fillForm(EXAMPLES[0].request);
  showState({ empty: true });
});
