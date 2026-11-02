#!/usr/bin/env python3
"""
Car-themed projector frame.

Draws a car-silhouette image laid out in real wall millimetres around the
frame (the centre of any image is hidden behind the frame, so the cars sit
just above and below it where the projection is sharpest), then runs
projector_frame.py with it.

    python3 examples/car_frame.py -o out_car            # STLs + preview
    python3 examples/car_frame.py -o out_car --preview-only

Any extra projector_frame.py options are passed through (e.g. --picture-w 130).
"""
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import projector_frame as pf  # noqa: E402

# Defaults tuned for a car silhouette: deeper frame + finer hole grid gives
# more rows of dots on the wall, and crisp on/off holes read best for shapes.
CAR_DEFAULTS = [
    "--picture-w", "150", "--picture-h", "100",   # landscape suits a car
    "--depth", "70", "--light-z", "58",
    "--pitch", "2.2", "--hole-min", "1.0", "--hole-max", "1.5",
    "--mode", "threshold", "--threshold", "0.5",
    "--fit", "stretch", "--blur", "0.3",
    "--cable-gap", "2.5",          # narrower wire channel through the reflection
]

PX_PER_MM = 3.0

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
    light = np.array([0.0, 0.0, p.light_z])
    centers = np.array([c[0] for c in pf.hole_grid(p)])
    xy = pf.project_to_wall(centers, light)
    ext_x = float(np.abs(xy[:, 0]).max()) + p.pitch * 2
    ext_y = float(np.abs(xy[:, 1]).max()) + p.pitch * 2
    w, h = int(2 * ext_x * PX_PER_MM), int(2 * ext_y * PX_PER_MM)
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)

    def px(x_mm, y_mm):
        return w / 2 + x_mm * PX_PER_MM, h / 2 - y_mm * PX_PER_MM

    half_w, half_h = p.picture_w / 2, p.picture_h / 2
    # Rays through the top wall land in a wedge above the frame whose half
    # width grows as (W/H) * y. That wedge has the densest dots, so size the
    # car to fit inside it. y_in = inner edge of the projection.
    y_in = half_h * p.light_z / (p.light_z - (p.back_t + p.bottom_margin))
    aspect = half_w / half_h
    gap = 2.0
    fit = aspect * (y_in + gap) / max(0.05, 0.5 - 0.1 * aspect)
    car_len = min(0.95 * fit, 2 * ext_x * 0.8)
    # Car above the frame, and its mirror reflection below it, wheels towards
    # the frame where the dots are densest. Solid silhouette (no window):
    # fine details get lost in the sparse outer rows.
    cy_mm = y_in + gap + (0.17 - 0.007) * car_len
    draw_car(d, *px(0, cy_mm), car_len * PX_PER_MM)
    draw_car(d, *px(0, -cy_mm), car_len * PX_PER_MM, mirror_v=True)

    # speed lines on the left and right walls
    xs_in = p.picture_w / 2 * p.light_z / (p.light_z - (p.back_t + p.bottom_margin))
    for side in (-1, 1):
        for k, yy in enumerate(np.linspace(-half_h * 0.8, half_h * 0.8, 5)):
            length = (ext_x - xs_in) * (0.35 + 0.25 * math.sin(k * 1.7 + side))
            a = side * (xs_in + 4)
            b = side * (xs_in + 4 + length)
            d.rectangle((*px(min(a, b), yy + 3), *px(max(a, b), yy - 3)), fill=255)
    return img


def main():
    argv = CAR_DEFAULTS + [a for a in sys.argv[1:]]
    args = pf.build_argparser().parse_args(argv)
    p = pf.Params(**{f.name: getattr(args, f.name) for f in pf.fields(pf.Params)})
    out = args.out
    os.makedirs(out, exist_ok=True)
    img_path = os.path.join(out, "car_image.png")
    make_image(p).save(img_path)
    print(f"Wrote {img_path}")
    return pf.main([img_path] + argv)


if __name__ == "__main__":
    sys.exit(main())
