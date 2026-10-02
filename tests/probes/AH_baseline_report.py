# -*- coding: utf-8 -*-
"""CB_FM_Amp 基线量化报告(离线,读实测 dump)。2026-09-29。

四类问题判定规则:
  大C/回绕   wire 长 / 端点 L1 下界 > 1.3 且含同轴向折返段
  S形/贴边   同轴两端(共 x 或共 y)却拐弯的绕行
  平行出线   同一 net 两条水平段 y 不同、x 区间重叠、走向相反或相同
  接地       GROUND 接入来线方向 vs 符号体方位(0° 体在右,实测建档)
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
dump = json.loads(json.load(open(os.path.join(HERE, "cb_amp_dump.json"), encoding="utf-8"))["stdout"])
pins = json.loads(json.load(open(os.path.join(HERE, "cb_amp_pins.json"), encoding="utf-8"))["stdout"])

wires = dump["wires"]
# net 归属:pin snap -> net
snap2net = {}
for name, pl in pins["pins"].items():
    for p in pl:
        snap2net[(round(p["snap"][0], 3), round(p["snap"][1], 3))] = p["net"]

def seglen(a, b):
    return abs(a[0]-b[0]) + abs(a[1]-b[1])

def bends(pts):
    return sum(1 for a, b, c in zip(pts, pts[1:], pts[2:])
               if (a[0] == b[0]) != (b[0] == c[0]))

def backtrack(pts):
    # 同轴来回:水平段序列里 x 先增后减(或反向);竖直段同理
    hs = [(min(a[0], b[0]), max(a[0], b[0])) for a, b in zip(pts, pts[1:]) if abs(a[1]-b[1]) < 1e-9]
    vs = [(min(a[1], b[1]), max(a[1], b[1])) for a, b in zip(pts, pts[1:]) if abs(a[0]-b[0]) < 1e-9]
    ok = True
    for seq in (hs, vs):
        dirs = [1 if s[1] > s[0] else -1 for s in seq if s[1] > s[0]]
        # 相邻同向段允许;方向翻转即折返
        for i in range(1, len(seq)):
            a, b = seq[i-1], seq[i]
            if b[1] > b[0] and a[1] > a[0] and b[0] < a[0] - 1e-9:
                ok = False
            if b[1] > b[0] and a[1] > a[0] and b[0] > a[1] + 1e-9:
                ok = False
    return ok

rows = []
total_len = total_lb = 0
for i, w in enumerate(wires):
    L = sum(seglen(a, b) for a, b in zip(w, w[1:]))
    lb = seglen(w[0], w[-1])
    total_len += L
    total_lb += lb
    nb = bends(w)
    net = "?"
    for p in (w[0], w[-1]):
        n2 = snap2net.get((round(p[0], 3), round(p[1], 3)))
        if n2:
            net = n2
            break
    rows.append((i+1, net, w, L, lb, L/lb if lb else 0, nb, backtrack(w)))

print("=== 每条 wire:net / 长 / L1下界 / 绕行系数 / 拐弯 / 折返 ===")
for i, net, w, L, lb, r, nb, bt in rows:
    flag = ""
    if r > 1.3: flag += " 大C/回绕"
    if lb > 0 and (abs(w[0][0]-w[-1][0]) < 1e-9 or abs(w[0][1]-w[-1][1]) < 1e-9) and nb > 0:
        flag += " S形/贴边绕行"
    if not bt: flag += " 反向折返"
    if flag:
        print(f"wire{i:2d} net={net:6s} L={L:6.2f} lb={lb:5.2f} r={r:4.2f} bends={nb}{flag}")
        print("      ", w)

print()
print(f"总导线长 {total_len:.2f} 格,L1 下界 {total_lb:.2f},总体绕行系数 {total_len/total_lb:.3f}")

# 平行出线:同 net 的水平段对
print()
print("=== 同 net 平行水平段(候选平行出线) ===")
hsegs = []
for i, w in enumerate(wires):
    wnet = "?"
    for p in (w[0], w[-1]):
        n2 = snap2net.get((round(p[0], 3), round(p[1], 3)))
        if n2:
            wnet = n2
            break
    for a, b in zip(w, w[1:]):
        if abs(a[1]-b[1]) < 1e-9 and abs(a[0]-b[0]) > 1e-9:
            hsegs.append((wnet, min(a[0], b[0]), max(a[0], b[0]), a[1], i+1))
for i in range(len(hsegs)):
    for j in range(i+1, len(hsegs)):
        n1, x1a, x1b, y1, w1 = hsegs[i]
        n2, x2a, x2b, y2, w2 = hsegs[j]
        if n1 != n2 or abs(y1-y2) < 1e-9:
            continue
        if min(x1b, x2b) - max(x1a, x2a) > 1e-9:
            print(f"net {n1}: y={y1}(wire{w1}) 与 y={y2}(wire{w2}) 平行,x 重叠 "
                  f"[{max(x1a,x2a):.2f},{min(x1b,x2b):.2f}], 间距 {abs(y1-y2):.2f}")

# GND 朝向
print()
print("=== GROUND 朝向核对(0° 符号体在 origin 右侧;竖直来线应 90/270) ===")
for g, (gx, gy) in {"G1": (8, -9), "G2": (25, -9), "G3": (14, 5), "G4": (16, -3),
                    "G5": (12, -3), "G6": (12, 3), "G7": (10, 12)}.items():
    # 找接入 wire:端点是 (gx,gy)
    for i, w in enumerate(wires):
        if any(abs(p[0]-gx) < 1e-9 and abs(p[1]-gy) < 1e-9 for p in (w[0], w[-1])):
            other = w[0] if (abs(w[-1][0]-gx) < 1e-9 and abs(w[-1][1]-gy) < 1e-9) else w[-1]
            if abs(other[0]-gx) < 1e-9:
                come_from_top = other[1] > gy
                need = 270 if come_from_top else 90
                print(f"{g} ({gx},{gy}): 来线竖直({'从上' if come_from_top else '从下'}), 应 {need}°, 实际 0° -> 错位")
            break
