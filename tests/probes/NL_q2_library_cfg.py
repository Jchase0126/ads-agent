# 只读探测 Q2：读取 TDK 库的 eesof_lib.cfg 变量（证明 cfg 是否被 ADS 解析）
import json

out = {}
LIBS = ["TDK_Component_Library_v2019.10", "TDK_Component_Library_v2019.10_tech"]
CFG_VARS = ["BOOT_AEL", "DESIGN_KIT_NAME", "DESIGN_KIT_ROOT", "VERSION", "LIB_BROWSER_CTL",
            "INPUT_DATA_PATH", "DEFAULTS_DESIGNS", "UNBOOT_AEL_FUNC", "TECH_DESC"]


def safe(label, fn):
    try:
        out[label] = fn()
    except Exception as e:  # noqa: BLE001
        out[label] = "ERR: %r" % (e,)


def _libs():
    from keysight.ads import de
    ws = de.active_workspace()
    res = {}
    for name in LIBS:
        entry = {}
        try:
            lib = ws.libraries[name]
        except Exception as e:  # noqa: BLE001
            res[name] = "ERR get: %r" % (e,)
            continue
        entry["path"] = str(lib.path)
        entry["lib_name"] = lib.lib_name
        entry["is_read_only"] = bool(lib.is_read_only)
        entry["is_an_ads_library"] = bool(lib.is_an_ads_library)
        cfg = {}
        for v in CFG_VARS:
            try:
                cfg[v] = lib.get_library_cfg_var(v)
            except Exception as e:  # noqa: BLE001
                cfg[v] = "ERR: %r" % (e,)
        entry["cfg_substituted"] = cfg
        raw = {}
        for v in ["BOOT_AEL", "DESIGN_KIT_ROOT"]:
            try:
                raw[v] = lib.get_raw_library_cfg_var(v)
            except Exception as e:  # noqa: BLE001
                raw[v] = "ERR: %r" % (e,)
        entry["cfg_raw"] = raw
        # 用绝对路径直接读文件是否存在
        import os
        boot = cfg.get("BOOT_AEL")
        if isinstance(boot, str):
            for cand in [boot, boot + ".ael", boot + ".atf"]:
                entry.setdefault("boot_ael_probe", {})[cand] = os.path.exists(cand)
        gf = os.path.join(str(lib.path), "eesof_lib.cfg")
        entry["eesof_lib_cfg_exists"] = os.path.exists(gf)
        res[name] = entry
    return res


safe("libraries", _libs)

print("PROBE_Q2_BEGIN")
print(json.dumps(out, indent=2, default=str))
print("PROBE_Q2_END")
