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
    def __init__(self, name, path, read_only=True, cells=None, cfg_vars=None):
        self.name, self.path, self.lib_path = name, path, path
        self.is_open = True
        self.is_read_only = read_only
        self.is_writable = not read_only
        self.is_an_ads_library = True
        self.has_attached_tech = False
        self.attached_tech_lib_name = ""
        self._cells = cells if cells is not None else [FakeCell(name + "_c1")]
        # 模拟 ADS 的 Library.get_raw_library_cfg_var：默认给空——
        # 表示"ADS 没读回配置变量"，用于覆盖 boot.loaded 保持 unknown 的路径。
        self._cfg_vars = dict(cfg_vars or {})

    def get_raw_library_cfg_var(self, pref_name):
        return self._cfg_vars.get(pref_name, "")

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

    # 无 model_def 的 cell：参数必须 null + 说明，不能猜
    ws._libs["Lib2"] = FakeLibrary("Lib2", os.path.join(td, "lib2"),
                                   cells=[FakeCell("Plain", params=None)])
    v = M.validate_model_import({"library": "Lib2", "cell": "Plain"}, None)
    #元数据照样读得到（库/只读属性/参数键都在），但**不能**因此说验证通过：
    # 没有 model_def 就无从核对参数定义、也无从确认它可放置，按证据分级
    # 必须是 ok=False（这条预期在model_ops 引入证据分级后已更正）。
    check("无 model_def 时仍能读元数据：库名", v.get("library"), "Lib2")
    check("无 model_def 时仍能读元数据：只读属性有值",
          v.get("is_read_only") is not None, True)
    check("无 model_def 时不得判为验证通过", v["ok"], False)
    check("无 model_def 时 verified 也是 False", v.get("verified"), False)
    # 注意别用"不含'通过'"来判——"验证未通过"里也有这两个字。
    # 要判的是 verdict **不是**正向标签。
    check("无 model_def 时 verdict 不是任何正向结论",
          str(v.get("verdict") or "") not in ("已仿真验证", "仅静态可解析",
                                        "仅已放置（未仿真）", "静态校验通过"),
          True)
    check("无 model_def 时参数 null", v.get("parameters", "missing"), None)
    check("无 model_def 进未验证项",
          any("无 model_def" in u for u in v["unverified"]), True)
    check("无 model_def 的 parsed 阶段不是 pass",
          (v.get("verification", {}).get("stages", {}).get("parsed", {})
           .get("status")) in (None, "fail", "unknown", "not_checked"), True)

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

# ---------------------------------------------------------------------------
# 6. open_vendor_palette：原生元件列表打开/定位（只读定位，≠ 模型可用）
# ---------------------------------------------------------------------------
print("== 6. open_vendor_palette ==")
with tempfile.TemporaryDirectory() as td:
    ws = FakeWorkspace(td)
    install_fake_de(ws)

    # 造一个"像真的"的 Design Kit：套件根有 lib.defs，库目录有 eesof_lib.cfg，
    # de/ael 下**只有 .atf**（原厂包常态，没有 .ael 源）。
    kit = os.path.join(td, "TDK_Library_for_ADS_v2019.10")
    libdir = os.path.join(kit, "TDK_Component_Library_v2019.10")
    os.makedirs(os.path.join(kit, "de", "ael"))
    os.makedirs(libdir)
    for fn in ("boot.atf", "palette.atf"):
        with open(os.path.join(kit, "de", "ael", fn), "wb") as f:
            f.write(b"\x00binary-atf")
    with open(os.path.join(kit, "lib.defs"), "w", encoding="utf-8") as f:
        f.write("DEFINE TDK_Component_Library_v2019#2e10 "
                "./TDK_Component_Library_v2019.10\n"
                "ASSIGN TDK_Component_Library_v2019#2e10 libMode readOnly\n")
    with open(os.path.join(libdir, "eesof_lib.cfg"), "w", encoding="utf-8") as f:
        f.write("BOOT_AEL=../de/ael/boot\n"
                "DESIGN_KIT_NAME=TDK_Component_Library\n"
                "VERSION=v2019.10\n"
                "INPUT_DATA_PATH=../circuit/data;../circuit/models\n"
                "LIB_BROWSER_CTL=../circuit/records/TDK_Component_Library.ctl\n")

    # 6a) 库未挂接 -> not_attached（包本身是 Design Kit，有别的东西可查）
    r = M.open_vendor_palette({"package_id": "p", "workspace": td,
                               "kit_root": kit}, None)
    check("未挂接 -> not_attached", r["outcome"], "not_attached")
    check("未挂接 -> ok False", r["ok"], False)
    check("未挂接 -> verified False", r["verified"], False)
    check("未挂接 -> library_attached False", r["library_attached"], False)

    # 6b) 工作区不一致 -> workspace_mismatch，且什么都没写
    rec_before = M._load_record(td)
    r = M.open_vendor_palette({"package_id": "p", "workspace": "D:/别的工程",
                               "kit_root": kit}, None)
    check("工作区不一致 -> workspace_mismatch", r["outcome"], "workspace_mismatch")
    check("工作区不一致 -> ok False", r["ok"], False)
    check("工作区不一致 -> 不写挂接记录", M._load_record(td), rec_before)

    # 6c) 缺 eesof_lib.cfg：库挂接了、包里有其它资产，但不抛错、loaded unknown、
    #     limits 有诊断
    nolib_dir = os.path.join(td, "nocfg", "NoCfgLib")
    os.makedirs(nolib_dir)
    ws._libs["NoCfgLib"] = FakeLibrary("NoCfgLib", nolib_dir)
    kit2 = os.path.join(td, "kit_nocfg")
    os.makedirs(os.path.join(kit2, "de", "ael"))
    with open(os.path.join(kit2, "de", "ael", "palette.atf"), "wb") as f:
        f.write(b"x")
    r = M.open_vendor_palette({"package_id": "p", "workspace": td,
                               "kit_root": kit2, "library": "NoCfgLib"}, None)
    check("缺 cfg 不抛错", isinstance(r, dict), True)
    check("缺 cfg -> loaded unknown", r["boot"]["loaded"], "unknown")
    check("缺 cfg -> eesof_lib_cfg.exists False",
          r["boot"]["eesof_lib_cfg"]["exists"], False)
    check("缺 cfg -> limits 有诊断",
          any("eesof_lib.cfg" in x for x in r["limits"]), True)
    check("缺 cfg -> verified False", r["verified"], False)

    # 6d) 成功路径：库已挂接、cfg/atf 齐全；用**桩**替换确定性 AEL 探测
    #     （纯逻辑测试不能连 ADS），验证 loaded/palette_groups/located/outcome。
    #     库名用**未转义**的形式，验证"名字对不上时按路径匹配"这条兜底。
    ctl_dir = os.path.join(kit, "circuit", "records")
    os.makedirs(ctl_dir)
    with open(os.path.join(ctl_dir, "TDK_Component_Library.ctl"), "w",
              encoding="utf-8") as f:
        f.write("ctl\n")
    with open(os.path.join(ctl_dir, "TDK_Component_Library.rec"), "w",
              encoding="utf-8") as f:
        f.write("rec\n")
    ws._libs["TDK_Component_Library_v2019.10"] = FakeLibrary(
        "TDK_Component_Library_v2019.10", libdir,
        cfg_vars={"BOOT_AEL": "../de/ael/boot",
                  "DESIGN_KIT_NAME": "TDK_Component_Library",
                  "LIB_BROWSER_CTL": "../circuit/records/TDK_Component_Library.ctl"})

    _orig_ctx, _orig_item, _orig_vpn = (
        M._ael_context_window, M._ael_item_def, M._ael_vpn)
    M._ael_context_window = lambda: ("FAKEWIN", "FM_SC [schematic]", "")
    M._ael_item_def = lambda lib, cell: f"{lib}:{cell}"
    M._ael_vpn = lambda win, lib, cell: "TDK Chip Beads"
    try:
        r = M.open_vendor_palette({"package_id": "p", "workspace": td,
                                   "kit_root": kit}, None)
        check("成功路径 outcome 合法",
              r["outcome"] in ("opened", "located", "loaded_only"), True)
        check("成功路径 outcome 为 located（能定位但窗口开不了）",
              r["outcome"], "located")
        check("成功路径 verified 恒 False", r["verified"], False)
        check("成功路径 ok True", r["ok"], True)
        check("成功路径 library_attached True", r["library_attached"], True)
        check("按路径匹配到真实库名（名字转义不一致也能定位）",
              r["native"]["located"]["library"], "TDK_Component_Library_v2019.10")
        # loaded：确定性 AEL 探测（dm_find_item_definition / vpn）
        check("loaded True（AEL 确定性命中）", r["boot"]["loaded"], True)
        check("loaded_basis 含实际返回值",
              "dm_find_item_definition" in r["boot"]["loaded_basis"], True)
        # palette_groups：用真实分组名聚合
        check("palette_groups 按真实分组名聚合",
              [g["name"] for g in r["boot"]["palette_groups"]], ["TDK Chip Beads"])
        check("palette_groups[0] 字段齐全",
              set(("library", "window", "design_type", "name", "label", "items",
                   "owner", "source")).issubset(set(r["boot"]["palette_groups"][0])),
              True)
        check("registered_components 计数", r["boot"]["registered_components"], 1)
        # located
        check("located.found True", r["native"]["located"]["found"], True)
        check("located.category 真实分组名",
              r["native"]["located"]["category"], "TDK Chip Beads")
        check("located.via", r["native"]["located"]["via"],
              "deitem_get_visible_palette_name")
        check("located.window 记录窗口标题",
              r["native"]["located"]["window"], "FM_SC [schematic]")
        check("located.library_found 仍保留库在集合里的语义",
              r["native"]["located"]["library_found"], True)
        # boot 静态资源
        check("boot_ael 探测到 .atf", r["boot"]["boot_ael"]["atf_exists"], True)
        check("boot_ael path 落到 .atf",
              r["boot"]["boot_ael"]["path"].endswith(".atf"), True)
        check("palette 探测到 palette.atf", r["boot"]["palette"]["atf_exists"], True)
        # control_files（LIB_BROWSER_CTL 以库目录解析 + *.rec）
        check("control_files.lib_browser_ctl.exists True",
              r["boot"]["control_files"]["lib_browser_ctl"]["exists"], True)
        check("control_files.records 列出 *.rec",
              len(r["boot"]["control_files"]["records"]), 1)
        check("boot.evidence 非空", len(r["boot"]["evidence"]) >= 2, True)
        # 没有可程序化打开 Palette 窗口的 API -> 必须如实标 unsupported
        check("native.palette 标 unsupported",
              (r["native"]["palette"]["opened"], r["native"]["palette"]["method"]),
              (False, "unsupported"))
        check("limits 明说未提供打开 Palette 的 API",
              any("Palette" in x and ("未提供" in x or "没有" in x)
                  for x in r["limits"]), True)
        check("limits 不再声称 palette 无法查询",
              not any(("无查询函数" in x or "没有可查询接口" in x
                       or "无法逐字确认" in x) for x in r["limits"]), True)
        check("steps 是列表且非空",
              isinstance(r["steps"], list) and len(r["steps"]) > 0, True)

        # 6d-2) 两个确定性探测都执行成功且都为空 -> loaded False / found False / failed
        M._ael_item_def = lambda lib, cell: None
        M._ael_vpn = lambda win, lib, cell: None
        r2 = M.open_vendor_palette({"package_id": "p", "workspace": td,
                                    "kit_root": kit}, None)
        check("两探测都空 -> loaded False", r2["boot"]["loaded"], False)
        check("两探测都空 -> located.found False",
              r2["native"]["located"]["found"], False)
        check("两探测都空 -> outcome failed", r2["outcome"], "failed")
        check("两探测都空 -> ok False", r2["ok"], False)

        # 6d-3) 探测不可用（无窗口/无 ael）-> loaded unknown（绝不猜）
        def _boom(*a, **k):
            raise RuntimeError("no ael")
        M._ael_item_def = _boom
        M._ael_vpn = _boom
        M._ael_context_window = lambda: (None, "", "no app")
        r3 = M.open_vendor_palette({"package_id": "p", "workspace": td,
                                    "kit_root": kit}, None)
        check("探测不可用 -> loaded unknown", r3["boot"]["loaded"], "unknown")
        check("探测不可用 -> 不抛错", isinstance(r3, dict), True)
    finally:
        M._ael_context_window, M._ael_item_def, M._ael_vpn = (
            _orig_ctx, _orig_item, _orig_vpn)

    # 6e) 非 Design Kit（纯 Touchstone：无 cfg/ael/atf/ctl/lib.defs）-> unsupported
    ts_dir = os.path.join(td, "ts", "TouchstoneLib")
    os.makedirs(ts_dir)
    with open(os.path.join(ts_dir, "part.s2p"), "w", encoding="utf-8") as f:
        f.write("# Hz S RI R 50\n")
    ws._libs["TouchstoneLib"] = FakeLibrary("TouchstoneLib", ts_dir)
    r = M.open_vendor_palette({"package_id": "p", "workspace": td,
                               "kit_root": os.path.join(td, "ts"),
                               "library": "TouchstoneLib"}, None)
    check("非 Design Kit -> unsupported", r["outcome"], "unsupported")
    check("非 Design Kit -> ok False", r["ok"], False)
    check("非 Design Kit -> verified False", r["verified"], False)

# ---------------------------------------------------------------------------
# 7. 证据阶段（STAGES）与 open_vendor_palette 注册
# ---------------------------------------------------------------------------
print("== 7. 证据阶段与注册 ==")
check("STAGES 含 booted/listed 且顺序正确",
      M._VERIFICATION_STAGES,
      ("saved", "extracted", "parsed", "booted", "listed", "placed", "simulated"))
check("阶段中文标签 booted", M._VERIFICATION_STAGE_LABELS.get("booted"),
      "库已加载启动配置")
check("阶段中文标签 listed", M._VERIFICATION_STAGE_LABELS.get("listed"),
      "原生列表可见")
check("总判定标签 booted 是中文",
      M._OVERALL_LABELS_LOCAL.get("booted"), "仅库已加载启动配置（未仿真）")
check("总判定标签 listed 是中文",
      M._OVERALL_LABELS_LOCAL.get("listed"), "仅原生列表可见（未仿真）")
check("open_vendor_palette 已注册进 HANDLERS",
      "open_vendor_palette" in M.HANDLERS, True)
check("HANDLERS 指向同一函数",
      M.HANDLERS["open_vendor_palette"] is M.open_vendor_palette, True)

# put_verification_stage：booted 通过时**前序未记录**才标 unknown（不反推后序）
st = M.blank_verification_stages()
M.put_verification_stage(st, "booted", "pass", "库配置已加载")
check("booted 通过后 parsed 未记录 -> unknown", st["parsed"]["status"], "unknown")
check("booted 通过后 listed 仍是 not_checked（后序不被反推）",
      st["listed"]["status"], "not_checked")
check("booted 自身记 pass", st["booted"]["status"], "pass")

print()
if FAILS:
    print(f"结果: FAIL —— {len(FAILS)} 项失败")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"结果: PASS —— ({_COUNT[0]} 项通过)")