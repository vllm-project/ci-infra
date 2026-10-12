"""SVG line charts: smooth lines over per-step averages, a stepped reference
line for quota, and a hover readout (static/app.js) listing every series."""

from __future__ import annotations

import html
import json
import math

E = html.escape


def nice_max(value: float) -> float:
    """A top for the y-axis whose half is also a round number."""
    value = max(value, 1)
    scale = 10 ** math.floor(math.log10(value))
    for m in (1, 1.2, 1.4, 1.6, 2, 4, 5, 6, 8, 10):
        if value <= m * scale:
            return m * scale
    return 10 * scale


def tick(value: float) -> str:
    """0.5 stays 0.5: a backlog that averages under one workload still reads."""
    return f"{value:,.0f}" if value == int(value) else f"{value:,.1f}"


def monotone(points: list[tuple[float, float]]) -> str:
    """A cubic path through points that never overshoots them.

    Fritsch-Carlson monotone interpolation: smooth like a spline, but a curve
    between two equal values stays flat and one between rising values never
    dips, so a smoothed line does not invent a peak or a negative.
    """
    n = len(points)
    if n == 1:
        return f"M{points[0][0]:.1f},{points[0][1]:.1f}"
    xs, ys = zip(*points)
    d = [xs[i + 1] - xs[i] for i in range(n - 1)]
    m = [(ys[i + 1] - ys[i]) / d[i] for i in range(n - 1)]
    t = [m[0]] + [0.0] * (n - 2) + [m[-1]]
    for i in range(1, n - 1):
        t[i] = 0.0 if m[i - 1] * m[i] <= 0 else (m[i - 1] + m[i]) / 2
    for i in range(n - 1):
        if m[i] == 0:
            t[i] = t[i + 1] = 0.0
            continue
        a, b = t[i] / m[i], t[i + 1] / m[i]
        s = a * a + b * b
        if s > 9:
            k = 3 / math.sqrt(s)
            t[i], t[i + 1] = k * a * m[i], k * b * m[i]
    out = [f"M{xs[0]:.1f},{ys[0]:.1f}"]
    for i in range(n - 1):
        h = d[i] / 3
        out.append(
            f"C{xs[i] + h:.1f},{ys[i] + t[i] * h:.1f} "
            f"{xs[i + 1] - h:.1f},{ys[i + 1] - t[i + 1] * h:.1f} "
            f"{xs[i + 1]:.1f},{ys[i + 1]:.1f}"
        )
    return "".join(out)


def segments(values: list, x, y) -> list[list[tuple[float, float]]]:
    """Runs of consecutive present values, as pixel points; a gap breaks a run."""
    runs, run = [], []
    for i, v in enumerate(values):
        if v is None:
            if run:
                runs.append(run)
            run = []
            continue
        run.append((x(i), y(v)))
    if run:
        runs.append(run)
    return runs


def line_chart(
    title: str,
    ticks: list[float],
    series: list[tuple[str, str, list]],
    *,
    ref: tuple[str, list] | None = None,
    extra: list[tuple[str, list]] | None = None,
    ymax: float | None = None,
    fmt: str = "time",
    width: int = 640,
    height: int = 200,
) -> str:
    """series is (css class, name, values); the first gets a faint area under
    it. ref is (name, values), drawn stepped, since quota changes in steps.
    extra rows appear only in the hover readout. fmt is how static/app.js
    labels the time axis: "time" or "day"."""
    left, right, top, bottom = 40, 64, 12, 24
    plot_w, plot_h = width - left - right, height - top - bottom
    values = [v for _, _, vs in series for v in vs if v is not None]
    if ref:
        values += [v for v in ref[1] if v is not None]
    # Charts shown side by side pass one ymax so their heights compare.
    ymax = nice_max(max(max(values, default=0), ymax or 0))
    n = max(1, len(ticks) - 1)

    def x(i: float) -> float:
        return left + plot_w * i / n

    def y(v: float) -> float:
        return top + plot_h * (1 - v / ymax)

    parts = []
    for frac in (0, 0.5, 1):
        yy = y(ymax * frac)
        parts.append(
            f'<line class="{"axis" if frac == 0 else "grid"}" x1="{left}" x2="{width - right}" '
            f'y1="{yy:.1f}" y2="{yy:.1f}"/>'
            f'<text class="tick" x="{left - 6}" y="{yy + 3:.1f}" text-anchor="end">'
            f"{tick(ymax * frac)}</text>"
        )
    for frac in (0, 0.25, 0.5, 0.75, 1):
        i = round(n * frac)
        anchor = "start" if frac == 0 else "end" if frac == 1 else "middle"
        parts.append(
            f'<text class="tick" x="{x(i):.1f}" y="{height - 6}" text-anchor="{anchor}" '
            f'data-ts="{ticks[i] if ticks else 0}" data-fmt="{fmt}"></text>'
        )
    if ref:
        runs = segments(ref[1], x, y)
        for run in runs:
            d = f"M{run[0][0]:.1f},{run[0][1]:.1f}" + "".join(
                f"H{px:.1f}V{py:.1f}" for px, py in run[1:]
            )
            parts.append(f'<path class="ref" d="{d}"/>')
        if runs:
            lx, ly = runs[-1][-1]
            parts.append(
                f'<text class="ref-label" x="{lx + 6:.1f}" y="{ly + 3:.1f}">{E(ref[0])}</text>'
            )
    for k, (cls, _, vs) in enumerate(series):
        runs = segments(vs, x, y)
        for run in runs:
            path = monotone(run)
            if k == 0 and len(run) > 1:
                base = y(0)
                parts.append(
                    f'<path class="area {cls}" d="{path}L{run[-1][0]:.1f},{base:.1f}'
                    f'L{run[0][0]:.1f},{base:.1f}Z"/>'
                )
            parts.append(f'<path class="line {cls}" d="{path}"/>')
        if runs:
            px, py = runs[-1][-1]
            parts.append(
                f'<circle class="dot {cls}" cx="{px:.1f}" cy="{py:.1f}" r="4"/>'
            )

    readout = [{"cls": c, "name": nm, "values": vs} for c, nm, vs in series]
    if ref:
        readout.append({"cls": "ref", "name": ref[0], "values": ref[1]})
    readout += [{"cls": "", "name": nm, "values": vs} for nm, vs in extra or []]
    keys = [(c, nm) for c, nm, _ in series] + ([("ref", ref[0])] if ref else [])
    legend = (
        "".join(
            f'<span class="key"><i class="k {c}"></i>{E(nm)}</span>' for c, nm in keys
        )
        if len(keys) > 1
        else ""
    )
    data = json.dumps({"ticks": ticks, "series": readout})
    return (
        f'<figure class="chart"><figcaption><span class="title">{E(title)}</span>'
        f'<span class="legend">{legend}</span></figcaption>'
        f'<div class="plot" data-chart="{E(data)}" data-geom="{left},{right},{width}">'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{E(title)}">'
        f'{"".join(parts)}<line class="crosshair" x1="0" x2="0" y1="{top}" y2="{top + plot_h}"/>'
        '</svg><div class="tip" hidden></div></div></figure>'
    )


def values_table(
    ticks: list[float], columns: list[tuple[str, list]], every: int, what: str
) -> str:
    """The numbers behind a chart, newest first, for reading without hovering."""

    def cell(v) -> str:
        return "-" if v is None else f"{v:,.1f}".removesuffix(".0")

    head = "".join(f'<th class="n">{E(name)}</th>' for name, _ in columns)
    rows = "".join(
        f'<tr><td><time data-ts="{ticks[i]}"></time></td>'
        + "".join(f'<td class="n">{cell(vs[i])}</td>' for _, vs in columns)
        + "</tr>"
        for i in range(len(ticks) - 1, -1, -max(1, every))
    )
    return (
        f"<details><summary>{E(what)}</summary>"
        f'<div class="table-wrap"><table class="dense"><thead><tr><th>Time</th>{head}</tr></thead>'
        f"<tbody>{rows}</tbody></table></div></details>"
    )


def stacked_chart(
    title: str,
    ticks: list[float],
    series: list[tuple[str, list]],
    *,
    ref: tuple[str, list] | None = None,
    ymax: float | None = None,
    fmt: str = "time",
    width: int = 640,
    height: int = 200,
    classes: list[str] | None = None,
) -> str:
    """series is (name, values), stacked bottom to top in order and colored by
    categorical slot (s1, s2, ...), so a band keeps its color whatever the
    others do; classes, when given, names each band's slot instead. Straight segments, not smoothed: two smoothed edges of one band
    can cross between points. ref is (name, values), drawn stepped over the
    stack - the quota the bands share."""
    left, right, top, bottom = 40, 64, 12, 24
    plot_w, plot_h = width - left - right, height - top - bottom
    n_points = len(ticks)
    totals = [0.0] * n_points
    edges = []
    for _, vs in series:
        low = list(totals)
        totals = [t + (v or 0) for t, v in zip(totals, vs)]
        edges.append((low, list(totals)))
    peak = max(totals, default=0)
    if ref:
        peak = max(peak, max((v for v in ref[1] if v is not None), default=0))
    # A caller showing two stacks side by side passes one ymax for both.
    ymax = nice_max(max(peak, ymax or 0))
    n = max(1, n_points - 1)
    slots = classes or [f"s{k + 1}" for k in range(len(series))]

    def x(i: float) -> float:
        return left + plot_w * i / n

    def y(v: float) -> float:
        return top + plot_h * (1 - v / ymax)

    parts = []
    for frac in (0, 0.5, 1):
        yy = y(ymax * frac)
        parts.append(
            f'<line class="{"axis" if frac == 0 else "grid"}" x1="{left}" x2="{width - right}" '
            f'y1="{yy:.1f}" y2="{yy:.1f}"/>'
            f'<text class="tick" x="{left - 6}" y="{yy + 3:.1f}" text-anchor="end">'
            f"{tick(ymax * frac)}</text>"
        )
    for frac in (0, 0.25, 0.5, 0.75, 1):
        i = round(n * frac)
        anchor = "start" if frac == 0 else "end" if frac == 1 else "middle"
        parts.append(
            f'<text class="tick" x="{x(i):.1f}" y="{height - 6}" text-anchor="{anchor}" '
            f'data-ts="{ticks[i] if ticks else 0}" data-fmt="{fmt}"></text>'
        )
    for k, (low, high) in enumerate(edges):
        if not any(h > lo for h, lo in zip(high, low)):
            continue
        up = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(high))
        down = " ".join(
            f"{x(i):.1f},{y(v):.1f}" for i, v in reversed(list(enumerate(low)))
        )
        parts.append(f'<polygon class="band {slots[k]}" points="{up} {down}"/>')
    if ref:
        for run in segments(ref[1], x, y):
            d = f"M{run[0][0]:.1f},{run[0][1]:.1f}" + "".join(
                f"H{px:.1f}V{py:.1f}" for px, py in run[1:]
            )
            parts.append(f'<path class="ref" d="{d}"/>')
            lx, ly = run[-1]
        parts.append(
            f'<text class="ref-label" x="{lx + 6:.1f}" y="{ly + 3:.1f}">{E(ref[0])}</text>'
        )

    readout = [
        {"cls": slots[k], "name": name, "values": vs}
        for k, (name, vs) in enumerate(series)
    ][::-1]
    readout.append(
        {"cls": "", "name": "total", "values": [round(t, 1) for t in totals]}
    )
    if ref:
        readout.append({"cls": "ref", "name": ref[0], "values": ref[1]})
    legend = "".join(
        f'<span class="key"><i class="k {slots[k]}"></i>{E(name)}</span>'
        for k, (name, _) in enumerate(series)
    ) + (f'<span class="key"><i class="k ref"></i>{E(ref[0])}</span>' if ref else "")
    data = json.dumps({"ticks": ticks, "series": readout})
    return (
        f'<figure class="chart"><figcaption><span class="title">{E(title)}</span>'
        f'<span class="legend">{legend}</span></figcaption>'
        f'<div class="plot" data-chart="{E(data)}" data-geom="{left},{right},{width}">'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{E(title)}">'
        f'{"".join(parts)}<line class="crosshair" x1="0" x2="0" y1="{top}" y2="{top + plot_h}"/>'
        '</svg><div class="tip" hidden></div></div></figure>'
    )


def share_bars(
    rows: list[tuple[str, list[float]]], parts: list[tuple[str, str]]
) -> str:
    """One bar per row, its segments the shares of a whole in order: parts is
    (name, slot class) for each segment, rows is (label, shares). The shares
    are written out beside each bar, so no reading depends on telling the
    colors apart."""
    out = []
    for label, shares in rows:
        segs = "".join(
            f'<i class="seg {cls}" style="width:{100 * v:.2f}%" title="{E(name)}: {100 * v:.1f}%"></i>'
            for (name, cls), v in zip(parts, shares)
            if v > 0
        )
        vals = " · ".join(
            f"<b>{100 * v:.{1 if 0 < v < 0.1 else 0}f}%</b> {E(name)}"
            for (name, _), v in zip(parts, shares)
        )
        out.append(
            f'<div class="share-row"><span class="who">{E(label)}</span>'
            f'<div class="share-bar">{segs}</div><span class="vals">{vals}</span></div>'
        )
    legend = "".join(
        f'<span class="key"><i class="k {cls}"></i>{E(name)}</span>'
        for name, cls in parts
    )
    return f'<div class="share-bars"><div class="legend">{legend}</div>{"".join(out)}</div>'


def gap_chart(
    title: str,
    ticks: list[float],
    a: tuple[str, str, list],
    b: tuple[str, str, list],
    gaps: tuple[str, str],
    *,
    series: list[tuple[str, str, list]] = (),
    ref: tuple[str, list] | None = None,
    fmt: str = "time",
    width: int = 640,
    height: int = 200,
) -> str:
    """Two lines, a and b - (css class, name, values) - with the space between
    them filled in a's color where a is above and b's where b is above, named
    by gaps (a above, b above). series are further lines, ref a stepped one.
    Straight segments throughout, so a fill meets its lines exactly; where the
    lines cross inside a step the fill is split at the crossing."""
    left, right, top, bottom = 40, 64, 12, 24
    plot_w, plot_h = width - left - right, height - top - bottom
    lines = [a, b, *series]
    values = [v for _, _, vs in lines for v in vs if v is not None]
    if ref:
        values += [v for v in ref[1] if v is not None]
    ymax = nice_max(max(values, default=0))
    n = max(1, len(ticks) - 1)

    def x(i: float) -> float:
        return left + plot_w * i / n

    def y(v: float) -> float:
        return top + plot_h * (1 - v / ymax)

    parts = []
    for frac in (0, 0.5, 1):
        yy = y(ymax * frac)
        parts.append(
            f'<line class="{"axis" if frac == 0 else "grid"}" x1="{left}" x2="{width - right}" '
            f'y1="{yy:.1f}" y2="{yy:.1f}"/>'
            f'<text class="tick" x="{left - 6}" y="{yy + 3:.1f}" text-anchor="end">'
            f"{tick(ymax * frac)}</text>"
        )
    for frac in (0, 0.25, 0.5, 0.75, 1):
        i = round(n * frac)
        anchor = "start" if frac == 0 else "end" if frac == 1 else "middle"
        parts.append(
            f'<text class="tick" x="{x(i):.1f}" y="{height - 6}" text-anchor="{anchor}" '
            f'data-ts="{ticks[i] if ticks else 0}" data-fmt="{fmt}"></text>'
        )

    def poly(points: list) -> str:
        return "M" + "L".join(f"{px:.1f},{py:.1f}" for px, py in points) + "Z"

    fills = {a[0]: [], b[0]: []}
    av, bv = a[2], b[2]
    for i in range(len(ticks) - 1):
        a0, a1, b0, b1 = av[i], av[i + 1], bv[i], bv[i + 1]
        if None in (a0, a1, b0, b1):
            continue
        d0, d1 = a0 - b0, a1 - b1
        x0, x1 = x(i), x(i + 1)
        if d0 * d1 >= 0:
            if d0 or d1:
                cls = a[0] if d0 + d1 > 0 else b[0]
                fills[cls].append(poly([(x0, y(a0)), (x1, y(a1)), (x1, y(b1)), (x0, y(b0))]))
            continue
        t = d0 / (d0 - d1)
        xc, yc = x0 + t * (x1 - x0), y(a0 + t * (a1 - a0))
        fills[a[0] if d0 > 0 else b[0]].append(poly([(x0, y(a0)), (xc, yc), (x0, y(b0))]))
        fills[a[0] if d1 > 0 else b[0]].append(poly([(xc, yc), (x1, y(a1)), (x1, y(b1))]))
    for cls, shapes in fills.items():
        if shapes:
            parts.append(f'<path class="gap {cls}" d="{"".join(shapes)}"/>')

    if ref:
        runs = segments(ref[1], x, y)
        for run in runs:
            d = f"M{run[0][0]:.1f},{run[0][1]:.1f}" + "".join(
                f"H{px:.1f}V{py:.1f}" for px, py in run[1:]
            )
            parts.append(f'<path class="ref" d="{d}"/>')
        if runs:
            lx, ly = runs[-1][-1]
            parts.append(
                f'<text class="ref-label" x="{lx + 6:.1f}" y="{ly + 3:.1f}">{E(ref[0])}</text>'
            )
    for cls, _, vs in lines:
        for run in segments(vs, x, y):
            d = "M" + "L".join(f"{px:.1f},{py:.1f}" for px, py in run)
            parts.append(f'<path class="line {cls}" d="{d}"/>')

    def diff(p: list, q: list) -> list:
        return [
            round(max(0.0, u - v), 1) if u is not None and v is not None else None
            for u, v in zip(p, q)
        ]

    readout = [{"cls": c, "name": nm, "values": vs} for c, nm, vs in lines]
    readout += [
        {"cls": f"gapk {a[0]}", "name": gaps[0], "values": diff(av, bv)},
        {"cls": f"gapk {b[0]}", "name": gaps[1], "values": diff(bv, av)},
    ]
    if ref:
        readout.append({"cls": "ref", "name": ref[0], "values": ref[1]})
    keys = [(c, nm) for c, nm, _ in lines] + [
        (f"gapk {a[0]}", gaps[0]),
        (f"gapk {b[0]}", gaps[1]),
    ] + ([("ref", ref[0])] if ref else [])
    legend = "".join(
        f'<span class="key"><i class="k {c}"></i>{E(nm)}</span>' for c, nm in keys
    )
    data = json.dumps({"ticks": ticks, "series": readout})
    return (
        f'<figure class="chart"><figcaption><span class="title">{E(title)}</span>'
        f'<span class="legend">{legend}</span></figcaption>'
        f'<div class="plot" data-chart="{E(data)}" data-geom="{left},{right},{width}">'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{E(title)}">'
        f'{"".join(parts)}<line class="crosshair" x1="0" x2="0" y1="{top}" y2="{top + plot_h}"/>'
        '</svg><div class="tip" hidden></div></div></figure>'
    )
