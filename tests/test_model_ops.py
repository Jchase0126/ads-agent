# -*- coding: utf-8 -*-
"""model_ops 纯逻辑测试：lib.defs 解析 / 路径解析 / 参数值解析 / 工具名齐全。

覆盖（全部不需要 ADS 进程）：
  * lib.defs 逐行 token 解析，并直接拿本机 ADS 2027 安装里的**真实**
    lib.defs 验证（DemoKit_mmWave / DemoKit_MTM / Keysight_Photonics_pdk /
    smithdg）—— 格式结论不是凭空想的；
  * 相对路径相对 DEFINE 所在文件解析（不是 cwd）、$VAR 未设时不猜路径、
    引号路径；
  * 参数值数字/单位拆分、master 'lib:cell[:view]' 解析；
  * HANDLERS 里后端 model_tools.py 真正调用的四个名字都在。

**这里测不到**（必须在 ADS 实机里验，已在 model_ops 模块 docstring
声明为"未实测"）：keysight.ads.de 真实对象的行为 —— 真实
LibDefList 解析、add_library 的实际落库效果、model_def 读出来的参数。

运行: python tests/test_model_ops.py
"""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "addon", "ads_agent")):
    if _p not in sys.path:
        sys.path.insert(0, os.path.abspath(_p))

import model_ops as M  # noqa: E402

FAILS = []
_COUNT = [0]


def check(name, got, want):
    _COUNT[0] += 1
    if got != want:
        FAILS.append(f"{name}: got {got!r} want {want!r}")
        print(f"  FAIL {name}: got {got!r} want {want!r}")
    else:
        print(f"  ok{name}")


# --- 1. 真实 lib.defs 文件（本机 ADS 2027 安装里的）-----------------------
REAL = [
    r"E:\ADS\Programfile\ADS2027\examples\DesignKit\DemoKit_mmWave\lib.defs",
    r"E:\ADS\Programfile\ADS2027\examples\DesignKit\DemoKit_MTM\lib.defs",
    r"E:\ADS\Programfile\ADS2027\design_kit\Keysight_Photonics_pdk\lib.defs",
    r"E:\ADS\Programfile\ADS2027\designguides\projects\smithdg\lib.defs",
]
print("== 1. 真实 lib.defs（官方解析器在无 ADS 进程时会失败，"
      "应自动退到内置解析器且结论正确）==")
for p in REAL:
    if not os.path.isfile(p):
        print(f"  skip {p}")
        continue
    r = M._read_lib_defs(p)
    names = [e["name"] for e in r["libraries"]]
    modes = [e["mode"] for e in r["libraries"]]
    print(f"    source={r['source']} libs={names} modes={modes}")
    if r["error"]:
        print(f"    (记录在案的降级原因: {r['error'][:110]})")
    if not names:
        FAILS.append(f"真实 lib.defs 解析出0 个库: {p}")
    for e in r["libraries"]:
        if e["declared_mode"] and not e["mode"]:
            FAILS.append(f"{p}: libMode {e['declared_mode']!r} 未映射到枚举名")
        if e["abs_path"] and not os.path.isdir(e["abs_path"]):
            FAILS.append(f"{p}: {e['name']} 解析出的路径不存在 {e['abs_path']}")

# smithdg 是 workspace 库（libMode shared）—— 确认 mode 映射正确
r = M._read_lib_defs(REAL[3])
if r["libraries"]:
    check("smithdg mode", r["libraries"][0]["mode"], "SHARED")

# 模式名映射表本身（纯查表，不需要 ADS）。表键一律小写，
# 文件里是 readOnly / nonShared 驼峰 —— 断言的是 lower() 之后的匹配。
check("readOnly->READ_ONLY", M._LIBMODE_MAP.get("readonly"), "READ_ONLY")
check("shared->SHARED", M._LIBMODE_MAP.get("shared"), "SHARED")
check("nonShared->NON_SHARED", M._LIBMODE_MAP.get("nonshared"), "NON_SHARED")
check("未知模式 -> None（不猜）", M._library_mode("nope"), None)

# _library_mode 取枚举成员时必须报「不在 ADS 进程里」而不是漏出
# ModuleNotFoundError —— 离线也要给出可读原因。
try:
    M._library_mode("readOnly")
    print("  ok当前解释器能 import keysight（意外，但不影响结论）")
    check("能import 时拿到真枚举", M._library_mode("readOnly").name, "READ_ONLY")
except RuntimeError as e:
    print(f"  ok无 ADS 进程时报错可读: {str(e)[:90]}")
    if "has no value" not in str(e) and "LibraryMode" not in str(e):
        FAILS.append(f"报错信息没说明枚举要求: {e}")

# --- 2. 相对路径解析：相对 DEFINE 所在文件，不是 cwd ---------------------
print("== 2. 路径解析 ==")
with tempfile.TemporaryDirectory() as td:
    root = os.path.join(td, "kit")
    libdir = os.path.join(root, "MyLib")
    os.makedirs(libdir)
    defs = os.path.join(root, "lib.defs")
    with open(defs, "w", encoding="utf-8") as f:
        f.write("DEFINE MyLib ./MyLib\nASSIGN MyLib libMode readOnly\n")
    # 先 chdir 到别处，确保不是靠 cwd 解析对��
    cwd = os.getcwd()
    os.chdir(td)
    try:
        r = M._read_lib_defs(defs)
    finally:
        os.chdir(cwd)
    check("相对路径解析到 DEFINE 同级", r["libraries"][0]["abs_path"],
          M._norm_path(libdir))
    check("相对路径已解析标志", r["libraries"][0]["abs_path_resolved"], True)
    check("mode 映射", r["libraries"][0]["mode"], "READ_ONLY")

# 引号路径（ADS 允许 DEFINE Foo "D:/Program Files/Foo"）
with tempfile.TemporaryDirectory() as td:
    defs = os.path.join(td, "lib.defs")
    with open(defs, "w", encoding="utf-8") as f:
        f.write('DEFINE Foo "D:/Program Files/Foo"\n')
    r = M._read_lib_defs(defs)
    check("引号路径切分", r["libraries"][0]["path_tokens"], ["D:/Program Files/Foo"])

# $VAR 未设 -> 解析不出来，返回空而不是猜一个
with tempfile.TemporaryDirectory() as td:
    defs = os.path.join(td, "lib.defs")
    with open(defs, "w", encoding="utf-8") as f:
        f.write("DEFINE Bar $NO_SUCH_VAR_XYZ/oalibs/bar\n")
    r = M._read_lib_defs(defs)
    check("未设环境变量不猜路径", r["libraries"][0]["abs_path"], "")
    check("未设环境变量标记", r["libraries"][0]["abs_path_resolved"], False)

# 绝对路径
check("绝对路径原样", M._resolve_defs_path(["D:/x/y"], "C:/defs"),
      M._norm_path("D:/x/y"))

# 空 tokens
check("空 tokens 返回空", M._resolve_defs_path([], "C:/defs"), "")

# --- 3. 内置解析器：INCLUDE /注释 / ASSIGN 归属 --------------------------
print("== 3. 内置解析器细节 ==")
text = (
    "# 注释行\n"
    "\n"
    "INCLUDE $HPEESOF_DIR/oalibs/analog_rf.defs\n"
    "DEFINE a ./a\n"
    "ASSIGN a libMode readOnly\n"
    "DEFINE b ./b\n"
    "ASSIGN b libMode shared\n"
    "ASSIGN b writePath b\n"
    "ASSIGN ghost libMode readOnly\n"          # DEFINE 在别的文件里
)
with tempfile.TemporaryDirectory() as td:
    defs = os.path.join(td, "lib.defs")
    with open(defs, "w", encoding="utf-8") as f:
        f.write(text)
    r = M._read_lib_defs(defs)
    by = {e["name"]: e for e in r["libraries"]}
    check("INCLUDE 计入", len(r["includes"]), 1)
    check("库数量", sorted(by), ["a", "b", "ghost"])
    check("a 只读", by["a"]["mode"], "READ_ONLY")
    check("b 共享", by["b"]["mode"], "SHARED")
    check("writePath 读到", by["b"]["write_path"], "b")
    check("孤立 ASSIGN 不丢", by["ghost"]["define_line"], None)

# --- 4. 参数值数字拆分 ---------------------------------------------------
print("== 4. 参数值解析 ==")
check("带单位频率", M._parse_number("2.4 GHz"), (2.4, "GHz"))
check("纯数字", M._parse_number("50"), (50.0, ""))
check("科学计数", M._parse_number("1e-3"), (0.001, ""))
check("负数", M._parse_number("-5 Ohm"), (-5.0, "Ohm"))
check("非数字", M._parse_number("abc"), (None, "abc"))
check("空串", M._parse_number(""), (None, ""))
check("纯单位串", M._parse_number("GHz"), (None, "GHz"))

# --- 5. master 解析 ------------------------------------------------------
print("== 5. master 解析 ==")
check("lib:cell", M._parse_master("Lib:Cell"), ("Lib", "Cell", ""))
check("lib:cell:view", M._parse_master("Lib:Cell:schematic"),
      ("Lib", "Cell", "schematic"))
check("只给 cell", M._parse_master("Cell"), ("", "Cell", ""))
check("分开给", M._parse_master("", "Lib", "Cell"), ("Lib", "Cell", ""))
check("master 覆盖空library", M._parse_master("Lib:Cell", "", ""),
      ("Lib", "Cell", ""))

# --- 6. _norm_path 空串陷阱（backend/server.py 有同样的坑注释）----------
print("== 6. 路径规范化 ==")
check("空串不被当成 '.'", M._norm_path(""), "")
check("None 安全", M._norm_path(None), "")
check("末尾斜杠归一", M._norm_path("C:/a/b/"), M._norm_path("C:/a/b"))

# --- 7. DISPATCH 名字齐全（后端 model_tools.py 依赖）--------------------
print("== 7. 工具名齐全性 ==")
NEEDED = {"attach_design_kit", "list_vendor_models", "get_vendor_model_info",
          "validate_model_import"}
missing = NEEDED - set(M.HANDLERS)
check("后端调用的4 个都在", missing, set())
for extra in ("list_readonly_libraries", "list_library_components",
              "inspect_component_model", "detach_design_kit"):
    check(f"别名 {extra} 已注册", extra in M.HANDLERS, True)
for name, fn in M.HANDLERS.items():
    if not callable(fn):
        FAILS.append(f"{name} 不可调用")
    # 处理器签名必须是 (args, ctx=None)
    import inspect
    sig = list(inspect.signature(fn).parameters)
    if sig[:1] != ["args"]:
        FAILS.append(f"{name} 签名不是 (args, ...): {sig}")

print()
if FAILS:
    print(f"结果: FAIL —— {len(FAILS)} 项失败")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"结果: PASS —— ({_COUNT[0]} 项通过)")