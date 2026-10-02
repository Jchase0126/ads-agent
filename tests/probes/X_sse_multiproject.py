"""X：风险3 真实 SSE 链路下的多项目并行验证（PySide6 offscreen + 真后端 + 真LLM）。

场景：
1. 项目 A 发长任务（驱动 run_simulation 真工具链）
2. A 在途时切到项目 B：B 视图应为空（不被 A 的流式事件污染）
3. 等 A 完成：A 的完整回复写回 ProjA
4. ProjB 发短消息：B 自己的会话正常、且不含 A 的内容
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, r"D:\Antenna\ADS Agent\tests")
from _harness import add_path  # noqa: E402

add_path("addon", "ads_agent")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

# 临时 config：真后端 8760 + 真实 DeepSeek key（从仓库 config 抄）
ROOT = r"D:\Antenna\ADS Agent"
TMP = tempfile.mkdtemp(prefix="ads_agent_sse_")
real_cfg = open(os.path.join(ROOT, "config.ini"), encoding="utf-8").read()
key = ""
token = ""
for ln in real_cfg.splitlines():
    if ln.strip().startswith("api_key"):
        key = ln.split("=", 1)[1].strip()
    if ln.strip().startswith("token ") or ln.strip() == "token":
        token = ln.split("=", 1)[1].strip()
tmp_cfg = os.path.join(TMP, "config.ini")
open(tmp_cfg, "w", encoding="utf-8").write(
    "[llm]\nbase_url = https://api.deepseek.com\nmodel = deepseek-chat\n"
    "api_key = " + key + "\n\n[backend]\nhost = 127.0.0.1\nport = 8760\n\n"
    "[ads]\nhost = 127.0.0.1\nport = 8762\ntoken = " + token + "\n")
os.environ["ADS_AGENT_CONFIG"] = tmp_cfg

import panel  # noqa: E402

panel.AgentPanelWidget._PROJECTS_FILE = os.path.join(TMP, "projects.json")

app = QApplication.instance() or QApplication([])
w = panel.AgentPanelWidget()
w.resize(900, 700)
w.show()


def pump(ms):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def wait_worker_done(timeout_s):
    end = time.time() + timeout_s
    while time.time() < end:
        app.processEvents()
        if w._worker is None or not w._worker.isRunning():
            pump(800)  # 尾部事件落盘
            return True
        time.sleep(0.05)
    return False


print("== 1) 项目 A 发长任务（真工具链 run_simulation）")
w._apply_project("ProjA", create=True)
w.input.setPlainText("请对 AI_lib:AI_probe_sim 跑一次仿真，然后只告诉我：仿真的状态和 S(2,1) 在 5.9GHz 附近的幅度（dB），两行以内。")
w._on_send()
assert w._worker is not None and w._worker.isRunning(), "A 的 worker 应已启动"
pump(3000)

print("== 2) A 在途时切到项目 B")
w._apply_project("ProjB", create=True)
pump(300)
b_entries_during = [e["kind"] for e in w.entries]
print("   B 视图 entries（应只有 welcome/空）:", b_entries_during[:4])

print("== 3) 等 A 完成")
ok_a = wait_worker_done(240)
a_entries = w.projects_data["projects"]["ProjA"]["entries"]
a_kinds = [e["kind"] for e in a_entries]
a_text = " ".join(str(e.get("text", "")) for e in a_entries)
for e in a_entries:
    print("   A entry:", e["kind"], str(e.get("text", ""))[:150].replace(chr(10), " | "))
print("   A 完成:", ok_a, " entries:", a_kinds[:10], "…共", len(a_entries))
print("   A 回复含仿真结果:", ("done" in a_text or "S" in a_text))

print("== 4) 项目 B 发短消息")
w._apply_project("ProjB")
w.input.setPlainText("只回答数字，不要调用工具：1+1 等于几？")
w._on_send()
ok_b = wait_worker_done(120)
b_entries = w.projects_data["projects"]["ProjB"]["entries"]
b_kinds = [e["kind"] for e in b_entries]
b_text = " ".join(str(e.get("text", "")) for e in b_entries)
for e in b_entries:
    print("   B entry:", e["kind"], str(e.get("text", ""))[:150].replace(chr(10), " | "))
print("   B 完成:", ok_b, " entries:", b_kinds, " 回复含 2:", "2" in b_text)

print("== 5) 归属核验")
a_final = " ".join(str(e.get("text", "")) for e in a_entries if e["kind"] == "assistant")
b_final = " ".join(str(e.get("text", "")) for e in b_entries if e["kind"] == "assistant")
cross1 = "AI_probe_sim" in b_text or "S(2,1)" in b_text
cross2 = "1+1" in a_final or "等于几" in a_final
print("   B 被 A 污染:", cross1, "  A 被 B 污染:", cross2)
pj = None
for cand in (os.path.join(TMP, "projects.json"),):
    if os.path.exists(cand):
        pj = json.load(open(cand, encoding="utf-8"))
        break
print("   projects.json 落盘:", pj is not None,
      " 项目:", sorted((pj or {}).get("projects", {}).keys()))
if pj:
    for name in ("ProjA", "ProjB"):
        pr = pj["projects"].get(name) or {}
        print(f"   {name}: entries={len(pr.get('entries', []))} history={len(pr.get('history', []))}")

print("== 结论 ==")
verdict = (ok_a and ok_b and not cross1 and not cross2
           and pj is not None and len(a_entries) >= 2 and len(b_entries) >= 2)
print("真实 SSE 多项目并行:", "通过" if verdict else "不通过")
