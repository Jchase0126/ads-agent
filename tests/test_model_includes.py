"""原厂模型 Include 的补齐逻辑（纯逻辑，不需要 ADS 进程）。

背景（2026-11-05 实机）：
  * 设计里放的是 ``ads_simulation:NetlistInclude``，``IncludeFiles`` 还是
    占位 ``[['', '']]`` —— 网表里**一条 #include 都没有**，仿真会报
    undefined model / coded model 加载失败；
  * SDK 没有删除实例的 API，所以只能在原地把文件填进去；
  * 这一栏按**工作区相对路径**解释：写绝对路径会被 ADS 再拼一次工作区
    根目录，变成 ``<ws>\\E:\\...``。

用假 design/instance 锁住这些语义，以及"不重复包含同一批模型文件"。
"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon" / "ads_agent"))
import model_ops  # noqa: E402


def make_inst(name, lib, cell, params=None):
    inst = types.SimpleNamespace(
        inst_name=name,
        inst_pins=[],
        master_lcv_name=types.SimpleNamespace(library_name=lib, cell_name=cell),
    )
    inst.parameters = []
    for key, value in (params or {}).items():
        inst.parameters.append(types.SimpleNamespace(name=key, value=value))
    return inst


class IncludesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "wrk"
        self.kit = self.ws / "models" / "VendorKit"
        (self.kit / "circuit" / "models").mkdir(parents=True)
        self.encode = self.kit / "circuit" / "models" / "encode.net"
        self.encode.write_text("#uselib \"Vendor_Encoded\", \"model\"\n")
        self.record = {"entries": {"Vendor": {"name": "Vendor", "kit_root": str(self.kit)}}}
        path = self.ws / ".ads_agent" / "attach_record.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self.record))

    def tearDown(self):
        self.tmp.cleanup()

    # ---- 纯函数 ----

    def test_include_pairs_accepts_sdk_and_string_forms(self):
        eq = self.assertEqual
        eq(model_ops._include_pairs("[['', '']]"), [])
        eq(model_ops._include_pairs([["a.net", "x"]]), [("a.net", "x")])
        eq(model_ops._include_pairs(["a.net"]), [("a.net", "")])
        eq(model_ops._include_pairs("not a list"), [])

    def test_include_role(self):
        eq = self.assertEqual
        eq(model_ops._include_role(make_inst("X", "ads_simulation", "NetlistInclude")), "generic")
        eq(model_ops._include_role(make_inst("X", "Vendor", "Vendor_Include")), "vendor")
        eq(model_ops._include_role(make_inst("X", "ads_rflib", "R")), "")

    def test_netlist_includes_reads_quoted_lines(self):
        text = ('#ifndef inc_x\n#include "C:/a/encode.net"\n'
                '; #include "commented.net"\n#include "C:/b/encode.net"\n#endif\n')
        eq = self.assertEqual
        eq(model_ops._netlist_includes(text),
           ["C:/a/encode.net", "C:/b/encode.net"])

    def test_include_entry_path_is_workspace_relative(self):
        ws = str(self.ws)
        inside = str(self.encode)
        rel = model_ops._include_entry_path(ws, inside)
        self.assertEqual(rel, "models/VendorKit/circuit/models/encode.net")
        outside = str(Path(self.tmp.name) / "elsewhere" / "x.net")
        self.assertEqual(model_ops._include_entry_path(ws, outside), outside)

    def test_file_key_is_separator_and_case_insensitive(self):
        a = model_ops._file_key(str(self.encode))
        b = model_ops._file_key(str(self.encode).replace(os.sep, "/").upper())
        self.assertEqual(a, b)

    # ---- prepare_design_dependencies ----

    def design_with(self, *insts):
        design = types.SimpleNamespace(instances=list(insts))
        added = []

        def add(master, point, name):
            added.append(name)
            lib, cell, _view = master.split(":")
            inst = make_inst(name, lib, cell)
            design.instances.append(inst)
            return inst

        design.add_instance = add
        design.added = added
        return design

    def cells(self, *names):
        return types.SimpleNamespace(cells=[
            types.SimpleNamespace(name=n, model_def=object(),
                                  view_exists=lambda view: view == "symbol")
            for n in names])

    def test_empty_netlistinclude_is_filled_and_no_vendor_include_added(self):
        inc = make_inst("INC1", "ads_simulation", "NetlistInclude",
                        {"IncludeFiles": [["", ""]], "IncludePath": [""]})
        design = self.design_with(make_inst("Lm", "Vendor", "DUT"), inc)
        with patch.object(model_ops, "_library_for", return_value=self.cells("Vendor_Include")):
            out = model_ops.prepare_design_dependencies(design, str(self.ws), place_includes=True)
        self.assertEqual(out["filled_includes"], ["INC1"])
        self.assertEqual(out["include_instances"], [])
        self.assertEqual(design.added, [])
        entries = model_ops._include_pairs(inc.parameters[0].value)
        self.assertEqual([e[0] for e in entries],
                         ["models/VendorKit/circuit/models/encode.net"])
        self.assertEqual(out["include_files"], [str(self.encode.resolve())])
        self.assertEqual(out["include_sources"].get("netlist_include"), "INC1")
        self.assertEqual(out["problems"], [])

    def test_second_prepare_is_idempotent(self):
        inc = make_inst("INC1", "ads_simulation", "NetlistInclude", {"IncludeFiles": [["", ""]]})
        design = self.design_with(make_inst("Lm", "Vendor", "DUT"), inc)
        with patch.object(model_ops, "_library_for", return_value=self.cells("Vendor_Include")):
            model_ops.prepare_design_dependencies(design, str(self.ws), place_includes=True)
            again = model_ops.prepare_design_dependencies(design, str(self.ws), place_includes=True)
        self.assertEqual(again["filled_includes"], [])
        self.assertEqual(again["include_instances"], [])
        self.assertEqual(again["problems"], [])

    def test_vendor_include_instance_counts_as_covered(self):
        inc = make_inst("MODEL_INCLUDE1", "Vendor", "Vendor_Include")
        design = self.design_with(make_inst("Lm", "Vendor", "DUT"), inc)
        with patch.object(model_ops, "_library_for", return_value=self.cells("Vendor_Include")):
            out = model_ops.prepare_design_dependencies(design, str(self.ws), place_includes=True)
        self.assertEqual(out["include_instances"], [])
        self.assertEqual(design.added, [])
        self.assertEqual(out["include_sources"]["Vendor"], "MODEL_INCLUDE1")

    def test_readonly_preflight_names_the_empty_include_and_repair_tool(self):
        inc = make_inst("INC1", "ads_simulation", "NetlistInclude", {"IncludeFiles": [["", ""]]})
        design = self.design_with(make_inst("Lm", "Vendor", "DUT"), inc)
        with patch.object(model_ops, "_library_for", return_value=self.cells("Vendor_Include")):
            with self.assertRaises(RuntimeError) as caught:
                model_ops.prepare_design_dependencies(design, str(self.ws))
        text = str(caught.exception)
        self.assertIn("repair_model_includes", text)
        self.assertIn("NetlistInclude", text)
        self.assertEqual(len(design.instances), 2)  # 只读预检不改设计

    def test_partial_cover_is_completed_in_place(self):
        extra = self.kit / "circuit" / "models" / "extra.net"
        extra.write_text("#uselib \"Vendor_Encoded\", \"model\"\n")
        inc = make_inst("INC1", "ads_simulation", "NetlistInclude",
                        {"IncludeFiles": [["models/VendorKit/circuit/models/extra.net", ""]]})
        design = self.design_with(make_inst("Lm", "Vendor", "DUT"), inc)
        with patch.object(model_ops, "_library_for", return_value=self.cells("Vendor_Include")):
            out = model_ops.prepare_design_dependencies(design, str(self.ws), place_includes=True)
        self.assertEqual(design.added, [])          # 不再追加第二个包含件
        self.assertEqual(out["include_instances"], [])
        self.assertEqual(out["filled_includes"], ["INC1"])
        files = [f for f, _note in model_ops._include_pairs(inc.parameters[0].value)]
        self.assertEqual(files, ["models/VendorKit/circuit/models/extra.net",
                                 "models/VendorKit/circuit/models/encode.net"])
        self.assertEqual(out["problems"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
