# -*- coding: utf-8 -*-
"""print_design PDF 渲染级复核：线穿字 / 斜线（对称布局通用映射拟合）。

映射拟合：从设计导线段与 PDF 线段的对应关系暴力搜索 K/X0/Y0 —— 任取两条
设计水平导线与两条 PDF 水平线段按端点配对解出候选映射，再数「多少条设计
导线在 PDF 里原样出现」，取最多者。比等长等距三元组法更通用（多臂布局
同一行会有多段导线，跨距不等；两级级联的行上有两段等长线）。

用法: python tests/render_check_sym.py <pdf> <case>
  case ∈ {ml, 2stage, 3way}  —— 取 tests/iterate_ml_sym_metrics.py 的案例
退出码非 0 = 存在线穿字或斜线。
"""
import sys

import pymupdf

import iterate_ml_sym_metrics as met

CASES = {
    "ml": (met.spec_ml, met.CONNS),
    "2stage": (None, met.CONN_2STAGE),
    "3way": (None, met.CONN_3WAY),
    "clin": (None, met.CONN_CLIN),
}


def design_wires(case):
    if case.endswith(".json"):
        # 设计坐标的真实导线清单（探针从 design.shapes 导出，折线逐段展开）
        raw = __import__("json").load(open(case))
        wires = []
        for w in raw:
            pts = [(float(x), float(y)) for x, y in w]
            for a, b in zip(pts, pts[1:]):
                wires.append((a, b))
        return None, wires
    import ads_ops
    get_spec, conns = CASES[case]
    specs = get_spec() if get_spec else (
        met.SPEC_2STAGE if case == "2stage"
        else met.SPEC_3WAY if case == "3way" else met.SPEC_CLIN)
    met._CUR_SPECS = specs
    placed = ads_ops.auto_layout_positions(specs, conns, met.VAR_SPEC)
    wires = []
    for c in conns:
        pa = met.pin_xy(placed[c["a"][0]], met.master_of(c["a"][0]), c["a"][1])
        pb = met.pin_xy(placed[c["b"][0]], met.master_of(c["b"][0]), c["b"][1])
        wires.append((pa, pb))
    return placed, wires


def main(pdf, case):
    placed, wires = design_wires(case)
    print(f"设计导线 {len(wires)} 条")
    page = pymupdf.open(pdf)[0]
    segs = []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                segs.append(((p1.x, p1.y), (p2.x, p2.y)))

    h_design = [(a, b) for a, b in wires if abs(a[1] - b[1]) < 1e-9]
    h_pdf = [(a, b) for a, b in segs if abs(a[1] - b[1]) < 1e-6
             and abs(a[0] - b[0]) > 1.0]

    def try_map(d1, d2, p1, p2):
        # 页 = (X0 + K*x, Y0 - K*y)，配对：d1.p1 -> p1.p1，d2.p1 -> p2.p1
        den = d2[0][0] - d1[0][0]
        if abs(den) < 1e-9:
            return None
        k = (p2[0][0] - p1[0][0]) / den
        if k <= 5:
            return None
        x0 = p1[0][0] - k * d1[0][0]
        y0 = p1[0][1] + k * d1[0][1]
        return k, x0, y0

    def hit(k, x0, y0, dw):
        (dx1, dy1), (dx2, dy2) = dw
        px1, py1 = x0 + k * dx1, y0 - k * dy1
        px2, py2 = x0 + k * dx2, y0 - k * dy2
        for (a, b) in segs:
            if (abs(a[0] - px1) < 1.0 and abs(a[1] - py1) < 1.0
                    and abs(b[0] - px2) < 1.0 and abs(b[1] - py2) < 1.0):
                return True
            if (abs(b[0] - px1) < 1.0 and abs(b[1] - py1) < 1.0
                    and abs(a[0] - px2) < 1.0 and abs(a[1] - py2) < 1.0):
                return True
        return False

    best = None
    for i1 in range(len(h_design)):
        for i2 in range(len(h_design)):
            if i1 == i2:
                continue
            for j1 in range(len(h_pdf)):
                for j2 in range(len(h_pdf)):
                    if j1 == j2:
                        continue
                    m = try_map(h_design[i1], h_design[i2],
                                h_pdf[j1], h_pdf[j2])
                    if not m:
                        continue
                    k, x0, y0 = m
                    score = sum(1 for w in wires if hit(k, x0, y0, w))
                    if best is None or score > best[0]:
                        best = (score, k, x0, y0)
    if not best or best[0] < len(wires) - 2:
        print(f"映射失败（最佳仅命中 {best[0] if best else 0}/{len(wires)} 条导线）")
        return 2
    score, k, x0, y0 = best
    print(f"映射: K={k:.3f} pt/格  X0={x0:.1f}  Y0={y0:.1f}  "
          f"导线命中 {score}/{len(wires)}")

    bad = 0
    MIN_PT = 1.2 * k  # 过滤符号自体装饰短线（VAR 框边划过自己的标签等）
    segs_l = [(a, b) for (a, b) in segs
              if abs(a[0] - b[0]) + abs(a[1] - b[1]) >= MIN_PT]
    for w in page.get_text("words"):
        x0p, y0p, x1p, y1p, text = w[:5]
        for (a, b) in segs_l:
            (xa, ya), (xb, yb) = a, b
            if abs(ya - yb) < 1e-6:
                if (y0p - 1.2 <= ya <= y1p + 1.2
                        and max(xa, xb) >= x0p - 1.2 and min(xa, xb) <= x1p + 1.2):
                    print(f"线穿字: '{text}' 设计x[{(min(xa, xb) - x0) / k:.2f},"
                          f"{(max(xa, xb) - x0) / k:.2f}] y={(y0 - ya) / k:.2f}")
                    bad += 1
            elif abs(xa - xb) < 1e-6:
                if (x0p - 1.2 <= xa <= x1p + 1.2
                        and max(ya, yb) >= y0p - 1.2 and min(ya, yb) <= y1p + 1.2):
                    print(f"线穿字: '{text}' 设计y[{(y0 - max(ya, yb)) / k:.2f},"
                          f"{(y0 - min(ya, yb)) / k:.2f}] x={(xa - x0) / k:.2f}")
                    bad += 1
    diag = [(a, b) for (a, b) in segs_l
            if abs(a[0] - b[0]) > 1.2 * k and abs(a[1] - b[1]) > 1.2 * k]
    for a, b in diag:
        print(f"斜线: ({a[0]:.0f},{a[1]:.0f})->({b[0]:.0f},{b[1]:.0f}) 页面pt")
    bad += len(diag)
    print("渲染复核:", "通过" if bad == 0 else f"{bad} 项命中")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "ml"))
