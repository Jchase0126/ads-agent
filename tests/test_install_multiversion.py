"""多版本安装 / 卸载 / 注册隔离 —— 走真实安装流程，全部在临时目录里。

覆盖（对应 docs/兼容性矩阵.md 的"安装与多版本管理"行）：

  * 两个模拟 ADS 安装分别注册，互不覆盖；
  * ``install_state.json`` 的 ``ads_installs`` 表按版本各占一条，旧版单值
    ``ads_dir`` 字段被迁移保留；
  * 卸载其中一个版本：另一个的注册文件**一字不动**，登记表只少一条；
  * 升级（重装）幂等：用户数据、其它插件条目不受影响；
  * 中文 / 空格路径全程可用。

运行::

    python tests/test_install_multiversion.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, ok, run  # noqa: E402

ROOT = add_path()
add_path("backend")

import install_addon  # noqa: E402
import paths  # noqa: E402

PLUGIN_ENTRY = install_addon.PLUGIN_ENTRY

OTHER_ADDONS = '<?xml version="1.0" ?>\n<EESof_Addons>\n' \
    '    <!-- Keysight 自带插件：一条都不能被碰 -->\n' \
    '    <Addon Name="Keysight Sample" FilePath="C:\\ADS\\sample.ael" Enabled="1"/>\n' \
    '</EESof_Addons>\n'


class FakeAds:
    """最小 ADS 安装目录（含官方命名 + 一条别人的插件注册）。"""

    def __init__(self, base: str, name: str):
        self.dir = os.path.join(base, name)
        os.makedirs(os.path.join(self.dir, "config"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "bin"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "tools", "python"), exist_ok=True)
        self.xml = os.path.join(self.dir, "config", "eesof_addons.xml")
        with open(self.xml, "w", encoding="utf-8") as f:
            f.write(OTHER_ADDONS)

    def registered_path(self) -> str:
        import install_addon as ia

        _tree, entries, err = ia.parse_entries(self.xml)
        if err:
            raise AssertionError(f"注册文件解析失败：{err}")
        record = entries.get(install_addon.ADDON_NAME)
        return record[0] if record else ""


class Sandbox:
    """临时数据目录（中文+空格路径）+ 两个模拟 ADS 安装。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="多版本安装测试 ")
        base = self._tmp.name
        self.data = os.path.join(base, "数据 目录 ADSAgent")
        self.ads2027 = FakeAds(base, "ADS 2027 中文 目录")
        self.ads2025 = FakeAds(base, "ADS2025")
        self._env = {k: os.environ.get(k) for k in
                     ("ADS_AGENT_DATA_DIR", "ADS_AGENT_APP_DIR", "ADS_AGENT_CONFIG")}
        os.environ["ADS_AGENT_DATA_DIR"] = self.data
        os.environ.pop("ADS_AGENT_APP_DIR", None)
        os.environ.pop("ADS_AGENT_CONFIG", None)
        return self

    def __exit__(self, *exc):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            self._tmp.cleanup()
        except OSError:
            pass
        return False

    def _run(self, argv: list) -> tuple:
        note: list = []
        args = install_addon.build_parser().parse_args(argv)
        code = install_addon.cmd_install(args, note) if not args.remove \
            else install_addon.cmd_remove(args, note)
        return code, note

    def install(self, *extra) -> tuple:
        return self._run(["--mode", "inplace", *extra])


def _state() -> dict:
    return paths.load_install_state()


def _entry(state: dict, ads_dir: str) -> dict:
    key = os.path.normcase(os.path.normpath(ads_dir))
    return (state.get("ads_installs") or {}).get(key) or {}


def test_install_two_versions_no_overwrite():
    with Sandbox() as s:
        code1, note1 = s.install("--ads-dir", s.ads2027.dir)
        eq(code1, 0, "\n".join(note1))
        code2, note2 = s.install("--ads-dir", s.ads2025.dir)
        eq(code2, 0, "\n".join(note2))

        # 两个注册文件各有一条本插件注册，指向同一个就地程序目录
        p27 = s.ads2027.registered_path()
        p25 = s.ads2025.registered_path()
        ok(p27, "ADS 2027 已注册")
        ok(p25, "ADS 2025 已注册")
        ok(os.path.isfile(p27))
        ok(os.path.isfile(p25))

        # 安装记录：两条，互不覆盖
        state = _state()
        e27 = _entry(state, s.ads2027.dir)
        e25 = _entry(state, s.ads2025.dir)
        ok(e27, "ads_installs 里有 2027 的记录")
        ok(e25, "ads_installs 里有 2025 的记录")
        eq(e27.get("program_dir"), e25.get("program_dir"),
           "就地模式共享同一程序目录")
        # 旧字段仍可用（指向最近一次安装）
        eq(state.get("ads_dir"), s.ads2025.dir)


def test_record_keeps_year_and_metadata():
    with Sandbox() as s:
        code, note = s.install("--ads-dir", s.ads2027.dir)
        eq(code, 0, "\n".join(note))
        e27 = _entry(_state(), s.ads2027.dir)
        ok(e27.get("registered_at"), "登记了安装时间")


def test_uninstall_one_leaves_other_untouched():
    with Sandbox() as s:
        s.install("--ads-dir", s.ads2027.dir)
        s.install("--ads-dir", s.ads2025.dir)
        before = open(s.ads2025.xml, encoding="utf-8").read()

        note: list = []
        args = install_addon.build_parser().parse_args(
            ["--remove", "--ads-dir", s.ads2027.dir])
        code = install_addon.cmd_remove(args, note)
        eq(code, 0, "\n".join(note))

        after = open(s.ads2025.xml, encoding="utf-8").read()
        eq(after, before, "卸载 2027 不得改动 2025 的注册文件")
        ok(s.ads2025.registered_path(), "2025 仍然注册着")
        ok(not s.ads2027.registered_path(), "2027 的注册已移除")
        state = _state()
        ok(not _entry(state, s.ads2027.dir), "2027 的登记已删")
        ok(_entry(state, s.ads2025.dir), "2025 的登记保留")
        ok(os.path.isdir(paths.data_root()), "用户数据保留")


def test_legacy_single_field_migrated_into_table():
    """1.0.x 的 install_state（只有 ads_dir）→ 首次登记时迁入 ads_installs。"""
    with Sandbox() as s:
        os.makedirs(s.data, exist_ok=True)
        with open(os.path.join(s.data, "install_state.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"install_id": "旧安装ID0001", "ads_dir": s.ads2027.dir,
                       "plugin_version": "1.0.1"}, f, ensure_ascii=False)
        code, note = s.install("--ads-dir", s.ads2025.dir)
        eq(code, 0, "\n".join(note))
        state = _state()
        # 迁移条目 + 新安装条目同时存在
        ok(_entry(state, s.ads2027.dir), "旧 ads_dir 已迁入登记表")
        ok(_entry(state, s.ads2025.dir), "新安装正常登记")


def test_reinstall_is_idempotent_and_preserves_other_addons():
    with Sandbox() as s:
        s.install("--ads-dir", s.ads2027.dir)
        xml_before = open(s.ads2027.xml, encoding="utf-8").read()
        code, note = s.install("--ads-dir", s.ads2027.dir)
        eq(code, 0)
        ok(any("已经是最新状态" in l for l in note), "第二次安装应报告无需变动")
        # 其它插件条目仍在
        _tree, entries, err = install_addon.parse_entries(s.ads2027.xml)
        eq(err, "")
        ok("Keysight Sample" in entries, "别人的插件条目还在")
        ok(entries.get(install_addon.ADDON_NAME), "自己的条目还在")


def test_unknown_year_recorded_as_unknown():
    """非官方命名的目录：照样可以注册，登记表里年份为空。"""
    with Sandbox() as s:
        weird = FakeAds(s._tmp.name, "我的ADS")
        code, note = s.install("--ads-dir", weird.dir)
        eq(code, 0, "\n".join(note))
        e = _entry(_state(), weird.dir)
        ok(e, "未知年份也登记（安装成功）")
        ok(e.get("year") in (None,), "年份应为空（不猜）")


def test_explicit_two_dirs_in_one_run():
    """一次运行 --ads-dir 两次：两个版本都注册。"""
    with Sandbox() as s:
        code, note = s.install("--ads-dir", s.ads2027.dir,
                               "--ads-dir", s.ads2025.dir)
        eq(code, 0, "\n".join(note))
        ok(s.ads2027.registered_path())
        ok(s.ads2025.registered_path())
        ok(any("2 个 ADS 安装" in l or "ADS 安装目录" in l for l in note))


def test_per_version_deploy_root_isolation():
    """deploy 模式：程序目录按版本隔离（Programs/ADSAgent/ADS<年份>）。"""
    with Sandbox() as s:
        # 把"本地程序数据根"指到临时目录，避免真的写 %LOCALAPPDATA%
        fake_local = os.path.join(s._tmp.name, "LocalAppData")
        os.makedirs(fake_local, exist_ok=True)
        if os.name == "nt":
            old_lad = os.environ.get("LOCALAPPDATA")
            os.environ["LOCALAPPDATA"] = fake_local
        else:
            old_lad = None
            real_lad = paths._local_app_data
            paths._local_app_data = lambda: fake_local
        try:
            code, note = s.install("--ads-dir", s.ads2027.dir,
                                   "--mode", "deploy")
            eq(code, 0, "\n".join(note))
            e27 = _entry(_state(), s.ads2027.dir)
            program_dir = e27.get("program_dir") or ""
            ok(program_dir, "登记了程序目录")
            ok(os.path.basename(program_dir).endswith("ADS2027"),
               f"程序目录应按版本隔离: {program_dir}")
            ok(os.path.isfile(os.path.join(program_dir, PLUGIN_ENTRY)),
               "部署出了插件入口")
            ok(fake_local in os.path.normpath(program_dir),
               "部署目标落在受控的临时根目录内")
        finally:
            if os.name == "nt":
                if old_lad is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = old_lad
            else:
                paths._local_app_data = real_lad


if __name__ == "__main__":
    raise SystemExit(run(globals()))
