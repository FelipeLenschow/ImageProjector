#!/usr/bin/env python3
"""
Projector picture frame STL generator.

A wall-hung picture frame with a point-like LED in the middle, behind the
picture. The four deep side walls are stencils: the image is cut out of them
as continuous shapes, so the LED's light projects the image, uninterrupted,
onto the wall *around* the frame.

Every cut-out is computed by ray tracing: a point on a side wall is open
when the ray LED -> point -> wall lands on a bright part of the image. The
cut edges follow the light rays through the wall's thickness, so the
projection keeps the full shape even where the light passes at a steep angle.

Each side wall is a separate flat panel, printed flat on the bed. Dark
"islands" in the image (e.g. a wheel hub inside a bright wheel) would fall
out of a stencil, so thin bridges are added automatically to hold them.

Coordinates (assembled frame):
    z = 0 is the wall, +z points out into the room.
    x is horizontal, y is vertical (+y up), origin at the frame centre.

Parts written to the output directory (each in its print orientation):
    back.stl         back plate: keyhole hangers, cable exit, alignment lip
    wall_top.stl     stencil panels, print flat (outside face down)
    wall_bottom.stl
    wall_left.stl
    wall_right.stl
    backer.stl       opaque plate behind the picture, carries the LED post
    bezel.stl        visible front frame with the picture window
    assembly.stl     everything assembled (for viewing only)
    preview.png      simulated projection on the wall
    target.png       the image as mapped onto the wall (for comparison)

Requires: numpy, pillow, scikit-image, manifold3d
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import sys
from dataclasses import dataclass, fields

import numpy as np
from PIL import Image, ImageDraw

try:
    from manifold3d import CrossSection, FillRule, Manifold, OpType
except ImportError:  # checked in main()
    CrossSection = FillRule = Manifold = OpType = None


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------

@dataclass
class Params:
    # Picture / overall size (mm). The box footprint equals the picture size.
    picture_w: float = 100.0      # width of the printed photo
    picture_h: float = 150.0      # height of the printed photo
    picture_t: float = 0.6        # thickness of photo (+ optional acetate)
    window_overlap: float = 5.0   # how much the bezel covers the photo edge

    # Box
    depth: float = 60.0           # wall -> front rim of the side walls
    wall_t: float = 1.6           # side panel thickness
    back_t: float = 3.0           # back plate thickness
    lip_h: float = 3.0            # alignment lip on the back plate (inside the panels)
    lip_t: float = 1.6
    panel_clearance: float = 0.2  # gap between panels and lip

    # Light
    light_z: float = 48.0         # height of the light's emitting point above the wall
    led_d: float = 5.0            # LED body diameter (5 mm LED by default)
    led_pocket: float = 5.0       # depth of the LED pocket in the post
    led_protrusion: float = 3.0   # emitter distance below the post tip
    post_d: float = 10.0          # LED post diameter

    # Stencil
    corner_margin: float = 1.5    # solid border at the panel ends
    bottom_margin: float = 1.5    # solid band above the lip
    max_magnification: float = 3.5  # how far from the frame the image reaches
    threshold: float = 0.5        # image brightness above this is cut open
    invert: bool = False
    fit: str = "cover"            # cover | contain | stretch
    min_web: float = 0.8          # thinner plastic strips are removed
    min_gap: float = 0.5          # narrower openings are closed
    bridge_w: float = 0.9         # width of the bridges holding islands
    min_island: float = 1.0       # dark islands smaller than this (mm^2 on the panel) are cut open
    resolution: float = 0.08      # stencil raster resolution on the panel (mm)
    cable_gap: float = 0.0        # optional solid strip at x=0 on the bottom panel

    # Bezel (front frame)
    bezel_face_t: float = 2.4
    bezel_skirt_t: float = 2.0
    bezel_flange: float = 6.0     # face width beyond the skirt
    skirt_overlap: float = 6.0    # how far the skirt slides over the panels
    fit_clearance: float = 0.25   # gap between skirt and panels (press fit)
    window_chamfer: float = 1.0

    # Backer
    backer_t: float = 2.4
    wire_groove_w: float = 2.8
    wire_groove_d: float = 1.2

    # Keyhole hangers (for a screw / nail in the wall)
    screw_head_d: float = 8.5
    screw_shank_d: float = 4.5
    screw_head_h: float = 3.0
    keyhole_lip: float = 2.0
    keyhole_slot: float = 10.0
    keyhole_spacing: float = 0.5  # x spacing as fraction of width (0 = one, centred)

    cable_hole_d: float = 4.0

    def z_band(self):
        z_lo = self.back_t + self.lip_h + self.bottom_margin
        z_hi = self.light_z * (1.0 - 1.0 / self.max_magnification)
        z_hi = min(z_hi, self.depth - self.skirt_overlap - 1.5)
        return z_lo, z_hi

    def post_len(self):
        return self.depth - (self.light_z + self.led_protrusion)

    def validate(self):
        z_lo, z_hi = self.z_band()
        errs = []
        if z_hi <= z_lo + 2:
            errs.append("No room for the stencil: raise light_z / depth / max_magnification.")
        if self.light_z >= self.depth:
            errs.append("light_z must be below the front rim (depth).")
        if self.post_len() < self.led_pocket + 1:
            errs.append("LED post too short: lower light_z or increase depth.")
        if self.fit not in ("cover", "contain", "stretch"):
            errs.append("fit must be cover, contain or stretch")
        if errs:
            raise SystemExit("Invalid parameters:\n  " + "\n  ".join(errs))


# --------------------------------------------------------------------------
# Wall panels
# --------------------------------------------------------------------------

EZ = np.array([0.0, 0.0, 1.0])


@dataclass
class Wall:
    name: str
    n: np.ndarray        # outward normal
    eu: np.ndarray       # along the panel; [eu, ez, n] is right handed
    off: float           # distance of the mid-plane from the centre
    half_len: float      # panel half length (u)
    cut_half: float      # half length of the region that may be cut


def walls(p: Params):
    W, H, t = p.picture_w, p.picture_h, p.wall_t
    res = []
    # top/bottom panels run the full width, left/right fit between them
    for name, n, half_len, off in (
        ("top", (0, 1, 0), W / 2, H / 2 - t / 2),
        ("bottom", (0, -1, 0), W / 2, H / 2 - t / 2),
        ("left", (-1, 0, 0), H / 2 - t - 0.05, W / 2 - t / 2),
        ("right", (1, 0, 0), H / 2 - t - 0.05, W / 2 - t / 2),
    ):
        n = np.array(n, float)
        eu = np.cross(EZ, n)
        inner_half = (W if name in ("top", "bottom") else H) / 2 - t
        res.append(Wall(name, n, eu, off, half_len, inner_half - p.corner_margin))
    return res


def mid_points(wall: Wall, u, z):
    """World points on a panel's mid-plane for local coords (u, z)."""
    u, z = np.broadcast_arrays(u, z)
    return (wall.n * wall.off + u[..., None] * wall.eu + z[..., None] * EZ)


def project_to_wall(points, light):
    """Project 3D points through the light onto the plane z=0."""
    zl = light[2]
    t = zl / (zl - points[..., 2])
    return light[:2] + t[..., None] * (points[..., :2] - light[:2])


def projection_extent(p: Params):
    """Half width/height of the wall area the stencils can light."""
    light = np.array([0.0, 0.0, p.light_z])
    _, z_hi = p.z_band()
    ex = ey = 0.0
    for w in walls(p):
        pts = mid_points(w, np.array([-w.cut_half, w.cut_half]), np.array([z_hi, z_hi]))
        xy = project_to_wall(pts, light)
        ex, ey = max(ex, np.abs(xy[:, 0]).max()), max(ey, np.abs(xy[:, 1]).max())
    return ex + 2.0, ey + 2.0


# --------------------------------------------------------------------------
# Image handling
# --------------------------------------------------------------------------

def builtin_pattern(name, size=1024):
    yy, xx = np.mgrid[-1:1:size * 1j, -1:1:size * 1j]
    r = np.hypot(xx, yy)
    th = np.arctan2(yy, xx)
    if name == "sunburst":
        img = (np.cos(th * 18) > 0.25) & (r > 0.15)
    elif name == "rings":
        img = np.sin(r * 40) > 0
    elif name == "waves":
        img = np.sin(xx * 12 + np.sin(yy * 6) * 2) > 0.3
    elif name == "stars":
        im = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(im)
        rng = np.random.default_rng(7)
        for _ in range(40):
            cx, cy = rng.uniform(0, size, 2)
            ro, ri = rng.uniform(15, 45), 0
            ri = ro * 0.4
            pts = [(cx + (ro if k % 2 == 0 else ri) * math.sin(k * math.pi / 5),
                    cy - (ro if k % 2 == 0 else ri) * math.cos(k * math.pi / 5)) for k in range(10)]
            d.polygon(pts, fill=255)
        return im
    else:
        raise SystemExit(f"unknown pattern {name!r}")
    return Image.fromarray((img * 255).astype(np.uint8), "L")


class WallImage:
    """Maps wall coordinates (mm) to image brightness in [0, 1]."""

    def __init__(self, img: Image.Image, p: Params, extent):
        img = img.convert("L")
        iw, ih = img.size
        sx, sy = iw / (2 * extent[0]), ih / (2 * extent[1])  # px per mm
        if p.fit == "cover":      # image fills the whole projection area (crops)
            sx = sy = min(sx, sy)
        elif p.fit == "contain":  # whole image visible (may leave dark margins)
            sx = sy = max(sx, sy)
        self.sx, self.sy, self.iw, self.ih = sx, sy, iw, ih
        a = np.asarray(img, dtype=float) / 255.0
        self.a = 1.0 - a if p.invert else a

    def sample(self, xy):
        """Bilinear sample; outside the image counts as black."""
        x = xy[..., 0] * self.sx + self.iw / 2 - 0.5
        y = -xy[..., 1] * self.sy + self.ih / 2 - 0.5
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

    def render(self, extent, px_per_mm):
        w, h = int(2 * extent[0] * px_per_mm), int(2 * extent[1] * px_per_mm)
        gx, gy = np.meshgrid((np.arange(w) + 0.5) / px_per_mm - extent[0],
                             extent[1] - (np.arange(h) + 0.5) / px_per_mm)
        v = self.sample(np.stack([gx, gy], -1))
        return Image.fromarray((v * 255).astype(np.uint8), "L")


# --------------------------------------------------------------------------
# Stencil computation (2D, in panel coords u, z)
# --------------------------------------------------------------------------

def polygon_area(c):
    x, y = c[:, 0], c[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def rect(u0, z0, u1, z1):
    return CrossSection([[(u0, z0), (u1, z0), (u1, z1), (u0, z1)]])


def stencil_region(p: Params, wall: Wall, wimg: WallImage, light):
    """Open (cut) region of a panel as a CrossSection in (u, z) coordinates."""
    from skimage.measure import find_contours

    z_lo, z_hi = p.z_band()
    r = p.resolution
    us = np.arange(-wall.cut_half, wall.cut_half + r / 2, r)
    zs = np.arange(z_lo, z_hi + r / 2, r)
    U, Z = np.meshgrid(us, zs)
    bright = wimg.sample(project_to_wall(mid_points(wall, U, Z), light))
    mask = np.pad(bright, 1, constant_values=0.0)
    contours = []
    for c in find_contours(mask, p.threshold):
        if len(c) < 3:
            continue
        pts = np.column_stack([us[0] + (c[:, 1] - 1) * r, zs[0] + (c[:, 0] - 1) * r])
        contours.append(pts)
    if not contours:
        return CrossSection()
    region = CrossSection(contours, FillRule.EvenOdd).simplify(r * 0.3)
    region = printable(region, p.min_web, p.min_gap)
    region = region ^ rect(-wall.cut_half, z_lo, wall.cut_half, z_hi)
    if wall.name == "bottom" and p.cable_gap > 0:
        region = region - rect(-p.cable_gap, z_lo - 1, p.cable_gap, z_hi + 1)
    return add_bridges(p, region).simplify(0.01)


def fill_small_islands(region, min_area):
    """Open up dark specks too small to be worth a bridge."""
    small = [np.asarray(c) for c in region.to_polygons()
             if -min_area < polygon_area(c) < 0]
    if not small:
        return region
    return region + CrossSection([c[::-1] for c in small])


def printable(region, min_web, min_gap, tiny=0.05):
    """Make thin details printable by thickening them instead of dropping them.

    Dark lines thinner than min_web become min_web wide plastic strips, and
    light lines narrower than min_gap become min_gap wide openings, so fine
    outlines in the image survive. Specks smaller than `tiny` mm^2 are dropped.
    """
    def pieces(cs):
        return CrossSection.batch_boolean(
            [c for c in cs.decompose() if c.area() > tiny] or [CrossSection()], OpType.Add)

    closed = region.offset(min_web / 2).offset(-min_web / 2)
    thin_dark = pieces(closed - region)
    region = closed - thin_dark.offset(min_web / 2)
    opened = region.offset(-min_gap / 2).offset(min_gap / 2)
    thin_light = pieces(region - opened)
    return opened + thin_light.offset(min_gap / 2)


def add_bridges(p: Params, region):
    """Hold dark islands with one short bridge each (they'd fall out otherwise).

    For every island, four candidate bridges are tried: up, down, left and
    right from the island to the nearest dark edge. The one that blocks the
    least light is used.
    """
    bw = p.bridge_w
    region = fill_small_islands(region, p.min_island)
    for _ in range(8):
        bridges = []
        for comp in region.decompose():
            polys = [np.asarray(c) for c in comp.to_polygons()]
            u_lo = min(c[:, 0].min() for c in polys) - 1
            u_hi = max(c[:, 0].max() for c in polys) + 1
            z_lo = min(c[:, 1].min() for c in polys) - 1
            z_hi = max(c[:, 1].max() for c in polys) + 1
            for c in polys:
                if polygon_area(c) >= 0:
                    continue  # outer contour; holes (islands) have negative area
                u = 0.5 * (c[:, 0].min() + c[:, 0].max())
                z = 0.5 * (c[:, 1].min() + c[:, 1].max())
                cands = []
                for strip, axis, sign in (
                        (rect(u - bw / 2, z, u + bw / 2, z_hi), 1, 1),    # up
                        (rect(u - bw / 2, z_lo, u + bw / 2, z), 1, -1),   # down
                        (rect(u, z - bw / 2, u_hi, z + bw / 2), 0, 1),    # right
                        (rect(u_lo, z - bw / 2, u, z + bw / 2), 0, -1)):  # left
                    # keep only the piece next to the island: island -> first dark edge
                    pieces = (comp ^ strip).decompose()
                    if not pieces:
                        continue
                    start = (u, z)[axis]
                    first = min(pieces, key=lambda pc: min(
                        abs(np.asarray(q)[:, axis] - start).min() for q in pc.to_polygons()))
                    cands.append(first.offset(0.05))
                if cands:
                    bridges.append(min(cands, key=lambda b_: b_.area()))
        if not bridges:
            break
        region = region - CrossSection.batch_boolean(bridges, OpType.Add)
        region = fill_small_islands(region, p.min_island)
    return region


def rasterize(region, wall: Wall, p: Params, r=0.03):
    """Binary raster of a stencil region on the panel grid (for the preview)."""
    from skimage.draw import polygon as fill_polygon

    z_lo, z_hi = p.z_band()
    w = int(2 * wall.cut_half / r) + 2
    h = int((z_hi - z_lo) / r) + 2
    mask = np.zeros((h, w), bool)
    for c in region.to_polygons():  # even-odd: every contour toggles its inside
        c = np.asarray(c)
        if len(c) < 3:
            continue
        rr, cc = fill_polygon((c[:, 1] - z_lo) / r, (c[:, 0] + wall.cut_half) / r, mask.shape)
        mask[rr, cc] ^= True
    return mask, r


# --------------------------------------------------------------------------
# Preview (simulated projection)
# --------------------------------------------------------------------------

def render_preview(p: Params, rasters, light, extent, path, px_per_mm=2.0):
    ext_x, ext_y = extent
    w, h = int(2 * ext_x * px_per_mm), int(2 * ext_y * px_per_mm)
    X, Y = np.meshgrid((np.arange(w) + 0.5) / px_per_mm - ext_x,
                       ext_y - (np.arange(h) + 0.5) / px_per_mm)
    zl = light[2]
    z_lo, _ = p.z_band()
    lit = np.zeros((h, w), bool)
    best_s = np.full((h, w), np.inf)
    for wall in walls(p):
        mask, r = rasters[wall.name]
        den = X * wall.n[0] + Y * wall.n[1]
        with np.errstate(divide="ignore", invalid="ignore"):
            s = np.where(den > 0, wall.off / np.where(den > 0, den, 1.0), 2.0)  # ray param at panel
        ok = (s > 0) & (s < 1)
        mx, my, mz = s * X, s * Y, zl * (1 - s)
        u = mx * wall.eu[0] + my * wall.eu[1]
        hit = ok & (np.abs(u) <= wall.half_len) & (s < best_s)  # first panel the ray meets
        best_s = np.where(hit, s, best_s)
        iu = np.round((u + wall.cut_half) / r).astype(int)
        iz = np.round((mz - z_lo) / r).astype(int)
        inside = hit & (iu >= 0) & (iu < mask.shape[1]) & (iz >= 0) & (iz < mask.shape[0])
        open_ = np.zeros_like(lit)
        open_[inside] = mask[iz[inside], iu[inside]]
        lit = np.where(hit, open_, lit)
    rr = np.sqrt(X ** 2 + Y ** 2 + zl ** 2)
    inten = np.where(lit, (zl / rr) ** 4, 0.0)
    if inten.max() > 0:
        # the eye sees brightness roughly logarithmically: show the falloff gently
        inten = np.where(lit, 0.6 + 0.4 * (inten / inten.max()) ** 0.3, 0.0)
    rgb = np.stack([inten * 255, inten * 246, inten * 225], -1).astype(np.uint8)
    out = Image.fromarray(rgb, "RGB")
    d = ImageDraw.Draw(out)

    def to_px(xy):
        return [((x + ext_x) * px_per_mm, (ext_y - y) * px_per_mm) for x, y in xy]

    bx = p.picture_w / 2 + p.fit_clearance + p.bezel_skirt_t + p.bezel_flange
    by = p.picture_h / 2 + p.fit_clearance + p.bezel_skirt_t + p.bezel_flange
    wx = p.picture_w / 2 - p.window_overlap
    wy = p.picture_h / 2 - p.window_overlap
    d.rectangle(to_px([(-bx, by), (bx, -by)]), fill=(60, 45, 35))
    d.rectangle(to_px([(-wx, wy), (wx, -wy)]), fill=(140, 140, 150))
    out.save(path)


# --------------------------------------------------------------------------
# Solid modelling
# --------------------------------------------------------------------------

def box(x0, y0, z0, x1, y1, z1):
    return Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))


def cyl(x, y, z0, z1, d, seg=32):
    return Manifold.cylinder(z1 - z0, d / 2, circular_segments=seg).translate((x, y, z0))


def panel_world(p: Params, wall: Wall, region, light):
    """The panel in assembled position with the stencil cut along the light rays."""
    t = p.wall_t
    L, n, eu, off = light, wall.n, wall.eu, wall.off
    # local coords: u along the panel, z height, w along the normal (0 = mid-plane)
    to_world = np.column_stack([eu, EZ, n, n * off])
    slab = box(-wall.half_len, p.back_t, -t / 2, wall.half_len, p.depth, t / 2).transform(to_world)
    if region.is_empty():
        return slab
    ext = t / 2 + 1.0
    cutter = Manifold.extrude(region, 2 * ext).translate((0, 0, -ext))

    def warp(v):
        # the point at depth w on the light ray through mid-plane point M(u, z):
        # L + (M - L) * (1 + w / off). Faces stay planar, edges follow the rays.
        v = np.asarray(v)
        M = (n * off)[None] + v[:, :1] * eu[None] + v[:, 1:2] * EZ[None]
        return L[None] + (M - L[None]) * (1.0 + v[:, 2:3] / off)

    return slab - cutter.warp_batch(warp)


def panel_print(p: Params, wall: Wall, world):
    """Lay a panel flat, outside face on the bed."""
    rows = np.array([-wall.eu, EZ, -wall.n])
    m = np.column_stack([rows, [0.0, -p.back_t, wall.off + p.wall_t / 2]])
    return world.transform(m)


def keyhole_positions(p: Params):
    H, W = p.picture_h, p.picture_w
    ky = H / 2 - p.wall_t - 4 - p.screw_head_d / 2 - p.keyhole_slot - 1
    if p.keyhole_spacing <= 0:
        return [(0.0, ky)]
    kx = p.keyhole_spacing * W / 2
    return [(-kx, ky), (kx, ky)]


def build_back(p: Params):
    W, H, t, bt = p.picture_w, p.picture_h, p.wall_t, p.back_t
    part = box(-W / 2, -H / 2, 0, W / 2, H / 2, bt)
    # alignment lip just inside the panels
    lx, ly = W / 2 - t - p.panel_clearance, H / 2 - t - p.panel_clearance
    lip = box(-lx, -ly, bt - 0.01, lx, ly, bt + p.lip_h) - box(
        -lx + p.lip_t, -ly + p.lip_t, bt - 1, lx - p.lip_t, ly - p.lip_t, bt + p.lip_h + 1)
    part = part + lip
    # keyhole hangers, open towards the wall only
    lip_k, hh, hd, sd, sl = (p.keyhole_lip, p.screw_head_h, p.screw_head_d,
                             p.screw_shank_d, p.keyhole_slot)
    boss_t = lip_k + hh + 1.6
    for kx, ky in keyhole_positions(p):
        part = part + box(kx - hd / 2 - 4, ky - hd / 2 - 4, 0, kx + hd / 2 + 4,
                          min(ky + sl + hd / 2 + 4, ly), boss_t)
        part = part - (cyl(kx, ky, -1, lip_k + hh, hd)
                       + box(kx - sd / 2, ky, -1, kx + sd / 2, ky + sl, lip_k + 0.01)
                       + box(kx - hd / 2, ky, lip_k, kx + hd / 2, ky + sl, lip_k + hh)
                       + cyl(kx, ky + sl, lip_k, lip_k + hh, hd))
    # cable: the wire runs down the bottom-left inside corner (always dark),
    # through a notch in the lip and a hole, then along a groove to the edge
    cd = p.cable_hole_d
    cx, cy = -lx + p.lip_t + cd / 2 + 0.5, -ly + p.lip_t + cd / 2 + 0.5
    part = part - box(-W / 2 + t - 0.5, -H / 2 + t - 0.5, bt, cx + cd / 2, cy + cd / 2, bt + p.lip_h + 1)
    part = part - cyl(cx, cy, -1, bt + 1, cd)
    part = part - box(cx - cd / 2, -H / 2 - 1, -1, cx + cd / 2, cy, min(2.0, bt - 0.8))
    return part


def build_backer(p: Params):
    """Printed front-face down; the LED post points up (towards the wall when assembled)."""
    W, H, bt = p.picture_w, p.picture_h, p.backer_t
    post_len = p.post_len()
    part = box(-W / 2, -H / 2, 0, W / 2, H / 2, bt) + cyl(0, 0, bt - 0.01, bt + post_len, p.post_d, 48)
    led = cyl(0, 0, bt + post_len - p.led_pocket, bt + post_len + 1, p.led_d + 0.3, 40)
    gw, gd = p.wire_groove_w, p.wire_groove_d
    groove_post = box(-gw / 2, -p.post_d, bt - gd, gw / 2, 0, bt + post_len + 1)
    # groove from the post to the bottom corner where the wire goes down.
    # Printed face down, so the corner at (+x, -y) here is bottom-left when assembled.
    ex, ey = W / 2 - p.wall_t, -(H / 2 - p.wall_t)
    ang = math.degrees(math.atan2(ey, ex))
    length = math.hypot(ex, ey) + 5
    groove_plate = (box(0, -gw / 2, bt - gd, length, gw / 2, bt + 1)
                    .rotate((0, 0, ang)))
    return part - led - groove_post - groove_plate


def build_bezel(p: Params):
    """Printed face down (z=0 is the visible front)."""
    W, H = p.picture_w, p.picture_h
    c, st, fl, ft = p.fit_clearance, p.bezel_skirt_t, p.bezel_flange, p.bezel_face_t
    sx, sy = W / 2 + c + st, H / 2 + c + st
    ox, oy = sx + fl, sy + fl
    skirt_len = p.backer_t + p.picture_t + p.skirt_overlap
    face = box(-ox, -oy, 0, ox, oy, ft)
    skirt = box(-sx, -sy, ft - 0.01, sx, sy, ft + skirt_len) - box(
        -W / 2 - c, -H / 2 - c, ft, W / 2 + c, H / 2 + c, ft + skirt_len + 1)
    wx, wy = W / 2 - p.window_overlap, H / 2 - p.window_overlap
    win = box(-wx, -wy, -1, wx, wy, ft + 1)
    ch = min(p.window_chamfer, ft - 0.4)
    if ch > 0:
        cs = CrossSection.square((2 * (wx + ch + 1), 2 * (wy + ch + 1)), center=True)
        k = (wx / (wx + ch + 1), wy / (wy + ch + 1))
        win = win + Manifold.extrude(cs, ch + 1, scale_top=k).translate((0, 0, -1))
    return face + skirt - win


def assembled(p: Params, back, panels_world, backer, bezel):
    D, bt, pt = p.depth, p.backer_t, p.picture_t
    flip_y = np.array([[-1, 0, 0, 0], [0, 1, 0, 0], [0, 0, -1, 0]], float)
    parts = [back] + list(panels_world)
    parts.append(backer.transform(flip_y).translate((0, 0, D + bt)))
    parts.append(bezel.transform(flip_y).translate((0, 0, D + bt + pt + p.bezel_face_t)))
    return Manifold.batch_boolean(parts, OpType.Add)


def write_stl(man, path, name="part"):
    man = man.simplify(1e-3)  # drop sliver triangles that collapse in float32
    mesh = man.to_mesh()
    v = np.asarray(mesh.vert_properties)[:, :3].astype(np.float32)
    f = np.asarray(mesh.tri_verts).astype(np.int64)
    tri = v[f]
    nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(nrm, axis=1, keepdims=True)
    nrm = (nrm / np.maximum(ln, 1e-30)).astype(np.float32)
    rec = np.zeros(len(f), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"], rec["v"] = nrm, tri
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
        description="Generate STL files for a wall-hung picture frame whose stencil "
                    "side walls project an image onto the wall around it.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("image", nargs="?", help="image to project (bright = light). "
                    "Omit to use --pattern.")
    ap.add_argument("--pattern", default="sunburst",
                    choices=["sunburst", "stars", "rings", "waves"],
                    help="built-in pattern used when no image is given")
    ap.add_argument("-o", "--out", default="out", help="output directory")
    ap.add_argument("--preview-only", action="store_true",
                    help="only compute the stencils and preview.png (no STL)")
    defaults = Params()
    for f in fields(Params):
        val = getattr(defaults, f.name)
        flag = "--" + f.name.replace("_", "-")
        if isinstance(val, bool):
            ap.add_argument(flag, action="store_true", default=val)
        else:
            ap.add_argument(flag, type=type(val), default=val)
    return ap


def params_from_args(args):
    return Params(**{f.name: getattr(args, f.name) for f in fields(Params)})


def lit_area(p: Params):
    """Geometry of the projection on the wall, all in mm (half sizes).

    inner: edge of the lit area next to the frame; outer: how far it reaches
    (the corners reach a little further, see projection_extent);
    bezel / window: the visible frame.
    """
    z_lo, z_hi = p.z_band()
    t_in = p.light_z / (p.light_z - z_lo)
    t_out = p.light_z / (p.light_z - z_hi)
    hx, hy = p.picture_w / 2 - p.wall_t / 2, p.picture_h / 2 - p.wall_t / 2
    b = p.fit_clearance + p.bezel_skirt_t + p.bezel_flange
    ext = projection_extent(p)
    return {
        "extent": [ext[0], ext[1]],
        "inner": [hx * t_in, hy * t_in],
        "outer": [hx * t_out, hy * t_out],
        "box": [p.picture_w / 2, p.picture_h / 2],
        "bezel": [p.picture_w / 2 + b, p.picture_h / 2 + b],
        "window": [p.picture_w / 2 - p.window_overlap, p.picture_h / 2 - p.window_overlap],
    }


def compose_wall_image(img: Image.Image, p: Params, width_mm, center_mm=(0.0, 0.0),
                       invert=False, px_per_mm=4.0):
    """Place a picture on the wall: `width_mm` wide, centred at `center_mm`
    (x right, y up, frame centre = 0). Returns an image covering exactly the
    lit wall area, to be used with fit="stretch". Outside the picture is dark."""
    ext_x, ext_y = projection_extent(p)
    w, h = int(round(2 * ext_x * px_per_mm)), int(round(2 * ext_y * px_per_mm))
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
        img = Image.alpha_composite(bg, rgba)
    pic = img.convert("L")
    if invert:
        pic = Image.eval(pic, lambda v: 255 - v)
    pw = max(1, int(round(width_mm * px_per_mm)))
    ph = max(1, int(round(width_mm * pic.height / pic.width * px_per_mm)))
    pic = pic.resize((pw, ph), Image.LANCZOS)
    out = Image.new("L", (w, h), 0)
    x0 = w / 2 + center_mm[0] * px_per_mm - pw / 2
    y0 = h / 2 - center_mm[1] * px_per_mm - ph / 2
    out.paste(pic, (int(round(x0)), int(round(y0))))
    return out


def generate(p: Params, img: Image.Image, out_dir, preview_only=False, log=print):
    """Compute the stencils and write preview.png, target.png and (unless
    preview_only) all STL parts to out_dir. Returns the list of files written."""
    if Manifold is None:
        raise SystemExit("manifold3d is required: pip install -r requirements.txt")
    p.validate()
    os.makedirs(out_dir, exist_ok=True)
    out = lambda name: os.path.join(out_dir, name)
    written = []

    light = np.array([0.0, 0.0, p.light_z])
    extent = projection_extent(p)
    wimg = WallImage(img, p, extent)
    z_lo, z_hi = p.z_band()
    log(f"Frame {p.picture_w:.0f} x {p.picture_h:.0f} x {p.depth:.0f} mm, light "
        f"{p.light_z:.0f} mm from the wall, stencil band z = {z_lo:.1f} .. {z_hi:.1f} mm")
    log(f"Projection covers about {2 * extent[0]:.0f} x {2 * extent[1]:.0f} mm of wall")

    regions, rasters = {}, {}
    for wall in walls(p):
        regions[wall.name] = stencil_region(p, wall, wimg, light)
        rasters[wall.name] = rasterize(regions[wall.name], wall, p)
    render_preview(p, rasters, light, extent, out("preview.png"))
    wimg.render(extent, 2.0).save(out("target.png"))
    written += [out("preview.png"), out("target.png")]
    log(f"Wrote {out('preview.png')} and {out('target.png')}")
    if preview_only:
        return written

    panels_world = []
    for wall in walls(p):
        world = panel_world(p, wall, regions[wall.name], light)
        panels_world.append(world)
        part = panel_print(p, wall, world)
        pieces = len(part.decompose())
        n = write_stl(part, out(f"wall_{wall.name}.stl"), f"wall_{wall.name}")
        written.append(out(f"wall_{wall.name}.stl"))
        note = "" if pieces == 1 else f"  WARNING: {pieces} separate pieces"
        log(f"Wrote {out(f'wall_{wall.name}.stl')} ({n} triangles){note}")
    back, backer, bezel = build_back(p), build_backer(p), build_bezel(p)
    for name, part in (("back", back), ("backer", backer), ("bezel", bezel)):
        n = write_stl(part, out(f"{name}.stl"), name)
        written.append(out(f"{name}.stl"))
        log(f"Wrote {out(f'{name}.stl')} ({n} triangles)")
    n = write_stl(assembled(p, back, panels_world, backer, bezel), out("assembly.stl"))
    written.append(out("assembly.stl"))
    log(f"Wrote {out('assembly.stl')} ({n} triangles) - for viewing only")
    return written


def main(argv=None):
    args = build_argparser().parse_args(argv)
    p = params_from_args(args)
    img = Image.open(args.image) if args.image else builtin_pattern(args.pattern)
    generate(p, img, args.out, args.preview_only, log=lambda s: print(s, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
