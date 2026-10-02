"""U：风险5 /health 长仿真忙碌状态 + 超时 timed_out 路径。"""
import json
import sys
import threading
import time
import urllib.request

ROOT = r"D:\Antenna\ADS Agent"
sys.path.insert(0, ROOT + r"\backend")
import ads_auth  # noqa: E402
import config  # noqa: E402

ADS = "http://127.0.0.1:8762"
_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
cfg = config.load()
_hdr = {ads_auth.TOKEN_HEADER: ads_auth.ensure_token(), "Content-Type": "application/json"}


def health():
    try:
        with _op.open(ADS + "/health", timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def call_raw(name, args, timeout, sink):
    body = json.dumps({"name": name, "args": args, "timeout": timeout}).encode()
    req = urllib.request.Request(ADS + "/execute", data=body, headers=_hdr, method="POST")
    t0 = time.time()
    try:
        with _op.open(req, timeout=timeout + 15) as r:
            sink["resp"] = (r.status, json.loads(r.read().decode()), time.time() - t0)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            detail = {}
        sink["resp"] = (e.code, detail, time.time() - t0)
    except Exception as e:  # noqa: BLE001
        sink["resp"] = ("exc", str(e), time.time() - t0)


print("== A) 正常长仿真：/health 全程采样")
samples, stop, sink = [], threading.Event(), {}


def poll():
    while not stop.is_set():
        h = health()
        samples.append((round(time.time() - t0, 1), h.get("busy"),
                        [(j.get("name"), j.get("elapsed_s"), j.get("timed_out")) for j in (h.get("jobs") or [])]))
        time.sleep(0.5)


pt = threading.Thread(target=poll, daemon=True)
pt.start()
th = threading.Thread(target=call_raw, args=("run_simulation",
                       {"library": "AI_lib", "cell": "AI_probe_sim"}, 900, sink))
t0 = time.time()
th.start()
th.join(timeout=300)
stop.set()
pt.join(timeout=2)
code, body, took = sink.get("resp", ("?", {}, 0))
print(f"  请求返回 code={code} 耗时={took:.1f}s status={body.get('status') or body.get('error', '')[:60]}")
busy_seq = [s for s in samples if s[1]]
print(f"  采样 {len(samples)} 条，其中 busy {len(busy_seq)} 条")
if busy_seq:
    print("  busy 值集合:", sorted(set(s[1] for s in busy_seq)))
    els = [j[1] for s in busy_seq for j in s[2] if j[0] == "run_simulation"]
    print(f"  run_simulation elapsed_s: {els[0]} -> {els[-1]} 单调递增={all(b >= a for a, b in zip(els, els[1:]))}")
    to = [j[2] for s in busy_seq for j in s[2]]
    print(f"  期间 timed_out 标记全部 False = {not any(to)}")
tail = [s for s in samples if s[1] is None]
print(f"  结束后 busy=None 采样 {len(tail)} 条（应为 >0）")

print("== B) 短超时触发：timeout=2s，作业应继续后台跑并标记 timed_out")
samples2, stop2, sink2 = [], threading.Event(), {}


def poll2():
    while not stop2.is_set():
        h = health()
        jobs = [(j.get("name"), j.get("elapsed_s"), j.get("timed_out")) for j in (h.get("jobs") or [])]
        samples2.append((round(time.time() - t0, 1), h.get("busy"), jobs))
        time.sleep(0.4)


pt2 = threading.Thread(target=poll2, daemon=True)
pt2.start()
th2 = threading.Thread(target=call_raw, args=("run_simulation",
                        {"library": "AI_lib", "cell": "AI_probe_sim"}, 2, sink2))
t0 = time.time()
th2.start()
th2.join(timeout=60)
code2, body2, took2 = sink2.get("resp", ("?", {}, 0))
print(f"  请求返回 code={code2} 耗时={took2:.1f}s error={str(body2.get('error'))[:80]}")
time.sleep(12)
stop2.set()
pt2.join(timeout=2)
after = [s for s in samples2 if any(j[2] for j in s[2])]
print(f"  超时后带 timed_out=True 的采样 {len(after)} 条")
if after:
    print("  首条:", after[0][0], "s ->", after[0][2])
    print("  末条:", after[-1][0], "s ->", after[-1][2])
h_end = health()
print("  最终 busy =", h_end.get("busy"), " jobs =", h_end.get("jobs"))
