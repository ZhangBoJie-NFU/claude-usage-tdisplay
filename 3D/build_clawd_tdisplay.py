#!/usr/bin/env python3
"""
Embed the TTGO T-Display enclosure into Clawd's eye area.

    python build_clawd_tdisplay.py

Reads Claude.3mf (the Clawd body) and TTGO_-_Tdisplay_lid.3mf (the T-Display
enclosure) and writes:
(Claude.3mf is under the MakerWorld Exclusive License and is not shipped with
the repo - download it from MakerWorld yourself)

    Clawd_TDisplay.3mf          Clawd body + the 4 enclosure parts, laid out for printing
    Clawd_TDisplay_preview.png  assembly preview

How: Clawd is scaled up and a pocket is cut into the face at eye height, sized to
the assembled enclosure (56.7 x 30.3 x 14 mm) plus clearance. The enclosure is
pressed in from the front, flush with the face, with the screen centered.
A slot on the right side lets the USB-C cable out, and a round hole in the back
lets you push the enclosure out again. The original eye cavities are filled first.

Requires: pip install numpy manifold3d matplotlib
"""

import os
import sys
import zipfile
import xml.etree.ElementTree as ET

import numpy as np
from manifold3d import CrossSection, JoinType, Manifold, Mesh

HERE = os.path.dirname(os.path.abspath(__file__))
CLAWD_3MF = os.path.join(HERE, "Claude.3mf")
CASE_3MF = os.path.join(HERE, "TTGO_-_Tdisplay_lid.3mf")
OUT_3MF = os.path.join(HERE, "Clawd_TDisplay.3mf")
OUT_PNG = os.path.join(HERE, "Clawd_TDisplay_preview.png")

# ----- Tunable parameters (mm) ---------------------------------------------- #
SCALE_XY = 0.7          # original is 0.5; at 0.7 the face is 84 x 56 mm, big enough for the enclosure
SCALE_Z = 0.84          # original is 0.6; keeps the original thickness ratio -> 33.6 mm thick
CLEARANCE = 0.2         # pocket gap per side; raise to 0.25-0.3 if the fit is too tight
POCKET_EXTRA_DEPTH = 0.2  # recess the enclosure front slightly so it doesn't stick out
USB_SLOT_W = 14.0       # USB-C plug housing width + margin
USB_SLOT_H = 10.4       # slot depth below the USB center = USB_SLOT_H / 2
PUSH_HOLE_D = 12.0      # diameter of the push-out hole in the back

# T-Display enclosure (front shell object_4, in the file's local coordinates, front at z = -7)
CASE_L, CASE_H, CASE_T = 56.69, 30.29, 14.0
CASE_R = 2.0                # radius of the 4 vertical edges (the front edge has no chamfer)
WINDOW_X = (-19.94, 8.45)   # x range of the screen window on the front shell
USB_Z = -2.4                # center of the USB opening (at the +x end, y = 0)

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


# --------------------------------------------------------------------------- #
#  3MF read / write
# --------------------------------------------------------------------------- #
def read_mesh(path_3mf, inner):
    with zipfile.ZipFile(path_3mf) as z:
        root = ET.fromstring(z.read(inner))
    mesh = root.find(".//{%s}mesh" % NS)
    v = np.array([[float(x.get(k)) for k in "xyz"] for x in mesh.find("{%s}vertices" % NS)])
    f = np.array([[int(t.get(k)) for k in ("v1", "v2", "v3")]
                  for t in mesh.find("{%s}triangles" % NS)])
    return v, f


def write_3mf(path, objects):
    """objects: [(name, verts, tris, (tx, ty, tz))], vertices already in print orientation."""
    res, build = [], []
    for i, (name, v, f, t) in enumerate(objects, start=1):
        vs = "".join('<vertex x="%.4f" y="%.4f" z="%.4f"/>' % tuple(p) for p in v)
        ts = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % tuple(q) for q in f)
        res.append('<object id="%d" name="%s" type="model"><mesh><vertices>%s</vertices>'
                   '<triangles>%s</triangles></mesh></object>' % (i, name, vs, ts))
        build.append('<item objectid="%d" transform="1 0 0 0 1 0 0 0 1 %.3f %.3f %.3f"/>'
                     % ((i,) + tuple(t)))
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<model unit="millimeter" xml:lang="en-US" xmlns="%s">'
             '<resources>%s</resources><build>%s</build></model>'
             % (NS, "".join(res), "".join(build)))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?>\n'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?>\n'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
                   'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
                   '</Relationships>')
        z.writestr("3D/3dmodel.model", model)


# --------------------------------------------------------------------------- #
#  Geometry
# --------------------------------------------------------------------------- #
def to_manifold(v, f):
    m = Mesh(vert_properties=np.asarray(v, np.float32), tri_verts=np.asarray(f, np.uint32))
    m.merge()
    man = Manifold(m)
    if man.status().name != "NoError":
        sys.exit(f"Mesh is not a closed solid: {man.status()}")
    return man


def box(x0, x1, y0, y1, z0, z1):
    return Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))


def from_manifold(man):
    m = man.to_mesh()
    return np.asarray(m.vert_properties)[:, :3], np.asarray(m.tri_verts)


def build_clawd():
    """Return (Clawd solid, enclosure placement). Axes: X right, Y up, Z toward the face (+Z is the face)."""
    v, f = read_mesh(CLAWD_3MF, "3D/Objects/object_1.model")
    clawd = to_manifold(v, f)

    # The original eyes are cavities at Z 10..20; fill them first (original coordinates)
    for x0, x1 in ((30, 40), (-40, -30)):
        clawd = clawd + box(x0, x1, 10, 30, 10, 20)
    clawd = clawd.scale((SCALE_XY, SCALE_XY, SCALE_Z))

    face_z = 20 * SCALE_Z
    eye_y = 20 * SCALE_XY                       # height of the original eye centers
    dx = -(WINDOW_X[0] + WINDOW_X[1]) / 2       # center the screen window on the face
    body_half_w = 60 * SCALE_XY

    # Pocket outline = enclosure outline offset outward by CLEARANCE, so the corner radius is CASE_R + CLEARANCE
    outline = CrossSection.square((CASE_L - 2 * CASE_R, CASE_H - 2 * CASE_R), center=True) \
        .offset(CASE_R + CLEARANCE, JoinType.Round, circular_segments=96)
    pocket_z0 = face_z - CASE_T - POCKET_EXTRA_DEPTH
    pocket = Manifold.extrude(outline, face_z + 1 - pocket_z0) \
        .translate((dx, eye_y, pocket_z0))

    # USB-C is at the enclosure's +x end; rotating it 180 deg about X maps z -> -z, so the center is at face_z - (7 + USB_Z)
    usb_z = face_z - (7 + USB_Z)
    usb_slot = box(dx + CASE_L / 2 - 1, body_half_w + 1,
                   eye_y - USB_SLOT_W / 2, eye_y + USB_SLOT_W / 2,
                   usb_z - USB_SLOT_H / 2, face_z + 1)

    push = Manifold.cylinder(face_z * 2, PUSH_HOLE_D / 2, circular_segments=64) \
        .translate((dx, eye_y, -face_z - 1))

    clawd = clawd - pocket - usb_slot - push
    return clawd, dict(dx=dx, eye_y=eye_y, face_z=face_z)


# --------------------------------------------------------------------------- #
#  Assembly preview
# --------------------------------------------------------------------------- #
def render_preview(clawd_v, clawd_f, shell_v, shell_f, place):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    # Front shell local coords -> Clawd coords: rotate 180 deg about X, front (z=-7) aligned with the face
    sv = np.column_stack([shell_v[:, 0] + place["dx"],
                          place["eye_y"] - shell_v[:, 1],
                          place["face_z"] - 7 - shell_v[:, 2] - POCKET_EXTRA_DEPTH])

    def shade(v, f, base):
        tri = v[f]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
        k = np.clip(n @ np.array([0.35, 0.45, 0.8]), 0.25, 1)[:, None]
        return tri, np.clip(np.array(base)[None, :] * (0.45 + 0.55 * k), 0, 1)

    # Put both meshes in one collection so matplotlib depth-sorts them together
    t1, c1 = shade(clawd_v, clawd_f, (0.85, 0.47, 0.34))
    t2, c2 = shade(sv, shell_f, (0.15, 0.15, 0.17))
    tri, fc = np.concatenate([t1, t2]), np.concatenate([c1, c2])
    # Model (X right, Y up, Z toward face) -> plot (x, y, z) = (X, -Z, Y): Y up, face toward -y
    tri = tri[..., [0, 2, 1]] * [1, -1, 1]

    views = [("front", 10, -90), ("front-right (USB slot)", 25, -50), ("back", 15, 120)]
    fig = plt.figure(figsize=(15, 5.5))
    for i, (title, el, az) in enumerate(views):
        ax = fig.add_subplot(1, 3, i + 1, projection="3d")
        ax.add_collection3d(Poly3DCollection(tri, facecolors=fc, edgecolor="none"))
        ax.set_xlim(-60, 60); ax.set_ylim(-60, 60); ax.set_zlim(-50, 70)
        ax.view_init(el, az)
        ax.set_box_aspect((1, 1, 1))
        ax.set_axis_off()
        ax.set_title(title)
    plt.tight_layout()
    plt.savefig(OUT_PNG, dpi=90)


# --------------------------------------------------------------------------- #
def main():
    if not os.path.isfile(CLAWD_3MF):
        sys.exit("Not found: %s\nDownload the 3MF of \"Clawd the Claude Code mascot "
                 "(AMS print-in-place)\" by h2d2 from MakerWorld and save it under this name." % CLAWD_3MF)
    clawd, place = build_clawd()
    cv, cf = from_manifold(clawd)
    lo, hi = cv.min(0), cv.max(0)
    print("Clawd size (W x H x D): %.1f x %.1f x %.1f mm" % tuple(hi - lo))
    print("Volume %.1f cm3" % (clawd.volume() / 1000))

    shell_v, shell_f = read_mesh(CASE_3MF, "3D/Objects/object_4.model")
    lid_v, lid_f = read_mesh(CASE_3MF, "3D/Objects/object_1.model")
    b1_v, b1_f = read_mesh(CASE_3MF, "3D/Objects/object_2.model")
    b2_v, b2_f = read_mesh(CASE_3MF, "3D/Objects/object_3.model")

    # The back cover and buttons print upside down, as in the original file: rotate 180 deg
    # about X (a rotation, so the triangle winding stays the same)
    flip = np.array([1, -1, -1])
    sit = lambda v: v - [0, 0, v[:, 2].min()]   # bottom face on the build plate
    objects = [
        # Clawd prints face up (same as the original file), no supports needed
        ("Clawd_body", sit(cv), cf, (128, 158, 0)),
        ("TDisplay_front_shell", sit(shell_v), shell_f, (88, 78, 0)),
        ("TDisplay_back_cover", sit(lid_v * flip), lid_f, (156, 78, 0)),
        ("TDisplay_button_1", sit(b1_v * flip), b1_f, (200, 78, 0)),
        ("TDisplay_button_2", sit(b2_v * flip), b2_f, (212, 78, 0)),
    ]
    write_3mf(OUT_3MF, objects)
    print("Wrote", OUT_3MF)

    render_preview(cv, cf, shell_v, shell_f, place)
    print("Wrote", OUT_PNG)


if __name__ == "__main__":
    main()
