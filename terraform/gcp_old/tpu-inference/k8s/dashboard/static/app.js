// Shared by the pages: local-time labels and chart hover readouts. The server
// renders in UTC epoch seconds; only the browser knows the reader's time zone.

const FORMATS = {
  time: {hour: "2-digit", minute: "2-digit"},
  day: {month: "short", day: "numeric"},
  datetime: {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"},
};

function label(ts, fmt) {
  return new Date(ts * 1000).toLocaleString([], FORMATS[fmt] || FORMATS.datetime);
}

for (const el of document.querySelectorAll("[data-ts]")) {
  const ts = Number(el.dataset.ts);
  if (el.tagName === "TIME") el.dateTime = new Date(ts * 1000).toISOString();
  el.textContent = label(ts, el.dataset.fmt);
}

// Hover: a crosshair snapped to the nearest step, and every series' value there.
for (const plot of document.querySelectorAll(".plot")) {
  const data = JSON.parse(plot.dataset.chart);
  const [left, right, width] = plot.dataset.geom.split(",").map(Number);
  const svg = plot.querySelector("svg"), tip = plot.querySelector(".tip");
  const hair = svg.querySelector(".crosshair"), n = Math.max(1, data.ticks.length - 1);
  svg.addEventListener("pointerleave", () => { tip.hidden = true; hair.style.visibility = "hidden"; });
  svg.addEventListener("pointermove", (ev) => {
    const box = svg.getBoundingClientRect(), scale = width / box.width;
    const sx = (ev.clientX - box.left) * scale;
    const i = Math.max(0, Math.min(n, Math.round((sx - left) / (width - left - right) * n)));
    const x = left + (width - left - right) * i / n;
    hair.setAttribute("x1", x); hair.setAttribute("x2", x); hair.style.visibility = "visible";
    tip.replaceChildren();
    const when = document.createElement("div"); when.className = "when";
    when.textContent = label(data.ticks[i], "datetime");
    tip.append(when);
    for (const s of data.series) {
      const row = document.createElement("div"); row.className = "row";
      const key = document.createElement("i"); key.className = "k " + s.cls;
      const val = document.createElement("b"); val.textContent = s.values[i] ?? "-";
      const name = document.createElement("span"); name.textContent = s.name;
      row.append(key, val, name); tip.append(row);
    }
    tip.hidden = false;
    const px = x / scale, tw = tip.offsetWidth;
    tip.style.left = Math.max(0, Math.min(box.width - tw, px + 12)) + "px";
  });
}

// A link into a collapsed section - a box in the overview's diagram, a source
// mark in the top bar - opens every <details> around its target before the
// browser scrolls there.
function reveal(id) {
  const target = id && document.getElementById(id);
  for (let el = target; el; el = el.parentElement) if (el.tagName === "DETAILS") el.open = true;
}
document.addEventListener("click", (ev) => {
  const a = ev.target.closest('a[href^="#"]');
  if (a) reveal(decodeURIComponent(a.getAttribute("href").slice(1)));
});
reveal(decodeURIComponent(location.hash.slice(1)));

// Overview: open or close every component's details at once.
const toggle = document.getElementById("expand-all");
if (toggle) {
  toggle.addEventListener("click", () => {
    const panels = [...document.querySelectorAll("details.component")];
    const open = !panels.every((p) => p.open);
    for (const p of panels) p.open = open;
    toggle.textContent = open ? "Collapse all" : "Expand all";
  });
}

// Show more: reveal the rows a table keeps back, or hide them again.
for (const b of document.querySelectorAll(".show-more")) {
  b.addEventListener("click", () => {
    const rows = document.getElementById(b.dataset.target);
    rows.hidden = !rows.hidden;
    b.textContent = rows.hidden ? b.dataset.more : b.dataset.fewer;
  });
}
