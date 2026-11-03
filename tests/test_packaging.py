"""打包：白名单、不夹带密钥、不含本机路径，以及**解出来的包能独立运行**。

最后一条最关键：前面几条都是在检查"仓库"，而用户拿到的是 ZIP。把包解开、
换到另一个目录、用干净的数据目录跑一遍 ``--status``，才算真的能发。
"""

import os
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run  # noqa: E402

ROOT = add_path()
add_path("backend")
sys.path.insert(0, os.path.join(ROOT, "tools"))

import release_manifest as manifest  # noqa: E402
import build_release  # noqa: E402
import paths  # noqa: E402


def test_manifest_covers_all_runtime_files():
    files, missing, violations = manifest.collect(ROOT)
    eq(missing, [], "白名单登记的文件必须都在磁盘上")
    eq(violations, [], "白名单不得命中排除规则")
    ok(len(files) >= 30, f"运行文件应有三十个以上，实际 {len(files)}")
    rels = [r.replace("\\", "/") for r, _ in files]
    for required in ("addon/ads_agent/__init__.py", "backend/server.py",
                     "backend/paths.py", "backend/instance.py",
                     "backend/adslocate.py", "config.example.ini",
                     "install_addon.py", "release_manifest.py", "check_env.py"):
        ok(required in rels, f"必须打包 {required}")


def test_no_user_data_in_manifest():
    files, _m, _v = manifest.collect(ROOT)
    rels = [r.replace("\\", "/") for r, _ in files]
    for forbidden in ("config.ini", "projects.json", "projects.json.bak",
                      "design_jobs/", "logs/", "tests/", "tools/", ".git"):
        ok(not any(forbidden in r for r in rels),
           f"白名单里不该出现 {forbidden}")


def test_no_secrets_in_manifest():
    eq(build_release.check_secrets(manifest.collect(ROOT)[0]), [],
       "打包文件里不得出现真实密钥")


def test_no_hardcoded_machine_paths():
    eq(build_release.check_hardcoded_paths(manifest.collect(ROOT)[0]), [],
       "安装目录必须自动探测，不能有硬编码的本机路径")


def test_exclusion_rules_cover_the_usual_suspects():
    for name in ("config.ini", "projects.json", "logs", "design_jobs",
                 "tests", "__pycache__", ".git", ".workbuddy"):
        ok(manifest.is_excluded(name), f"{name} 必须被排除规则命中")
    for name in ("server.py", "config.example.ini", "install_addon.py"):
        ok(not manifest.is_excluded(name), f"{name} 不能被排除")


def test_scan_for_leaks_finds_real_config():
    """使用临时夹具，干净 checkout 不需要开发者的真实配置和会话。"""
    with tempfile.TemporaryDirectory() as fixture:
        for name, content in (("config.ini", "[llm]\napi_key = fake-test-key\n"),
                              ("projects.json", "{}")):
            with open(os.path.join(fixture, name), "w", encoding="utf-8") as f:
                f.write(content)
        leaks = manifest.scan_for_leaks(fixture)
        ok("config.ini" in leaks, "应检测出临时配置文件")
        ok("projects.json" in leaks, "应检测出临时会话文件")


def test_manifest_covers_every_addon_module_that_is_imported():
    """白名单必须覆盖 addon/ads_agent 下**所有被 import 的本地模块**。

    回归 ADSAgent v1.3.0：ADDON_FILES 漏登记 geometry_policy / schematic_rules，
    而 ads_ops 在模块顶层 import 它们 → 全新安装后 ads_ops 整个导入失败，
    DISPATCH 不存在，21 个工具全部挂掉。清单是分发链路的唯一白名单，
    漏一个模块的代价是"装了不能用"，必须在打包前就拦住。

    判定用静态 AST 求入口可达闭包，不 import 这些模块本身
    （它们要求 ADS 运行时，测试机上导不进来）。
    """
    import ast

    addon_dir = os.path.join(ROOT, "addon", "ads_agent")
    local = {fn[:-3] for fn in os.listdir(addon_dir)
             if fn.endswith(".py") and not fn.startswith("__")}
    local.add("__init__")

    edges = {}
    for name in sorted(local):
        path = os.path.join(addon_dir, name + ".py")
        with open(path, encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read(), filename=path)
        deps = set()
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for nm in names:
                if nm in local:
                    deps.add(nm)
        edges[name] = deps

    # 从真正的入口出发：__init__（ADS 加载）、panel（界面）、
    # toolserver（工具服务）、ads_ops（工具实现）
    seen, stack = set(), [r for r in ("__init__", "panel", "toolserver", "ads_ops")
                          if r in local]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(edges.get(cur, ()))

    registered = {fn[:-3] for fn in manifest.ADDON_FILES if fn.endswith(".py")}

    missing = sorted(seen - registered)
    eq(missing, [],
       "入口可达但白名单漏登记的模块（全新安装会 ImportError）："
       + ", ".join(m + ".py" for m in missing))

    ghosts = sorted(registered - local)
    eq(ghosts, [],
       "白名单登记了磁盘上不存在的模块：" + ", ".join(g + ".py" for g in ghosts))

    # 顺带：未可达的模块（可选依赖/离线工具）也必须登记，
    # 否则它们在包里缺失属于"悄悄降级"，同样不该发生。
    orphans = sorted(local - registered)
    eq(orphans, [],
       "白名单漏登记这些模块：" + ", ".join(o + ".py" for o in orphans))


def test_install_selfcheck_catches_incomplete_deploy():
    """安装后自检必须能抓到"模块缺失"和"模块未登记"两种不完整。

    v1.3.0 的故障是文件按清单复制成功、但清单本身漏登记，
    所以自检必须**以清单为权威**去比对磁盘，而不是只数磁盘上有几个文件。
    """
    import install_addon
    import shutil

    # 正常情况：当前仓库应当自检通过
    eq(install_addon.verify_deployed(ROOT, []), [], "当前仓库应通过安装自检")

    with tempfile.TemporaryDirectory() as tmp:
        addon = os.path.join(tmp, "addon", "ads_agent")
        shutil.copytree(os.path.join(ROOT, "addon", "ads_agent"), addon)

        # 情形一：清单登记了但磁盘上没有（= 复制不全/清单漏打包上游）
        os.remove(os.path.join(addon, "geometry_policy.py"))
        problems = install_addon.verify_deployed(tmp, [])
        ok(any("geometry_policy" in p for p in problems),
           f"缺少 geometry_policy.py 应被自检抓到：{problems}")

        # 情形二：模块在磁盘上，但清单没登记（= 改了代码忘了登记）
        # 用一份去掉 geometry_policy 的清单模拟
        real = list(manifest.ADDON_FILES)
        try:
            manifest.ADDON_FILES = [n for n in real if n != "schematic_rules.py"]
            with open(os.path.join(addon, "geometry_policy.py"), "w",
                      encoding="utf-8") as f:
                f.write("# restored\n")
            problems = install_addon.verify_deployed(tmp, [])
            ok(any("schematic_rules" in p for p in problems),
               f"清单漏登记但仍在磁盘上的模块应被抓到：{problems}")
        finally:
            manifest.ADDON_FILES = real


def test_build_produces_clean_zip():
    with tempfile.TemporaryDirectory() as out:
        code = build_release.main(["--out", out])
        eq(code, 0, "打包应当成功")
        zips = [n for n in os.listdir(out) if n.endswith(".zip")]
        eq(len(zips), 1, "应产出一个 ZIP")
        name = zips[0]
        contains(name, paths.PLUGIN_VERSION, "包名必须带版本号")
        with zipfile.ZipFile(os.path.join(out, name)) as zf:
            names = zf.namelist()
        bad = [n for n in names
               if n.endswith((".pyc", ".log"))
               or "/__pycache__/" in n
               or n.split("/")[-1] in {"config.ini", "projects.json"}]
        eq(bad, [], f"ZIP 里不该有缓存/日志/用户数据：{bad}")


def test_built_zip_runs_standalone():
    """解开 ZIP → 换目录 → 用干净的数据目录跑 --status。

    这是"包能不能发出去"的最后一关：验证的不是仓库，是产物本身。
    """
    with tempfile.TemporaryDirectory() as out:
        build_release.main(["--out", out])
        name = next(n for n in os.listdir(out) if n.endswith(".zip"))
        extract = os.path.join(out, "解开的位置 带空格")
        with zipfile.ZipFile(os.path.join(out, name)) as zf:
            zf.extractall(extract)

        root = os.path.join(extract, f"ADSAgent-{paths.PLUGIN_VERSION}")

        # 造一个假 ADS 目录，避免动真实注册文件
        fake_ads = os.path.join(out, "ADS 安装目录")
        os.makedirs(os.path.join(fake_ads, "config"))
        os.makedirs(os.path.join(fake_ads, "bin"))
        os.makedirs(os.path.join(fake_ads, "tools", "python"))
        with open(os.path.join(fake_ads, "config", "eesof_addons.xml"), "w",
                  encoding="utf-8") as f:
            f.write('<?xml version="1.0" ?>\n<EESof_Addons>\n'
                    '    <Addon Name="SnP Utilities" FilePath="$HPEESOF_DIR\\x" Enabled="1" />\n'
                    '</EESof_Addons>\n')

        data = os.path.join(out, "数据 目录")
        env = dict(os.environ,
                   ADS_AGENT_DATA_DIR=data,
                   PYTHONIOENCODING="utf-8")
        env.pop("ADS_AGENT_CONFIG", None)

        proc = subprocess.run(
            [sys.executable, os.path.join(root, "install_addon.py"),
             "--ads-dir", fake_ads, "--mode", "inplace"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=120,
        )
        eq(proc.returncode, 0, "解出来的包应当能安装成功："
           + (proc.stdout or "") + (proc.stderr or ""))
        joined = (proc.stdout or "")
        contains(joined, "ADS Agent", "安装输出应提到插件名")
        contains(joined, "重启 ADS", "应提示重启 ADS")

        # 注册确实写进了假 ADS 的那份配置
        body = open(os.path.join(fake_ads, "config", "eesof_addons.xml"),
                    encoding="utf-8").read()
        contains(body, "ADS Agent")
        contains(body, "SnP Utilities", "官方插件条目必须还在")

        # 数据目录被建出来并生成了配置
        ok(os.path.isdir(os.path.join(data, "logs")), "应建出日志目录")
        ok(os.path.isfile(os.path.join(data, "config.ini")), "首启应生成配置")
        cfg = open(os.path.join(data, "config.ini"), encoding="utf-8").read()
        ok("sk-" not in cfg, "生成的配置里绝不能有真实密钥")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "打包"))
