#!/usr/bin/env python3
"""
把 TTGO T-Display 外殼嵌進 Clawd 的眼睛位置。

    python build_clawd_tdisplay.py

讀 Claude.3mf（Clawd 本體）與 TTGO_-_Tdisplay_lid.3mf（T-Display 外殼），輸出：
（Claude.3mf 是 MakerWorld Exclusive 授權，不隨 repo 附上，請自行從 MakerWorld 下載）

    Clawd_TDisplay.3mf          Clawd 本體 + 外殼 4 個零件，已擺好列印方向
    Clawd_TDisplay_preview.png  組裝示意圖

做法：Clawd 放大後，臉部在眼睛高度挖出一個口袋，大小 = 組裝好的外殼外型
(56.7 x 30.3 x 14 mm) + 間隙，外殼從正面壓入，與臉齊平，螢幕置中。
右側開一道槽讓 USB-C 線出去，背面開圓孔方便把外殼頂出來。
原本的眼睛凹洞會先填平。

需要: pip install numpy manifold3d matplotlib
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

# ----- 可調參數 (mm) -------------------------------------------------------- #
SCALE_XY = 0.7          # 原檔是 0.5；0.7 時臉部 84 x 56 mm，放得下外殼
SCALE_Z = 0.84          # 原檔是 0.6；維持原本的厚度比例 -> 33.6 mm 厚
CLEARANCE = 0.2         # 口袋每邊比外殼大多少；太緊就加大到 0.25-0.3
POCKET_EXTRA_DEPTH = 0.2  # 外殼正面比臉內縮一點，避免凸出
USB_SLOT_W = 14.0       # USB-C 插頭外殼寬度 + 餘裕
USB_SLOT_H = 10.4       # 槽從 USB 中心往下的深度 = USB_SLOT_H / 2
PUSH_HOLE_D = 12.0      # 背面頂出孔直徑

# T-Display 外殼（前殼 object_4，座標為原檔本地座標，正面在 z = -7）
CASE_L, CASE_H, CASE_T = 56.69, 30.29, 14.0
CASE_R = 2.0                # 四個垂直邊的圓角半徑（正面邊緣沒有倒角）
WINDOW_X = (-19.94, 8.45)   # 螢幕窗在前殼上的 x 範圍
USB_Z = -2.4                # USB 開口中心（在 +x 端，y = 0）

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


# --------------------------------------------------------------------------- #
#  3MF 讀寫
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
    """objects: [(name, verts, tris, (tx, ty, tz))]，頂點已是列印方向。"""
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
#  幾何
# --------------------------------------------------------------------------- #
def to_manifold(v, f):
    m = Mesh(vert_properties=np.asarray(v, np.float32), tri_verts=np.asarray(f, np.uint32))
    m.merge()
    man = Manifold(m)
    if man.status().name != "NoError":
        sys.exit(f"網格不是封閉實體: {man.status()}")
    return man


def box(x0, x1, y0, y1, z0, z1):
    return Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))


def from_manifold(man):
    m = man.to_mesh()
    return np.asarray(m.vert_properties)[:, :3], np.asarray(m.tri_verts)


def build_clawd():
    """回傳 (Clawd 實體, 外殼放置參數)。座標：X 向右、Y 向上、Z 朝臉（+Z 是臉）。"""
    v, f = read_mesh(CLAWD_3MF, "3D/Objects/object_1.model")
    clawd = to_manifold(v, f)

    # 原檔眼睛是 Z 10..20 的凹洞，先填平（原始座標）
    for x0, x1 in ((30, 40), (-40, -30)):
        clawd = clawd + box(x0, x1, 10, 30, 10, 20)
    clawd = clawd.scale((SCALE_XY, SCALE_XY, SCALE_Z))

    face_z = 20 * SCALE_Z
    eye_y = 20 * SCALE_XY                       # 原本眼睛中心的高度
    dx = -(WINDOW_X[0] + WINDOW_X[1]) / 2       # 讓螢幕窗在臉上置中
    body_half_w = 60 * SCALE_XY

    # 口袋輪廓 = 外殼輪廓往外等距 CLEARANCE，圓角半徑因此是 CASE_R + CLEARANCE
    outline = CrossSection.square((CASE_L - 2 * CASE_R, CASE_H - 2 * CASE_R), center=True) \
        .offset(CASE_R + CLEARANCE, JoinType.Round, circular_segments=96)
    pocket_z0 = face_z - CASE_T - POCKET_EXTRA_DEPTH
    pocket = Manifold.extrude(outline, face_z + 1 - pocket_z0) \
        .translate((dx, eye_y, pocket_z0))

    # USB-C 在外殼 +x 端；外殼繞 X 軸轉 180 度後 z -> -z，所以中心在 face_z - (7 + USB_Z)
    usb_z = face_z - (7 + USB_Z)
    usb_slot = box(dx + CASE_L / 2 - 1, body_half_w + 1,
                   eye_y - USB_SLOT_W / 2, eye_y + USB_SLOT_W / 2,
                   usb_z - USB_SLOT_H / 2, face_z + 1)

    push = Manifold.cylinder(face_z * 2, PUSH_HOLE_D / 2, circular_segments=64) \
        .translate((dx, eye_y, -face_z - 1))

    clawd = clawd - pocket - usb_slot - push
    return clawd, dict(dx=dx, eye_y=eye_y, face_z=face_z)


# --------------------------------------------------------------------------- #
#  組裝示意圖
# --------------------------------------------------------------------------- #
def render_preview(clawd_v, clawd_f, shell_v, shell_f, place):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    # 前殼本地座標 -> Clawd 座標：繞 X 軸轉 180 度，正面 (z=-7) 對齊臉
    sv = np.column_stack([shell_v[:, 0] + place["dx"],
                          place["eye_y"] - shell_v[:, 1],
                          place["face_z"] - 7 - shell_v[:, 2] - POCKET_EXTRA_DEPTH])

    def shade(v, f, base):
        tri = v[f]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
        k = np.clip(n @ np.array([0.35, 0.45, 0.8]), 0.25, 1)[:, None]
        return tri, np.clip(np.array(base)[None, :] * (0.45 + 0.55 * k), 0, 1)

    # 兩個網格放進同一個 collection，matplotlib 才會一起排前後順序
    t1, c1 = shade(clawd_v, clawd_f, (0.85, 0.47, 0.34))
    t2, c2 = shade(sv, shell_f, (0.15, 0.15, 0.17))
    tri, fc = np.concatenate([t1, t2]), np.concatenate([c1, c2])
    # 模型 (X 右, Y 上, Z 朝臉) -> 繪圖 (x, y, z) = (X, -Z, Y)：Y 朝上，臉朝 -y
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
        sys.exit("找不到 %s\n請到 MakerWorld 下載「Clawd the Claude Code mascot "
                 "(AMS print-in-place)」（作者 h2d2）的 3MF，存成這個檔名。" % CLAWD_3MF)
    clawd, place = build_clawd()
    cv, cf = from_manifold(clawd)
    lo, hi = cv.min(0), cv.max(0)
    print("Clawd 尺寸 (寬 x 高 x 厚): %.1f x %.1f x %.1f mm" % tuple(hi - lo))
    print("體積 %.1f cm3" % (clawd.volume() / 1000))

    shell_v, shell_f = read_mesh(CASE_3MF, "3D/Objects/object_4.model")
    lid_v, lid_f = read_mesh(CASE_3MF, "3D/Objects/object_1.model")
    b1_v, b1_f = read_mesh(CASE_3MF, "3D/Objects/object_2.model")
    b2_v, b2_f = read_mesh(CASE_3MF, "3D/Objects/object_3.model")

    # 後蓋與按鈕原檔就是翻面列印：繞 X 軸轉 180 度（是旋轉，三角形方向不用反）
    flip = np.array([1, -1, -1])
    sit = lambda v: v - [0, 0, v[:, 2].min()]   # 底面貼平台
    objects = [
        # Clawd 臉朝上列印（和原檔相同），不需支撐
        ("Clawd_body", sit(cv), cf, (128, 158, 0)),
        ("TDisplay_front_shell", sit(shell_v), shell_f, (88, 78, 0)),
        ("TDisplay_back_cover", sit(lid_v * flip), lid_f, (156, 78, 0)),
        ("TDisplay_button_1", sit(b1_v * flip), b1_f, (200, 78, 0)),
        ("TDisplay_button_2", sit(b2_v * flip), b2_f, (212, 78, 0)),
    ]
    write_3mf(OUT_3MF, objects)
    print("已輸出", OUT_3MF)

    render_preview(cv, cf, shell_v, shell_f, place)
    print("已輸出", OUT_PNG)


if __name__ == "__main__":
    main()
