"""Small line charts drawn with Pillow (no extra packages). Used by $pulse and $aura history.

line_chart() returns PNG bytes. The picture is drawn 3x larger and shrunk, so lines and dots have smooth edges.
"""
import io
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

BG = (43, 45, 49)
GRID = (62, 65, 71)
TEXT = (181, 186, 193)
DIM = (120, 125, 133)
GOLD = (255, 184, 50)
MINT = (87, 242, 135)
RED = (237, 66, 69)
SKY = (88, 166, 255)

_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/opentype/inter/Inter-Regular.otf",
          "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/Library/Fonts/Arial.ttf",
          "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf", "Arial.ttf")


def _font(size: int):
    for path in _FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:                                   # very old Pillow
        return ImageFont.load_default()


def _nice_step(span: float, ticks: int = 4) -> float:
    raw = span / max(1, ticks)
    for step in (1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if step >= raw:
            return step
    return raw


def line_chart(series: Sequence[dict], *, width: int = 720, height: int = 300, lo: Optional[float] = None,
               hi: Optional[float] = None, min_span: float = 10, unit: str = "", zero_line: bool = False,
               x_left: str = "", x_right: str = "", dots: bool = False, fill: bool = True) -> bytes:
    """series: [{"values": [..], "color": (r,g,b), "label": "Gateway"}].  All series share the same x axis (oldest -> newest).
    The y axis fits the data (so a calm line is still drawn as a real line with real numbers next to it)."""
    S = 3
    W, H = width * S, height * S
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img, "RGBA")
    f_small, f_label = _font(11 * S), _font(12 * S)

    allv = [float(v) for s in series for v in s["values"]]
    if not allv:
        allv = [0.0]
    vmin, vmax = (min(allv) if lo is None else lo), (max(allv) if hi is None else hi)
    if vmax - vmin < min_span:                           # keep a calm line from filling the whole height
        mid = (vmax + vmin) / 2
        vmin, vmax = mid - min_span / 2, mid + min_span / 2
    if lo is None:
        vmin = max(0.0, vmin - (vmax - vmin) * 0.12) if vmin >= 0 else vmin - (vmax - vmin) * 0.12
    if hi is None:
        vmax = vmax + (vmax - vmin) * 0.12
    step = _nice_step(vmax - vmin)
    first = (int(vmin // step) + (0 if vmin % step == 0 else 1)) * step

    left, right, top, bottom = 52 * S, 16 * S, 34 * S, 30 * S
    x0, x1, y0, y1 = left, W - right, top, H - bottom

    def X(i, n):
        return x0 + (x1 - x0) * (i / (n - 1) if n > 1 else 0.5)

    def Y(v):
        return y1 - (y1 - y0) * (float(v) - vmin) / (vmax - vmin)

    # grid + y labels
    t = first
    while t <= vmax + 1e-9:
        y = Y(t)
        d.line([(x0, y), (x1, y)], fill=GRID, width=S)
        txt = f"{t:g}"
        w = d.textlength(txt, font=f_small)
        d.text((x0 - 8 * S - w, y - 7 * S), txt, fill=DIM, font=f_small)
        t += step
    if zero_line and vmin < 0 < vmax:
        y = Y(0)
        for x in range(int(x0), int(x1), 10 * S):
            d.line([(x, y), (x + 5 * S, y)], fill=TEXT, width=S)
    if unit:
        d.text((x0 - 44 * S, 10 * S), unit, fill=DIM, font=f_small)
    if x_left:
        d.text((x0, H - 22 * S), x_left, fill=DIM, font=f_small)
    if x_right:
        w = d.textlength(x_right, font=f_small)
        d.text((x1 - w, H - 22 * S), x_right, fill=DIM, font=f_small)

    # legend (top right)
    lx = x1
    for s in reversed(series):
        if not s.get("label"):
            continue
        w = d.textlength(s["label"], font=f_label)
        lx -= w
        d.text((lx, 8 * S), s["label"], fill=TEXT, font=f_label)
        lx -= 16 * S
        d.ellipse([lx, 13 * S, lx + 9 * S, 22 * S], fill=s["color"])
        lx -= 18 * S

    # lines
    for s in series:
        vals = [float(v) for v in s["values"]]
        n = len(vals)
        if n == 0:
            continue
        pts = [(X(i, n), Y(v)) for i, v in enumerate(vals)]
        col = s["color"]
        if fill and n > 1:
            poly = pts + [(pts[-1][0], y1), (pts[0][0], y1)]
            d.polygon(poly, fill=(*col, 34))
        if n > 1:
            d.line(pts, fill=col, width=int(2.4 * S), joint="curve")
        if dots or n == 1:
            for (px, py), v in zip(pts, vals):
                c = col
                if s.get("signed"):
                    c = MINT if v > 0 else RED if v < 0 else TEXT
                d.ellipse([px - 4 * S, py - 4 * S, px + 4 * S, py + 4 * S], fill=c, outline=BG, width=S)
        px, py = pts[-1]                                  # newest value: bigger dot with a soft halo
        if s.get("signed"):
            col = MINT if vals[-1] > 0 else RED if vals[-1] < 0 else TEXT
        d.ellipse([px - 9 * S, py - 9 * S, px + 9 * S, py + 9 * S], fill=(*col, 50))
        d.ellipse([px - 4.5 * S, py - 4.5 * S, px + 4.5 * S, py + 4.5 * S], fill=col, outline=BG, width=S)

    out = io.BytesIO()
    img.resize((width, height), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()
