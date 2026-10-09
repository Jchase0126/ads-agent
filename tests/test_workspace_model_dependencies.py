"""Workspace isolation, factory mapping resolution, includes and ADS callbacks.

These exercise bundled API contracts with substitutes, not an ADS runtime.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon" / "ads_agent"))
import model_ops
import ads_ops


class DependenciesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "target_wrk"
        self.ws.mkdir()
        self.kit = self.ws / "models" / "VendorKit"
        (self.kit / "circuit" / "config").mkdir(parents=True)
        (self.kit / "circuit" / "models").mkdir()
        self.target = self.kit / "circuit" / "models" / "Vendor_Encoded.library"
        self.target.write_bytes(b"encoded")
        (self.kit / "circuit" / "config" / "ADSlibconfig").write_text(
            "Vendor_Encoded FactoryFolder/circuit/models/Vendor_Encoded.library\n")
        self.record = {"entries": {"Vendor": {"name": "Vendor", "kit_root": str(self.kit)}}}
        self.save_record()

    def tearDown(self):
        self.tmp.cleanup()

    def save_record(self):
        path = self.ws / ".ads_agent" / "attach_record.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(self.record))

    def test_mapping_rebinds_symbolic_factory_folder(self):
        plan = model_ops.simulation_dependency_plan(str(self.ws), ["Vendor"])
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["mappings"]["Vendor_Encoded"], str(self.target.resolve()))

    def test_mapping_missing_blocks_readiness(self):
        self.target.unlink()
        self.assertFalse(model_ops.simulation_dependency_plan(str(self.ws), ["Vendor"])["ok"])

    def test_other_workspace_root_rejected(self):
        self.record["entries"]["Vendor"]["kit_root"] = self.tmp.name
        self.save_record()
        plan = model_ops.simulation_dependency_plan(str(self.ws), ["Vendor"])
        self.assertFalse(plan["ok"])
        self.assertFalse(plan["mappings"])

    def test_factory_netlist_encoded_names_require_mapping(self):
        file = self.kit / "circuit" / "models" / "encode.net"
        file.write_text('#uselib "MissingEncoded", "model"\n')
        plan = model_ops.simulation_dependency_plan(str(self.ws), ["Vendor"])
        result = model_ops.check_simulation_netlist(plan, f'#include "{file.as_posix()}"')
        self.assertFalse(result["ok"])
        file.write_text('#uselib "Vendor_Encoded", "model"\n#uselib "ckt", "S2P"\n')
        self.assertTrue(model_ops.check_simulation_netlist(plan, f'#include "{file.as_posix()}"')["ok"])

    def test_stale_other_workspace_include_rejected(self):
        (self.kit / "circuit" / "models" / "encode.net").write_text("factory")
        plan = model_ops.simulation_dependency_plan(str(self.ws), ["Vendor"])
        other = Path(self.tmp.name) / "old_wrk" / "circuit" / "models" / "encode.net"
        result = model_ops.check_simulation_netlist(plan, f'#include "{other.as_posix()}"')
        self.assertFalse(result["ok"])

    def test_conflicting_encoded_names_block(self):
        other = self.ws / "OtherKit"
        (other / "circuit" / "config").mkdir(parents=True)
        (other / "circuit" / "models").mkdir()
        (other / "circuit" / "models" / "Vendor_Encoded.library").write_bytes(b"different")
        (other / "circuit" / "config" / "ADSlibconfig").write_text(
            "Vendor_Encoded OtherKit/circuit/models/Vendor_Encoded.library\n")
        self.record["entries"]["Other"] = {"kit_root": str(other)}
        self.save_record()
        self.assertFalse(model_ops.simulation_dependency_plan(str(self.ws), ["Vendor", "Other"])["ok"])

    def design(self):
        dut = types.SimpleNamespace(master_lcv_name=types.SimpleNamespace(
            library_name="Vendor", cell_name="DUT"), inst_name="X1")
        design = types.SimpleNamespace(instances=[dut])
        def add(master, point, name):
            lib, cell, view = master.split(":")
            instance = types.SimpleNamespace(inst_name=name, inst_pins=[],
                master_lcv_name=types.SimpleNamespace(library_name=lib, cell_name=cell))
            design.instances.append(instance)
            return instance
        design.add_instance = add
        return design

    def cells(self, *names):
        return types.SimpleNamespace(cells=[types.SimpleNamespace(
            name=n, model_def=object(), view_exists=lambda view: view == "symbol") for n in names])

    def test_auto_include_is_idempotent(self):
        design = self.design()
        with patch.object(model_ops, "_library_for", return_value=self.cells("TechInclude")):
            first = model_ops.prepare_design_dependencies(design, str(self.ws), place_includes=True)
            second = model_ops.prepare_design_dependencies(design, str(self.ws), place_includes=True)
        self.assertEqual(first["include_instances"], ["MODEL_INCLUDE1"])
        self.assertEqual(second["include_instances"], [])
        self.assertEqual(len(design.instances), 2)

    def test_readonly_missing_include_blocks_without_mutation(self):
        design = self.design()
        with patch.object(model_ops, "_library_for", return_value=self.cells("Vendor_Include")):
            with self.assertRaisesRegex(RuntimeError, "Include"):
                model_ops.prepare_design_dependencies(design, str(self.ws))
        self.assertEqual(len(design.instances), 1)

    def test_ambiguous_include_is_not_guessed(self):
        design = self.design()
        with patch.object(model_ops, "_library_for", return_value=self.cells("One_Include", "Two_Include")):
            with self.assertRaisesRegex(RuntimeError, "Include"):
                model_ops.prepare_design_dependencies(design, str(self.ws), place_includes=True)
        self.assertEqual(len(design.instances), 1)

    def test_enum_form_and_callback(self):
        param = types.SimpleNamespace(name="PartNumber", value='"NULL"', form_name="NULL",
            definition=types.SimpleNamespace(formset=types.SimpleNamespace(
                find_form_by_name=lambda value: types.SimpleNamespace(
                    is_constant_form=lambda form: True) if value == "V123" else None)))
        calls = []
        inst = types.SimpleNamespace(parameters=[param], inst_name="X1",
            invoke_item_parameter_changed_callback=lambda names: calls.append(names))
        ads_ops._set_param(inst, "PartNumber", '"V123"')
        self.assertEqual(param.form_name, "V123")
        self.assertEqual(calls, [["PartNumber"]])
        param.value, param.netlist_value = "1", "1"
        spec = [{"name": "X1", "params": {"PartNumber": "V123"}}]
        self.assertEqual(ads_ops._verify_params({"X1": inst}, spec), [])
        self.assertEqual(ads_ops._netlist_parameter_specs({"X1": inst}, spec)[0]["params"],
                         {"PartNumber": "1"})

    def test_string_quoting_preserved_and_callback_error_propagates(self):
        param = types.SimpleNamespace(name="File", value='"old"')
        def fail(names):
            raise RuntimeError("factory callback failed")
        inst = types.SimpleNamespace(parameters=[param], inst_name="X1",
            invoke_item_parameter_changed_callback=fail)
        with self.assertRaisesRegex(RuntimeError, "factory callback"):
            ads_ops._set_param(inst, "File", "new")
        self.assertEqual(param.value, '"new"')

    def test_simulator_mapping_environment_is_child_only(self):
        calls = []
        class FakeSimulator:
            def _execute(self, *args, extra_env=None, **kwargs):
                calls.append(dict(extra_env))
            def run_netlist(self, netlist, output_dir, dataset_name):
                self._execute(extra_env={"KEEP": "yes"})
                (Path(output_dir) / (dataset_name + ".ds")).write_bytes(b"dataset")
        keysight = types.ModuleType("keysight")
        toolbox = types.ModuleType("keysight.edatoolbox")
        toolbox.ads = types.SimpleNamespace(CircuitSimulator=FakeSimulator)
        output = self.ws / "sim_output"
        output.mkdir()
        before = dict(os.environ)
        audit = {"_vendor_dependencies": model_ops.simulation_dependency_plan(str(self.ws), ["Vendor"])}
        with patch.dict(sys.modules, {"keysight": keysight, "keysight.edatoolbox": toolbox}):
            result = ads_ops._simulate("netlist", str(output), "test", "", audit)
        self.assertEqual(before, dict(os.environ))
        self.assertEqual(calls[0]["ADSLIBCONFIG_PATH"], str(output).replace("\\", "/"))
        self.assertEqual(calls[0]["KEEP"], "yes")
        config = (output / "ADSlibconfig").read_text()
        self.assertIn(str(self.target).replace("\\", "/"), config)
        self.assertEqual(result["status"], "done")


if __name__ == "__main__":
    unittest.main()
