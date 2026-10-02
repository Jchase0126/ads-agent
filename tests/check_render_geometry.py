"""对 print_design 渲染的 PDF 做几何级核对：
1. 所有长线段（wire）提取并解码回设计坐标；
2. 所有文字 bbox 提取；
3. 检查线-文字相交、文字-文字重叠、斜线。

print_design 的页面映射随设计 bbox 自适应（K/X0/Y0 每轮可能不同）。
给 --wires wires.json（设计坐标 wire bbox 清单，tests/probes/AC_wires.py 导出）
即可自动拟合映射；否则回退到 v2 固定系数。

用法: python tests/check_render_geometry.py <pdf> --wires wires.json [--near 0.15]
退出码非 0 表示存在相交/重叠。
"""
import json
import sys

import pymupdf

# 回退：v2 渲染的坐标映射
K = 30.95
X0, Y0 = 99.85, 228.5


def fit_mapping(pdf, wires_des):
    """用设计 wire 清单拟合 PDF 映射（无旋转无镜像，仅平移+缩放）。"""
    from collections import defaultdict
    page = pymupdf.open(pdf)[0]
    ph = []
    for d in page.get_drawings():
        for item in d['items']:
            if item[0] == 'l':
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 0.3 and abs(p1.x - p2.x) > 20:
                    ph.append((p1.y, min(p1.x, p2.x), max(p1.x, p2.x)))
    dh = [w for w in wires_des if abs(w[1] - w[3]) < 1e-6 and w[2] - w[0] > 0.5]
    if len(ph) < 3 or len(dh) < 3:
        return None
    g_pdf = defaultdict(list)
    for y, a, b in ph:
        g_pdf[round(y, 0)].append((a, b, y))
    g_des = defaultdict(list)
    for x1, y1, x2, y2 in dh:
        g_des[round(y1, 1)].append((x1, x2, y1))
    pdf_top = max(g_pdf.values(), key=len)
    des_top = max(g_des.values(), key=len)
    if len(pdf_top) != len(des_top) or len(pdf_top) < 3:
        return None
    pdf_top.sort(key=lambda t: t[0])
    des_top.sort(key=lambda t: t[0])
    y_pdf = pdf_top[0][2]
    y_des = des_top[0][2]
    k = (sum(t[1] - t[0] for t in pdf_top) /
         sum(t[1] - t[0] for t in des_top))
    x0 = pdf_top[0][0] - k * des_top[0][0]
    y0 = y_pdf + k * y_des
    # 残差验证：PDF 水平线解码 y 应大多落在设计 wire 的 y 集合上
    # （元件符号框线不在 wire 集内，允许少量不可解释线）
    des_ys = sorted({round(w[1], 2) for w in wires_des if abs(w[1] - w[3]) < 1e-6})
    ok = 0
    for y, a, b in ph:
        yd = (y0 - y) / k
        if any(abs(yd - v) < 0.06 for v in des_ys):
            ok += 1
    if ok < len(ph) * 0.6:
        return None
    return (k, x0, y0)


def to_des(px, py):
    return (px - X0) / K, (Y0 - py) / K


def extract(pdf):
    page = pymupdf.open(pdf)[0]
    wires = []      # (x1,y1,x2,y2) 设计坐标
    diagonal = []
    for d in page.get_drawings():
        for item in d['items']:
            if item[0] == 'l':
                p1, p2 = item[1], item[2]
                a = to_des(p1.x, p1.y)
                b = to_des(p2.x, p2.y)
                if abs(a[0] - b[0]) < 0.02 and abs(a[1] - b[1]) < 0.02:
                    continue
                if abs(a[0] - b[0]) < 0.02 or abs(a[1] - b[1]) < 0.02:
                    wires.append((a, b))
                elif abs(a[0] - b[0]) > 0.3 and abs(a[1] - b[1]) > 0.3:
                    diagonal.append((a, b, d.get('stroke_color')))
    texts = []
    for w in page.get_text('words'):
        x0, y0, x1, y1, t = w[:5]
        a = to_des(x0, y0)
        b = to_des(x1, y1)
        texts.append((min(a[0], b[0]), min(a[1], b[1]),
                      max(a[0], b[0]), max(a[1], b[1]), t))
    return wires, diagonal, texts


def seg_rect_hit(seg, rect, near=0.0):
    """水平/竖直线段 vs 矩形（含 near 外扩）。"""
    (x1, y1), (x2, y2) = seg
    l, b, r, t = rect[0] - near, rect[1] - near, rect[2] + near, rect[3] + near
    if abs(y1 - y2) < 1e-9:  # 水平
        if not (b - 1e-9 <= y1 <= t + 1e-9):
            return False
        ox = max(min(x1, x2), l), min(max(x1, x2), r)
        return ox[0] <= ox[1] + 1e-9
    if abs(x1 - x2) < 1e-9:  # 竖直
        if not (l - 1e-9 <= x1 <= r + 1e-9):
            return False
        oy = max(min(y1, y2), b), min(max(y1, y2), t)
        return oy[0] <= oy[1] + 1e-9
    return False


def main():
    global K, X0, Y0
    pdf = sys.argv[1]
    near = 0.15
    if '--near' in sys.argv:
        near = float(sys.argv[sys.argv.index('--near') + 1])
    min_len = 1.2  # 短于此的是元件符号自体装饰线，不参与导线-文字核对
    if '--minlen' in sys.argv:
        min_len = float(sys.argv[sys.argv.index('--minlen') + 1])
    if '--wires' in sys.argv:
        wires_des = json.loads(open(sys.argv[sys.argv.index('--wires') + 1],
                                    encoding='utf-8').read())
        fitted = fit_mapping(pdf, wires_des)
        if fitted:
            K, X0, Y0 = fitted
            print(f'mapping fitted: K={K:.4f} X0={X0:.2f} Y0={Y0:.2f}')
        else:
            print('mapping fit FAILED, falling back to fixed v2 mapping')
    wires, diagonal, texts = extract(pdf)
    wires = [w for w in wires
             if abs(w[0][0] - w[1][0]) + abs(w[0][1] - w[1][1]) >= min_len]
    print(f'wires={len(wires)} texts={len(texts)} diagonals={len(diagonal)}')
    bad = 0
    for a, b, c in diagonal:
        if c not in ((0, 0, 0), None):
            continue  # 彩色装饰线（如选区高亮）不算
        print(f'  [斜线] ({a[0]:.2f},{a[1]:.2f})->({b[0]:.2f},{b[1]:.2f})')
        bad += 1
    hits = []
    for w in wires:
        for t in texts:
            # 相交（near=0）
            if seg_rect_hit(w, t):
                hits.append(('CROSS', w, t))
            elif near > 0 and seg_rect_hit(w, t, near=near):
                hits.append(('NEAR', w, t))
    for kind, w, t in hits:
        (x1, y1), (x2, y2) = w
        print(f'  [{kind}] 线({x1:.2f},{y1:.2f})-({x2:.2f},{y2:.2f}) '
              f'vs 文字[{t[4]}] x[{t[0]:.2f},{t[2]:.2f}] y[{t[1]:.2f},{t[3]:.2f}]')
        bad += (1 if kind == 'CROSS' else 0)
    # 文字-文字重叠（不同行但 bbox 相交面积>0）
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a, b = texts[i], texts[j]
            ox = min(a[2], b[2]) - max(a[0], b[0])
            oy = min(a[3], b[3]) - max(a[1], b[1])
            if ox > 0.02 and oy > 0.02:
                print(f'  [文字重叠] [{a[4]}] 与 [{b[4]}] '
                      f'(交叠 {ox:.2f}x{oy:.2f})')
                bad += 1
    print('RESULT:', 'FAIL' if bad else 'PASS', f'({bad} problems)')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
