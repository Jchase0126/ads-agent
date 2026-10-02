"""W：闭环5 失败留产物 —— 临时 cell 删 Riso 变量 => 网表生成后仿真报错。"""
import json
import os
import sys
import urllib.request

ROOT = r"D:\Antenna\ADS Agent"
sys.path.insert(0, ROOT + r"\backend")
sys.path.insert(0, ROOT + r"\tests")
import ads_auth  # noqa: E402
from repair_wilkinson_5g9_layout import SPEC  # noqa: E402

_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_hdr = {"Content-Type": "application/json", ads_auth.TOKEN_HEADER: ads_auth.ensure_token()}

spec = json.loads(json.dumps(SPEC))
spec["library"], spec["cell"] = "AI_lib", "AI_probe_fail"
spec["recreate"] = True
spec["layout"] = "auto"
spec["var"]["values"].pop("Riso")  # 制造网表级错误：Riso 未定义
r = tools_call = None
import config  # noqa: E402
import tools  # noqa: E402
cfg = config.load()
res = tools.call(cfg, "build_schematic", spec)
print("临时 cell 建好:", res.get("n_instances"), "实例")

ds_spec = {
    "design": {"library": "AI_lib", "cell": "AI_probe_fail"},
    "requirement": "故意失败：Riso 未定义",
    "band": {"start": 4.5, "stop": 7.5, "unit": "GHz"},
    "metrics": [{"kind": "min_in_band", "expr": "dB(S(2,1))", "target": "-3", "unit": "dB"}],
    "simulate": True,
}
req = urllib.request.Request("http://127.0.0.1:8760/design/run",
    data=json.dumps(ds_spec).encode(), headers=_hdr, method="POST")
with _op.open(req, timeout=280) as resp:
    job = json.loads(resp.read().decode()).get("job")
print("stage =", job.get("stage"), " verdict =", job.get("verdict"))
print("error =", str(job.get("error"))[:160])
art = job.get("artifacts") or {}
out_dir = art.get("output_dir") or ""
print("output_dir =", out_dir)
if out_dir and os.path.isdir(out_dir):
    print("产物:", sorted(os.listdir(out_dir))[:8])
    print("netlist 存在:", os.path.exists(os.path.join(out_dir, "netlist.ckt")))
sm = (job.get("iterations")[-1].get("metrics") or []) if job.get("iterations") else []
print("指标判定:", [(m.get("id"), m.get("actual"), m.get("pass")) for m in sm] or "无（unknown）")
jf = os.path.join(ROOT, "design_jobs", job.get("job_id") + ".json")
print("job 文件留存:", os.path.exists(jf))
