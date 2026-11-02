#!/usr/bin/env python3
"""
Car projector frame: one big car centred on the frame.

The car picture (white = light, black = dark) is scaled to fill the wall area
the stencils can light, centred on the frame. The frame hides the middle of
the car; the projection draws the rest of it around the frame.

    python3 examples/car_frame.py -o out_car                    # STLs + preview
    python3 examples/car_frame.py -o out_car --preview-only     # preview only
    python3 examples/car_frame.py --car-image my_car.png -o out # another car

Options:
    --car-image PATH   car stencil picture (default: examples/supra.jpg)
    --car-fill F       share of the lit wall area the car fills (default 0.97)
    --car-offset MM    move the car up (+) or down (-) on the wall
Any other projector_frame.py option is passed through (e.g. --depth 80).
"""
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import projector_frame as pf  # noqa: E402

# Landscape photo (a car is wide); the car picture is laid out in real wall
# millimetres, so it is stretched exactly over the lit area.
CAR_DEFAULTS = ["--picture-w", "150", "--picture-h", "100", "--fit", "stretch"]
PX_PER_MM = 4.0


def make_image(p: pf.Params, car: Image.Image, fill=0.97, offset=0.0):
    """The car as large as fits in the lit area, centred on the frame."""
    ext_x, ext_y = pf.projection_extent(p)
    width = min(2 * ext_x * fill, 2 * ext_y * fill * car.width / car.height)
    return pf.compose_wall_image(car, p, width, (0.0, offset), px_per_mm=PX_PER_MM)


def pop_option(argv, name, default, conv=str):
    if name in argv:
        i = argv.index(name)
        value = conv(argv[i + 1])
        del argv[i:i + 2]
        return value
    return default


def main():
    argv = sys.argv[1:]
    car_path = pop_option(argv, "--car-image", os.path.join(HERE, "supra.jpg"))
    fill = pop_option(argv, "--car-fill", 0.97, float)
    offset = pop_option(argv, "--car-offset", 0.0, float)
    argv = CAR_DEFAULTS + argv
    args = pf.build_argparser().parse_args(argv)
    p = pf.params_from_args(args)
    os.makedirs(args.out, exist_ok=True)
    img_path = os.path.join(args.out, "car_image.png")
    make_image(p, Image.open(car_path), fill, offset).save(img_path)
    print(f"Wrote {img_path}")
    return pf.main([img_path] + argv)


if __name__ == "__main__":
    sys.exit(main())
