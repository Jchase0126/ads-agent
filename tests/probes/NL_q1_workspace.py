# 只读探测 Q1：当前工作区 + 已挂接库
import json

out = {}


def safe(label, fn):
    try:
        out[label] = fn()
    except Exception as e:  # noqa: BLE001
        out[label] = "ERR: %r" % (e,)


try:
    from keysight.ads import de
    out["import_de"] = "ok"
except Exception as e:  # noqa: BLE001
    out["import_de"] = "ERR: %r" % (e,)

out["workspace_is_open"] = "n/a"
try:
    from keysight.ads import de
    out["workspace_is_open"] = de.workspace_is_open()
except Exception as e:  # noqa: BLE001
    out["workspace_is_open"] = "ERR: %r" % (e,)


def _ws():
    from keysight.ads import de
    ws = de.active_workspace()
    d = {}
    d["path"] = str(ws.path)
    d["is_open"] = bool(ws.is_open)
    d["lib_defs_file"] = str(ws.lib_defs_file)
    d["library_names"] = sorted(ws.library_names)
    d["writable_library_names"] = sorted(ws.writable_library_names)
    return d


safe("active_workspace", _ws)

print("PROBE_Q1_BEGIN")
print(json.dumps(out, indent=2, default=str))
print("PROBE_Q1_END")
