"""只读探测：为「写入备份 + 保存后只读复核 + 仿真前门禁」收集 API 事实。

结论会固化进 ads_ops.py 的实现注释。全程不做任何写盘操作
（APPEND 句柄不 save 就关闭，磁盘不变）。
"""
import os

w = ws()
ws_path = str(w.path)
print("workspace:", ws_path)

# ---- 1) 库目录布局：cell 物理目录名 vs 报告的 cell 名（验证转义规则） ----
print("\n== 1) 库目录布局 ==")
for lib in (w.libraries or []):
    ln = str(lib.name)
    lp = os.path.join(ws_path, ln)
    if not os.path.isdir(lp):
        print(f"LIB {ln}: 目录不存在于 {lp}")
        continue
    dirs = sorted(d for d in os.listdir(lp)
                  if os.path.isdir(os.path.join(lp, d)))[:15]
    print(f"LIB {ln}: dirs={dirs}")
    cells = [str(c.name) for c in (lib.cells or [])][:8]
    if cells:
        print(f"     cells={cells}")

# ---- 2) ws/library/cell/view 对象上有没有路径属性 ----
print("\n== 2) 路径相关属性 ==")
target = None
for lib in (w.libraries or []):
    for c in (lib.cells or []):
        try:
            views = [str(getattr(v, "view_name", getattr(v, "name", "")))
                     for v in (c.views or [])]
        except Exception:
            continue
        if "schematic" in views:
            target = (str(lib.name), str(c.name))
            break
    if target:
        break
print("样本设计:", target)
if target:
    tl, tc = target
    d = odesign(tl, tc, "schematic", write=False)
    try:
        pathish = lambda obj: [n for n in dir(obj) if "path" in n.lower()]
        print("design.path 属性:", pathish(d))
        for attr in pathish(d):
            try:
                print(f"  design.{attr} =", getattr(d, attr))
            except Exception as e:
                print(f"  design.{attr} -> {type(e).__name__}: {e}")
        try:
            cell_obj = d.cell
            print("design.cell path 属性:", pathish(cell_obj))
        except Exception as e:
            print("design.cell ->", type(e).__name__, e)
    finally:
        d.close_design()

# ---- 3) APPEND 句柄未关时，READ_ONLY 重开是否可用（save_verify 的前提） ----
print("\n== 3) APPEND 打开期间重开只读 ==")
if target:
    tl, tc = target
    da = odesign(tl, tc, "schematic", write=True)
    try:
        n_a = len(list(da.instances))
        print(f"APPEND 句柄实例数: {n_a}")
        try:
            dr = odesign(tl, tc, "schematic", write=False)
            try:
                n_r = len(list(dr.instances))
                print(f"同一设计 READ_ONLY 重开实例数: {n_r}  -> {'一致' if n_r == n_a else '不一致！'}")
            finally:
                dr.close_design()
        except Exception as e:
            print("READ_ONLY 重开失败:", type(e).__name__, e)
    finally:
        da.close_design()

# ---- 4) 已连线设计的 InstPin.net 是否有值（连通性核对的前提） ----
print("\n== 4) InstPin.net 实测 ==")
if target:
    tl, tc = target
    d = odesign(tl, tc, "schematic", write=False)
    try:
        for inst in list(d.instances)[:6]:
            name = getattr(inst, "inst_name", "") or getattr(inst, "name", "")
            master = getattr(inst, "master_name", "") or ""
            rows = []
            try:
                for p in list(inst.inst_pins)[:4]:
                    net = getattr(p, "net", None)
                    rows.append(str(net))
            except Exception as e:
                rows = [f"<{type(e).__name__}>"]
            print(f"  {name}({master}): nets={rows}")
        # 网络集合
        try:
            nets_attr = [n for n in dir(d) if "net" in n.lower()]
            print("  design 上 net 相关成员:", nets_attr)
        except Exception:
            pass
    finally:
        d.close_design()

print("\n探测完成")
