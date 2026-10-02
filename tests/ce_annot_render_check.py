# -*- coding: utf-8 -*-
"""CE 标注验收:print_design PDF 渲染级测量(修改前后通用)。

用法: python tests/ce_annot_render_check.py <pdf> <wires.json> [--json out.json]

测量(设计坐标):
  1. 每个实例的 文字bbox-符号bbox 距离(归属距离: 文字框到符号框的间隙;
     相交/接触=0);
  2. 文字-文字重叠对;
  3. 文字-导线相交(水平线×竖放文字=soft 单列);
  4. 图面范围。
依赖 pymupdf;实例符号 bbox 从 stdin 传入的 JSON(probe 导出)读取:
  {"inst": [x0,y0,x1,y1], ...}
"""
import json
import sys

import pymupdf

HERE = __import__("os").path.dirname(__file__)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from check_render_geometry import fit_mapping  # noqa: E402


def main():
    pdf = sys.argv[1]
    wires = json.load(open(sys.argv[2], encoding="utf-8"))
    segs_des = []
    for poly in wires:
        pts = [tuple(p) for p in poly]
        for a, b in zip(pts, pts[1:]):
            segs_des.append([a[0], a[1], b[0], b[1]])
    inst_boxes = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}
    m = fit_mapping(pdf, segs_des)
    if m is None:
        print("MAPPING-FAIL")
        return 2
    k, x0, y0 = m

    def to_des(px, py):
        return ((px - x0) / k, (y0 - py) / k)

    page = pymupdf.open(pdf)[0]
    words = page.get_text("words")  # x0,y0,x1,y1,word,...
    # 词按行聚合(同一 text block 同 y)
    lines = {}
    for w in words:
        key = (w[5], w[6])  # block, line
        box = (w[0], w[1], w[2], w[3])
        if key in lines:
            b = lines[key]
            lines[key] = (min(b[0], box[0]), min(b[1], box[1]),
                          max(b[2], box[2]), max(b[3], box[3]),
                          b[4] + " " + w[4])
        else:
            lines[key] = (box[0], box[1], box[2], box[3], w[4])
    texts = []
    for b, ln in lines.items():
        x0p, y0p, x1p, y1p, s = ln
        dx0, dy1 = to_des(x0p, y1p)
        dx1, dy0 = to_des(x1p, y0p)
        texts.append({"text": s, "box": [round(dx0, 3), round(dy0, 3),
                                         round(dx1, 3), round(dy1, 3)]})

    def gap(a, b):
        dx = max(b[0] - a[2], a[0] - b[2], 0)
        dy = max(b[1] - a[3], a[1] - b[3], 0)
        return round((dx * dx + dy * dy) ** 0.5, 3)

    def overlap(a, b, pad=0.02):
        return not (a[2] < b[0] - pad or b[2] < a[0] - pad
                    or a[3] < b[1] - pad or b[3] < a[1] - pad)

    # 1. 归属距离:每条文字到"最近的符号"及该符号名
    report = {"mapping": [round(v, 4) for v in (k, x0, y0)],
              "texts": texts, "attribution": [], "text_text": [],
              "text_wire_hard": [], "text_wire_soft": []}
    for t in texts:
        best_i, best_g = None, None
        for name, bb in inst_boxes.items():
            g = gap(t["box"], bb)
            if best_g is None or g < best_g:
                best_g, best_i = g, name
        report["attribution"].append({
            "text": t["text"][:40], "nearest": best_i, "gap": best_g,
            "inside": bool(best_i and overlap(t["box"],
                                              inst_boxes[best_i], 0.0))})

    # 2. 文字-文字重叠
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            if overlap(texts[i]["box"], texts[j]["box"]):
                report["text_text"].append(
                    [texts[i]["text"][:30], texts[j]["text"][:30]])

    # 3. 文字-导线相交(渲染文字框 × 设计导线段)
    #    box 约定 [l, top, r, bottom](设计坐标,y 向上)
    for t in texts:
        z = t["box"]
        for x1, y1, x2, y2 in segs_des:
            if abs(y1 - y2) < 1e-6:
                horiz = True
                if not (z[3] - 0.05 <= y1 <= z[1] + 0.05
                        and max(x1, x2) >= z[0] - 0.05
                        and min(x1, x2) <= z[2] + 0.05):
                    continue
            elif abs(x1 - x2) < 1e-6:
                horiz = False
                if not (z[0] - 0.05 <= x1 <= z[2] + 0.05
                        and max(y1, y2) >= z[3] - 0.05
                        and min(y1, y2) <= z[1] + 0.05):
                    continue
            else:
                continue
            entry = [t["text"][:30], [x1, y1, x2, y2]]
            (report["text_wire_soft"] if horiz else
             report["text_wire_hard"]).append(entry)

    xs = [c for t in texts for c in (t["box"][0], t["box"][2])]
    ys = [c for t in texts for c in (t["box"][1], t["box"][3])]
    report["extent"] = [round(min(xs), 2), round(min(ys), 2),
                        round(max(xs), 2), round(max(ys), 2)]
    out = json.dumps(report, ensure_ascii=False)
    if "--json" in sys.argv:
        json.dump(report, open(sys.argv[sys.argv.index("--json") + 1], "w",
                               encoding="utf-8"), ensure_ascii=False, indent=1)
    human = [f"映射 k={k:.3f}; 文字块 {len(texts)} 个;图面范围 {report['extent']}",
             f"文字互压 {len(report['text_text'])} 对: "
             + "; ".join(f"{a}|{b}" for a, b in report["text_text"][:8]),
             f"线穿字-硬 {len(report['text_wire_hard'])} 处, "
             f"线穿字-水平软 {len(report['text_wire_soft'])} 处"]
    for a in report["attribution"]:
        if a["gap"] is not None and a["gap"] > 0.6:
            human.append(f"  悬空: '{a['text']}' 距最近符号 {a['nearest']} "
                         f"gap={a['gap']}")
    print("\n".join(human))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
