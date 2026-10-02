"""T：风险1 sim_off_main_thread 线程安全验证（并行 vs 串行）。

1. 建临时 cell AI_probe_sim（5G9 同构，SP 15001 点 => 仿真足够长）
2. 并行模式跑 run_simulation：期间轮询 /health + run_python ping 测主线程响应
3. read_traces 取 S(2,1) 基准
4. config.ini [agent] sim_off_main_thread=false 重跑：串行期间 ping 应阻塞
5. 逐点对比两次 S(2,1)，恢复 config
"""

import json
import sys
import threading
import time
import urllib.request

ROOT = r"D:\Antenna\ADS Agent"
sys.path.insert(0, ROOT + r"\backend")
sys.path.insert(0, ROOT + r"\tests")

import config  # noqa: E402
import tools  # noqa: E402

ADS = "http://127.0.0.1:8762"
_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
cfg = config.load()


def health():
    try:
        with _op.open(ADS + "/health", timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def set_flag(flag):
    """改 config.ini 的 [agent] sim_off_main_thread（保留注释的行级替换）。"""
    p = ROOT + r"\config.ini"
    lines = open(p, encoding="utf-8").read().splitlines()
    out, found, section = [], False, False
    for ln in lines:
        if ln.strip().startswith("["):
            section = ln.strip() == "[agent]"
            out.append(ln)
            continue
        if section and ln.strip().startswith("sim_off_main_thread"):
            out.append("sim_off_main_thread = " + flag)
            found = True
        else:
            out.append(ln)
    if not found:
        fixed, inserted, sec = [], False, False
        for ln in out:
            if ln.strip() == "[agent]":
                sec = True
                fixed.append(ln)
                continue
            if sec and ln.strip().startswith("[") and not inserted:
                fixed.append("sim_off_main_thread = " + flag)
                inserted = True
            fixed.append(ln)
        if not inserted:
            fixed.append("sim_off_main_thread = " + flag)
        out = fixed
    open(p, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")


def run_sim_async(lib, cell, sink):
    try:
        sink["result"] = tools.call(cfg, "run_simulation", {"library": lib, "cell": cell})
    except Exception as e:  # noqa: BLE001
        sink["error"] = str(e)


def ping_main_thread(tag):
    t0 = time.time()
    try:
        r = tools.call(cfg, "run_python", {"code": "print('pong')"})
        dt = time.time() - t0
        ok = "pong" in (r.get("stdout") or "")
        print(f"  [{tag}] 主线程 ping: {'通' if ok else '异常'} 延迟 {dt:.2f}s")
        return dt
    except Exception as e:  # noqa: BLE001
        dt = time.time() - t0
        print(f"  [{tag}] 主线程 ping 失败({dt:.1f}s): {str(e)[:80]}")
        return dt


print("== 1) 建临时 cell（5G9 同构 + 15001 点）")
from repair_wilkinson_5g9_layout import SPEC  # noqa: E402

spec = json.loads(json.dumps(SPEC))
spec["library"], spec["cell"] = "AI_lib", "AI_probe_sim"
spec["recreate"] = True
spec["layout"] = "auto"
sp = [i for i in spec["instances"] if i["name"] == "SP1"][0]
sp["params"]["Step"] = "0.00005 GHz"
r = tools.call(cfg, "build_schematic", spec)
print("  placed:", len(r.get("placed", [])),
      "conn ok:", all(c.get("status") == "ok" for c in r["connections"]))

stash = {}
for mode, flag in (("并行(后台线程)", None), ("串行(主线程)", "false")):
    print(f"== 2) {mode} 跑仿真")
    set_flag("true" if flag is None else flag)
    h = health()
    print("   config 生效值 =", h.get("sim_off_main_thread"))

    samples, sink, stop = [], {}, threading.Event()

    def poller():
        while not stop.is_set():
            h2 = health()
            jobs = h2.get("jobs") or []
            if h2.get("busy") and jobs:
                samples.append((time.time(), h2["busy"], jobs[0].get("elapsed_s")))
            time.sleep(0.3)

    pt = threading.Thread(target=poller, daemon=True)
    pt.start()
    st = threading.Thread(target=run_sim_async, args=("AI_lib", "AI_probe_sim", sink))
    t0 = time.time()
    st.start()
    for k in range(6):
        time.sleep(0.8)
        ping_main_thread(f"{mode} t+{0.8*(k+1):.1f}s")
    st.join(timeout=300)
    stop.set()
    pt.join(timeout=2)
    dur = time.time() - t0
    res = sink.get("result") or {}
    err = sink.get("error")
    print(f"  仿真总耗时 {dur:.1f}s status={res.get('status') or ('ERR ' + str(err)[:80])}")
    print(f"  /health 忙碌采样 {len(samples)} 条 busy={sorted(set(s[1] for s in samples))}")
    if samples:
        els = [s[2] for s in samples if s[2] is not None]
        mono = all(b >= a for a, b in zip(els, els[1:]))
        print(f"  elapsed_s {els[0]} -> {els[-1]} 递增={mono}")
    tr = tools.call(cfg, "read_traces", {"path": res.get("dataset_path"),
                                         "expressions": ["S(2,1)"]})
    d = (tr.get("traces") or {}).get("S(2,1)") or {}
    stash[mode] = (d.get("x") or [], d.get("y") or [], dur, samples)

set_flag("true")
print("== 4) 并行 vs 串行 逐点对比")
xp, yp, *_ = stash["并行(后台线程)"]
xs, ys, *_ = stash["串行(主线程)"]
if len(yp) == len(ys) and yp:
    worst = max(abs(complex(a) - complex(b)) / max(abs(complex(b)), 1e-12)
                for a, b in zip(yp, ys))
    print(f"  点数一致 {len(yp)}，最大相对差 {worst:.3g}（<1e-9 视为一致）")
else:
    print(f"  点数不一致 {len(yp)} vs {len(ys)}")
