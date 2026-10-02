"""对 Z_live_regression 创建的临时设计制造端口短路并验证仿真门禁。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import config  # noqa: E402
import tools  # noqa: E402


def main(cell):
    cfg = config.load()
    code = f'''
from ads_ops import _bind_pins, _find_pin_on
d = odesign("AI_lib", "{cell}", write=True)
try:
    port = next(i for i in d.instances if i.name == "PORT1")
    _bind_pins(d, _find_pin_on(port, 1), _find_pin_on(port, 2))
    save(d)
finally:
    d.close_design()
print("shorted", "{cell}")
'''
    changed = tools.call(cfg, "run_python", {"code": code})
    assert changed.get("ok"), changed
    gate = tools.call(cfg, "check_connections", {"library": "AI_lib", "cell": cell})
    problems = "\n".join(gate.get("problems") or [])
    assert "同一网络" in problems and "PORT1" in problems, gate
    print("SHORT_GATE", problems, flush=True)
    try:
        tools.call(cfg, "run_simulation", {"library": "AI_lib", "cell": cell})
    except tools.AdsToolError as error:
        assert "同一网络" in str(error), str(error)
        print("SIM_BLOCKED", str(error)[:180], flush=True)
    else:
        raise AssertionError("端口短路仍被放行")

    cleanup = tools.call(cfg, "run_python", {
        "code": f"de.get_open_library('AI_lib').delete_cell('{cell}'); print('deleted')"
    })
    assert cleanup.get("ok"), cleanup
    print("TEMP_CELL_DELETED", cell, flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
