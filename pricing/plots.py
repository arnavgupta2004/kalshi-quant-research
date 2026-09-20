"""Reliability diagrams as hand-written SVG (no plotting dependency; byte-for-byte reproducible).

A reliability diagram plots, for each probability bin, the observed frequency of YES against the
mean forecast.  Perfect calibration is the diagonal.  Points above it: the event happened MORE often
than forecast; below: less often.  Whiskers are 95% event-cluster bootstrap intervals; the bars
along the bottom show how many forecasts each bin holds (a point with few forecasts and a wide
whisker should not be over-read).
"""

from __future__ import annotations

from dataclasses import dataclass

W, H = 560, 520
L, R, T, B = 56, 16, 34, 120  # margins: room below for the count histogram and the axis label
PW, PH = W - L - R, H - T - B


@dataclass(frozen=True)
class Point:
    mean_p: float
    freq: float
    lo: float | None
    hi: float | None
    n: int


def _x(v: float) -> float:
    return L + v * PW


def _y(v: float) -> float:
    return T + (1 - v) * PH


def reliability_svg(series: dict[str, list[Point]], title: str, subtitle: str = "") -> str:
    colours = ["#1f6feb", "#d1242f", "#2da44e", "#8250df"]
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        'font-family="Helvetica, Arial, sans-serif" font-size="11">',
        f'<rect width="{W}" height="{H}" fill="white"/>',
        f'<text x="{L}" y="16" font-size="13" font-weight="bold">{_esc(title)}</text>',
    ]
    if subtitle:
        out.append(f'<text x="{L}" y="29" fill="#555">{_esc(subtitle)}</text>')
    for t in (0, 0.25, 0.5, 0.75, 1.0):  # grid and ticks
        out.append(
            f'<line x1="{_x(t):.1f}" y1="{T}" x2="{_x(t):.1f}" y2="{T + PH}" stroke="#eee"/>'
        )
        out.append(
            f'<line x1="{L}" y1="{_y(t):.1f}" x2="{L + PW}" y2="{_y(t):.1f}" stroke="#eee"/>'
        )
        out.append(f'<text x="{_x(t):.1f}" y="{T + PH + 14}" text-anchor="middle">{t:g}</text>')
        out.append(f'<text x="{L - 6}" y="{_y(t) + 4:.1f}" text-anchor="end">{t:g}</text>')
    out.append(f'<rect x="{L}" y="{T}" width="{PW}" height="{PH}" fill="none" stroke="#999"/>')
    out.append(
        f'<line x1="{_x(0):.1f}" y1="{_y(0):.1f}" x2="{_x(1):.1f}" y2="{_y(1):.1f}" '
        'stroke="#888" stroke-dasharray="5,4"/>'
    )
    out.append(
        f'<text x="{L + PW / 2}" y="{T + PH + 30}" text-anchor="middle">mean forecast</text>'
    )
    out.append(
        f'<text transform="translate(14,{T + PH / 2}) rotate(-90)" text-anchor="middle">'
        "frequency of YES</text>"
    )
    top = max((p.n for pts in series.values() for p in pts), default=1)
    for k, (name, pts) in enumerate(series.items()):
        c = colours[k % len(colours)]
        pts = sorted(pts, key=lambda p: p.mean_p)
        if len(pts) > 1:
            path = " ".join(f"{_x(p.mean_p):.1f},{_y(p.freq):.1f}" for p in pts)
            out.append(f'<polyline points="{path}" fill="none" stroke="{c}" stroke-opacity="0.5"/>')
        for p in pts:
            if p.lo is not None and p.hi is not None:
                out.append(
                    f'<line x1="{_x(p.mean_p):.1f}" y1="{_y(p.lo):.1f}" x2="{_x(p.mean_p):.1f}" '
                    f'y2="{_y(p.hi):.1f}" stroke="{c}"/>'
                )
            out.append(
                f'<circle cx="{_x(p.mean_p):.1f}" cy="{_y(p.freq):.1f}" r="3.2" fill="{c}"/>'
            )
        # count histogram under the axis
        for p in pts:
            h = 44 * p.n / top
            out.append(
                f'<rect x="{_x(p.mean_p) - 4 + 5 * k:.1f}" y="{T + PH + 84 - h:.1f}" width="4" '
                f'height="{h:.1f}" fill="{c}" fill-opacity="0.6"/>'
            )
        out.append(f'<rect x="{L + 8 + 150 * k}" y="{H - 22}" width="10" height="10" fill="{c}"/>')
        out.append(f'<text x="{L + 24 + 150 * k}" y="{H - 13}">{_esc(name)}</text>')
    out.append(f'<text x="{L}" y="{T + PH + 40 + 56}" fill="#555">forecasts per bin (bars)</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
