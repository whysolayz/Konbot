"""KON GLASS icon generator - draws every bot icon from scratch (no source sheet needed).

    python tools/make_icons.py        (needs pillow + numpy)

Look: same Kon colours (light yellow -> golden -> amber -> orange -> dark orange), but every icon is now a soft filled
shape with a diagonal gradient, a glossy light on the top edge, a darker lower edge, an engraved dark detail and a
cream highlight - so it has depth instead of being a flat outline. Team / signal icons keep their real colours
(red / blue / green) because the colour is the meaning there. Also redraws assets/panel.png (voice control panel).
"""
import math
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "icons")
N = 1024                                  # drawing canvas (100 design units); downscaled to 128 = smooth edges

KON = [(0xFF, 0xD6, 0x5A), (0xFF, 0xB8, 0x32), (0xFF, 0x9B, 0x22), (0xF5, 0x82, 0x19), (0xD9, 0x5A, 0x12)]
RED = [(0xFF, 0xA0, 0xA0), (0xF2, 0x5C, 0x5F), (0xE0, 0x35, 0x3C), (0xB5, 0x20, 0x2B), (0x85, 0x12, 0x1E)]
BLUE = [(0xA4, 0xC4, 0xFF), (0x6C, 0x8D, 0xFF), (0x4A, 0x66, 0xE8), (0x34, 0x47, 0xBD), (0x22, 0x2E, 0x8A)]
GREEN = [(0xB8, 0xFF, 0xC8), (0x6F, 0xF0, 0x93), (0x3A, 0xD0, 0x6C), (0x24, 0xA2, 0x50), (0x15, 0x74, 0x38)]
CREAM = (255, 246, 216)


def u(v):
    return v * N / 100.0


class Layer:
    """One mask. add = paint shape, cut=True = erase shape (transparent hole)."""
    def __init__(self):
        self.im = Image.new("L", (N, N), 0)
        self.d = ImageDraw.Draw(self.im)

    @staticmethod
    def _f(cut):
        return 0 if cut else 255

    def rr(self, x0, y0, x1, y1, r=0, cut=False):
        self.d.rounded_rectangle([u(x0), u(y0), u(x1), u(y1)], radius=u(r), fill=self._f(cut))

    def circ(self, cx, cy, r, cut=False):
        self.d.ellipse([u(cx - r), u(cy - r), u(cx + r), u(cy + r)], fill=self._f(cut))

    def ell(self, x0, y0, x1, y1, cut=False):
        self.d.ellipse([u(x0), u(y0), u(x1), u(y1)], fill=self._f(cut))

    def ring(self, cx, cy, r, w, cut=False):
        self.d.ellipse([u(cx - r - w / 2), u(cy - r - w / 2), u(cx + r + w / 2), u(cy + r + w / 2)],
                       outline=self._f(cut), width=int(u(w)))

    def poly(self, pts, cut=False, soft=0):
        self.d.polygon([(u(x), u(y)) for x, y in pts], fill=self._f(cut))
        if soft:
            self.line(list(pts) + [pts[0]], soft, cut)

    def line(self, pts, w, cut=False):
        f = self._f(cut)
        P = [(u(x), u(y)) for x, y in pts]
        for a, b in zip(P, P[1:]):
            self.d.line([a, b], fill=f, width=int(u(w)))
        r = u(w) / 2
        for x, y in P:
            self.d.ellipse([x - r, y - r, x + r, y + r], fill=f)

    def rod(self, p0, p1, w, cut=False):
        """flat-ended bar from p0 to p1"""
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        n = math.hypot(dx, dy) or 1
        nx, ny = -dy / n * w / 2, dx / n * w / 2
        self.poly([(p0[0] + nx, p0[1] + ny), (p1[0] + nx, p1[1] + ny), (p1[0] - nx, p1[1] - ny), (p0[0] - nx, p0[1] - ny)], cut)

    def arc(self, cx, cy, r, a0, a1, w, cut=False):
        """a0 -> a1 in degrees, maths direction (0 = right, 90 = up)"""
        n = max(8, int(abs(a1 - a0) / 4))
        pts = [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
                cy - r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]
        self.line(pts, w, cut)

    def head(self, tip, ang, size, cut=False):
        """arrow head, tip at `tip`, pointing in direction ang (image degrees: 0 right, 90 down)"""
        a = math.radians(ang)
        def pt(da, d):
            return (tip[0] - d * math.cos(a + math.radians(da)), tip[1] - d * math.sin(a + math.radians(da)))
        self.poly([tip, pt(34, size), pt(-34, size)], cut, soft=size * 0.18)


def arc_arrow(B, cx, cy, r, a0, a1, w, hs=26):
    """arc a0 -> a1 (maths degrees) with an arrow head at the end, aimed along the real tangent"""
    sgn = 1 if a1 > a0 else -1
    B.arc(cx, cy, r, a0, a1 - sgn * 8, w)
    a = math.radians(a1)
    ex, ey = cx + r * math.cos(a), cy - r * math.sin(a)
    dx, dy = sgn * -math.sin(a), sgn * -math.cos(a)
    B.head((ex + dx * hs * 0.55, ey + dy * hs * 0.55), math.degrees(math.atan2(dy, dx)), hs)


def quad(p0, p1, p2, n=14):
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]) for t in [i / n for i in range(n + 1)]]


def path(start, segs):
    pts = [start]
    cur = start
    for c, e in segs:
        pts += quad(cur, c, e)[1:]
        cur = e
    return pts


def star(cx, cy, R, r, n=5, rot=-90):
    pts = []
    for i in range(n * 2):
        rad = R if i % 2 == 0 else r
        a = math.radians(rot + i * 180 / n)
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    return pts


def sparkle(cx, cy, R, k=0.26):
    r = R * k
    return [(cx, cy - R), (cx + r, cy - r), (cx + R, cy), (cx + r, cy + r), (cx, cy + R), (cx - r, cy + r), (cx - R, cy), (cx - r, cy - r)]


def bust(B, cx, cy, k=1.0):
    B.circ(cx, cy - 14 * k, 15 * k)
    B.ell(cx - 26 * k, cy + 6 * k, cx + 26 * k, cy + 46 * k)


SHIELD = path((50, 7), [((68, 17), (86, 19)), ((86, 19), (86, 46)), ((86, 72), (50, 93)), ((14, 72), (14, 46)),
                        ((14, 19), (14, 19)), ((32, 17), (50, 7))])
HEART = [(50, 90)]


def heart(B, cx=50, cy=50, k=1.0, cut=False):
    B.circ(cx - 17 * k, cy - 10 * k, 21 * k, cut)
    B.circ(cx + 17 * k, cy - 10 * k, 21 * k, cut)
    B.poly([(cx - 36 * k, cy - 2 * k), (cx + 36 * k, cy - 2 * k), (cx, cy + 40 * k)], cut, soft=6 * k)


# ============================================================================================ icon drawings
# each icon: f(B, D, L) - B = gradient body, D = dark engraved detail, L = cream highlight
ICONS = {}


def icon(name, pal=KON):
    def deco(f):
        ICONS[name] = (f, pal)
        return f
    return deco


@icon("lock")
def _(B, D, L):
    B.line([(33, 46), (33, 36)], 9); B.arc(50, 36, 17, 0, 180, 9); B.line([(67, 36), (67, 46)], 9)
    B.rr(20, 44, 80, 90, 11)
    D.circ(50, 62, 7); D.rr(46.5, 64, 53.5, 78, 3)
    L.rr(26, 49, 34, 52.5, 2)


@icon("unlock")
def _(B, D, L):
    B.line([(33, 26), (33, 46)], 9); B.arc(50, 26, 17, 10, 180, 9)
    B.rr(20, 44, 80, 90, 11)
    D.circ(50, 62, 7); D.rr(46.5, 64, 53.5, 78, 3)
    L.rr(26, 49, 34, 52.5, 2)


@icon("hide")
def _(B, D, L):
    B.poly(path((6, 50), [((50, 8), (94, 50)), ((50, 92), (6, 50))]), soft=2)
    D.circ(50, 50, 15); L.circ(55, 45, 5)
    for lay in (B, D, L):
        lay.line([(24, 88), (78, 12)], 19, cut=True)
    B.line([(24, 86), (78, 14)], 8)


@icon("show")
def _(B, D, L):
    B.poly(path((6, 50), [((50, 8), (94, 50)), ((50, 92), (6, 50))]), soft=2)
    D.circ(50, 50, 16); L.circ(56, 44, 5.5)


@icon("logout")
def _(B, D, L):
    B.rr(12, 12, 58, 88, 9)
    D.circ(46, 50, 4)
    B.poly([(50, 36), (68, 36), (68, 24), (92, 50), (68, 76), (68, 64), (50, 64)], soft=4, cut=True)
    B.poly([(54, 40), (72, 40), (72, 31), (88, 50), (72, 69), (72, 60), (54, 60)], soft=3)


@icon("ban")
def _(B, D, L):
    B.ring(50, 50, 36, 12); B.line([(25, 75), (75, 25)], 12)


@icon("user_add")
def _(B, D, L):
    bust(B, 38, 40, 1.0)
    B.circ(76, 32, 19, cut=True)
    B.line([(76, 21), (76, 43)], 8); B.line([(65, 32), (87, 32)], 8)


@icon("users")
def _(B, D, L):
    B.circ(66, 30, 12); B.ell(46, 48, 90, 84)
    B.circ(36, 36, 19, cut=True); B.ell(8, 54, 64, 100, cut=True)
    B.circ(36, 34, 14); B.ell(11, 53, 61, 92)


@icon("signal")
def _(B, D, L):
    for i, x in enumerate((10, 32, 54, 76)):
        B.rr(x, 66 - i * 17, x + 15, 90, 5)
    L.rr(13.5, 71, 17, 80, 2)


@icon("edit")
def _(B, D, L):
    P0, P1, hw = (74, 24), (40, 58), 12
    nx = hw * 0.7071
    B.rod(P0, P1, hw * 2)
    B.poly([(P1[0] - nx, P1[1] - nx), (P1[0] + nx, P1[1] + nx), (16, 86)], soft=3)
    D.rod((62, 42), (68, 36), 24)
    L.rod((56, 36), (74, 18), 4)


@icon("volume")
def _(B, D, L):
    B.poly([(10, 38), (28, 38), (50, 18), (50, 82), (28, 62), (10, 62)], soft=5)
    B.arc(50, 50, 20, -42, 42, 8); B.arc(50, 50, 36, -42, 42, 8)


@icon("speaker")
def _(B, D, L):
    B.poly([(12, 38), (30, 38), (54, 16), (54, 84), (30, 62), (12, 62)], soft=5)
    B.arc(54, 50, 22, -45, 45, 9)


@icon("mute")
def _(B, D, L):
    B.poly([(10, 38), (28, 38), (50, 18), (50, 82), (28, 62), (10, 62)], soft=5)
    B.line([(64, 38), (88, 62)], 8); B.line([(88, 38), (64, 62)], 8)


@icon("crown")
def _(B, D, L):
    B.poly([(8, 72), (12, 28), (33, 50), (50, 18), (67, 50), (88, 28), (92, 72)], soft=6)
    B.rr(10, 74, 90, 90, 6)
    for x, y in ((12, 26), (50, 16), (88, 26)):
        B.circ(x, y, 6)
    L.circ(50, 62, 5); L.circ(30, 64, 3.2); L.circ(70, 64, 3.2)


@icon("check")
def _(B, D, L):
    B.circ(50, 50, 42); L.line([(30, 52), (44, 66), (71, 36)], 11)


@icon("cross")
def _(B, D, L):
    B.circ(50, 50, 42); L.line([(34, 34), (66, 66)], 10); L.line([(66, 34), (34, 66)], 10)


@icon("check_outline")
def _(B, D, L):
    B.ring(50, 50, 36, 10); B.line([(32, 52), (45, 65), (70, 36)], 10)


@icon("cross_filled")
def _(B, D, L):
    B.circ(50, 50, 42); B.line([(34, 34), (66, 66)], 10, cut=True); B.line([(66, 34), (34, 66)], 10, cut=True)


@icon("hourglass")
def _(B, D, L):
    B.poly([(26, 14), (74, 14), (74, 24), (54, 48), (54, 52), (74, 76), (74, 86), (26, 86), (26, 76), (46, 52), (46, 48), (26, 24)], soft=4)
    B.rr(18, 6, 82, 17, 5); B.rr(18, 83, 82, 94, 5)
    L.poly([(37, 80), (63, 80), (50, 64)]); D.rr(48.5, 40, 51.5, 60, 1)


@icon("clock")
def _(B, D, L):
    B.circ(50, 50, 42)
    L.line([(50, 50), (50, 26)], 8); L.line([(50, 50), (68, 60)], 8); L.circ(50, 50, 5)
    for a in range(0, 360, 90):
        x, y = 50 + 33 * math.cos(math.radians(a)), 50 + 33 * math.sin(math.radians(a))
        D.circ(x, y, 2.6)


@icon("info")
def _(B, D, L):
    B.circ(50, 50, 42); L.circ(50, 30, 6); L.line([(50, 45), (50, 70)], 10)


@icon("swap")
def _(B, D, L):
    B.line([(14, 33), (68, 33)], 9); B.head((90, 33), 0, 24)
    B.line([(86, 67), (32, 67)], 9); B.head((10, 67), 180, 24)


@icon("left")
def _(B, D, L):
    B.poly([(88, 36), (48, 36), (48, 16), (10, 50), (48, 84), (48, 64), (88, 64)], soft=6)


@icon("right")
def _(B, D, L):
    B.poly([(12, 36), (52, 36), (52, 16), (90, 50), (52, 84), (52, 64), (12, 64)], soft=6)


@icon("mic")
def _(B, D, L):
    B.rr(35, 8, 65, 56, 15)
    B.arc(50, 46, 26, 180, 360, 8); B.line([(50, 74), (50, 88)], 8); B.line([(34, 90), (66, 90)], 8)
    L.rr(41, 15, 45, 34, 2); D.line([(42, 44), (58, 44)], 3); D.line([(42, 36), (58, 36)], 3)


@icon("settings")
def _(B, D, L):
    for a in range(0, 360, 45):
        r = math.radians(a)
        B.rod((50 + 24 * math.cos(r), 50 + 24 * math.sin(r)), (50 + 42 * math.cos(r), 50 + 42 * math.sin(r)), 15)
    B.circ(50, 50, 31); B.circ(50, 50, 13, cut=True); D.circ(50, 50, 13)
    L.circ(50, 50, 6)


@icon("warning")
def _(B, D, L):
    B.poly([(50, 10), (93, 86), (7, 86)], soft=9)
    L.line([(50, 37), (50, 59)], 9); L.circ(50, 73, 5.2)


@icon("tools")
def _(B, D, L):
    B.line([(70, 70), (40, 40)], 13); B.circ(31, 31, 20); B.circ(24, 24, 9, cut=True)
    B.line([(31, 31), (12, 12)], 12, cut=True)
    B.rod((76, 24), (36, 64), 11); B.poly([(36, 64), (30, 58), (20, 84)], soft=2)
    L.rod((74, 28), (66, 36), 6)


@icon("bolt")
def _(B, D, L):
    B.poly([(60, 5), (20, 56), (45, 56), (38, 95), (80, 40), (55, 40)], soft=6)
    L.poly([(58, 14), (34, 48), (50, 48)])


@icon("refresh")
def _(B, D, L):
    arc_arrow(B, 50, 50, 30, 165, 28, 11, 28)
    arc_arrow(B, 50, 50, 30, -15, -152, 11, 28)


@icon("shield_check")
def _(B, D, L):
    B.poly(SHIELD, soft=2); L.line([(33, 50), (45, 62), (68, 36)], 10)


@icon("user_minus")
def _(B, D, L):
    bust(B, 38, 40, 1.0); B.circ(76, 32, 19, cut=True); B.line([(65, 32), (87, 32)], 8)


@icon("leaf")
def _(B, D, L):
    B.poly(path((14, 88), [((6, 30), (80, 12))]) + path((80, 12), [((94, 78), (22, 90))])[1:], soft=3)
    D.line([(20, 82), (62, 40)], 5); D.line([(42, 62), (44, 40)], 3); D.line([(42, 62), (64, 62)], 3)


@icon("headset")
def _(B, D, L):
    B.arc(50, 50, 34, 0, 180, 9); B.rr(10, 48, 28, 76, 9); B.rr(72, 48, 90, 76, 9)
    B.line([(81, 76), (74, 88), (54, 88)], 6); B.circ(52, 88, 6)
    L.rr(14, 53, 17, 66, 1.5)


@icon("help")
def _(B, D, L):
    B.circ(50, 50, 42)
    L.arc(50, 38, 13, 160, -70, 9); L.line([(56, 47), (50, 56), (50, 61)], 9); L.circ(50, 76, 5.4)


@icon("home")
def _(B, D, L):
    B.poly([(50, 10), (94, 50), (82, 50), (82, 90), (18, 90), (18, 50), (6, 50)], soft=5)
    D.rr(41, 62, 59, 90, 6); L.rr(22, 54, 27, 62, 2)


@icon("menu")
def _(B, D, L):
    for y in (18, 43, 68):
        B.rr(12, y, 88, y + 15, 7.5)
    L.circ(22, 25.5, 2.8); L.circ(22, 50.5, 2.8); L.circ(22, 75.5, 2.8)


@icon("sparkles")
def _(B, D, L):
    B.poly(sparkle(40, 58, 36), soft=3); B.poly(sparkle(76, 24, 17), soft=2); B.poly(sparkle(78, 76, 12), soft=2)
    L.poly(sparkle(40, 58, 12))


@icon("stopwatch")
def _(B, D, L):
    B.circ(50, 56, 35); B.rr(41, 6, 59, 17, 4); B.rr(45, 14, 55, 24, 2); B.line([(78, 28), (84, 22)], 8)
    L.line([(50, 56), (50, 36)], 8); L.circ(50, 56, 5)
    D.circ(50, 26, 2.2)


@icon("plus")
def _(B, D, L):
    B.circ(50, 50, 42); L.line([(50, 30), (50, 70)], 10); L.line([(30, 50), (70, 50)], 10)


@icon("minus")
def _(B, D, L):
    B.circ(50, 50, 42); L.line([(30, 50), (70, 50)], 10)


@icon("trophy")
def _(B, D, L):
    B.arc(24, 32, 14, 100, 270, 7); B.arc(76, 32, 14, 80, -90, 7)
    B.poly([(24, 10), (76, 10), (72, 44), (50, 62), (28, 44)], soft=5)
    B.rr(44, 58, 56, 76, 2); B.rr(28, 76, 72, 90, 6)
    L.poly(star(50, 33, 12, 5, 5)); L.rr(31, 16, 35, 30, 2)


@icon("coins")
def _(B, D, L):
    for y in (62, 42, 22):
        B.ell(10, y - 3, 90, y + 29, cut=True); B.ell(13, y, 87, y + 26)
    L.ell(30, 25, 70, 38); D.ell(37, 28, 63, 35)


@icon("wallet")
def _(B, D, L):
    B.rr(12, 14, 74, 32, 7); B.rr(8, 24, 92, 86, 13)
    B.rr(56, 46, 96, 68, 10, cut=True); B.rr(60, 49, 94, 65, 8)
    L.circ(72, 57, 4.5); L.rr(14, 30, 40, 33, 1.5)


@icon("gift_box")
def _(B, D, L):
    B.rr(14, 46, 86, 92, 7); B.rr(8, 30, 92, 48, 6)
    L.rr(44, 30, 56, 92); B.ring(37, 20, 9, 7); B.ring(63, 20, 9, 7); L.circ(50, 24, 6)


@icon("gift")
def _(B, D, L):
    B.rr(12, 40, 88, 92, 8); B.rr(8, 26, 92, 44, 6)
    B.poly(path((50, 26), [((30, -6), (24, 14)), ((22, 26), (50, 26))]), soft=2)
    B.poly(path((50, 26), [((70, -6), (76, 14)), ((78, 26), (50, 26))]), soft=2)
    L.rr(44, 26, 56, 92)


@icon("heart")
def _(B, D, L):
    heart(B, 50, 46, 1.05); L.ell(24, 24, 36, 38)


@icon("heart_broken")
def _(B, D, L):
    heart(B, 50, 46, 1.05)
    B.line([(54, 6), (42, 34), (58, 48), (44, 90)], 6, cut=True)
    D.line([(54, 10), (42, 34), (58, 48), (46, 78)], 2.5)


@icon("message")
def _(B, D, L):
    B.rr(8, 12, 92, 70, 18); B.poly([(24, 64), (24, 92), (52, 66)], soft=4)
    L.circ(30, 41, 5); L.circ(50, 41, 5); L.circ(70, 41, 5)


@icon("messages")
def _(B, D, L):
    B.rr(34, 8, 94, 52, 14)
    B.rr(4, 36, 68, 78, 14, cut=True); B.rr(8, 40, 64, 74, 11)
    B.poly([(18, 72), (18, 92), (38, 74)], soft=3)
    L.circ(26, 57, 3.6); L.circ(36, 57, 3.6); L.circ(46, 57, 3.6)


@icon("mail")
def _(B, D, L):
    B.rr(6, 20, 94, 80, 11); L.line([(13, 29), (50, 58), (87, 29)], 6)


@icon("bell_ring")
def _(B, D, L):
    B.circ(50, 38, 26); B.rr(24, 38, 76, 68, 3); B.poly([(12, 78), (88, 78), (76, 62), (24, 62)], soft=4)
    B.circ(50, 12, 5.5); B.ell(38, 76, 62, 92)
    L.rr(33, 24, 38, 46, 2.5)


@icon("play")
def _(B, D, L):
    B.poly([(24, 12), (90, 50), (24, 88)], soft=12)


@icon("terminal")
def _(B, D, L):
    B.rr(6, 14, 94, 86, 13)
    L.line([(24, 36), (42, 50), (24, 64)], 8); L.line([(50, 66), (74, 66)], 8)


@icon("star_fill")
def _(B, D, L):
    B.poly(star(50, 53, 43, 20), soft=9); L.poly(star(50, 53, 14, 7), soft=0)


@icon("link")
def _(B, D, L):
    B.line([(22, 78), (44, 56)], 24); B.line([(22, 78), (44, 56)], 10, cut=True)
    B.line([(56, 44), (78, 22)], 24); B.line([(56, 44), (78, 22)], 10, cut=True)
    B.rod((38, 62), (62, 38), 9)


@icon("image")
def _(B, D, L):
    B.rr(6, 14, 94, 86, 12)
    D.poly([(12, 78), (36, 42), (52, 64), (64, 50), (88, 78)], soft=3); L.circ(70, 34, 8)


@icon("palette")
def _(B, D, L):
    B.poly(path((50, 8), [((6, 8), (6, 50))]) + path((6, 50), [((6, 92), (50, 92))])[1:] + path((50, 92), [((60, 92), (62, 80))])[1:]
           + path((62, 80), [((64, 68), (76, 68))])[1:] + path((76, 68), [((94, 68), (94, 50))])[1:] + path((94, 50), [((94, 22), (50, 8))])[1:], soft=2)
    B.circ(30, 66, 7.5, cut=True)
    D.circ(32, 38, 7); L.circ(54, 26, 7); D.circ(76, 42, 7); L.circ(22, 54, 6)


@icon("upload")
def _(B, D, L):
    B.poly([(50, 8), (82, 42), (60, 42), (60, 66), (40, 66), (40, 42), (18, 42)], soft=6)
    B.line([(14, 70), (14, 88), (86, 88), (86, 70)], 9)


@icon("tag")
def _(B, D, L):
    B.poly([(8, 14), (50, 14), (92, 56), (56, 92), (8, 44)], soft=8)
    B.circ(28, 33, 8, cut=True); D.circ(28, 33, 8)


@icon("diamond")
def _(B, D, L):
    B.poly([(26, 14), (74, 14), (94, 38), (50, 90), (6, 38)], soft=4)
    D.line([(8, 38), (92, 38)], 2.5); D.line([(26, 15), (36, 38), (50, 88), (64, 38), (74, 15)], 2.5)
    L.poly([(30, 19), (45, 19), (38, 34)])


@icon("skull")
def _(B, D, L):
    B.circ(50, 44, 35); B.rr(32, 62, 68, 90, 9)
    B.circ(37, 46, 10, cut=True); B.circ(63, 46, 10, cut=True); B.poly([(50, 56), (44, 68), (56, 68)], cut=True, soft=2)
    D.circ(37, 46, 10); D.circ(63, 46, 10); D.line([(42, 78), (42, 88)], 2.5); D.line([(50, 78), (50, 88)], 2.5); D.line([(58, 78), (58, 88)], 2.5)


@icon("stop")
def _(B, D, L):
    B.rr(14, 14, 86, 86, 17); L.rr(22, 21, 30, 25, 2)


@icon("repeat")
def _(B, D, L):
    B.line([(24, 50), (24, 32), (66, 32)], 9); B.head((90, 32), 0, 24)
    B.line([(76, 50), (76, 68), (34, 68)], 9); B.head((10, 68), 180, 24)


@icon("fire")
def _(B, D, L):
    B.poly(path((52, 4), [((60, 30), (82, 48))]) + path((82, 48), [((98, 80), (50, 94))])[1:] + path((50, 94), [((2, 82), (18, 50))])[1:]
           + path((18, 50), [((24, 38), (34, 34))])[1:] + path((34, 34), [((34, 18), (52, 4))])[1:], soft=2)
    L.poly(path((50, 50), [((68, 66), (62, 82))]) + path((62, 82), [((52, 92), (40, 84))])[1:] + path((40, 84), [((32, 68), (50, 50))])[1:])


@icon("gamepad")
def _(B, D, L):
    B.rr(4, 26, 96, 76, 24)
    B.rr(14, 60, 40, 92, 12); B.rr(60, 60, 86, 92, 12)
    D.rr(20, 44, 40, 52, 3); D.rr(26, 38, 34, 58, 3)
    L.circ(66, 44, 5); L.circ(78, 52, 5)


@icon("target")
def _(B, D, L):
    B.ring(50, 50, 36, 10); B.ring(50, 50, 18, 10); B.circ(50, 50, 6)
    B.line([(50, 4), (50, 16)], 7); B.line([(50, 84), (50, 96)], 7); B.line([(4, 50), (16, 50)], 7); B.line([(84, 50), (96, 50)], 7)


@icon("folder")
def _(B, D, L):
    B.rr(6, 18, 44, 40, 8); B.rr(6, 26, 94, 86, 11)
    L.rr(12, 31, 88, 34.5, 1.5)


@icon("user_star")
def _(B, D, L):
    bust(B, 38, 42, 1.0); B.circ(76, 28, 19, cut=True)
    B.poly(star(76, 29, 15, 7), soft=3)


@icon("grid")
def _(B, D, L):
    for x in (10, 54):
        for y in (10, 54):
            B.rr(x, y, x + 36, y + 36, 10)
    L.rr(14, 14, 20, 18, 2)


@icon("rock")
def _(B, D, L):
    B.poly([(12, 62), (20, 34), (44, 18), (74, 24), (90, 50), (82, 80), (50, 90), (22, 82)], soft=10)
    D.line([(30, 38), (46, 52), (70, 46)], 2.6); D.line([(46, 52), (50, 80)], 2.6)
    L.poly([(26, 40), (40, 28), (52, 30), (36, 44)])


@icon("paper")
def _(B, D, L):
    B.rr(18, 8, 82, 92, 9); B.poly([(60, 8), (82, 30), (60, 30)], cut=True)
    L.poly([(62, 12), (78, 28), (62, 28)], soft=2)
    D.line([(30, 48), (70, 48)], 4); D.line([(30, 62), (70, 62)], 4); D.line([(30, 76), (54, 76)], 4)


@icon("scissors")
def _(B, D, L):
    B.line([(38, 56), (74, 8)], 11); B.line([(62, 56), (26, 8)], 11)
    B.ring(30, 76, 12, 9); B.ring(70, 76, 12, 9)
    D.circ(50, 50, 3.4)


@icon("tap")
def _(B, D, L):
    B.rr(41, 26, 58, 64, 8.5); B.rr(28, 52, 82, 92, 16)
    B.arc(49.5, 27, 13, 35, 145, 5.5); B.arc(49.5, 27, 23, 40, 140, 5.5)
    D.line([(47, 70), (47, 82)], 2.6); D.line([(58, 70), (58, 82)], 2.6); D.line([(69, 70), (69, 82)], 2.6)


@icon("team_red", RED)
def _(B, D, L):
    B.poly(SHIELD, soft=2)
    L.poly([(50, 24), (66, 32), (66, 46), (50, 66), (34, 46), (34, 32)], soft=2)
    D.poly([(50, 31), (59, 36), (59, 46), (50, 58), (41, 46), (41, 36)])


@icon("team_blue", BLUE)
def _(B, D, L):
    B.poly(SHIELD, soft=2)
    L.poly([(50, 24), (66, 32), (66, 46), (50, 66), (34, 46), (34, 32)], soft=2)
    D.poly([(50, 31), (59, 36), (59, 46), (50, 58), (41, 46), (41, 36)])


@icon("light_red", RED)
def _(B, D, L):
    B.circ(50, 50, 42); L.ell(28, 20, 56, 38)


@icon("light_green", GREEN)
def _(B, D, L):
    B.circ(50, 50, 42); L.ell(28, 20, 56, 38)


@icon("shield_star")
def _(B, D, L):
    B.poly(SHIELD, soft=2); L.poly(star(50, 50, 22, 10), soft=2)


@icon("swap_small")
def _(B, D, L):
    B.line([(20, 20), (78, 78)], 11); B.line([(80, 20), (22, 78)], 11)
    B.line([(10, 36), (36, 10)], 10); B.line([(90, 36), (64, 10)], 10)
    B.circ(18, 82, 7); B.circ(82, 82, 7)


@icon("prev")
def _(B, D, L):
    B.poly([(72, 14), (24, 50), (72, 86)], soft=14)


@icon("next")
def _(B, D, L):
    B.poly([(28, 14), (76, 50), (28, 86)], soft=14)


@icon("rewind")
def _(B, D, L):
    B.rr(12, 16, 25, 84, 5); B.poly([(88, 16), (38, 50), (88, 84)], soft=10)


@icon("fast_forward")
def _(B, D, L):
    B.rr(75, 16, 88, 84, 5); B.poly([(12, 16), (62, 50), (12, 84)], soft=10)


@icon("member")
def _(B, D, L):
    bust(B, 50, 44, 1.05)


@icon("thumbs_up")
def _(B, D, L):
    B.rr(8, 42, 26, 90, 6)
    B.poly([(32, 46), (46, 10), (58, 12), (58, 40), (88, 40), (92, 54), (84, 86), (32, 86)], soft=9)
    L.rr(12, 48, 15.5, 62, 1.5)


@icon("ticket")
def _(B, D, L):
    B.rr(6, 24, 94, 76, 9); B.circ(6, 50, 10, cut=True); B.circ(94, 50, 10, cut=True)
    for y in (30, 40, 50, 60, 70):
        D.circ(68, y, 2.4)
    L.poly(star(36, 50, 13, 6), soft=2)


@icon("user_plus_arrow")
def _(B, D, L):
    bust(B, 38, 44, 1.0); B.circ(76, 28, 19, cut=True)
    B.poly([(76, 14), (88, 28), (80, 28), (80, 40), (72, 40), (72, 28), (64, 28)], soft=3)


# ============================================================================================ rendering
def gradient(b, pal):
    ys, xs = np.nonzero(b > 0.04)
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    yy, xx = np.mgrid[0:N, 0:N]
    t = np.clip(0.65 * (yy - y0) / max(1, y1 - y0) + 0.35 * (xx - x0) / max(1, x1 - x0), 0, 1)
    pos = t * (len(pal) - 1)
    i = np.clip(np.floor(pos).astype(int), 0, len(pal) - 2)
    f = (pos - i)[..., None]
    p = np.array(pal, float)
    return p[i] * (1 - f) + p[i + 1] * f, (y0, y1, x0, x1)


def blur(a, r):
    return np.asarray(Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(r)), float) / 255


def render(fn, pal):
    """Clean 2D gradient icon: crisp silhouette, restrained shading, no glossy/3D effects."""
    B, D, L = Layer(), Layer(), Layer()
    fn(B, D, L)
    body = np.asarray(B.im, float) / 255
    detail = np.asarray(D.im, float) / 255 * (body > 0.02)
    light = np.asarray(L.im, float) / 255 * (body > 0.02)
    rgb, _ = gradient(body, pal)
    dark = np.asarray(pal[-1], float) * 0.38
    rgb = rgb * (1 - detail[..., None] * 0.88) + dark * (detail[..., None] * 0.88)
    rgb = rgb * (1 - light[..., None] * 0.94) + np.asarray(CREAM, float) * (light[..., None] * 0.94)
    # Very subtle inner edge gives the shape definition without a bevel or drop shadow.
    alpha = np.clip(body, 0, 1)
    rgba = np.dstack([np.clip(rgb, 0, 255), alpha * 255]).astype(np.uint8)
    img = Image.fromarray(rgba, "RGBA")
    ys, xs = np.nonzero(alpha > 0.04)
    cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    side = max(xs.max() - xs.min(), ys.max() - ys.min()) * 128 / 102
    box = (int(cx - side / 2), int(cy - side / 2), int(cx + side / 2), int(cy + side / 2))
    return img.crop(box).resize((128, 128), Image.LANCZOS)


# Varied semantic palettes keep the icon set colourful without changing silhouettes.
VIOLET = [(220, 205, 255), (170, 135, 255), (126, 91, 235), (88, 58, 190), (55, 35, 130)]
CYAN = [(190, 246, 255), (105, 220, 245), (50, 177, 220), (28, 125, 180), (16, 78, 125)]
PINK = [(255, 210, 232), (255, 145, 195), (238, 88, 155), (190, 48, 120), (125, 25, 82)]
GOLD = [(255, 239, 170), (255, 205, 90), (244, 157, 45), (205, 105, 24), (135, 62, 12)]
MINT = [(195, 255, 224), (115, 235, 177), (57, 190, 130), (28, 135, 90), (13, 82, 55)]
BLUEGRAD = [(195, 220, 255), (125, 175, 255), (75, 125, 235), (45, 80, 185), (22, 45, 120)]
CORAL = [(255, 218, 195), (255, 163, 125), (240, 112, 82), (190, 65, 48), (120, 35, 30)]
TEAL = [(190, 250, 240), (105, 220, 205), (45, 175, 165), (25, 120, 125), (12, 70, 85)]

def assign_palettes():
    groups = [
        (VIOLET, "sparkles palette diamond skull".split()),
        (CYAN, "signal stopwatch tap gamepad target".split()),
        (PINK, "heart heart_broken gift_box gift bell_ring mail".split()),
        (GOLD, "crown trophy coins wallet star_fill fire bolt".split()),
        (MINT, "leaf check check_outline shield_check shield_star thumbs_up".split()),
        (BLUEGRAD, "headset info help home menu folder grid image link".split()),
        (CORAL, "warning ban cross cross_filled stop".split()),
        (TEAL, "volume speaker mute mic messages message ticket".split()),
    ]
    for pal, names in groups:
        for name in names:
            if name in ICONS:
                fn, _ = ICONS[name]
                ICONS[name] = (fn, pal)
    # Give every remaining icon a different place in the colour rotation too.
    rotation = [VIOLET, CYAN, PINK, GOLD, MINT, BLUEGRAD, CORAL, TEAL]
    for index, (name, (fn, pal)) in enumerate(list(ICONS.items())):
        if pal is KON:
            ICONS[name] = (fn, rotation[index % len(rotation)])

assign_palettes()

# ---------- control panel image (voice panel) ----------
PANEL = [("lock", "LOCK", "Lock VC"), ("unlock", "UNLOCK", "Unlock VC"), ("hide", "HIDE", "Hide VC"),
         ("show", "UNHIDE", "Unhide VC"), ("logout", "VCKICK", "Kick User"),
         ("ban", "BANUSER", "Ban User"), ("user_add", "PERMITUSER", "Permit User"), ("users", "LIMIT", "Set Limit"),
         ("signal", "STATUS", "VC Status"), ("edit", "RENAME", "Rename VC"),
         ("volume", "BITRATE", "Bitrate"), ("crown", "TRANSFER", "Transfer")]


def font(size, bold=True):
    for p in ("/usr/share/fonts/opentype/inter/Inter-Bold.otf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def make_panel():
    W, H = 1150, 580
    cols, tw, th, gx, gy = 5, 195, 150, 20, 25
    img = Image.new("RGBA", (W, H), (10, 7, 3, 255))
    d = ImageDraw.Draw(img)
    f_small, f_big = font(13), font(17)
    for i, (icon_name, cap, label) in enumerate(PANEL):
        r, c = divmod(i, cols)
        x, y = 45 + c * (tw + gx), 45 + r * (th + gy)
        card = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        cd = ImageDraw.Draw(card)
        cd.rounded_rectangle([0, 0, tw - 1, th - 1], radius=24, fill=(28, 19, 8, 255), outline=(72, 46, 12, 255), width=2)
        cd.rounded_rectangle([3, 3, tw - 4, 52], radius=21, fill=(40, 27, 10, 255))   # soft lit top
        img.alpha_composite(card, (x, y))
        ic = Image.open(os.path.join(OUT, f"{icon_name}.png"))
        ic = ic.resize((72, 72), Image.LANCZOS)
        img.alpha_composite(ic, (x + (tw - 72) // 2, y + 14))
        d.text((x + tw / 2, y + 102), cap, font=f_small, fill=(255, 184, 50, 255), anchor="mm")
        d.text((x + tw / 2, y + 124), label, font=f_big, fill=(255, 255, 255, 255), anchor="mm")
    img.convert("RGB").save(os.path.join(ROOT, "assets", "panel.png"), optimize=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):                                  # only the icons the bot uses stay (no leftovers)
        if f.endswith(".png") and f[:-4] not in ICONS:
            os.remove(os.path.join(OUT, f))
    for name, (fn, pal) in ICONS.items():
        render(fn, pal).save(os.path.join(OUT, f"{name}.png"), optimize=True)
    make_panel()
    print("icons:", len(ICONS), "+ panel.png")


if __name__ == "__main__":
    main()
