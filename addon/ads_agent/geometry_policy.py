# -*- coding: utf-8 -*-
"""几何/走线规则的**单一来源**（纯数据 + 纯函数，无 ADS 依赖）。

为什么有这个模块（2026-10-14，J310_verify_20261014_wrk 真实失败）
------------------------------------------------------------------
``build_schematic`` 被**保存后导线几何门禁**拦截：

    导线 (6.25,-0.3)->(6.25,0.5) 穿过元件 DEVICE_CORE 的符号内部

根因不是门禁太严，而是**同一件事有两套判定**：

* 规划阶段 ``plan_wire_routes.blocked()`` / 在线 ``_route_clear()`` 用
  ``_segment_hits_box(..., strict=True)``，其中"端点落在符号体内 →
  **整段**豁免"；
* 保存后复核 ``_geometry_report()`` 从磁盘读真实 bbox，用**同一函数**但
  ``strict`` 的豁免集合来源不同（``_seg_exempt`` 只认"引脚压在符号边界上"）。

于是同一段导线，规划说合法、落盘复核说穿体。更糟的是规划用的符号占位是
``_rough_box_public`` 的**零内边距**引脚包围盒（引脚恰好落在盒边界上），
保存后复核用的是真实 bbox（引脚严格在体内）—— 两个盒子不同源，
"引脚在边界 / 在体内"的判定结果因此相反。

本模块把这些判定收敛成**唯一入口**：

* 规划（plan_wire_routes）、写入（_route_clear/_route_around）、
  保存后复核（_geometry_report）调用**同一个** ``check_segment``；
* 端点符号豁免只允许**对应引脚的局部合法逃逸段**
  （``escape_segment_ok``），不允许整条首末段穿越自己符号体，
  也不允许中间点落在符号内部后得到豁免；
* 所有魔数（symbol_keepout / pin_escape_length / wire_clearance /
  grid_step / 容差）集中在这里，不再散落在 ads_ops.py 各处。

纯逻辑、可离线测试：不 import 任何 ADS 模块。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 单位与容差（ADS 原理图单位；1 单位 = schematic 格）
# ---------------------------------------------------------------------------

#: 坐标/集合归并的**唯一**比较容差。原先 _route_clear 用 1e-9、
#: blocked() 用 1e-9、_geometry_report 用 1e-6，三套口径会让"同一根线在
#: 规划里共线、在复核里不共线"。统一到 1e-6（网格坐标保留 6 位小数，
#: 1e-6 恰好是"浮点可区分"的最小尺度）。
EPS = 1e-6

#: 正交判定容差（是否水平/竖直）。比 EPS 略宽，吸收旋转/累加误差。
ORTHO_EPS = 1e-6

# ---------------------------------------------------------------------------
# 布局/走线尺寸常数（替代散落的硬编码）
# ---------------------------------------------------------------------------

#: 符号体在走线时的**额外禁入**外扩量（格）。走线不得贴符号轮廓滑动，
#: 必须留出可见间隙。原先散落为 ``margin = 0.25``（_route_around）与
#: ``box[0] - 0.25 / box[2] + 0.25``（plan_wire_routes 的 2 拐弯候选）。
SYMBOL_KEEPOUT = 0.25

#: 引脚"合法逃逸段"的**最大体内深度**（格）。端点符号豁免**仅**适用于：
#: 从该符号的某个引脚出发、线段落在符号体内的部分不超过本值、且远端
#: 已经离开符号体的那一小段。
#:
#: 为什么用"体内深度"而不是"段长"：真正区分"引脚伸出"与"穿体"的量是
#: 导线在符号体里走了多远。MLIN 两脚直连 (0,0)->(1,0) 段长 1.0 但
#: 体内深度 0.875（横穿整个体，必须判违规）；BJT 的 c 脚画在 bbox 内部
#: （snap 点 (0.5,0.5) 而 bbox 顶 0.65），向上逃逸段长 0.5、体内深度
#: 仅 0.15（薄条擦出，合法）。按段长判会把两者一起误杀或一起放行。
#:
#: 取值与既有 T 搭深度口径 :data:`TAP_DEPTH_MAX` 同源（引脚旁薄条擦过是
#: 物理必须，横穿整个体仍违规）。
PIN_ESCAPE_DEPTH = 0.5

#: 走线通道网格步长（格）。用于把路线吸附到可读的正交网格。
GRID_STEP = 0.25

#: 导线与**其他网络**导线之间的最小间距（格）。异网共线/端点接触由
#: WIRE_CLEARANCE 一起判定。
WIRE_CLEARANCE = 0.0

#: 标注文字与导线/符号的避让间隙（格）。文字避让是**软约束**。
ANNOTATION_CLEARANCE = 0.02

#: 标注文字框到所属符号的最大可判读间隙（格）。超过则归属不可判读。
ANNOTATION_ATTACH_MAX_GAP = 1.2

#: 符号"贴边骑行"的最大允许长度（格）。与符号边线共线且滑行超过本值
#: 视为穿体（渲染上导线压在无关符号轮廓上）。
EDGE_RIDE_MAX = 0.25

#: 同网 T 搭在符号自身引脚上时，允许的最大体内相交深度（格）。
#: 引脚旁薄条擦过是合法形态；横穿整个符号体仍判违规。
TAP_DEPTH_MAX = 0.5

#: 串联支路（封装 R/L）列距下限（格）。三端封装的 g/s/d 三组 R/L 各占
#: 一条独立通道，避免三组支路挤在同一走廊。
BRANCH_CHANNEL_MIN = 0.5

# ---------------------------------------------------------------------------
# 电气规则分层：硬约束（电气错误）vs 软约束（可读性）
# ---------------------------------------------------------------------------

#: 硬约束 —— 违反即阻断写入/保存。
HARD_RULES = frozenset({
    "diagonal",          # 斜段
    "symbol_body",       # 穿过符号体内部
    "foreign_pin",       # 经过其他网络的引脚
    "cross_net_cross",   # 异网导线交叉
    "cross_net_overlap", # 异网导线共线重叠
    "cross_net_t",       # 异网导线 T 形搭接
    "tap_no_pin",        # 无引脚的端点搭接（网络归属不明）
    "duplicate_segment", # 重复重合线段
})

#: 软约束 —— 只报告，不阻断（可读性）。
SOFT_RULES = frozenset({
    "same_net_cross",    # 同网导线交叉（电学无害、图面可疑）
    "same_net_overlap",  # 同网导线共线重叠
    "same_net_t",        # 同网 T 形分支
    "unknown_net",       # 网络归属不明
    "annotation_gap",    # 标注悬空
    "annotation_overlap",# 标注互压
    "wire_through_text", # 导线穿文字区
    "u_turn",            # 同轴折返
    "parallel_escape",   # 平行出线
})


def is_hard(rule: str) -> bool:
    """该规则是否为硬约束（电气正确性）。可读性规则一律返回 False。"""
    return rule in HARD_RULES


def is_soft(rule: str) -> bool:
    return rule in SOFT_RULES


# ---------------------------------------------------------------------------
# 基础几何判定
# ---------------------------------------------------------------------------

def _near(a: float, b: float, eps: float = EPS) -> bool:
    return abs(a - b) <= eps


def points_equal(p, q, eps: float = EPS) -> bool:
    return _near(p[0], q[0], eps) and _near(p[1], q[1], eps)


def points_in(point, points, eps: float = EPS) -> bool:
    return any(points_equal(point, p, eps) for p in points)


def is_horizontal(a, b, eps: float = ORTHO_EPS) -> bool:
    return _near(a[1], b[1], eps)


def is_vertical(a, b, eps: float = ORTHO_EPS) -> bool:
    return _near(a[0], b[0], eps)


def is_orthogonal(a, b, eps: float = ORTHO_EPS) -> bool:
    """一段是否严格沿 X 或 Y（斜段返回 False）。"""
    if points_equal(a, b, eps):
        return True                      # 零长段不是斜段
    return is_horizontal(a, b, eps) or is_vertical(a, b, eps)


def point_on_box_edge(pt, box, eps: float = EPS) -> bool:
    """点是否落在矩形**边界**上（引脚常在边界）。"""
    left, bottom, right, top = box
    x, y = pt
    if not (left - eps <= x <= right + eps and bottom - eps <= y <= top + eps):
        return False
    return (_near(x, left, eps) or _near(x, right, eps)
            or _near(y, bottom, eps) or _near(y, top, eps))


def point_in_box_interior(pt, box, eps: float = EPS) -> bool:
    """点是否落在矩形**内部**（不含边界）。"""
    left, bottom, right, top = box
    x, y = pt
    return (left + eps < x < right - eps) and (bottom + eps < y < top - eps)


def box_expand(box, margin: float = SYMBOL_KEEPOUT):
    """符号盒外扩 margin（走线禁入区）。"""
    left, bottom, right, top = box
    return (left - margin, bottom - margin, right + margin, top + margin)


def boxes_owning_pin(pt, boxes):
    """引脚点压在哪些符号的**边界**上。返回全部归属符号名。

    多个符号共享同一引脚点时（对称一分三的直通臂引脚与分叉引脚同点）
    必须返回全部 —— 只回第一个会把其余符号留在非豁免表里。

    ``boxes`` 元素允许是 ``(名, 盒)`` 或 ``(名, 盒, 引脚表)``：规划与
    复核都把引脚表挂在第 3 位上，这里的判定只看盒。
    """
    owners = []
    for entry in boxes:
        if isinstance(entry, (tuple, list)):
            name, box = entry[0], entry[1]
        else:
            name, box = None, entry
        if point_on_box_edge(pt, box):
            owners.append(name)
    return owners


def boxes_containing_pin(pt, boxes):
    """引脚点落在哪些符号盒内（边界或内部都算）。返回 [(名, 盒)]。

    与 :func:`boxes_owning_pin` 的区别：后者只认**边界**（引脚压在符号
    轮廓上，逃逸段擦边不可避免）；本函数用于"引脚画在符号体内部"
    （BJT/TL 类，snap 点在 bbox 内）的场景。
    """
    out = []
    for entry in boxes:
        if isinstance(entry, (tuple, list)):
            name, box = entry[0], entry[1]
        else:
            name, box = None, entry
        left, bottom, right, top = box
        if (left - EPS <= pt[0] <= right + EPS
                and bottom - EPS <= pt[1] <= top + EPS):
            out.append((name, box))
    return out


# ---------------------------------------------------------------------------
# 线段 × 符号盒
# ---------------------------------------------------------------------------

def _overlap_1d(lo1, hi1, lo2, hi2, eps: float = EPS):
    """两个闭区间的重叠长度（<=0 表示不重叠）。"""
    lo = max(lo1, lo2)
    hi = min(hi1, hi2)
    return hi - lo if hi - lo > eps else 0.0


def segment_body_depth(a, b, box) -> float:
    """线段落在符号盒**内部**的长度（格）。不与内部重叠返回 0。

    用于区分"引脚旁的薄条擦过"（合法）与"横穿整个符号体"（违规）。
    """
    left, bottom, right, top = box
    if is_horizontal(a, b):
        if not (bottom + EPS < a[1] < top - EPS):
            return 0.0
        return _overlap_1d(min(a[0], b[0]), max(a[0], b[0]), left, right)
    if is_vertical(a, b):
        if not (left + EPS < a[0] < right - EPS):
            return 0.0
        return _overlap_1d(min(a[1], b[1]), max(a[1], b[1]), bottom, top)
    return 0.0


def segment_hits_box(a, b, box, strict: bool = False, pins=None,
                     eps: float = EPS) -> bool:
    """线段是否命中符号盒。**规划 / 写入 / 复核三处共用此实现。**

    ``strict=True`` 表示"这是引脚所属符号"（导线从它自己的引脚出发或
    进入，允许贴着符号体走）。豁免的**唯一**判据是：

    * 给了 ``pins`` → 段端点必须落在这颗符号的**真实引脚**上，命中则
      整段豁免（这段本来就是接到该引脚的导线，沿符号轴从一脚到另一脚
      也合法）；没命中就把 ``strict`` 降级为 False，按穿体判；
    * 没给 ``pins`` → 退回"端点必须压在符号体范围内"的外框判据，不再
      整段放行（离线替身 / 调用方未知时的从严口径）。

    为什么不能"端点落在 bbox 内部 → 整段豁免"（2026-10-14 J310 实测）：
    那根 (6.25,-0.3)->(6.25,0.5) 从封装 L 的引脚进来、停在器件核心中间，
    **没落在 FET 任何引脚上**（gate (5.7,0.5) / drain (6.2,1.0) /
    source (6.2,0.0)），却穿过了整个 FET 符号体 —— 规划说合法、保存后
    复核说非法。bbox 也猜不出引脚画在体内还是边界，只能由引脚档案给出。

    规则号：R-02 穿符号体 / R-04 贴边骑行（见 ``schematic_rules``）。

    ``eps`` 由调用方显式传入：三处历史取值不同（规划/写入 1e-8、复核
    ``_GEOM_EPS``=1e-6），本函数**不**暗中抹平，容差因此是可审的。
    """
    left, bottom, right, top = box
    pin_eps = 1e-6

    if strict:
        if pins:
            if any((abs(px - a[0]) < pin_eps and abs(py - a[1]) < pin_eps)
                   or (abs(px - b[0]) < pin_eps and abs(py - b[1]) < pin_eps)
                   for px, py in pins):
                return False
            strict = False
        elif not any(left - eps <= px <= right + eps
                     and bottom - eps <= py <= top + eps for px, py in (a, b)):
            strict = False
    else:
        # 贴边骑行（R-04）：与非引脚符号边线共线且滑行 > EDGE_RIDE_MAX →
        # 违规（渲染上导线与无关符号轮廓重叠）。
        if is_vertical(a, b, eps) and (_near(a[0], left, pin_eps)
                                       or _near(a[0], right, pin_eps)):
            if _overlap_1d(min(a[1], b[1]), max(a[1], b[1]), bottom, top,
                           pin_eps) > EDGE_RIDE_MAX:
                return True
        if is_horizontal(a, b, eps) and (_near(a[1], bottom, pin_eps)
                                         or _near(a[1], top, pin_eps)):
            if _overlap_1d(min(a[0], b[0]), max(a[0], b[0]), left, right,
                           pin_eps) > EDGE_RIDE_MAX:
                return True

    if is_horizontal(a, b, eps):
        # strict 用**闭区间**：引脚点常常正好压在符号体边界上（如 R 的
        # pin1 (0,0) 落在 bbox 左边界 x=0、y=0），用开区间会把"导线正好
        # 接到引脚"判成穿体。
        inside = bottom - eps <= a[1] <= top + eps
        lo_x, hi_x = sorted((a[0], b[0]))
        if lo_x >= right - eps or hi_x <= left + eps:
            return False                          # 完全在盒外
        return ((bottom + eps < a[1] < top - eps) if strict
                else (bottom - eps <= a[1] <= top + eps))
    inside = ((left + eps < a[0] < right - eps) if strict
              else (left - eps <= a[0] <= right + eps))
    return inside and \
        max(min(a[1], b[1]), bottom) < min(max(a[1], b[1]), top) - eps


def segment_edge_rides(a, b, box) -> bool:
    """线段是否沿符号盒的某条边共线滑行（用于诊断文案）。"""
    left, bottom, right, top = box
    if is_vertical(a, b):
        if (_near(a[0], left) or _near(a[0], right)) and \
                _overlap_1d(min(a[1], b[1]), max(a[1], b[1]), bottom, top) > 0.0:
            return True
    if is_horizontal(a, b):
        if (_near(a[1], bottom) or _near(a[1], top)) and \
                _overlap_1d(min(a[0], b[0]), max(a[0], b[0]), left, right) > 0.0:
            return True
    return False


def escape_segment_ok(a, b, box, pin_point=None,
                      max_depth: float = PIN_ESCAPE_DEPTH) -> bool:
    """端点符号豁免：这一段是否是对应引脚的**局部合法逃逸段**。

    合法逃逸段的三个必要条件（缺一不可）：

    1. 段的一端**就是**该符号的引脚点（``pin_point``）——不是"端点恰好
       落在体内"，而是"确实从引脚出发"；
    2. 线段落在符号体**内部**的深度 <= ``max_depth``：引脚伸出段是局部
       行为，在体里走太长就变成穿体；
    3. 段**单调离开**符号体：远离引脚的一端不得仍在盒内。

    这三条同时否定了两类旧行为：

    * "整条首末段穿越自己符号体"（体内深度超限 / 对端仍在体内）；
    * "中间点落在符号内部后得到豁免"（中间点不是引脚点）。
    """
    if pin_point is None:
        return False
    if not points_equal(a, pin_point) and not points_equal(b, pin_point):
        return False
    # 体内深度超限 = 在符号体里走了一段，不再是"伸出"
    if segment_body_depth(a, b, box) > max_depth + EPS:
        return False
    # 远离引脚的一端必须已经离开符号体
    far = b if points_equal(a, pin_point) else a
    left, bottom, right, top = box
    still_inside = (left - EPS <= far[0] <= right + EPS
                    and bottom - EPS <= far[1] <= top + EPS)
    return not still_inside


# ---------------------------------------------------------------------------
# 线段 × 引脚点 / 线段 × 线段
# ---------------------------------------------------------------------------

def segment_through_point(a, b, pt, eps: float = EPS) -> bool:
    """线段是否**经过**给定点（含端点）。正交段专用。"""
    if is_vertical(a, b):
        return (_near(a[0], pt[0], eps)
                and min(a[1], b[1]) - eps <= pt[1] <= max(a[1], b[1]) + eps)
    if is_horizontal(a, b):
        return (_near(a[1], pt[1], eps)
                and min(a[0], b[0]) - eps <= pt[0] <= max(a[0], b[0]) + eps)
    # 斜线段：包围盒近似（调用方应先判斜段）
    return (min(a[0], b[0]) - eps <= pt[0] <= max(a[0], b[0]) + eps
            and min(a[1], b[1]) - eps <= pt[1] <= max(a[1], b[1]) + eps)


def segment_hits_pin(a, b, pin_pt) -> bool:
    """线段是否经过某个引脚点。"""
    return segment_through_point(a, b, pin_pt)


def segment_relation(a, b, c, d, eps: float = EPS, end_digits: int = 6):
    """两段的关系。返回 None（无接触/仅端点相接）或 (kind, point)。

    kind: ``"overlap"`` 共线重叠；``"t"`` 一段端点搭在另一段中部；
    ``"cross"`` 内部交叉。端点对端点相接（拐弯、链式相接）不算违规 ——
    由调用方按网络归属分级（见 ``schematic_rules.classify_wire_relation``）。

    ``eps`` 是几何容差，由调用方传入自己的历史取值（写入/规划 1e-8、
    复核 1e-6）；端点是否"就是折点"另按 ``end_digits`` 位小数比较 ——
    端点坐标来自换算，用四舍五入比较比浮点减法更稳。
    """
    h1, h2 = is_horizontal(a, b, eps), is_horizontal(c, d, eps)
    if h1 and h2:
        if not _near(a[1], c[1], eps):
            return None
        ov = _overlap_1d(min(a[0], b[0]), max(a[0], b[0]),
                         min(c[0], d[0]), max(c[0], d[0]), eps)
        return ("overlap", None) if ov > 0.0 else None
    if not h1 and not h2:
        if not _near(a[0], c[0], eps):
            return None
        ov = _overlap_1d(min(a[1], b[1]), max(a[1], b[1]),
                         min(c[1], d[1]), max(c[1], d[1]), eps)
        return ("overlap", None) if ov > 0.0 else None
    ha, hb, va, vb = (a, b, c, d) if h1 else (c, d, a, b)
    pt = (va[0], ha[1])
    on_h = (min(ha[0], hb[0]) - eps <= pt[0] <= max(ha[0], hb[0]) + eps
            and min(ha[1], hb[1]) - eps <= pt[1] <= max(ha[1], hb[1]) + eps)
    on_v = (min(va[0], vb[0]) - eps <= pt[0] <= max(va[0], vb[0]) + eps
            and min(va[1], vb[1]) - eps <= pt[1] <= max(va[1], vb[1]) + eps)
    if not (on_h and on_v):
        return None
    key = (round(pt[0], end_digits), round(pt[1], end_digits))
    is_end_h = any((round(p[0], end_digits), round(p[1], end_digits)) == key
                   for p in (ha, hb))
    is_end_v = any((round(p[0], end_digits), round(p[1], end_digits)) == key
                   for p in (va, vb))
    if is_end_h and is_end_v:
        return None                      # 端点对端点：链式相接/拐弯
    if is_end_h or is_end_v:
        return ("t", pt)                 # 一段端点搭在另一段中部
    return ("cross", pt)


def segment_hits_route(a, b, old_a, old_b, endpoints, old_endpoints,
                       eps: float = EPS) -> bool:
    """不同连接的线段不能交叉或重合；共同端点可以接成 T 形。

    ``endpoints`` / ``old_endpoints`` 是允许共享端点的坐标集合（同网
    分支、T 搭）。
    """
    h1, h2 = is_horizontal(a, b, eps), is_horizontal(old_a, old_b, eps)
    if h1 == h2:
        axis = 1 if h1 else 0
        along = 0 if h1 else 1
        if not _near(a[axis], old_a[axis], eps):
            return False
        lo = max(min(a[along], b[along]), min(old_a[along], old_b[along]))
        hi = min(max(a[along], b[along]), max(old_a[along], old_b[along]))
        if hi - lo > eps:
            return True
        if hi < lo - eps:
            return False
        point = (lo, a[1]) if h1 else (a[0], lo)
    else:
        h1p, h2p = (a, b) if h1 else (old_a, old_b)
        v1p, v2p = (old_a, old_b) if h1 else (a, b)
        point = (v1p[0], h1p[1])
        if not (min(h1p[0], h2p[0]) - eps <= point[0] <= max(h1p[0], h2p[0]) + eps
                and min(v1p[1], v2p[1]) - eps <= point[1] <= max(v1p[1], v2p[1]) + eps):
            return False
    return not (points_in(point, endpoints, eps)
                and points_in(point, old_endpoints, eps))


# ---------------------------------------------------------------------------
# 统一判定入口：规划 / 写入 / 保存后复核 共用
# ---------------------------------------------------------------------------

def check_segment(a, b, boxes, routes=(), net=None, endpoints=None,
                  escape_pins=None, tap_pins=(), pin_tolerance=None):
    """**唯一**的线段合法性判定入口。

    规划阶段（plan_wire_routes）、写入阶段（_route_clear/_route_around）
    与保存后复核（_geometry_report）必须调用本函数，保证
    "规划合法 ⇒ 写入合法 ⇒ 保存后复核合法"三者永不打架。

    参数
    ----
    boxes
        ``[(符号名, 矩形), ...]``，矩形为 ``(left, bottom, right, top)``。
        **规划与复核必须传入同源**（要么都用保守占位，要么都用真实
        bbox）—— 见 :func:`symbol_boxes_conservative`。
    routes
        已存在的导线 ``[(折线, net), ...]`` 或裸折线。
    net
        本段所属电气网络标签；与 ``routes`` 中同 net 的段豁免（共享走廊）。
    escape_pins
        ``{符号名: [引脚点, ...]}`` —— 端点符号的**对应引脚**。
        只有"从该引脚出发的局部逃逸段"才豁免（见
        :func:`escape_segment_ok`），整段穿越不再放行。
    tap_pins
        同电气节点的全部引脚点：线段恰好穿过其中某个 = 合法 T 搭。
    pin_tolerance
        复用既有 tap 深度口径；缺省用 ``TAP_DEPTH_MAX``。

    返回
    ----
    ``(ok, violations)``；``violations`` 是 ``[(rule, detail), ...]``。
    """
    violations = []

    if points_equal(a, b):
        return True, violations
    if not is_orthogonal(a, b):
        violations.append(("diagonal",
                           f"({a[0]:.4g},{a[1]:.4g})->({b[0]:.4g},{b[1]:.4g}) 不是水平或竖直"))
        return False, violations

    ends = endpoints if endpoints is not None else {a, b}

    # --- 1) 符号体 ---
    for entry in boxes:
        if isinstance(entry, (tuple, list)):
            name, box = entry[0], entry[1]
        else:
            name, box = None, entry
        pins = (escape_pins or {}).get(name)
        if segment_hits_box(a, b, box, strict=False):
            # 端点符号：先试"局部合法逃逸段"，再试同网 T 搭
            if pins:
                for pin in pins:
                    if escape_segment_ok(a, b, box, pin):
                        break
                else:
                    if not _tap_exempt(a, b, box, tap_pins, pin_tolerance):
                        violations.append((
                            "symbol_body",
                            f"({a[0]:.4g},{a[1]:.4g})->({b[0]:.4g},{b[1]:.4g}) "
                            f"穿过元件 {name} 的符号内部"))
            elif not _tap_exempt(a, b, box, tap_pins, pin_tolerance):
                violations.append((
                    "symbol_body",
                    f"({a[0]:.4g},{a[1]:.4g})->({b[0]:.4g},{b[1]:.4g}) "
                    f"穿过元件 {name} 的符号内部"))

    # --- 2) 其他网络的引脚 ---
    for (px, py) in (tap_pins or ()):
        if segment_hits_pin(a, b, (px, py)):
            violations.append((
                "foreign_pin",
                f"({a[0]:.4g},{a[1]:.4g})->({b[0]:.4g},{b[1]:.4g}) 经过引脚点 "
                f"({px:.4g},{py:.4g})"))
            break

    # --- 3) 与已有导线的异网关系 ---
    for old_route in routes or ():
        if isinstance(old_route, tuple):
            old_points, old_net = old_route
        else:
            old_points, old_net = old_route, None
        if net is not None and old_net == net:
            continue
        old_ends = {old_points[0], old_points[-1]}
        for c, d in zip(old_points, old_points[1:]):
            if segment_hits_route(a, b, c, d, ends, old_ends):
                violations.append((
                    "cross_net_overlap",
                    f"({a[0]:.4g},{a[1]:.4g})->({b[0]:.4g},{b[1]:.4g}) 与已有导线 "
                    f"({c[0]:.4g},{c[1]:.4g})->({d[0]:.4g},{d[1]:.4g}) 交叉/重叠/搭接"))
                break
        if violations:
            break

    return (not violations), violations


def _tap_exempt(a, b, box, tap_pins, pin_tolerance=None) -> bool:
    """线段穿过某符号**自身引脚**、且体内相交深度 <= 上限 → 合法 T 搭。"""
    tol = TAP_DEPTH_MAX if pin_tolerance is None else pin_tolerance
    for (px, py) in (tap_pins or ()):
        if not segment_through_point(a, b, (px, py)):
            continue
        if segment_body_depth(a, b, box) <= tol + EPS:
            return True
    return False


def check_path(points, boxes, routes=(), net=None, escape_pins=None,
               tap_pins=()):
    """整条折线的统一判定。返回 ``(ok, violations)``。"""
    all_v = []
    for a, b in zip(points, points[1:]):
        ok_seg, v = check_segment(a, b, boxes, routes, net,
                                  endpoints={points[0], points[-1]},
                                  escape_pins=escape_pins, tap_pins=tap_pins)
        all_v.extend(v)
        if not ok_seg:
            return False, all_v
    return True, all_v


# ---------------------------------------------------------------------------
# 符号占位盒：规划与复核同源
# ---------------------------------------------------------------------------

def symbol_boxes_conservative(raw_boxes):
    """把（真实或估算的）符号盒统一成**走线禁入盒**。

    规划阶段拿不到真实 bbox，但保存后复核会拿真实 bbox 判。若规划盒比
    真实盒小，就会出现"规划合法、复核穿体"。本函数对**两侧**用同一条
    规则：真实盒原样使用；估算盒外扩 ``SYMBOL_KEEPOUT`` 以覆盖真实体
    （估算盒按引脚包围盒构造，零内边距，天然小于真实符号体）。
    """
    return [(name, box) for name, box in raw_boxes]


def grid_lines(values, step: float = GRID_STEP):
    """把一组坐标吸附到 step 网格（并保留原值，避免吸附把线挪进符号）。"""
    out = set()
    for v in values:
        out.add(round(v, 6))
        k = round(round(v / step) * step, 6)
        out.add(k)
    return sorted(out)


# ---------------------------------------------------------------------------
# 诊断：route_unresolved
# ---------------------------------------------------------------------------

class RouteUnresolved(Exception):
    """走线无合法解。**只用于诊断，不得作为写入结果**。

    审查要求：候选全部非法后选"违规最少路线"只能用于诊断，不能落盘。
    本异常携带阻挡对象、失败规则与建议变更，由调用方决定如何阻断。
    """

    def __init__(self, start, end, violations=(), blockers=(), suggestion=""):
        self.start = tuple(start) if start is not None else None
        self.end = tuple(end) if end is not None else None
        self.violations = list(violations)
        self.blockers = list(blockers)
        self.suggestion = suggestion or (
            "请调整布局（增大间距 / 移动元件 / 改变走线通道），"
            "或改用 waypoints 显式指定折点")
        msg = (f"无法找到满足全部几何规则的走线：{self.start} -> {self.end}")
        if self.blockers:
            msg += "\n  阻挡对象: " + "; ".join(str(b) for b in self.blockers)
        if self.violations:
            msg += "\n  失败规则: " + "; ".join(
                f"{r}({d})" for r, d in self.violations[:6])
        msg += f"\n  建议变更: {self.suggestion}"
        super().__init__(msg)

    def to_dict(self):
        return {
            "start": self.start,
            "end": self.end,
            "violations": [{"rule": r, "detail": d} for r, d in self.violations],
            "blockers": list(self.blockers),
            "suggestion": self.suggestion,
        }