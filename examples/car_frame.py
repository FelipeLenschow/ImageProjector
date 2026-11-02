#!/usr/bin/env python3
"""
Car-themed projector frame.

Draws a car-silhouette image laid out in real wall millimetres around the
frame (the centre of any image is hidden behind the frame): a car above the
frame, its mirror reflection below and speed lines on the sides. Then it runs
projector_frame.py with it, which cuts the image as continuous stencils into
the four side panels.

    python3 examples/car_frame.py -o out_car            # STLs + preview
    python3 examples/car_frame.py -o out_car --preview-only

Any extra projector_frame.py options are passed through (e.g. --depth 70).
"""
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import projector_frame as pf  # noqa: E402

# Landscape photo (a car is wide) and the image stretched to exactly the
# wall area the stencils light, so the car is drawn in real millimetres.
CAR_DEFAULTS = [
    "--picture-w", "150", "--picture-h", "100",
    "--fit", "stretch",
]

PX_PER_MM = 4.0

# Side profile of a generic sports coupe, nose to the right.
# x: 0..1 along the length, y: 0..~0.32 up from the ground.
BODY = [
    (0.02, 0.10), (0.01, 0.17), (0.04, 0.21), (0.10, 0.225),   # tail
    (0.27, 0.24), (0.37, 0.315), (0.47, 0.33), (0.58, 0.32),   # rear deck, roof
    (0.68, 0.25), (0.86, 0.215), (0.97, 0.185), (0.995, 0.14),  # windscreen, bonnet, nose
    (0.985, 0.09), (0.93, 0.075), (0.88, 0.075),               # front bumper
    (0.87, 0.105), (0.84, 0.135), (0.79, 0.15), (0.74, 0.135),  # front wheel arch
    (0.71, 0.105), (0.70, 0.075),
    (0.32, 0.075), (0.31, 0.105), (0.28, 0.135), (0.23, 0.15),  # rear wheel arch
    (0.18, 0.135), (0.15, 0.105), (0.14, 0.075), (0.04, 0.08),
]
WINDOW = [(0.30, 0.235), (0.38, 0.295), (0.47, 0.31), (0.56, 0.30),
          (0.645, 0.245), (0.47, 0.235)]
WHEELS = [(0.23, 0.075), (0.79, 0.075)]  # centre (x, y)
WHEEL_R = 0.068


def draw_car(draw, cx, cy, length, flip=False, mirror_v=False, window=False, value=255):
    """Draw the car centred at (cx, cy) image pixels, `length` px long."""
    def tf(pts):
        out = []
        for x, y in pts:
            x = x - 0.5
            if flip:
                x = -x
            dy = (y - 0.17) * length
            out.append((cx + x * length, cy + dy if mirror_v else cy - dy))
        return out

    draw.polygon(tf(BODY), fill=value)
    if window:
        draw.polygon(tf(WINDOW), fill=0)
    for wx, wy in WHEELS:
        (px, py), = tf([(wx, wy)])
        r = WHEEL_R * length
        draw.ellipse((px - r, py - r, px + r, py + r), fill=value)
        ri = r * 0.45
        draw.ellipse((px - ri, py - ri, px + ri, py + ri), fill=0)


def make_image(p: pf.Params):
    ext_x, ext_y = pf.projection_extent(p)
    w, h = int(2 * ext_x * PX_PER_MM), int(2 * ext_y * PX_PER_MM)
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)

    def px(x_mm, y_mm):
        return w / 2 + x_mm * PX_PER_MM, h / 2 - y_mm * PX_PER_MM

    half_w, half_h = p.picture_w / 2, p.picture_h / 2
    z_lo, _ = p.z_band()
    t_min = p.light_z / (p.light_z - z_lo)
    y_in, x_in = half_h * t_min, half_w * t_min   # inner edge of the lit area
    # Car above the frame, wheels near the frame where the light is brightest,
    # and its mirror reflection below.
    room = ext_y - y_in - 4
    car_len = min(0.8 * room / 0.33, 2 * ext_x * 0.85)
    cy_mm = y_in + 3 + (0.17 - 0.007) * car_len
    draw_car(d, *px(0, cy_mm), car_len * PX_PER_MM, window=True)
    draw_car(d, *px(0, -cy_mm), car_len * PX_PER_MM, mirror_v=True, window=True)
    # speed lines left and right
    for side in (-1, 1):
        for k, yy in enumerate(np.linspace(-half_h * 0.75, half_h * 0.75, 5)):
            length = (ext_x - x_in) * (0.35 + 0.25 * math.sin(k * 1.7 + side))
            a, b = side * (x_in + 4), side * (x_in + 4 + length)
            r = 2.5 * PX_PER_MM
            (x0, y0), (x1, y1) = px(min(a, b), yy + 2.5), px(max(a, b), yy - 2.5)
            d.rounded_rectangle((x0, y0, x1, y1), radius=r, fill=255)
    return img


def main():
    argv = CAR_DEFAULTS + [a for a in sys.argv[1:]]
    args = pf.build_argparser().parse_args(argv)
    p = pf.params_from_args(args)
    out = args.out
    os.makedirs(out, exist_ok=True)
    img_path = os.path.join(out, "car_image.png")
    make_image(p).save(img_path)
    print(f"Wrote {img_path}")
    return pf.main([img_path] + argv)


if __name__ == "__main__":
    sys.exit(main())
