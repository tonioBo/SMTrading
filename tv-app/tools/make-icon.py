"""Draws tv-app/icon.png (512x423, the size Samsung asks for a TV app icon) with the
standard library only: the SMTrading mark of web/public/favicon.svg (green rounded square,
three bars, the rising arrow) centred on the TV screens' dark background. 4x supersampling
for smooth edges. Usage: python3 tv-app/tools/make-icon.py"""

import math
import struct
import zlib
from pathlib import Path

W, H, SS = 512, 423, 4
BG_TOP, BG_BOTTOM = (7, 11, 25), (3, 5, 12)
GREEN, WHITE = (5, 150, 105), (255, 255, 255)
MARK = 300  # side of the mark in pixels (the 32-unit favicon grid scaled)
OX, OY = (W - MARK) / 2, (H - MARK) / 2
U = MARK / 32


def rounded_rect(x, y, x0, y0, w, h, r):
    cx = min(max(x, x0 + r), x0 + w - r)
    cy = min(max(y, y0 + r), y0 + h - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r and x0 <= x <= x0 + w and y0 <= y <= y0 + h


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


# favicon.svg geometry (32-unit grid)
BARS = [(5.5, 21, 5, 5), (13.5, 17, 5, 6), (21.5, 13, 5, 8)]
STEMS = [((8, 19), (8, 27)), ((16, 15), (16, 25)), ((24, 11), (24, 23))]
ARROW = [((4.5, 14.5), (12.5, 9.5)), ((12.5, 9.5), (17.5, 11.5)), ((17.5, 11.5), (26, 4.5)),
         ((21, 4), (27, 4)), ((27, 4), (27, 10))]


def colour(x, y):
    """The colour at (x, y) in pixels (a sample point)."""
    t = y / H
    bg = tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
    gx, gy = (x - OX) / U, (y - OY) / U
    if not rounded_rect(gx, gy, 0, 0, 32, 32, 7):
        return bg
    for bx, by, bw, bh in BARS:
        if rounded_rect(gx, gy, bx, by, bw, bh, 1):
            return WHITE
    for (ax, ay), (bx, by) in STEMS:
        if seg_dist(gx, gy, ax, ay, bx, by) <= 0.75:
            return WHITE
    for (ax, ay), (bx, by) in ARROW:
        if seg_dist(gx, gy, ax, ay, bx, by) <= 1.1:
            return WHITE
    return GREEN


def render() -> bytes:
    rows = []
    for y in range(H):
        row = bytearray([0])  # filter: none
        for x in range(W):
            acc = [0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    c = colour(x + (sx + 0.5) / SS, y + (sy + 0.5) / SS)
                    acc[0] += c[0]
                    acc[1] += c[1]
                    acc[2] += c[2]
            row += bytes(round(v / (SS * SS)) for v in acc)
        rows.append(bytes(row))
    return b"".join(rows)


def png(raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "icon.png"
    out.write_bytes(png(render()))
    print(f"scritto {out} ({W}x{H})")
