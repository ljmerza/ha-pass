"""Generate the Home Assistant add-on store assets — icon.png and logo.png.

Unlike generate_icons.py, which the Dockerfile runs at build time for the PWA
icons, these two files are committed to the repo root: the Supervisor reads them
straight from the repository, never from the built image. Run this by hand and
commit the output whenever the artwork changes — it is deterministic, so a
re-run with no edits produces byte-identical files.

Shapes and colours come from generate_icons.py so the store listing, the PWA
icon and the app itself stay the same product.
"""
import math
import os

from generate_icons import (
    ICON_B,
    ICON_G,
    ICON_R,
    _house_pixels,
    encode_rgba_png,
    make_maskable_png,
)


# HA recommends a 1:1 128x128 icon and a ~250x100 logo.
ICON_SIZE = 128
LOGO_W, LOGO_H = 250, 100

# Supersampling factor — the logo has curves and diagonals, so it is rasterised
# at SS x SS subsamples per pixel and box-filtered down into the alpha channel.
SS = 4

# Logo lockup: house mark on the left, "HomePass" wordmark to its right.
# _house_pixels reserves 15% padding inside its box, so the drawn house is
# MARK_BOX * 0.7 tall and the padding doubles as the logo's left/top margin.
MARK_BOX = 80
MARK_X, MARK_Y = 2, 10
WORD_X = 86          # left edge of the wordmark ink
WORD_BASELINE = 64   # baseline, chosen so the caps centre on the house
CAP = 28.0           # cap height; the widths below are all multiples of it
X_HEIGHT = 0.72 * CAP
STROKE = 0.155 * CAP
LETTER_GAP = 0.09 * CAP


# ── Primitive shapes ────────────────────────────────────────
# Every glyph is a union of axis-aligned rectangles and ring sectors. Angles are
# degrees CCW from east in a y-up frame (the raster y axis points down).

def _rect(x0: float, y0: float, x1: float, y1: float):
    return ("rect", x0, y0, x1, y1)


def _ring(cx: float, cy: float, ro: float, ri: float, a0: float = 0.0, a1: float = 360.0):
    return ("ring", cx, cy, ro, ri, a0, a1)


def _bbox(shape) -> tuple[float, float, float, float]:
    if shape[0] == "rect":
        return shape[1], shape[2], shape[3], shape[4]
    _, cx, cy, ro, _ri, _a0, _a1 = shape
    return cx - ro, cy - ro, cx + ro, cy + ro


def _hit(shape, x: float, y: float) -> bool:
    if shape[0] == "rect":
        _, x0, y0, x1, y1 = shape
        return x0 <= x <= x1 and y0 <= y <= y1

    _, cx, cy, ro, ri, a0, a1 = shape
    dx, dy = x - cx, cy - y  # flip y so angles read counter-clockwise
    dist = math.hypot(dx, dy)
    if not (ri <= dist <= ro):
        return False

    span = (a1 - a0) % 360.0 or 360.0
    ang = math.degrees(math.atan2(dy, dx)) % 360.0
    return (ang - a0) % 360.0 <= span


# ── Glyphs ──────────────────────────────────────────────────
# Each builder returns (shapes, advance width) for its ink starting at x0 with
# the baseline at `base`. Geometric/Futura-ish: circular bowls, single-storey a.

def _glyph_H(x0: float, base: float):
    w = 0.68 * CAP
    top = base - CAP
    return [
        _rect(x0, top, x0 + STROKE, base),
        _rect(x0 + w - STROKE, top, x0 + w, base),
        _rect(x0, base - (CAP + STROKE) / 2, x0 + w, base - (CAP - STROKE) / 2),
    ], w


def _glyph_o(x0: float, base: float):
    r = X_HEIGHT / 2
    return [_ring(x0 + r, base - r, r, r - STROKE)], 2 * r


def _glyph_m(x0: float, base: float):
    r = 0.40 * X_HEIGHT
    top = base - X_HEIGHT
    arch_cy = top + r
    return [
        _rect(x0, top, x0 + STROKE, base),
        _ring(x0 + r, arch_cy, r, r - STROKE, 0, 180),
        _rect(x0 + 2 * r - STROKE, top, x0 + 2 * r, base),
        _ring(x0 + 3 * r - STROKE, arch_cy, r, r - STROKE, 0, 180),
        _rect(x0 + 4 * r - 2 * STROKE, top, x0 + 4 * r - STROKE, base),
    ], 4 * r - STROKE


def _glyph_e(x0: float, base: float):
    r = X_HEIGHT / 2
    cy = base - r
    return [
        # Ring drawn CCW from 340 deg round to 300 deg, leaving the usual
        # lower-right aperture open.
        _ring(x0 + r, cy, r, r - STROKE, 340, 300),
        _rect(x0, cy - STROKE / 2, x0 + 2 * r, cy + STROKE / 2),
    ], 2 * r


def _glyph_P(x0: float, base: float):
    r = 0.30 * CAP
    # Half-circle bowl centred inside the stem, so the two merge without a seam.
    return [
        _rect(x0, base - CAP, x0 + STROKE, base),
        _ring(x0 + STROKE / 2, base - CAP + r, r, r - STROKE, -90, 90),
    ], STROKE / 2 + r


def _glyph_a(x0: float, base: float):
    r = X_HEIGHT / 2
    return [
        _ring(x0 + r, base - r, r, r - STROKE),
        _rect(x0 + 2 * r - STROKE, base - X_HEIGHT, x0 + 2 * r, base),
    ], 2 * r


def _glyph_s(x0: float, base: float):
    r = X_HEIGHT / 4
    return [
        # Upper bowl: CCW from the right terminal over the top and down the left.
        _ring(x0 + r, base - X_HEIGHT + r, r, r - STROKE, 350, 270),
        # Lower bowl: CCW from the left terminal along the bottom and up the right.
        _ring(x0 + r, base - r, r, r - STROKE, 190, 90),
    ], 2 * r


GLYPHS = {
    "H": _glyph_H,
    "o": _glyph_o,
    "m": _glyph_m,
    "e": _glyph_e,
    "P": _glyph_P,
    "a": _glyph_a,
    "s": _glyph_s,
}


def _wordmark(text: str, x0: float, base: float) -> list:
    shapes = []
    x = x0
    for ch in text:
        glyph, advance = GLYPHS[ch](x, base)
        shapes.extend(glyph)
        x += advance + LETTER_GAP
    return shapes


# ── Rasteriser ──────────────────────────────────────────────

def _blend(cov: list[list[int]], px: int, py: int, hits: int) -> None:
    """Keep the strongest coverage — shapes are a union, not a sum."""
    if 0 <= px < LOGO_W and 0 <= py < LOGO_H and hits > cov[py][px]:
        cov[py][px] = hits


def _draw_shapes(cov: list[list[int]], shapes: list) -> None:
    for shape in shapes:
        bx0, by0, bx1, by1 = _bbox(shape)
        for py in range(max(0, int(by0)), min(LOGO_H, int(by1) + 2)):
            for px in range(max(0, int(bx0)), min(LOGO_W, int(bx1) + 2)):
                hits = 0
                for sy in range(SS):
                    y = py + (sy + 0.5) / SS
                    for sx in range(SS):
                        if _hit(shape, px + (sx + 0.5) / SS, y):
                            hits += 1
                _blend(cov, px, py, hits)


def _draw_mark(cov: list[list[int]]) -> None:
    """Downsample the shared house silhouette, rendered at SS x scale."""
    rows = _house_pixels(MARK_BOX * SS, bg_opaque=False)
    for py in range(MARK_BOX):
        for px in range(MARK_BOX):
            hits = 0
            for sy in range(SS):
                row = rows[py * SS + sy]
                for sx in range(SS):
                    # +1 skips the row's filter byte, +3 picks the alpha channel
                    if row[1 + 4 * (px * SS + sx) + 3]:
                        hits += 1
            _blend(cov, MARK_X + px, MARK_Y + py, hits)


def make_logo_png() -> bytes:
    """House mark plus the HomePass wordmark, primary on a transparent field."""
    cov = [[0] * LOGO_W for _ in range(LOGO_H)]
    _draw_mark(cov)
    _draw_shapes(cov, _wordmark("HomePass", WORD_X, WORD_BASELINE))

    total = SS * SS
    rows = []
    for py in range(LOGO_H):
        row = bytearray(b"\x00")  # filter byte
        for px in range(LOGO_W):
            alpha = (cov[py][px] * 255 + total // 2) // total
            row.extend([ICON_R, ICON_G, ICON_B, alpha] if alpha else [0, 0, 0, 0])
        rows.append(bytes(row))

    return encode_rgba_png(LOGO_W, LOGO_H, rows)


if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))

    # Same silhouette and solid #F2F0E9 field as the maskable PWA icon.
    icon_path = os.path.join(root, "icon.png")
    with open(icon_path, "wb") as f:
        f.write(make_maskable_png(ICON_SIZE))
    print(f"Generated {icon_path} ({ICON_SIZE}x{ICON_SIZE})")

    logo_path = os.path.join(root, "logo.png")
    with open(logo_path, "wb") as f:
        f.write(make_logo_png())
    print(f"Generated {logo_path} ({LOGO_W}x{LOGO_H})")
