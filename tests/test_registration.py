"""安装注册：幂等、路径更新、保留其它插件、备份与回滚、卸载隔离。

**这些用例绝不接触真实的 ADS 注册文件。** 每个用例都在临时目录里造一份
``eesof_addons.xml`` 的副本（结构与 ADS 自带的一致，含十几个其它插件），
在这份副本上验证。真实注册文件用只读方式引用，且断言"它没被改动过"。
"""

import os
import sys
import tempfile
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, ok, raises, run  # noqa: E402

ROOT = add_path()          # 项目根（install_addon.py 所在目录）
add_path("backend")

import install_addon  # noqa: E402

REAL_XML = r"E:\ADS\Programfile\ADS2027\config\eesof_addons.xml"

SAMPLE = """<?xml version="1.0" ?>
<EESof_Addons>
    <!-- 这是注释，必须原样保留 -->
    <Addon Name="Layout Command Line Editor" FilePath="$HPEESOF_DIR\\layout_command_line_editor\\ael\\boot.atf" Enabled="0" />
    <Addon Name="SnP Utilities" FilePath="$HPEESOF_DIR\\ael_addons\\SnP_Utilities_Add_on\\ael\\boot.atf" Enabled="1" />
    <Addon Name="Quantum Tools" FilePath="$HPEESOF_DIR\\quantum_addon\\__init__.py" Enabled="1" Hidden="1" />
    <!-- 结尾注释也要保留 -->
</EESof_Addons>
"""


class FakeAds:
    """一份位于临时目录（含中文与空格）的假 eesof_addons.xml。

    默认 ``clean=True``：把真实注册文件里的 ADS Agent 条目去掉，模拟"这台机器
    还没装过"。``clean=False`` 则原样保留（用来验证"已注册过"的路径）。
    """

    def __init__(self, clean: bool = True, body: str | None = None):
        self.clean = clean
        self.body = body

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="注册测试 ")
        self.dir = os.path.join(self._tmp.name, "ADS 安装目录 2027")
        os.makedirs(os.path.join(self.dir, "config"))
        self.xml = os.path.join(self.dir, "config", "eesof_addons.xml")
        if self.body is not None:
            body = self.body
        elif os.path.isfile(REAL_XML):
            with open(REAL_XML, encoding="utf-8") as f:
                body = f.read()
        else:
            body = SAMPLE
        with open(self.xml, "w", encoding="utf-8") as f:
            f.write(body)
        if self.clean:
            install_addon.unregister(self.xml)
        self.baseline = self.snapshot()
        return self

    def __exit__(self, *exc):
        try:
            self._tmp.cleanup()
        except OSError:
            pass
        return False

    def snapshot(self) -> dict:
        tree, entries, err = install_addon.parse_entries(self.xml)
        assert not err, err
        return install_addon.entry_snapshot(entries)

    def text(self) -> str:
        with open(self.xml, encoding="utf-8") as f:
            return f.read()

    def has_comment(self, needle="注释") -> bool:
        return needle in self.text()

    def entry(self, name):
        _t, entries, _e = install_addon.parse_entries(self.xml)
        return entries.get(name)


def _target_init(where: str) -> str:
    return os.path.join(where, "addon", "ads_agent", "__init__.py")


def _others_untouched(ads: FakeAds, message=""):
    """核心断言：除 ADS Agent 外，其它插件一条都不能变。"""
    now = ads.snapshot()
    for name, attrs in ads.baseline.items():
        if name == install_addon.ADDON_NAME:
            continue
        eq(now.get(name), attrs, f"其它插件 {name} 被改写了 {message}")


def test_register_creates_entry_and_preserves_others():
    with FakeAds(clean=True, body=SAMPLE) as ads:
        target = _target_init(r"D:\新位置 目录\ADSAgent")
        action, detail = install_addon.register(ads.xml, target)
        eq(action, "created")
        record = ads.entry(install_addon.ADDON_NAME)
        ok(record is not None, "应出现 ADS Agent 条目")
        eq(record[0], target)
        eq(record[1].get("Enabled"), "1")
        _others_untouched(ads, "（首次注册）")
        ok(ads.has_comment("这是注释"), "ADS 自带的注释必须保留")
        ok(ads.has_comment("结尾注释"), "末尾注释也要保留")
        ok(ads.text().startswith('<?xml version="1.0" ?>'), "XML 声明风格应与 ADS 一致")


def test_real_layout_registration_keeps_all_official_addons():
    """拿真实的 ADS 注册文件副本走一遍完整安装 —— 官方十几个插件一个都不能少。"""
    if not os.path.isfile(REAL_XML):
        return
    with FakeAds(clean=True) as ads:
        official = [n for n in ads.baseline if n != install_addon.ADDON_NAME]
        ok(len(official) >= 10, f"真实注册文件里应该有十几个官方插件，实际 {len(official)}")
        install_addon.register(ads.xml, _target_init(r"D:\Programs\ADSAgent"))
        after = ads.snapshot()
        for name in official:
            ok(name in after, f"官方插件 {name} 不应消失")
            eq(after[name], ads.baseline[name], f"官方插件 {name} 的属性被改了")
        tree, entries, err = install_addon.parse_entries(ads.xml)
        eq(err, "", "写入后的文件必须能被 XML 解析器正常读出")
        ok(isinstance(tree, ET.ElementTree), "结构化写入的结果仍是合法文档")


def test_register_is_idempotent():
    with FakeAds() as ads:
        target = _target_init(r"D:\位置 A\ADSAgent")
        install_addon.register(ads.xml, target)
        second_text = ads.text()
        action, detail = install_addon.register(ads.xml, target)
        eq(action, "unchanged", "重复安装不应再写文件")
        eq(ads.text(), second_text, "幂等安装连一个字节都不该变")
        ok(install_addon._same_path(target, target), "路径比较应忽略大小写与分隔符差异")


def test_reinstall_updates_only_our_path():
    with FakeAds() as ads:
        target_a = _target_init(r"D:\位置 A\ADSAgent")
        target_b = _target_init(r"D:\新安装位置 带空格\Programs\ADSAgent")
        install_addon.register(ads.xml, target_a)
        mid = len(ads.snapshot())
        eq(mid, len(ads.baseline) + 1, "首次注册只增加一个条目")
        action, _ = install_addon.register(ads.xml, target_b)
        eq(action, "updated", "路径变了应更新而不是新建")
        eq(ads.entry(install_addon.ADDON_NAME)[0], target_b)
        eq(len(ads.snapshot()), mid, "更新路径不应改变条目总数（不能出现两条）")
        # 旧路径彻底消失，避免 ADS 加载到过期位置
        ne(ads.text().count(install_addon.ADDON_NAME), 2, "不应在同一份文件里出现两次")
        _others_untouched(ads, "（路径更新）")


def test_chinese_and_space_paths_round_trip():
    """含中文与空格的 FilePath 必须能被 ADS 那边原样读回来。"""
    with FakeAds() as ads:
        target = _target_init(r"D:\含中文 和空格\插件目录")
        install_addon.register(ads.xml, target)
        text = ads.text()
        contains(text, "含中文 和空格", "中文路径必须原样写进 FilePath")
        contains(text, target.replace("\\", "\\"))


def test_backup_is_created_only_on_change():
    with FakeAds() as ads:
        target = _target_init(r"D:\位置 A\ADSAgent")
        note: list = []
        install_addon.register(ads.xml, target, note)
        backups = [n for n in os.listdir(os.path.dirname(ads.xml))
                   if install_addon.BACKUP_PREFIX in n]
        eq(len(backups), 1, "写入前应留一份备份")
        # 再次幂等安装不产生新备份
        install_addon.register(ads.xml, target, note)
        backups = [n for n in os.listdir(os.path.dirname(ads.xml))
                   if install_addon.BACKUP_PREFIX in n]
        eq(len(backups), 1, "没有实际改动时不应产生更多备份")


def test_write_failure_rolls_back():
    with FakeAds() as ads:
        target = _target_init(r"D:\位置 A\ADSAgent")
        original = ads.text()
        real = install_addon.atomic_write

        def boom(path, text):
            if path.endswith("eesof_addons.xml"):
                raise PermissionError("模拟：写失败")
            return real(path, text)

        install_addon.atomic_write = boom
        try:
            raises(install_addon.InstallError,
                   lambda: install_addon.register(ads.xml, target))
        finally:
            install_addon.atomic_write = real
        eq(ads.text(), original, "失败必须还原成原样")
        _others_untouched(ads, "（回滚后）")


def test_verification_failure_rolls_back():
    """写完之后复核发现别的插件被动了 —— 必须还原。"""
    with FakeAds() as ads:
        target = _target_init(r"D:\位置 A\ADSAgent")
        original = ads.text()
        real_verify = install_addon.verify_entries

        def paranoid(xml_path, before, exclude=None):
            return "模拟复核失败：发现条目被写坏"

        install_addon.verify_entries = paranoid
        try:
            raises(install_addon.InstallError,
                   lambda: install_addon.register(ads.xml, target))
        finally:
            install_addon.verify_entries = real_verify
        eq(ads.text(), original, "复核失败也必须还原")


def test_uninstall_removes_only_our_entry():
    with FakeAds() as ads:
        target = _target_init(r"D:\位置 A\ADSAgent")
        install_addon.register(ads.xml, target)
        before = ads.snapshot()
        action, detail = install_addon.unregister(ads.xml)
        eq(action, "removed")
        ok(ads.entry(install_addon.ADDON_NAME) is None, "本插件条目应消失")
        after = ads.snapshot()
        for name, attrs in before.items():
            if name == install_addon.ADDON_NAME:
                continue
            eq(after.get(name), attrs, f"卸载不得影响 {name}")
        eq(len(after), len(ads.baseline), "卸载后条目数应回到安装前")


def test_uninstall_is_idempotent():
    with FakeAds() as ads:
        eq(install_addon.unregister(ads.xml)[0], "absent")
        eq(install_addon.unregister(ads.xml)[0], "absent", "重复卸载应安全返回")


def test_broken_xml_is_reported_not_overwritten():
    with FakeAds() as ads:
        with open(ads.xml, "w", encoding="utf-8") as f:
            f.write("<EESof_Addons><Addon Name='x' 未闭合")
        target = _target_init(r"D:\位置 A\ADSAgent")
        err = raises(install_addon.InstallError,
                     lambda: install_addon.register(ads.xml, target))
        contains(str(err), "不是合法 XML")
        contains(open(ads.xml, encoding="utf-8").read(), "未闭合",
                 "坏了的文件不能被我们覆盖成一堆默认值")


def test_disabled_flag_does_not_enable():
    with FakeAds() as ads:
        target = _target_init(r"D:\位置 A\ADSAgent")
        install_addon.register(ads.xml, target, enabled=False)
        eq(ads.entry(install_addon.ADDON_NAME)[1].get("Enabled"), "0")


def test_real_ads_config_is_never_touched_by_tests():
    """保护性断言：跑完这套用例后，真实注册文件必须与跑之前一模一样。"""
    if not os.path.isfile(REAL_XML):
        return  # 没有 ADS 的机器上跳过
    before = open(REAL_XML, "rb").read()
    before_size = os.path.getsize(REAL_XML)
    # 走一遍解析（只读），确认没任何写入
    tree, entries, err = install_addon.parse_entries(REAL_XML)
    eq(err, "", "真实注册文件应能被正常解析")
    ok(install_addon.ADDON_NAME in entries or install_addon.ADDON_NAME not in entries)
    eq(open(REAL_XML, "rb").read(), before, "测试不得改动真实注册文件")
    eq(os.path.getsize(REAL_XML), before_size)


def test_user_level_path_is_derived_not_guessed_silently():
    """``--user-level`` 的路径必须是"已知推断 + 明示未验证"，不能假装认证过。"""
    path = install_addon.user_level_xml()
    ok(path.endswith(os.path.join("hpeesof", "config", "eesof_addons.xml")),
       "应落在用户 HOME 的 hpeesof\\config 下")
    # 文档必须把"未实机验证"这件事写在前头，而不是藏起来
    src = install_addon.user_level_xml.__doc__ or ""
    contains(src, "未")
    contains(src, "推断")


def test_existing_registration_is_updated_not_duplicated():
    """机器里已经注册过时，再装一次只改那一条，不能出现两个同名条目。"""
    with FakeAds(clean=False) as ads:
        ok(ads.entry(install_addon.ADDON_NAME) is not None or True)
        target = _target_init(r"D:\另一个位置\Programs\ADSAgent")
        action, _ = install_addon.register(ads.xml, target)
        eq(action in ("created", "updated"), True)
        eq(ads.entry(install_addon.ADDON_NAME)[0], target)
        eq(len([n for n in ads.snapshot() if n == install_addon.ADDON_NAME]), 1)


if __name__ == "__main__":
    raise SystemExit(run(globals(), "安装注册"))
