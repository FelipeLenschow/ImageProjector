#!/usr/bin/env python3
"""
Projector picture frame STL generator.

A wall-hung picture frame whose deep ("tall") side walls are perforated with
small holes. A single point-like LED sits in the middle of the frame, behind
the picture. Its light escapes only through the holes in the side walls and
lands on the wall *around* the frame, drawing an image (a halo) on it.

Every hole is placed by ray tracing: for each cell of a regular grid on the
side walls we trace the ray LED -> cell -> wall, sample the target image where
that ray lands, and cut a hole sized by the image brightness there. Each hole
is cut along its own ray direction, so the light passes cleanly through the
wall even at grazing angles.

Coordinates (assembled frame):
    z = 0 is the wall, +z points out into the room.
    x is horizontal, y is vertical (+y up), origin at the frame centre.

Parts written to the output directory:
    body.stl     back plate (keyhole hangers, cable exit) + perforated walls
    backer.stl   opaque plate behind the picture, carries the LED post
    bezel.stl    the visible front frame with the picture window
    assembly.stl all parts in their assembled position (for viewing only)
    preview.png  simulated projection on the wall
    target.png   the image as it was mapped onto the wall (for comparison)

Assembly, from the wall outwards:
    body -> backer (sits on the wall rims, LED post pointing at the wall)
         -> picture -> bezel (slides over the front of the body)

Requires: numpy, pillow, manifold3d   (pip install -r requirements.txt)
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import time
from dataclasses import dataclass, fields

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

try:
    from manifold3d import CrossSection, Manifold, OpType
except ImportError:  # allow --preview-only without manifold3d
    CrossSection = Manifold = OpType = None


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------

@dataclass
class Params:
    # Picture / overall size (mm). The body footprint equals the picture size.
    picture_w: float = 100.0      # width of the printed photo
    picture_h: float = 150.0      # height of the printed photo
    picture_t: float = 0.6        # thickness of photo (+ optional acetate)
    window_overlap: float = 5.0   # how much the bezel covers the photo edge

    # Body
    depth: float = 55.0           # depth of the side walls, wall -> front rim
    wall_t: float = 2.0           # side wall thickness
    back_t: float = 3.0           # back plate thickness

    # Light
    light_z: float = 42.0         # height of the light's emitting point above the wall
    led_d: float = 5.0            # LED body diameter (5 mm LED by default)
    led_pocket: float = 5.0       # depth of the LED pocket in the post
    led_protrusion: float = 3.0   # emitter distance below the post tip
    post_d: float = 10.0          # LED post diameter

    # Holes
    pitch: float = 2.6            # grid spacing of the holes on the walls
    hole_min: float = 0.9         # smallest hole that is still cut
    hole_max: float = 1.8         # largest hole (pitch - hole_max = min web)
    hole_shape: str = "circle"    # circle | square | diamond
    hole_segments: int = 10       # polygon segments for circular holes
    corner_margin: float = 3.0    # keep holes away from the wall corners
    bottom_margin: float = 2.5    # keep holes away from the back plate
    max_magnification: float = 4.0  # limits how far from the frame dots land
    cable_gap: float = 4.0        # no holes within +-this of x=0 on the bottom wall

    # Image mapping
    mode: str = "halftone"        # halftone | threshold
    threshold: float = 0.15       # brightness below this -> no hole
    gamma: float = 1.0
    invert: bool = False
    fit: str = "cover"            # cover | contain | stretch
    blur: float = 0.6             # pre-blur, in units of the hole pitch

    # Bezel (front frame)
    bezel_face_t: float = 2.4
    bezel_skirt_t: float = 2.0
    bezel_flange: float = 6.0     # face width beyond the skirt
    skirt_overlap: float = 6.0    # how far the skirt slides over the body
    fit_clearance: float = 0.25   # gap between skirt and body (press fit)
    window_chamfer: float = 1.0

    # Backer
    backer_t: float = 2.4
    wire_groove_w: float = 2.8
    wire_groove_d: float = 1.2

    # Keyhole hangers (for a screw / nail in the wall)
    screw_head_d: float = 8.5     # head diameter (+ clearance)
    screw_shank_d: float = 4.5    # shank diameter (+ clearance)
    screw_head_h: float = 3.0     # room for the head
    keyhole_lip: float = 2.0      # plastic between wall and screw head
    keyhole_slot: float = 10.0    # slide distance
    keyhole_spacing: float = 0.5  # keyhole x spacing as fraction of width (0 = one, centred)

    # Cable exit
    cable_hole_d: float = 4.0

    def derived(self):
        W, H = self.picture_w, self.picture_h
        z_lo = self.back_t + self.bottom_margin
        z_hi = self.light_z * (1.0 - 1.0 / self.max_magnification)
        z_hi = min(z_hi, self.depth - self.skirt_overlap - 2.0)
        post_tip = self.light_z + self.led_protrusion
        post_len = self.depth - post_tip
        return dict(W=W, H=H, z_lo=z_lo, z_hi=z_hi, post_tip=post_tip, post_len=post_len)

    def validate(self):
        d = self.derived()
        errs = []
        if d["z_hi"] <= d["z_lo"] + self.pitch:
            errs.append("No room for holes: raise light_z / depth or max_magnification.")
        if self.light_z >= self.depth:
            errs.append("light_z must be below the front rim (depth).")
        if d["post_len"] < self.led_pocket + 1:
            errs.append("LED post too short: lower light_z or increase depth.")
        if self.hole_max >= self.pitch - 0.4:
            errs.append("hole_max leaves less than 0.4 mm web; increase pitch.")
        if self.hole_min > self.hole_max:
            errs.append("hole_min > hole_max.")
        if self.mode not in ("halftone", "threshold"):
            errs.append("mode must be halftone or threshold")
        if self.fit not in ("cover", "contain", "stretch"):
            errs.append("fit must be cover, contain or stretch")
        if self.hole_shape not in ("circle", "square", "diamond"):
            errs.append("hole_shape must be circle, square or diamond")
        if errs:
            raise SystemExit("Invalid parameters:\n  " + "\n  ".join(errs))


# --------------------------------------------------------------------------
# Geometry of the perforated walls
# --------------------------------------------------------------------------

EZ = np.array([0.0, 0.0, 1.0])


def walls(p: Params):
    """Four side walls: (name, outward normal, mid-plane offset, usable half length)."""
    W, H, t = p.picture_w, p.picture_h, p.wall_t
    res = []
    for name, n, off, inner_half in (
        ("right", (1, 0, 0), W / 2 - t / 2, H / 2 - t),
        ("left", (-1, 0, 0), W / 2 - t / 2, H / 2 - t),
        ("top", (0, 1, 0), H / 2 - t / 2, W / 2 - t),
        ("bottom", (0, -1, 0), H / 2 - t / 2, W / 2 - t),
    ):
        n = np.array(n, float)
        eu = np.cross(EZ, n)  # along the wall; [eu, ez, n] is right handed
        res.append((name, n, eu, off, inner_half - t / 2 - p.corner_margin))
    return res


def hole_grid(p: Params):
    """All candidate hole cells: centre, normal, u-axis, wall name."""
    d = p.derived()
    zs = np.arange(d["z_lo"] + p.pitch / 2, d["z_hi"] - p.pitch / 2 + 1e-9, p.pitch)
    cells = []
    for name, n, eu, off, half in walls(p):
        n_u = int(math.floor(2 * half / p.pitch))
        us = (np.arange(n_u) - (n_u - 1) / 2) * p.pitch
        for u in us:
            if name == "bottom" and abs(u) < p.cable_gap:
                continue
            for z in zs:
                c = n * off + eu * u + EZ * z
                cells.append((c, n, eu, name))
    return cells


def project_to_wall(points, light):
    """Project 3D points through the light onto the plane z=0."""
    points = np.atleast_2d(points)
    zl = light[2]
    t = zl / (zl - points[:, 2])
    return light[None, :2] + t[:, None] * (points[:, :2] - light[None, :2])


def hole_outline(shape, dia, segments):
    """2D outline (local u,v) of a hole."""
    r = dia / 2
    if shape == "circle":
        a = np.linspace(0, 2 * np.pi, segments, endpoint=False)
        return np.stack([r * np.cos(a), r * np.sin(a)], 1)
    if shape == "square":
        return np.array([[-r, -r], [r, -r], [r, r], [-r, r]])
    # diamond: self supporting when printed (45 deg overhangs)
    return np.array([[0, -r], [r, 0], [0, r], [-r, 0]]) * math.sqrt(2) * 0.85


# --------------------------------------------------------------------------
# Image handling
# --------------------------------------------------------------------------

def builtin_pattern(name, size=1024):
    yy, xx = np.mgrid[-1:1:size * 1j, -1:1:size * 1j]
    r = np.hypot(xx, yy)
    th = np.arctan2(yy, xx)
    if name == "sunburst":
        img = (np.cos(th * 18) > 0.25).astype(float)
        img *= (r > 0.15)
    elif name == "rings":
        img = (np.sin(r * 40) > 0).astype(float)
    elif name == "stars":
        rng = np.random.default_rng(7)
        img = np.zeros((size, size))
        for _ in range(60):
            cx, cy = rng.uniform(0, size, 2)
            rad = rng.uniform(4, 22)
            img[(np.hypot(np.arange(size)[None, :] - cx, np.arange(size)[:, None] - cy)) < rad] = 1
    elif name == "waves":
        img = (np.sin(xx * 12 + np.sin(yy * 6) * 2) > 0.3).astype(float)
    else:
        raise SystemExit(f"unknown pattern {name!r}")
    return Image.fromarray((img * 255).astype(np.uint8), "L")


class WallImage:
    """Maps wall coordinates (mm) to image brightness in [0, 1]."""

    def __init__(self, img: Image.Image, p: Params, extent_x, extent_y, mm_per_cell):
        img = img.convert("L")
        iw, ih = img.size
        wall_w, wall_h = 2 * extent_x, 2 * extent_y
        sx, sy = iw / wall_w, ih / wall_h  # px per mm
        if p.fit == "cover":      # image fills the whole projection area (crops)
            sx = sy = min(sx, sy)
        elif p.fit == "contain":  # whole image visible (may leave dark margins)
            sx = sy = max(sx, sy)
        self.sx, self.sy = sx, sy
        self.iw, self.ih = iw, ih
        radius = float(p.blur * mm_per_cell * (sx + sy) / 2)
        if radius > 0.3:
            img = img.filter(ImageFilter.GaussianBlur(radius))
        a = np.asarray(img, dtype=float) / 255.0
        if p.invert:
            a = 1.0 - a
        a = np.clip(a, 0, 1) ** p.gamma
        self.a = a

    def sample(self, xy):
        """Bilinear sample; outside the image counts as black."""
        x = xy[:, 0] * self.sx + self.iw / 2 - 0.5
        y = -xy[:, 1] * self.sy + self.ih / 2 - 0.5
        inside = (x >= -0.5) & (x <= self.iw - 0.5) & (y >= -0.5) & (y <= self.ih - 0.5)
        x = np.clip(x, 0, self.iw - 1)
        y = np.clip(y, 0, self.ih - 1)
        x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
        x1, y1 = np.minimum(x0 + 1, self.iw - 1), np.minimum(y0 + 1, self.ih - 1)
        fx, fy = x - x0, y - y0
        a = self.a
        v = (a[y0, x0] * (1 - fx) * (1 - fy) + a[y0, x1] * fx * (1 - fy)
             + a[y1, x0] * (1 - fx) * fy + a[y1, x1] * fx * fy)
        return np.where(inside, v, 0.0)

    def render(self, extent_x, extent_y, px_per_mm):
        """The image as mapped on the wall, for comparison with the preview."""
        w, h = int(2 * extent_x * px_per_mm), int(2 * extent_y * px_per_mm)
        gx, gy = np.meshgrid((np.arange(w) + 0.5) / px_per_mm - extent_x,
                             extent_y - (np.arange(h) + 0.5) / px_per_mm)
        v = self.sample(np.stack([gx.ravel(), gy.ravel()], 1)).reshape(h, w)
        return Image.fromarray((v * 255).astype(np.uint8), "L")


def hole_diameters(brightness, p: Params):
    b = np.asarray(brightness)
    if p.mode == "threshold":
        return np.where(b >= max(p.threshold, 1e-6), p.hole_max, 0.0)
    k = np.clip((b - p.threshold) / max(1e-6, 1 - p.threshold), 0, 1)
    d = np.sqrt(p.hole_min ** 2 + (p.hole_max ** 2 - p.hole_min ** 2) * k)  # area ~ brightness
    return np.where(b > p.threshold, d, 0.0)


# --------------------------------------------------------------------------
# Hole layout
# --------------------------------------------------------------------------

@dataclass
class Hole:
    center: np.ndarray
    normal: np.ndarray
    eu: np.ndarray
    dia: float


def layout_holes(p: Params, img: Image.Image):
    light = np.array([0.0, 0.0, p.light_z])
    cells = hole_grid(p)
    centers = np.array([c[0] for c in cells])
    wall_xy = project_to_wall(centers, light)
    ext_x = float(np.abs(wall_xy[:, 0]).max()) + p.pitch * 2
    ext_y = float(np.abs(wall_xy[:, 1]).max()) + p.pitch * 2
    # typical size of one cell on the wall, used for anti-aliasing blur
    mm_per_cell = p.pitch * p.light_z / (p.light_z - np.median(centers[:, 2]))
    wimg = WallImage(img, p, ext_x, ext_y, mm_per_cell)
    dias = hole_diameters(wimg.sample(wall_xy), p)
    holes = [Hole(c[0], c[1], c[2], float(dd)) for c, dd in zip(cells, dias) if dd > 0]
    return holes, light, wimg, (ext_x, ext_y), len(cells)


# --------------------------------------------------------------------------
# Preview rendering (simulated projection)
# --------------------------------------------------------------------------

def render_preview(p: Params, holes, light, extent, path, px_per_mm=2.0):
    """Simulated projection: every hole drawn as the light spot it casts on the wall."""
    ext_x, ext_y = extent
    w, h = int(2 * ext_x * px_per_mm), int(2 * ext_y * px_per_mm)
    zl = light[2]
    out = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(out)

    def to_px(xy):
        return [((x + ext_x) * px_per_mm, (ext_y - y) * px_per_mm) for x, y in xy]

    seg = max(p.hole_segments, 12)
    items = []
    for hole in holes:
        ol = hole_outline(p.hole_shape, hole.dia, seg)
        pts3 = hole.center[None, :] + ol[:, :1] * hole.eu[None, :] + ol[:, 1:] * EZ[None, :]
        xy = project_to_wall(pts3, light)
        cx, cy = xy.mean(0)
        r = math.sqrt(cx * cx + cy * cy + zl * zl)
        items.append(((zl / r) ** 4, xy))
    if items:
        imax = max(i for i, _ in items)
        for inten, xy in sorted(items, key=lambda t: t[0]):
            g = (inten / imax) ** 0.45
            d.polygon(to_px(xy), fill=(int(255 * g), int(235 * g), int(190 * g)))
    bx = p.picture_w / 2 + p.fit_clearance + p.bezel_skirt_t + p.bezel_flange
    by = p.picture_h / 2 + p.fit_clearance + p.bezel_skirt_t + p.bezel_flange
    wx = p.picture_w / 2 - p.window_overlap
    wy = p.picture_h / 2 - p.window_overlap
    d.rectangle(to_px([(-bx, by), (bx, -by)]), fill=(60, 45, 35))
    d.rectangle(to_px([(-wx, wy), (wx, -wy)]), fill=(140, 140, 150))
    out.save(path)


# --------------------------------------------------------------------------
# Solid modelling (manifold3d)
# --------------------------------------------------------------------------

def box(x0, y0, z0, x1, y1, z1):
    return Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))


def cyl(x, y, z0, z1, d, seg=32):
    return Manifold.cylinder(z1 - z0, d / 2, circular_segments=seg).translate((x, y, z0))


def hole_solid(p: Params, hole: Hole, light):
    """A prism with the hole outline in the wall plane, sheared along the light ray."""
    ol = hole_outline(p.hole_shape, hole.dia, p.hole_segments)
    length = p.wall_t + 1.0
    prism = Manifold.extrude(CrossSection([ol.tolist()]), length).translate((0, 0, -length / 2))
    ray = hole.center - light
    s = ray / float(ray @ hole.normal)  # unit step along the normal, following the ray
    m = np.column_stack([hole.eu, EZ, s, hole.center])
    return prism.transform(m)


def keyhole_positions(p: Params):
    d = p.derived()
    ky = (d["H"] / 2 - p.wall_t - 4 - p.screw_head_d / 2 - p.keyhole_slot - 1)
    if p.keyhole_spacing <= 0:
        return [(0.0, ky)]
    kx = p.keyhole_spacing * d["W"] / 2
    return [(-kx, ky), (kx, ky)]


def build_body(p: Params, holes, light, log):
    d = p.derived()
    W, H, D, t = d["W"], d["H"], p.depth, p.wall_t

    shell = box(-W / 2, -H / 2, 0, W / 2, H / 2, D) - box(
        -W / 2 + t, -H / 2 + t, p.back_t, W / 2 - t, H / 2 - t, D + 1)

    t0 = time.time()
    if holes:
        cutters = [hole_solid(p, h, light) for h in holes]
        # union in chunks keeps memory and run time reasonable
        chunk = 400
        parts = [Manifold.batch_boolean(cutters[i:i + chunk], OpType.Add)
                 for i in range(0, len(cutters), chunk)]
        shell = Manifold.batch_boolean([shell] + parts, OpType.Subtract)
    log(f"  cut {len(holes)} holes in {time.time() - t0:.1f}s")

    # keyhole hangers: open towards the wall only
    lip, hh, hd, sd, sl = (p.keyhole_lip, p.screw_head_h, p.screw_head_d,
                           p.screw_shank_d, p.keyhole_slot)
    boss_t = lip + hh + 1.6
    for kx, ky in keyhole_positions(p):
        boss = box(kx - hd / 2 - 4, ky - hd / 2 - 4, 0, kx + hd / 2 + 4, ky + sl + hd / 2 + 4, boss_t)
        shell = shell + boss
        cut = (cyl(kx, ky, -1, lip + hh, hd)                                 # head entry
               + box(kx - sd / 2, ky, -1, kx + sd / 2, ky + sl, lip + 0.01)  # shank slot
               + box(kx - hd / 2, ky, lip, kx + hd / 2, ky + sl, lip + hh)   # head channel
               + cyl(kx, ky + sl, lip, lip + hh, hd))                        # rounded end
        shell = shell - cut

    # cable: hole through the back plate near the bottom wall + groove to the edge
    cy = -H / 2 + t + p.cable_hole_d / 2 + 1.0
    gw = p.cable_hole_d
    shell = shell - cyl(0, cy, -1, p.back_t + 1, p.cable_hole_d)
    shell = shell - box(-gw / 2, -H / 2 - 1, -1, gw / 2, cy, min(2.0, p.back_t - 0.8))
    return shell


def build_backer(p: Params):
    """Printed front-face down; the LED post points up (towards the wall when assembled)."""
    d = p.derived()
    W, H, bt = d["W"], d["H"], p.backer_t
    plate = box(-W / 2, -H / 2, 0, W / 2, H / 2, bt)
    post_len = d["post_len"]
    post = cyl(0, 0, bt - 0.01, bt + post_len, p.post_d, 48)
    part = plate + post
    led = cyl(0, 0, bt + post_len - p.led_pocket, bt + post_len + 1, p.led_d + 0.3, 40)
    gw, gd = p.wire_groove_w, p.wire_groove_d
    groove_post = box(-gw / 2, -p.post_d, bt - gd, gw / 2, 0, bt + post_len + 1)
    groove_plate = box(-gw / 2, -H / 2 - 1, bt - gd, gw / 2, 0, bt + 1)
    return part - led - groove_post - groove_plate


def build_bezel(p: Params):
    """Printed face down (z=0 is the visible front)."""
    d = p.derived()
    W, H = d["W"], d["H"]
    c, st, fl = p.fit_clearance, p.bezel_skirt_t, p.bezel_flange
    ft = p.bezel_face_t
    sx, sy = W / 2 + c + st, H / 2 + c + st          # skirt outer half sizes
    ox, oy = sx + fl, sy + fl                         # face outer half sizes
    skirt_len = p.backer_t + p.picture_t + p.skirt_overlap
    face = box(-ox, -oy, 0, ox, oy, ft)
    skirt = box(-sx, -sy, ft - 0.01, sx, sy, ft + skirt_len) - box(
        -W / 2 - c, -H / 2 - c, ft, W / 2 + c, H / 2 + c, ft + skirt_len + 1)
    wx, wy = W / 2 - p.window_overlap, H / 2 - p.window_overlap
    ch = min(p.window_chamfer, ft - 0.4)
    # window with a 45 degree chamfer on the visible side
    win = box(-wx, -wy, -1, wx, wy, ft + 1)
    if ch > 0:
        cs = CrossSection.square((2 * (wx + ch + 1), 2 * (wy + ch + 1)), center=True)
        k = ((wx + 0.0) / (wx + ch + 1), (wy + 0.0) / (wy + ch + 1))
        cham = Manifold.extrude(cs, ch + 1, scale_top=k).translate((0, 0, -1))
        win = win + cham
    return face + skirt - win


def assembled(p: Params, body, backer, bezel):
    D, bt, pt = p.depth, p.backer_t, p.picture_t
    flip_y = np.array([[-1, 0, 0, 0], [0, 1, 0, 0], [0, 0, -1, 0]], float)
    b = backer.transform(flip_y).translate((0, 0, D + bt))
    z_front = D + bt + pt + p.bezel_face_t
    z = bezel.transform(flip_y).translate((0, 0, z_front))
    return body + b + z


# --------------------------------------------------------------------------
# STL output
# --------------------------------------------------------------------------

def write_stl(man, path, name="part"):
    mesh = man.to_mesh()
    v = np.asarray(mesh.vert_properties)[:, :3].astype(np.float32)
    f = np.asarray(mesh.tri_verts).astype(np.int64)
    tri = v[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.where(ln > 0, n / np.maximum(ln, 1e-30), 0).astype(np.float32)
    rec = np.zeros(len(f), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"], rec["v"] = n, tri
    with open(path, "wb") as fh:
        fh.write(name.encode()[:80].ljust(80, b"\0"))
        fh.write(struct.pack("<I", len(f)))
        fh.write(rec.tobytes())
    return len(f)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_argparser():
    ap = argparse.ArgumentParser(
        description="Generate STL files for a wall-hung picture frame whose perforated "
                    "side walls project an image onto the wall around it.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("image", nargs="?", help="image to project (bright = light). "
                    "Omit to use --pattern.")
    ap.add_argument("--pattern", default="sunburst",
                    choices=["sunburst", "stars", "rings", "waves"],
                    help="built-in pattern used when no image is given")
    ap.add_argument("-o", "--out", default="out", help="output directory")
    ap.add_argument("--preview-only", action="store_true",
                    help="only compute the hole layout and preview.png (fast)")
    defaults = Params()
    for f in fields(Params):
        val = getattr(defaults, f.name)
        flag = "--" + f.name.replace("_", "-")
        if isinstance(val, bool):
            ap.add_argument(flag, action="store_true", default=val)
        else:
            ap.add_argument(flag, type=type(val), default=val)
    return ap


def main(argv=None):
    args = build_argparser().parse_args(argv)
    p = Params(**{f.name: getattr(args, f.name) for f in fields(Params)})
    p.validate()
    os.makedirs(args.out, exist_ok=True)
    log = lambda s: print(s, flush=True)

    img = Image.open(args.image) if args.image else builtin_pattern(args.pattern)
    holes, light, wimg, extent, n_cells = layout_holes(p, img)
    d = p.derived()
    log(f"Frame body {p.picture_w:.0f} x {p.picture_h:.0f} x {p.depth:.0f} mm, "
        f"light at {p.light_z:.0f} mm from the wall")
    log(f"Hole band z = {d['z_lo']:.1f} .. {d['z_hi']:.1f} mm; "
        f"{len(holes)} holes out of {n_cells} grid cells")
    log(f"Projection covers about {2 * extent[0]:.0f} x {2 * extent[1]:.0f} mm of wall")

    render_preview(p, holes, light, extent, os.path.join(args.out, "preview.png"))
    wimg.render(*extent, 2.0).save(os.path.join(args.out, "target.png"))
    log(f"Wrote {args.out}/preview.png and {args.out}/target.png")
    if args.preview_only:
        return 0

    if Manifold is None:
        raise SystemExit("manifold3d is required for STL output: pip install manifold3d")
    log("Building body ...")
    body = build_body(p, holes, light, log)
    backer = build_backer(p)
    bezel = build_bezel(p)
    for name, part in (("body", body), ("backer", backer), ("bezel", bezel)):
        n = write_stl(part, os.path.join(args.out, f"{name}.stl"), name)
        log(f"Wrote {args.out}/{name}.stl ({n} triangles, {part.volume() / 1000:.1f} cm^3)")
    n = write_stl(assembled(p, body, backer, bezel), os.path.join(args.out, "assembly.stl"))
    log(f"Wrote {args.out}/assembly.stl ({n} triangles) - for viewing only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
