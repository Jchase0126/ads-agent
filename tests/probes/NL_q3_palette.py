# 只读探测 Q3：palette 是否已注册？AEL 侧 boot 副作用 + 窗口枚举
import json

out = {}

# boot.ael 里定义的 AEL 全局变量：若存在（非 null）则说明 boot 已跑
BOOT_DECLS = [
    "TDK_DK_PATH", "TDK_DK_CIRCUIT_DATA_DIR", "TDK_DK_BITMAP_DIR",
    "TDK_DK_CIRCUIT_AEL_DIR", "TDK_DK_DE_AEL_DIR", "TDK_DK_CIRCUIT_MODELS_DIR",
    "DEMOKIT_MMWAVE_NAME", "adstempvar",
]
# 可能的 palette 相关 AEL 函数名（含团队提到的）
AEL_FUNCS = [
    "de_define_library_palette", "de_define_palette_group", "dk_define_palette_group",
    "deitem_get_visible_palette_name", "de_get_palette_groups", "de_current_palette_group",
    "de_undefine_library_palette", "de_define_library_palette_group",
    "de_get_visible_palette_name", "de_get_palettes",
    "de_add_library_to_workspace_read_only_mode", "de_open_library_read_only_mode",
    "de_is_library_open", "de_get_open_libraries", "de_get_library_config_file_var",
]


def safe(label, fn):
    try:
        out[label] = fn()
    except Exception as e:  # noqa: BLE001
        out[label] = "ERR: %r" % (e,)


def _decls():
    from keysight.ads import ael
    res = {}
    for name in BOOT_DECLS:
        try:
            v = ael.decl(convert=False)
            val = getattr(v, name)
            res[name] = {"is_null": bool(val.is_null()), "type": val.ael_type_name()}
        except Exception as e:  # noqa: BLE001
            res[name] = "ERR: %r" % (e,)
    return res


def _funcs():
    from keysight.ads import ael
    res = {}
    for name in AEL_FUNCS:
        try:
            v = ael.decl(convert=False)
            val = getattr(v, name)
            res[name] = {"is_null": bool(val.is_null()), "type": val.ael_type_name()}
        except Exception as e:  # noqa: BLE001
            res[name] = "ERR: %r" % (e,)
    return res


safe("boot_decls", _decls)
safe("ael_funcs", _funcs)


def _windows():
    from keysight.ads import de
    from keysight.ads.de import app
    res = {}
    for wt_name in ["MAIN_WINDOW", "SCHEMATIC_WINDOW", "LAYOUT_WINDOW", "SYMBOL_WINDOW",
                    "BROWSER_WINDOW", "SUBSTRATE_WINDOW", "NOTEBOOK_WINDOW", "CONFIG_VIEW_WINDOW"]:
        wt = getattr(app.WindowType, wt_name)
        wins = app.find_windows_by_type(wt)
        res[wt_name] = [{"title": w.title, "type": int(w.window_type)} for w in wins]
    return res


safe("windows", _windows)

print("PROBE_Q3_BEGIN")
print(json.dumps(out, indent=2, default=str))
print("PROBE_Q3_END")
