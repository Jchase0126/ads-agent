"""手动实测：真实模型流 → 仅只读工具 → 真实 ADS（需要运行中的 ADS）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
import config
import llm
import tools


def main():
    cfg = config.load()
    allowed = {"get_workspace_info", "list_designs"}
    schemas = [t for t in tools.TOOLS if t["function"]["name"] in allowed]
    msg, _ = llm.chat_stream(cfg, [{"role": "user", "content":
        "请立即调用 get_workspace_info 和 list_designs 查看当前 ADS 工作区。"
        "这是只读连通性验证，不要修改任何设计。"}], tools=schemas)
    calls = msg.get("tool_calls") or []
    print("complete=", msg.get("complete"), "calls=",
          [c["function"]["name"] for c in calls], flush=True)
    if msg.get("complete") is not True or not calls:
        raise RuntimeError("完整模型流没有返回工具调用")
    for call in calls:
        name = call["function"]["name"]
        if name not in allowed:
            raise RuntimeError("拒绝非只读工具: " + name)
        args = json.loads(call["function"]["arguments"])
        result = tools.call(cfg, name, args)
        print(name, json.dumps(result, ensure_ascii=False, default=str)[:1800], flush=True)


if __name__ == "__main__":
    main()
