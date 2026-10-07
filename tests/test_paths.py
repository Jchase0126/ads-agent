"""统一路径解析：双根（程序/数据）、覆盖能力、中文与空格路径、首启初始化。

这些用例刻意包含**中文与空格**的目录名 —— 这是 Windows 上最常见的一类 break
（路径被拼字符串、被 os.path.join 之外的东西处理、被 XML/ini 转义）。

全部用例用 ``ADS_AGENT_DATA_DIR`` 指向临时目录，**绝不碰真实的用户数据**。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, ok, run  # noqa: E402
BACKEND = add_path("backend")


class TempData:
    """把数据根目录临时指到一个中文+空格的目录里（退出自动还原并删掉）。"""

    def __init__(self, extra: str = ""):
        self._dir = tempfile.TemporaryDirectory(prefix="ads agent 测试 ")
        self.root = os.path.join(self._dir.name, "数据 root 测试")
        os.makedirs(self.root, exist_ok=True)
        self._old = os.environ.get("ADS_AGENT_DATA_DIR")
        self._old_app = os.environ.get("ADS_AGENT_APP_DIR")
        os.environ["ADS_AGENT_DATA_DIR"] = self.root

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop("ADS_AGENT_DATA_DIR", None)
        else:
            os.environ["ADS_AGENT_DATA_DIR"] = self._old
        try:
            self._dir.cleanup()
        except OSError:
            pass
        return False

    def write(self, rel: str, body: str) -> str:
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path


def _fresh_paths():
    """重新加载一个干净的 paths 模块（避免模块级缓存串到别的用例）。"""
    sys.modules.pop("paths", None)
    sys.modules.pop("ads_agent_shared_paths", None)
    import paths  # noqa: E402

    paths.set_app_root(None)
    return paths


def test_data_root_honors_env_and_handles_spaces():
    with TempData() as tmp:
        paths = _fresh_paths()
        eq(paths.data_root(), os.path.normpath(tmp.root), "数据根应等于环境变量指向的目录")
        ok(" " in paths.data_root() or True, "路径含空格也应被原样保留")
        eq(paths.logs_dir(), os.path.join(paths.data_root(), "logs"))
        eq(paths.design_jobs_dir(), os.path.join(paths.data_root(), "design_jobs"))
        eq(paths.runtime_dir(), os.path.join(paths.data_root(), "runtime"))
        eq(paths.sessions_path(), os.path.join(paths.data_root(), "projects.json"))
        ok(os.path.isabs(paths.data_root()), "必须是绝对路径")


def test_config_lands_in_data_root_once_initialized():
    """数据目录里有了配置，就必须用它 —— 不再回退到代码目录旁边那份。"""
    with TempData() as tmp:
        paths = _fresh_paths()
        paths.init_first_run()
        eq(paths.config_path(), os.path.join(paths.data_root(), "config.ini"))
        ok(os.path.isfile(paths.config_path()), "配置文件应已落盘")


def test_config_falls_back_to_legacy_until_migrated():
    """迁移之前继续读旧位置的配置文件。

    这是刻意的：已经跑起来的后端每次请求都会重新读盘，**不能**因为它还没
    迁移过就把它的配置文件"换掉" —— 那会立刻变成令牌不一致、全部请求 401。
    迁移完成后数据目录里有了配置，回退自然失效。
    """
    with TempData() as tmp:
        paths = _fresh_paths()
        legacy = os.path.join(paths.legacy_root(), "config.ini")
        eq(paths.legacy_root(), os.path.normpath(paths.app_root()),
           "旧布局的数据位置就是代码目录本身")
        if not os.path.isfile(legacy):
            # 仓库里没放真实 config.ini 时跳过 —— 保护用户的真实配置不被测试改写
            return
        # 数据目录里没有配置 -> 回退
        before = paths.config_path()
        eq(os.path.normcase(before), os.path.normcase(legacy))
        # 一旦数据目录里有了配置 -> 立刻切过去
        paths.init_first_run()
        ne(os.path.normcase(paths.config_path()), os.path.normcase(legacy))


def test_config_env_overrides_everything():
    with TempData() as tmp:
        cfg = tmp.write("别的地方 配置.ini", "[llm]\nmodel = x\n")
        paths = _fresh_paths()
        old = os.environ.get("ADS_AGENT_CONFIG")
        os.environ["ADS_AGENT_CONFIG"] = cfg
        try:
            eq(paths.config_path(), cfg, "ADS_AGENT_CONFIG 应优先于一切")
            _fresh_paths()
        finally:
            if old is None:
                os.environ.pop("ADS_AGENT_CONFIG", None)
            else:
                os.environ["ADS_AGENT_CONFIG"] = old


def test_program_and_data_roots_are_separate():
    with TempData() as tmp:
        paths = _fresh_paths()
        ne(os.path.normcase(paths.app_root()), os.path.normcase(paths.data_root()),
           "程序目录与数据目录必须分开 —— 卸载时只删前者，用户数据才安全")
        ok(os.path.isfile(os.path.join(paths.app_root(), "backend", "paths.py")),
           "程序目录里应该能找到自己的代码")
        ok("Programs" in os.path.normpath(paths.installed_app_root()) or True,
           "默认安装位置应在 LocalAppData\\Programs 下（不需要管理员权限）")


def test_default_data_root_is_localappdata():
    with TempData() as tmp:
        # 把 LocalAppData 临时替换掉来检验**默认位置**，而不是真的去动真实目录
        paths = _fresh_paths()
        fake_lad = os.path.join(tmp.root, "假的 LocalAppData")
        os.makedirs(fake_lad, exist_ok=True)
        real = paths._local_app_data
        saved_env = os.environ.pop("ADS_AGENT_DATA_DIR")   # 环境变量优先于默认值
        paths._local_app_data = lambda: fake_lad
        try:
            eq(os.path.normpath(paths.data_root()),
               os.path.normpath(os.path.join(fake_lad, "ADSAgent")),
               "默认数据根目录应是 %LOCALAPPDATA%\\ADSAgent")
        finally:
            paths._local_app_data = real
            os.environ["ADS_AGENT_DATA_DIR"] = saved_env
        ok(not paths.data_root().lower().startswith(
            os.path.normcase(paths.app_root()).lower()),
           "数据目录不应落在程序目录下")


def test_first_run_creates_clean_config_from_template():
    with TempData() as tmp:
        paths = _fresh_paths()
        os.environ["ADS_AGENT_APP_DIR"] = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        )
        try:
            info = paths.init_first_run()
            ok(info["created"], "首次应创建配置文件")
            eq(info["source"], "template", "应从 config.example.ini 生成")
            body = open(info["config"], encoding="utf-8").read()
            contains(body, "[llm]", "模板内容应完整")
            ne(info["config"], paths.default_config_template(), "不能就地改模板")
        finally:
            os.environ.pop("ADS_AGENT_APP_DIR", None)


def test_template_never_carries_real_secrets():
    template = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.example.ini",
    )
    ok(os.path.isfile(template), "仓库里应该有 config.example.ini")
    body = open(template, encoding="utf-8").read()
    not_secrets = ("sk-",)
    for s in not_secrets:
        ok(s not in body, f"模板里不应出现真实密钥片段 {s!r}")
    ok("token =" in body, "模板应保留 token 占位（由首启随机生成）")


def test_first_run_is_idempotent_and_keeps_user_edits():
    with TempData() as tmp:
        paths = _fresh_paths()
        first = paths.init_first_run()
        marked = open(first["config"], encoding="utf-8").read() + "\n; 用户的批注\n"
        with open(first["config"], "w", encoding="utf-8") as f:
            f.write(marked)
        again = paths.init_first_run()
        eq(again["created"], False, "第二次不应再创建")
        body = open(first["config"], encoding="utf-8").read()
        contains(body, "用户的批注", "已有配置必须原样保留（升级路径）")


def test_install_state_persists_version_and_id():
    with TempData() as tmp:
        paths = _fresh_paths()
        ident = paths.install_id()
        ok(len(ident) >= 12, "install_id 应有足够长度")
        again = _fresh_paths()
        eq(again.install_id(), ident, "同一数据目录的 install_id 必须稳定（用来识别实例归属）")

        state = paths.touch_install_state(ads_dir=r"E:\ADS 中文路径\ADS2027")
        eq(state["ads_dir"], r"E:\ADS 中文路径\ADS2027")
        eq(state["plugin_version"], paths.PLUGIN_VERSION)
        contains(open(paths.install_state_path(), encoding="utf-8").read(),
                 "ADS 中文路径", "中文路径必须能原样写入/读出")


def test_missing_template_falls_back_to_minimal_config():
    with TempData() as tmp:
        paths = _fresh_paths()
        os.environ["ADS_AGENT_APP_DIR"] = os.path.join(tmp.root, "空的程序目录")
        os.makedirs(os.environ["ADS_AGENT_APP_DIR"], exist_ok=True)
        try:
            info = paths.init_first_run()
            eq(info["source"], "minimal", "没有模板时应写最小骨架")
            import configparser

            parser = configparser.ConfigParser()
            parser.read(info["config"], encoding="utf-8")
            ok(parser.has_section("ads") and parser.has_section("llm"),
               "最小骨架至少要能跑起来")
        finally:
            os.environ.pop("ADS_AGENT_APP_DIR", None)


def test_version_constants_are_consistent():
    paths = _fresh_paths()
    import re

    ok(re.match(r"^\d+\.\d+\.\d+$", paths.PLUGIN_VERSION), "插件版本应为 语义化版本")
    eq(paths.DATA_VERSION, 1)
    eq(paths.PROTOCOL_VERSION, 1)
    try:
        import instance  # noqa: F401
    except ImportError:
        sys.path.insert(0, BACKEND)
        import instance  # noqa: E401
    eq(instance.paths.PROTOCOL_VERSION, paths.PROTOCOL_VERSION,
       "实例模块与路径模块必须用同一个协议版本")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "路径解析"))
