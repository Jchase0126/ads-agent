"""只读探测（第 2 轮）：以可写库 AI_lib 里的真实设计为样本。

要核实：
  1) Cell.path / design.cell.path 的返回值（备份定位的正路）
  2) 已连线设计 InstPin.net 是否有值（连通性核对前提）
  3) APPEND 句柄先关、再 READ_ONLY 重开的实例数核对（save_verify 顺序）
  4) lib.cells 上的 cell.path 是否同样可用（不用打开设计就能定位备份源）

全程不写盘。
"""
import os

w = ws()
ws_path = str(w.path)
print("workspace:", ws_path)

ai = None
for lib in (w.libraries or []):
    if str(lib.name) == "AI_lib":
        ai = lib
        break
print("AI_lib found:", ai is not None)

# ---- 4) lib.cells 上的 cell.path（不打开设计就能定位） ----
print("\n== cell.path（lib.cells 迭代）==")
cell_paths = {}
for c in (ai.cells or []):
    cn = str(c.name)
    try:
        p = str(getattr(c, "path", ""))
    except Exception as e:
        p = f"<{type(e).__name__}: {e}>"
    cell_paths[cn] = p
    print(f"  {cn}: path={p}  exists={os.path.isdir(p) if p and not p.startswith('<') else '-'}")

# ---- 1)+2)+3) 打开真实设计 ----
target = None
for cn in cell_paths:
    try:
        views = [str(getattr(v, "view_name", "")) for v in (ai.cell(cn).views or [])]
    except Exception:
        continue
    if "schematic" in views:
        target = cn
        break
print("\n样本:", target)

if target:
    d = odesign("AI_lib", target, "schematic", write=False)
    try:
        insts = list(d.instances)
        print("READ_ONLY 实例数:", len(insts))
        try:
            print("design.cell.path =", str(getattr(d.cell, "path", "")))
        except Exception as e:
            print("design.cell.path ->", type(e).__name__, e)
        print("\n== InstPin.net 实测 ==")
        for inst in insts[:8]:
            name = getattr(inst, "inst_name", "") or getattr(inst, "name", "")
            master = getattr(inst, "master_name", "") or ""
            rows = []
            try:
                for p in list(inst.inst_pins):
                    rows.append(str(getattr(p, "net", None)))
            except Exception as e:
                rows = [f"<{type(e).__name__}>"]
            print(f"  {name}({master}): nets={rows}")
        netish = [n for n in dir(d) if "net" in n.lower()]
        print("design net 相关成员:", netish)
    finally:
        try:
            d.close_design()
        except Exception as e:
            print("close(READ_ONLY) ->", type(e).__name__, e)

    # ---- 3) 先开 APPEND → 关 → 重开 READ_ONLY，核对实例数 ----
    print("\n== save_verify 顺序实测（APPEND 先关再重开只读）==")
    da = odesign("AI_lib", target, "schematic", write=True)
    n_a = len(list(da.instances))
    da.close_design()
    print("APPEND 实例数:", n_a, "（句柄已关）")
    dr = odesign("AI_lib", target, "schematic", write=False)
    n_r = len(list(dr.instances))
    dr.close_design()
    print("READ_ONLY 重开实例数:", n_r, "->", "一致" if n_a == n_r else "不一致！")

print("\n探测完成")
