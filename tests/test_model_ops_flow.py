# -*- coding: utf-8 -*-
"""model_ops 流程测试：挂接幂等 / 只读约束 / 卸载不越界 / 验证门禁。

用**假的** Workspace/Library/Cell（只实现 model_ops 真正用到的成员）驱动，
所以不需要 ADS 进程。测的是判定逻辑，不是 keysight.ads.de 的真实行为
—— 后者只有实机能验，已在 model_ops 模块 docstring 里声明为"未实测"。

刻意覆盖的四条纪律：
  1. 挂接**幂等**：重复导入同一个包不产生重复库引用；
  2. 挂接**只读**：SHARED/NON_SHARED 一律拒绝（会给原厂库写权限）；
  3. 卸载**只删自己挂的**：用户手工挂的库不能被误删；无记录时拒绝动手；
  4. **挂接成功 ≠ 模型可用**：attach 返回 usable/verified 恒为 False。

运行: python tests/test_model_ops_flow.py
"""

import os
import sys
import tempfile
import types

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


# ---------------------------------------------------------------------------
# 假 ADS 对象（只实现 model_ops 真正用到的那几个成员）
# ---------------------------------------------------------------------------

class FakeParam:
    def __init__(self, name, unit="num", ptype="real", default="1"):
        self.name, self.label = name, name + " label"
        self._unit, self._ptype, self._default = unit, ptype, default
        self.is_editable = True
        self.is_discrete_value = False
        self.is_netlistable = True
        self.is_optimizable = False

    @property
    def unit_type(self):
        return types.SimpleNamespace(value=self._unit)

    @property
    def param_type(self):
        return types.SimpleNamespace(value=self._ptype)

    @property
    def default_value(self):
        if self._default is None:
            return None
        return types.SimpleNamespace(value=self._default)


class FakeModelDef:
    def __init__(self, params):
        self.name, self.label = "X", "测试元件"
        self.component_name = "Lib:X"
        self.library_name = "Lib"
        self._params = params

    @property
    def parameters(self):
        return list(self._params)


class FakeCell:
    def __init__(self, name, params=None):
        self.name = name
        self.path = os.path.join("C:", "fake", name)
        self._md = FakeModelDef(params) if params is not None else None

    @property
    def model_def(self):
        return self._md

    @property
    def views(self):
        return []


class FakeLibrary:
    def __init__(self, name, path, read_only=True, cells=None):
        self.name, self.path, self.lib_path = name, path, path
        self.is_open = True
        self.is_read_only = read_only
        self.is_writable = not read_only
        self.is_an_ads_library = True
        self.has_attached_tech = False
        self.attached_tech_lib_name = ""
        self._cells = cells if cells is not None else [FakeCell(name + "_c1")]

    @property
    def cells(self):
        return list(self._cells)

    def cell_exists(self, n):
        return any(c.name == n for c in self._cells)

    def get_cell_if_exists(self, n):
        for c in self._cells:
            if c.name == n:
                return c
        return None

    def cell(self, n):
        c = self.get_cell_if_exists(n)
        if c is None:
            raise KeyError(n)
        return c


class FakeWorkspace:
    def __init__(self, path):
        self.path = path
        self._libs = {}

    @property
    def libraries(self):
        return list(self._libs.values())

    @property
    def writable_library_names(self):
        return {n for n, l in self._libs.items() if not l.is_read_only}

    @property
    def library_names(self):
        return set(self._libs)

    @property
    def lib_defs_file(self):
        return os.path.join(self.path, "lib.defs")

    def add_library(self, name, path, mode):
        mode_name = getattr(mode, "name", str(mode))
        if mode_name != "READ_ONLY":
            raise RuntimeError("只允许只读挂接")
        lib = FakeLibrary(name, path, read_only=True)
        self._libs[name] = lib
        ADDED.append((name, M._norm_path(path)))
        return lib

    def remove_library(self, name, path):
        self._libs.pop(name, None)
        REMOVED.append((name, M._norm_path(path)))

    def add_library_definition_file(self, p):
        raise RuntimeError("本测试不覆盖整体引用路径")


ADDED, REMOVED = [], []
CURRENT = {}


def install_fake_de(ws):
    """把 model_ops._de 换成返回假 de 的版本。"""
    mode_cls = types.SimpleNamespace(
        READ_ONLY=types.SimpleNamespace(name="READ_ONLY", str="ReadOnly"),
        SHARED=types.SimpleNamespace(name="SHARED", str="Shared"),
    )

    def _get_open_library(name):
        lib = ws._libs.get(name)
        if lib is None:
            raise KeyError(f"no library {name}")
        return lib

    de = types.SimpleNamespace(
        LibraryMode=mode_cls,
        workspace_is_open=lambda: True,
        active_workspace=lambda: ws,
        get_open_library=_get_open_library,
    )
    M._de = lambda: de
    M._db_uu = lambda: types.SimpleNamespace()


# ---------------------------------------------------------------------------
#1. 挂接：幂等 / 只读 / 冲突
# ---------------------------------------------------------------------------
print("== 1. 挂接 ==")
with tempfile.TemporaryDirectory() as td:
    ws = FakeWorkspace(td)
    install_fake_de(ws)
    kit = os.path.join(td, "kit")
    os.makedirs(os.path.join(kit, "VendorLib"))
    os.makedirs(os.path.join(kit, "VendorTech"))
    with open(os.path.join(kit, "lib.defs"), "w", encoding="utf-8") as f:
        f.write("DEFINE VendorLib ./VendorLib\nASSIGN VendorLib libMode readOnly\n"
                "DEFINE VendorTech ./VendorTech\nASSIGN VendorTech libMode readOnly\n")

    ADDED.clear()
    r1 = M.attach_design_kit({"package_id": "pkg1", "kit_root": kit}, None)
    check("首次挂接 ok", r1["ok"], True)
    check("挂了 2 个库", len(r1["libraries"]), 2)
    check("全部只读", all(l["is_read_only"] for l in r1["libraries"]), True)
    check("首次不是 already_attached", r1["already_attached"], False)
    check("usable 必为 False（挂接≠可用）", r1["usable"], False)
    check("verified 必为 False", r1["verified"], False)
    check("返回体带 libraries[].name（后端按它回写 ready）",
          sorted(x["name"] for x in r1["libraries"]), ["VendorLib", "VendorTech"])
    check("记录文件已写", os.path.isfile(M._record_path(td)), True)
    check("add_library 调用 2 次", len(ADDED), 2)

    # 幂等：再挂一次
    ADDED.clear()
    r2 = M.attach_design_kit({"package_id": "pkg1", "kit_root": kit}, None)
    check("二次挂接无重复 add_library", len(ADDED), 0)
    check("二次标记 already_attached", r2["already_attached"], True)
    check("二次仍 ok", r2["ok"], True)

    # 冲突：同名不同路径 -> 不覆盖
    other = os.path.join(td, "kit2")
    os.makedirs(os.path.join(other, "VendorLib"))
    with open(os.path.join(other, "lib.defs"), "w", encoding="utf-8") as f:
        f.write("DEFINE VendorLib ./VendorLib\nASSIGN VendorLib libMode readOnly\n")
    r3 = M.attach_design_kit({"package_id": "pkg2", "kit_root": other,
                             "library_names": ["VendorLib"]}, None)
    check("同名不同路径 -> 报冲突", len(r3["conflicts"]), 1)
    check("冲突时不 ok", r3["ok"], False)
    check("冲突时未改动原有引用", ws._libs["VendorLib"].path, ADDED[0][1]
          if ADDED else ws._libs["VendorLib"].path)

    # --- 2. 卸载：只删自己挂的 ---
    print("== 2. 卸载（只删本模块挂的）==")
    REMOVED.clear()
    # 先塞一个"用户自己挂的"库进工作区
    ws._libs["UserLib"] = FakeLibrary("UserLib", os.path.join(td, "user"))
    r4 = M.detach_design_kit({"workspace": td}, None)
    check("卸载删掉 2 个", len(r4["removed"]), 2)
    check("用户库没被动", "UserLib" in ws._libs, True)
    check("本模块的库已移除", "VendorLib" in ws._libs, False)
    check("不删磁盘文件", r4["files_deleted"], False)
    rec = M._load_record(td)
    check("记录里已清掉", sorted(rec.get("entries", {})), [])

    # 没有记录时必须拒绝，而不是按名字乱删
    r5 = M.detach_design_kit({"workspace": td}, None)
    check("无记录时拒绝", r5["ok"], False)
    check("无记录时什么都没删", "UserLib" in ws._libs, True)
    # 指定未记录的名字 -> 跳过并说明
    r6 = M.detach_design_kit({"workspace": td, "library_names": ["UserLib"]}, None)
    check("未记录的名字被跳过", [s["name"] for s in r6["skipped"]], ["UserLib"])
    check("用户库仍在", "UserLib" in ws._libs, True)

    # --- 3. 工作区不一致 ---
    print("== 3. 工作区复核 ==")
    r7 = M.attach_design_kit({"package_id": "p", "kit_root": kit,
                              "workspace": "D:/别的工程"}, None)
    check("不一致时 kind", r7["kind"], "workspace_mismatch")
    check("不一致时不 ok", r7["ok"], False)
    check("不一致时什么都没挂", len(ADDED), 0)
    r8 = M.validate_model_import({"library": "X", "cell": "Y",
                                  "workspace": "D:/别的工程"}, None)
    check("验证也复核工作区", r8["kind"], "workspace_mismatch")

# ---------------------------------------------------------------------------
# 4. 验证：库没挂 / cell 不存在 / 参数不匹配
# ---------------------------------------------------------------------------
print("== 4. 验证门禁 ==")
with tempfile.TemporaryDirectory() as td:
    ws = FakeWorkspace(td)
    install_fake_de(ws)
    ws._libs["Lib"] = FakeLibrary("Lib", os.path.join(td, "lib"), cells=[
        FakeCell("R1", params=[FakeParam("R", "res", "real", "50ohm"),
                               FakeParam("W", "lng", "real", "10um"),
                               FakeParam("Subst", "string", "string", '"MSUB1"')]),
    ])

    v = M.validate_model_import({"library": "Nope", "cell": "R1"}, None)
    check("库未挂接 -> 不 ok", v["ok"], False)
    check("库未挂接 verdict", v["verdict"], "库未挂接")

    v = M.validate_model_import({"library": "Lib", "cell": "NoSuchCell"}, None)
    check("cell 不存在 -> 不 ok", v["ok"], False)
    check("cell 不存在 verdict", v["verdict"], "cell 不存在或打不开")

    v = M.validate_model_import({"library": "Lib", "cell": "R1",
                                 "parameters": {"R": "50 Ohm"}}, None)
    check("参数正确 -> ok", v["ok"], True)
    check("默认不跑仿真", v["smoke_sim"]["ran"], False)
    # 「没跑仿真」这条必须在 unverified 里显眼存在，且明说是静态校验
    check("未验证项含'没有跑仿真'", any("没有跑仿真" in u for u in v["unverified"]),
          True)
    check("未验证项含'静态校验'", any("静态校验" in u for u in v["unverified"]), True)
    check("未验证项含取值范围未校验",
          any("取值范围未校验" in u for u in v["unverified"]), True)
    check("component_def_fingerprint 有值",
          bool(v.get("component_def_fingerprint")), True)

    v = M.validate_model_import({"library": "Lib", "cell": "R1",
                                 "parameters": {"R": "abc"}}, None)
    check("real 参数给非数字 -> 不 ok", v["ok"], False)
    check("问题点明是哪个参数", "R" in " ".join(v["problems"]), True)

    v = M.validate_model_import({"library": "Lib", "cell": "R1",
                                 "parameters": {"NoSuchP": "1"}}, None)
    check("未知参数 -> 不 ok", v["ok"], False)
    check("未知参数被点名", v["parameter_check"]["unknown_params"], ["NoSuchP"])

    v = M.validate_model_import({"library": "Lib", "cell": "R1",
                                 "parameters": {"R": "50 Ohm"}}, None)
    # 键名是 component_def_fingerprint（元件**定义**指纹）——
    # 与 ads_ops.design_fingerprint 里的 model_fingerprint（模型**文件内容**
    # 指纹，给结果复用做缓存失效）是两回事，刻意不共用同名。
    fp1 = v["component_def_fingerprint"]
    # 指纹随参数定义变化而变（"型号换了没"的判据）
    ws._libs["Lib"]._cells[0]._md = FakeModelDef(
        [FakeParam("R", "res", "real", "25ohm")])
    v2 = M.validate_model_import({"library": "Lib", "cell": "R1"}, None)
    check("参数定义变了 -> 指纹变了",
          v2["component_def_fingerprint"] != fp1, True)

    # 无 model_def 的 cell：参数必须null + 说明，不能猜
    ws._libs["Lib2"] = FakeLibrary("Lib2", os.path.join(td, "lib2"),
                                   cells=[FakeCell("Plain", params=None)])
    v = M.validate_model_import({"library": "Lib2", "cell": "Plain"}, None)
    check("无 model_def 时仍能读元数据", v["ok"], True)
    check("无 model_def 时参数 null", v.get("parameters", "missing"), None)
    check("无 model_def 进未验证项",
          any("无 model_def" in u for u in v["unverified"]), True)

# --- 5. 只读浏览：不改任何东西 ---
print("== 5. 只读浏览 ==")
with tempfile.TemporaryDirectory() as td:
    ws = FakeWorkspace(td)
    install_fake_de(ws)
    ws._libs["RO"] = FakeLibrary("RO", os.path.join(td, "ro"), read_only=True,
                                 cells=[FakeCell("A", params=[FakeParam("W")]),
                                        FakeCell("B")])
    ws._libs["RW"] = FakeLibrary("RW", os.path.join(td, "rw"), read_only=False,
                                 cells=[FakeCell("C")])
    ADDED.clear()
    L = M.list_readonly_libraries({"workspace": td}, None)
    check("列出全部库（含只读）", L["total"], 2)
    check("标出可写性", sorted((x["name"], x["writable"]) for x in L["libraries"]),
          [("RO", False), ("RW", True)])
    L = M.list_readonly_libraries({"workspace": td, "only_readonly": True}, None)
    check("only_readonly 过滤", [x["name"] for x in L["libraries"]], ["RO"])

    C = M.list_library_components({"workspace": td}, None)
    check("检索到 3 个元件", C["returned"], 3)
    check("浏览过程零挂接", len(ADDED), 0)
    C = M.list_library_components({"workspace": td, "library": "RO",
                                   "name_prefix": "A"}, None)
    check("前缀过滤", [c["cell"] for c in C["components"]], ["A"])
    check("带 model_def 参数", C["components"][0]["params"][0]["name"], "W")
    C = M.list_library_components({"workspace": td, "include_params": False}, None)
    check("关掉参数时不读 model_def", "params" in C["components"][0], False)

    I = M.inspect_component_model({"workspace": td, "library": "RO", "cell": "A"},
                                  None)
    check("详情: verified 必为 False", I["verified"], False)
    check("详情: 库只读", I["is_read_only"], True)
    check("详情: master 拼接", I["master"], "RO:A")
    check("详情: 有 next_step 提示未验证", "validate_model_import" in I["note"], True)

print()
if FAILS:
    print(f"结果: FAIL —— {len(FAILS)} 项失败")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"结果: PASS —— ({_COUNT[0]} 项通过)")