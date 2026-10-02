"""2026-09-27 临时 Wilkinson 设计的 ADS 建图/仿真/指标实机复验。"""

import json
import os
import secrets
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import ads_auth  # noqa: E402
import config  # noqa: E402
import tools  # noqa: E402
from repair_wilkinson_5g9_layout import SPEC  # noqa: E402


def main():
    cfg = config.load()
    cell = "AI_verify_" + secrets.token_hex(4)
    print("CELL", cell, flush=True)

    result = tools.call(cfg, "run_python", {"code": "raise SystemExit('live probe')"})
    assert result.get("ok") is False and "SystemExit" in result.get("stdout", ""), result
    workspace = tools.call(cfg, "get_workspace_info", {})
    assert workspace, "SystemExit 后工具服务不可用"
    print("SYSTEM_EXIT_RECOVERY OK", flush=True)

    spec = json.loads(json.dumps(SPEC))
    spec["library"] = "AI_lib"
    spec["cell"] = cell
    spec["recreate"] = False
    spec["layout"] = "auto"
    controller = next(i for i in spec["instances"] if i["name"] == "SP1")
    controller["params"]["Step"] = "0.00005 GHz"  # 60001 个点，覆盖旧 50000 点上限

    built = tools.call(cfg, "build_schematic", spec)
    assert built.get("n_instances") == 18, built
    assert len(built.get("connections") or []) == 15, built
    assert all(c.get("status") == "ok" for c in built["connections"]), built
    print("BUILD", built["n_instances"], len(built["connections"]), flush=True)

    gate = tools.call(cfg, "check_connections", {"library": "AI_lib", "cell": cell})
    assert not gate.get("problems"), gate
    print("GATE OK", flush=True)

    sim = tools.call(cfg, "run_simulation", {"library": "AI_lib", "cell": cell})
    assert sim.get("status") == "done" and sim.get("dataset_path"), sim
    print("SIM", sim["status"], sim["dataset_path"], flush=True)

    expr = "dB(S(2,1))"
    traces = tools.call(cfg, "read_traces", {
        "path": sim["dataset_path"], "expressions": [expr], "max_points": 0,
    })
    trace = traces["traces"][expr]
    assert len(trace["x"]) > 50000 and not trace.get("truncated"), trace
    print("FULL_TRACE", len(trace["x"]), flush=True)

    body = {
        "design": {"library": "AI_lib", "cell": cell},
        "band": {"start": 5.8, "stop": 6.0, "unit": "GHz"},
        "metrics": [{"kind": "min_in_band", "expr": expr,
                     "target": -10, "unit": "dB"}],
        "simulate": False,
        "reuse_dataset": True,
        "dataset_path": sim["dataset_path"],
        "output_dir": sim["output_dir"],
    }
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        "http://127.0.0.1:8760/design/run",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 ads_auth.TOKEN_HEADER: ads_auth.ensure_token()},
        method="POST",
    )
    with opener.open(req, timeout=180) as response:
        job = json.load(response)["job"]
    metric = job["metrics"][0]
    assert metric["exact"] is True, metric
    assert metric["actual"] == min(
        y for x, y in zip(trace["x"], trace["y"]) if 5.8e9 <= x <= 6.0e9
    ), metric
    stored = job["artifacts"]["traces"][expr]
    assert len(stored["x"]) <= 600, len(stored["x"])
    print("METRIC", metric["actual"], "EXACT", metric["exact"],
          "DISPLAY", len(stored["x"]), "JOB", job["job_id"], flush=True)


if __name__ == "__main__":
    main()
