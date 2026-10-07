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
