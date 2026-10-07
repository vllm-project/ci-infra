// Shared by the live and history pages: local-time labels, chart hover
// readouts, and the history range picker. The server renders in UTC epoch
// seconds; only the browser knows the reader's time zone.

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

// History: dates are local days; the server takes epoch seconds.
const range = document.getElementById("range");
if (range) {
  const day = (ts) => {
    const d = new Date(ts * 1000);
    return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, "0"), String(d.getDate()).padStart(2, "0")].join("-");
  };
  const from = range.elements.namedItem("from"), to = range.elements.namedItem("to");
  from.value = day(Number(range.dataset.start));
  to.value = day(Number(range.dataset.end) - 1);
  range.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const [fy, fm, fd] = from.value.split("-").map(Number);
    const [ty, tm, td] = to.value.split("-").map(Number);
    const start = new Date(fy, fm - 1, fd).getTime() / 1000;
    const end = Math.min(new Date(ty, tm - 1, td + 1).getTime() / 1000, Date.now() / 1000);
    if (end > start) location.search = "?start=" + Math.floor(start) + "&end=" + Math.floor(end);
  });
}
