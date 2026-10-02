"""在线验收：设计写入安全 + 仿真门禁（需要在 ADS 2027 里开着 AI_lib 工作区）。

通过回环工具服务直接派发新工具（toolserver 按 mtime 热重载 ads_ops.py），
在 scratch cell ``zz_gate_probe`` 上走完整序列：

  1. build_schematic 只放一根 MLIN（Subst 指向不存在的 MSUB1、无控制器）
     -> 应成功落盘，且 sim_readiness 点名「缺控制器 + 基板引用断裂」；
  2. run_simulation -> 门禁必须拦截，报错点名缺什么（磁盘不被碰）；
  3. build_schematic（APPEND）补 MSUB1 + S_Param（仍无端口）
     -> run_simulation 拦截，点名缺端口；
  4. build_schematic（APPEND）补 Term1/Term2 + VAR + 连线
     -> 门禁放行，真跑 hpeesofsim，产出 .ds；
  5. set_design_variables 改 VAR -> 返回 verify.on_disk_ok=true；
  6. 确认 ads_agent_backups/ 里生成了备份。

运行::

    python tests/probe_gate_acceptance.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BACKEND = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ads_auth  # noqa: E402

URL = "http://127.0.0.1:8761/execute"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
LIB = "AI_lib"
CELL = "zz_gate_" + time.strftime("%H%M%S")   # 每次验收用新 cell，避免撞名
_results = []


def call(name, args, timeout=300):
    req = urllib.request.Request(
        URL,
        data=json.dumps({"name": name, "args": args, "timeout": timeout}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 ads_auth.TOKEN_HEADER: ads_auth.ensure_token()},
        method="POST")
    try:
        with _OPENER.open(req, timeout=timeout + 30) as resp:
            return json.loads(resp.read().decode("utf-8")), True
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {"error": f"HTTP {e.code}"}
        return body, False


def step(label, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    _results.append((mark, label))
    print(f"[{mark}] {label}")
    if detail:
        print("    " + str(detail).replace("\n", "\n    ")[:1200])


def main() -> int:
    # 1) 只读体检一个既有设计（顺带确认工具服务与新代码都在）
    body, okflag = call("check_connections",
                        {"library": "AI_lib", "cell": "Wilkinson_2G4"})
    step("check_connections 既有设计", okflag and body.get("n_instances", 0) > 0,
         f"实例 {body.get('n_instances')} 网络 {body.get('n_nets')} "
         f"控制器 {body.get('controllers')} 端口 {body.get('ports')} "
         f"基板断裂 {body.get('broken_substrate_refs')}")

    # 2) 建图阶段 A：只有 MLIN（Subst 断 + 无控制器）
    body, okflag = call("build_schematic", {
        "library": LIB, "cell": CELL,
        "instances": [{"master": "ads_tlines:MLIN", "name": "ML1", "x": 0, "y": 0,
                       "params": {"Subst": "MSUB1", "W": "1 mm", "L": "10 mm"}}],
    })
    rd = (body.get("sim_readiness") or {}).get("problems") or []
    step("build_schematic 新 cell（含只读复核）", okflag and body.get("n_instances") == 1,
         f"backup={body.get('backup')} problems={len(rd)}")
    joined = "\n".join(rd)
    step("sim_readiness 点名缺控制器 + 基板断裂",
         "没有仿真控制器" in joined and "基板引用缺失" in joined, joined[:400])

    # 3) 门禁拦截：缺控制器
    body, okflag = call("run_simulation", {"library": LIB, "cell": CELL})
    step("run_simulation 被门禁拦截（缺控制器）",
         (not okflag) and "仿真前检查未通过" in str(body.get("error", "")),
         str(body.get("error", ""))[:500])

    # 4) 阶段 B：补 MSUB1 + S_Param（仍无端口）
    body, okflag = call("build_schematic", {
        "library": LIB, "cell": CELL,
        "instances": [
            {"master": "ads_tlines:MSUB", "name": "MSUB1", "x": 0, "y": 1200},
            {"master": "ads_simulation:S_Param", "name": "SP1", "x": 0, "y": -1000,
             "params": {"Start": "2 GHz", "Stop": "3 GHz", "Step": "0.01 GHz"}},
        ],
    })
    step("build_schematic 追加（APPEND）基板+控制器", okflag and body.get("n_instances") == 3,
         f"实例数={body.get('n_instances')} 网络={body.get('n_nets')} "
         f"连通核对={body.get('connections')}")

    # 5) 门禁拦截：有控制器没端口
    body, okflag = call("run_simulation", {"library": LIB, "cell": CELL})
    err = str(body.get("error", ""))
    step("run_simulation 被门禁拦截（缺端口）",
         (not okflag) and "没有任何端口" in err, err[:500])

    # 6) 阶段 C：补 Term + VAR + 连线（Term 的 2 号脚是射频信号脚，1 号是地）
    body, okflag = call("build_schematic", {
        "library": LIB, "cell": CELL,
        "instances": [
            {"master": "ads_simulation:Term", "name": "Term1", "x": -500, "y": 0},
            {"master": "ads_simulation:Term", "name": "Term2", "x": 500, "y": 0},
        ],
        "var": {"name": "VAR1", "x": 0, "y": 800, "values": {"Z0": "50 Ohm"}},
        "connections": [
            {"a": ["Term1", 2], "b": ["ML1", 1]},
            {"a": ["ML1", 2], "b": ["Term2", 2]},
        ],
    })
    conns = body.get("connections") or []
    conns_ok = all(c.get("status") == "ok" for c in conns) and len(conns) == 2
    step("build_schematic 追加 Term+VAR+连线，连通核对通过",
         okflag and body.get("n_instances") == 6 and conns_ok
         and (body.get("parameters_ok")),
         f"实例数={body.get('n_instances')} 连线={conns} 参数OK={body.get('parameters_ok')} "
         f"备份={body.get('backup', {}).get('path', '')}")

    # 7) 门禁放行：真仿真
    body, okflag = call("run_simulation", {"library": LIB, "cell": CELL}, timeout=600)
    step("run_simulation 门禁放行并产出 .ds",
         okflag and body.get("status") == "done"
         and os.path.exists(str(body.get("dataset_path", ""))),
         f"status={body.get('status')} dataset={body.get('dataset_path')} "
         f"variables={len(body.get('variables') or [])}个 "
         f"audit={body.get('audit')}")

    # 7b) 电学正确性：连真的通了，功率才能到 2 端口（S21 接近 0 dB）
    ds = str(body.get("dataset_path", ""))
    body, okflag = call("read_traces", {
        "path": ds, "expressions": ["dB(S(2,1))", "dB(S(1,1))"]}, timeout=300)
    traces = body.get("traces") or {}
    s21 = traces.get("dB(S(2,1))") or {}
    s11 = traces.get("dB(S(1,1))") or {}
    s21_mean = sum(s21.get("y") or []) / max(1, len(s21.get("y") or []))
    s11_max = max(s11.get("y") or [-999])
    step("电学验证：S21≈0dB（连通）且 S11 不为 0dB（非全反射）",
         okflag and s21_mean > -3 and s11_max < -3,
         f"S21 均值 {s21_mean:.2f} dB，S11 最大 {s11_max:.2f} dB"
         + ("" if s21.get("y") else f"（原始返回: {str(body)[:200]}）"))

    # 8) set_design_variables 带保存后只读复核
    body, okflag = call("set_design_variables",
                        {"library": LIB, "cell": CELL, "values": {"Z0": "70.7 Ohm"}})
    step("set_design_variables 落盘复核通过",
         okflag and (body.get("verify") or {}).get("on_disk_ok") is True,
         f"changed={body.get('changed')} backup={body.get('backup', {}).get('path', '')}")

    # 9) 备份目录确实存在
    body, okflag = call("get_workspace_info", {})
    ws_path = str(body.get("path", ""))
    bdir = os.path.join(ws_path, "ads_agent_backups")
    backups = os.listdir(bdir) if os.path.isdir(bdir) else []
    step("ads_agent_backups/ 已生成备份", any(d.startswith(f"AI_lib__{CELL}__") for d in backups),
         f"{bdir} -> {[d for d in backups if CELL in d]}")

    print("-" * 60)
    fails = [r for r in _results if r[0] == "FAIL"]
    print(f"验收结果: {'全部通过' if not fails else '存在失败: ' + str(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
