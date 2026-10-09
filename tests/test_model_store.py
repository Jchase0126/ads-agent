"""模型压缩包资产层测试（纯逻辑，不需要 ADS / PySide6 / 网络）。

覆盖任务要求的这些风险点：
  * ZIP 保存、重复上传、同名不同内容；
  * 中文路径、空格路径、嵌套包结构；
  * Touchstone / Design Kit / 混合包识别；
  * 路径越界、损坏 ZIP、大小与压缩比限制；
  * 并发导入、失败恢复；
  * 工作区切换、工程迁移后的引用重新定位；
  * 模型变化导致缓存失效（指纹部分）；
  * 状态机：解压结束**不能**直接标就绪。

这些断言写的是"必须成立的行为"，不是"某个实现细节" —— 例如识别函数
叫什么不重要，重要的是"含 lib.defs + .s2p 的包必须判成 Design Kit 且
给出证据"，以及"提不到厂商时 vendor 必须是 None 而不是猜一个"。
"""

import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, ok, raises, run  # noqa: E402

add_path("backend")
import model_store  # noqa: E402


# ---------------------------------------------------------------------------
# 造样本
# ---------------------------------------------------------------------------

def make_zip(path, entries, *, dirs=()):
    """entries: {归档内路径: bytes 或 str}；dirs: 显式目录条目。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for d in dirs:
            z.writestr(d if d.endswith("/") else d + "/", b"")
        for name, payload in entries.items():
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            z.writestr(name, payload)
    return path


TOUCHSTONE_HEADER = """! 2-port S-parameter
# GHz S RI R 50
!freq  S11 S21 S12 S22
"""


def sample_touchstone(path, name="vendor_models.zip"):
    return make_zip(path, {
        f"{name[:-4]}/part_a.s2p": TOUCHSTONE_HEADER + "1.0 0.5 0.9 0.9 0.5\n"
                                       "2.0 0.4 0.8 0.8 0.4\n",
        f"{name[:-4]}/part_b.s2p": TOUCHSTONE_HEADER + "1.0 0.6 0.7 0.7 0.6\n",
        f"{name[:-4]}/README.md": "# models\n使用说明：把文件喂给 S 参数元件。\n",
    })


def sample_design_kit(path, root="VendorKit_v1.2"):
    return make_zip(path, {
        f"{root}/lib.defs": (
            "# Library Defs\n"
            "DEFINE VendorKit ./VendorKit\n"
            "ASSIGN VendorKit libMode readOnly\n"
        ),
        f"{root}/VendorKit/.oalib": '<?xml version="1.0"?><Library/>',
        f"{root}/circuit/ael/VENDOR_PART1.atf": "atf content",
        f"{root}/circuit/ael/VENDOR_PART2.atf": "atf content",
        f"{root}/readme.txt": "Vendor Component Library\nVersion 1.2\nCopyright ACME\n",
    })


def sample_mixed(path):
    """既含 lib.defs 又含大量 s2p —— 应判为 Design Kit（内含 Touchstone 模型）。"""
    return make_zip(path, {
        "Infineon_RF/lib.defs": "DEFINE Infineon_RF ./Infineon_RF\n"
                                 "ASSIGN Infineon_RF libMode readOnly\n",
        "Infineon_RF/Infineon_RF/.oalib": "<Library/>",
        "Infineon_RF/circuit/ael/BFP181.ael": "ael",
        "Infineon_RF/s2p/BFP181.s2p": TOUCHSTONE_HEADER + "1.0 0.5 0.9 0.9 0.5\n",
        "Infineon_RF/readme.txt": "Version 2.1\nDate 2016/04/06\n",
    })


def sample_nested(path):
    """两层同名嵌套根目录（真实 TDK v56 就是这个形态）。"""
    return make_zip(path, {
        "TDK_Component_Library_v56/TDK_Component_Library_v56/lib.defs":
            "DEFINE TDK ./TDK\n",
        "TDK_Component_Library_v56/TDK_Component_Library_v56/TDK/circuit/ael/"
        "TDK_ACT45B.atf": "atf",
    })


# ---------------------------------------------------------------------------
# 保存 / 去重 / 同名不同内容
# ---------------------------------------------------------------------------

def test_save_archive_and_manifest():
    with tempfile.TemporaryDirectory() as ws:
        src = sample_touchstone(os.path.join(ws, "src", "models.zip"))
        rec = model_store.save_archive(ws, "models.zip", source_path=src,
                                       source_session="proj1")
        ok(rec.get("package_id", "").startswith("pkg_"),
           f"package_id 应形如 pkg_<hash>，实际 {rec.get('package_id')!r}")
        eq(len(rec["sha256"]), 64, "应记录完整 SHA-256")
        ok(rec.get("size_bytes", 0) > 0, "应记录文件大小")
        eq(rec.get("source_session"), "proj1", "应记录来源会话")

        root = model_store.store_root(ws)
        # 原始 ZIP 必须落在 archives/<package_id>/ 下，与解压目录分开
        archive = os.path.join(ws, rec["archive_relpath"])
        ok(os.path.isfile(archive), f"原始 ZIP 应存在于 {rec['archive_relpath']}")
        ok("archives" in rec["archive_relpath"], "原始 ZIP 应在 archives/ 下")
        ok(rec.get("extract_relpath") in (None, ""),
           "未解压时不应有解压路径")

        # 状态必须是"已保存"，绝不是"已就绪"
        eq(rec.get("state"), model_store.STATE_SAVED,
           "上传后只能标已保存 —— 上传不代表模型可用")
        # 厂商未知时必须为 None，不能按文件名编造
        ok(rec.get("vendor") in (None, ""),
           f"无法可靠识别的厂商必须是未知，实际 {rec.get('vendor')!r}")


def test_duplicate_upload_reuses_asset():
    with tempfile.TemporaryDirectory() as ws:
        src = sample_touchstone(os.path.join(ws, "src", "models.zip"))
        a = model_store.save_archive(ws, "models.zip", source_path=src)
        b = model_store.save_archive(ws, "models.zip", source_path=src,
                                     source_session="proj2")
        eq(a["package_id"], b["package_id"], "相同内容应复用同一资产")
        ok(b.get("reused"), "重复上传应标记为复用")
        eq(b.get("ref_count"), 2, "引用计数应递增")
        root = model_store.store_root(ws)
        eq(len(model_store.list_packages(root)), 1, "不应产生第二条资产记录")


def test_same_name_different_content_kept_apart():
    with tempfile.TemporaryDirectory() as ws:
        a = sample_touchstone(os.path.join(ws, "src", "models.zip"))
        b = make_zip(os.path.join(ws, "src2", "models.zip"), {
            "other/part_c.s2p": TOUCHSTONE_HEADER + "1.0 0.1 0.2 0.2 0.1\n"})
        rec_a = model_store.save_archive(ws, "models.zip", source_path=a)
        rec_b = model_store.save_archive(ws, "models.zip", source_path=b)
        ne(rec_a["package_id"], rec_b["package_id"],
           "同名但内容不同必须是两个独立资产，不能静默覆盖")
        for rec in (rec_a, rec_b):
            ok(os.path.isfile(os.path.join(ws, rec["archive_relpath"])),
               "两份原始 ZIP 都必须保留")


def test_chinese_and_space_paths():
    with tempfile.TemporaryDirectory() as base:
        ws = os.path.join(base, "我的工程 space", "工作区")
        os.makedirs(ws)
        src = sample_design_kit(os.path.join(ws, "源文件 目录", "套件 包.zip"))
        rec = model_store.save_archive(ws, "套件 包.zip", source_path=src)
        root = model_store.store_root(ws)
        ok(os.path.isfile(os.path.join(ws, rec["archive_relpath"])),
           "中文+空格路径下原始 ZIP 应保存成功")
        rec2 = model_store.scan_archive(root, rec["package_id"])
        model_store.extract_package(root, rec["package_id"])
        rec3 = model_store.index_models(root, rec["package_id"])
        ok(rec3 is not None, "中文路径下解压与建索引都应可用")


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------

def test_identify_touchstone():
    with tempfile.TemporaryDirectory() as ws:
        rec = model_store.save_archive(
            ws, "models.zip",
            source_path=sample_touchstone(os.path.join(ws, "s", "m.zip")))
        root = model_store.store_root(ws)
        out = model_store.scan_archive(root, rec["package_id"])
        eq(out.get("package_kind"), "touchstone",
           "含 .s2p 的包应识别为 Touchstone")
        ok(out.get("kind_evidence"),
           "识别必须给出依据，不能只给结论")
        ok(out.get("kind_confidence"), "应给出置信度")


def test_identify_design_kit():
    with tempfile.TemporaryDirectory() as ws:
        rec = model_store.save_archive(
            ws, "kit.zip",
            source_path=sample_design_kit(os.path.join(ws, "s", "k.zip")))
        root = model_store.store_root(ws)
        out = model_store.scan_archive(root, rec["package_id"])
        eq(out.get("package_kind"), "design_kit",
           "含 lib.defs + 库结构的包应识别为 Design Kit")
        eq(out.get("version"), "1.2",
           "版本应从包内文档的真实文本提取，不靠猜")
        ok(out.get("version_evidence"),
           "版本提取要能指出依据来自哪个文件的哪一行")
        ok(out.get("vendor") in (None, ""),
           "厂商必须来自包里**可靠的**声明；一段 'ACME Corp' 说明文字"
           "不足以认定为供应商，宁可标未知也不编造")


def test_identify_mixed_prefers_design_kit():
    """含 lib.defs 又含大量 s2p 的包：判 Design Kit，但要说明内含 Touchstone。"""
    with tempfile.TemporaryDirectory() as ws:
        rec = model_store.save_archive(
            ws, "mixed.zip", source_path=sample_mixed(os.path.join(ws, "s", "x.zip")))
        root = model_store.store_root(ws)
        out = model_store.scan_archive(root, rec["package_id"])
        eq(out.get("package_kind"), "design_kit",
           "有 lib.defs 的包应判为 Design Kit（内含 Touchstone 模型）")
        blob = " ".join(out.get("kind_evidence") or [])
        contains(blob.lower(), "s2p",
                 "混合包的识别依据里要能看到 s2p 的存在，否则模型文件会被忽略")


def test_identify_nested_roots():
    with tempfile.TemporaryDirectory() as ws:
        rec = model_store.save_archive(
            ws, "v56.zip", source_path=sample_nested(os.path.join(ws, "s", "n.zip")))
        root = model_store.store_root(ws)
        out = model_store.scan_archive(root, rec["package_id"])
        eq(out.get("package_kind"), "design_kit",
           "两层同名嵌套根目录也要能识别出套件根")
        roots = (out.get("library_attach") or {}).get("kit_root_candidates") or []
        ok(roots, "应给出套件根目录（嵌套两层也要能定位到真正的套件根）")


def test_unknown_package_not_forced():
    with tempfile.TemporaryDirectory() as ws:
        path = make_zip(os.path.join(ws, "s", "other.zip"), {
            "docs/readme.txt": "some other simulator models",
            "data/table.dat": "1 2 3\n",
        })
        rec = model_store.save_archive(ws, "other.zip", source_path=path)
        root = model_store.store_root(ws)
        out = model_store.scan_archive(root, rec["package_id"])
        eq(out.get("package_kind"), "unknown",
           "既非 Touchstone 也非 Design Kit 的包必须如实判为未识别")
        ok(out.get("vendor") in (None, ""), "未知包不得编造厂商")


# ---------------------------------------------------------------------------
# 安全
# ---------------------------------------------------------------------------

def _zip_with_raw_name(path, arcname, payload=b"pwned"):
    """构造一个归档内路径越界的 ZIP（zipfile 会照写，不做校验）。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(arcname, payload)
    return path


def test_reject_path_traversal():
    with tempfile.TemporaryDirectory() as ws:
        path = _zip_with_raw_name(os.path.join(ws, "s", "evil.zip"),
                                  "../../../../evil.txt")
        rec = model_store.save_archive(ws, "evil.zip", source_path=path)
        root = model_store.store_root(ws)
        raises(model_store.UnsafeArchive,
               lambda: model_store.extract_package(root, rec["package_id"]),
               "路径穿越必须被拒绝")
        ok(not os.path.exists(os.path.join(os.path.dirname(ws), "evil.txt")),
           "绝不能真的写到工作区之外")


def test_reject_absolute_and_drive_paths():
    for arcname in ("/etc/evil.txt", "C:/evil.txt", "C:\\evil.txt", "\\\\srv\\evil.txt"):
        with tempfile.TemporaryDirectory() as ws:
            path = _zip_with_raw_name(os.path.join(ws, "s", "e.zip"), arcname)
            rec = model_store.save_archive(ws, "e.zip", source_path=path)
            root = model_store.store_root(ws)
            raises(model_store.UnsafeArchive,
                   lambda: model_store.extract_package(root, rec["package_id"]),
                   f"绝对/盘符/UNC 路径 {arcname} 必须被拒绝")


def test_reject_symlink_entries():
    with tempfile.TemporaryDirectory() as ws:
        path = os.path.join(ws, "s", "link.zip")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("real.txt", b"hello")
            info = zipfile.ZipInfo("link_to_outside")
            # 0xA1FF0000 = 普通文件 + 符号链接（Unix 高 16 位）
            info.external_attr = (0xA1FF) << 16
            info.create_system = 3        # Unix
            z.writestr(info, "/etc/passwd")
        rec = model_store.save_archive(ws, "link.zip", source_path=path)
        root = model_store.store_root(ws)
        raises(model_store.UnsafeArchive,
               lambda: model_store.extract_package(root, rec["package_id"]),
               "符号链接条目必须被拒绝（可能越界）")


def test_reject_corrupt_zip():
    with tempfile.TemporaryDirectory() as ws:
        bad = os.path.join(ws, "s", "broken.zip")
        os.makedirs(os.path.dirname(bad), exist_ok=True)
        with open(bad, "wb") as f:
            f.write(b"PK\x03\x04 this is not a real zip at all")
        # 上传本身应仍然成功（原始文件先保存下来，便于用户重试）
        rec = model_store.save_archive(ws, "broken.zip", source_path=bad)
        root = model_store.store_root(ws)
        raises(Exception,
               lambda: model_store.scan_archive(root, rec["package_id"]),
               "损坏 ZIP 在检查阶段必须报错")
        ok(os.path.isfile(os.path.join(ws, rec["archive_relpath"])),
           "检查失败也要保留原始 ZIP")


def test_enforce_limits():
    with tempfile.TemporaryDirectory() as ws:
        # 炸弹式：高压缩比 + 超多条目
        path = os.path.join(ws, "s", "bomb.zip")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("big.bin", b"\0" * (8 * 1024 * 1024))
        rec = model_store.save_archive(ws, "bomb.zip", source_path=path)
        root = model_store.store_root(ws)
        raises(model_store.UnsafeArchive,
               lambda: model_store.extract_package(
                   root, rec["package_id"], limits={"max_total_bytes": 1024 * 1024}),
               "累计解压大小超限必须拒绝")
        raises(model_store.UnsafeArchive,
               lambda: model_store.extract_package(
                   root, rec["package_id"], limits={"max_entries": 0}),
               "文件数量超限必须拒绝")
        raises(model_store.UnsafeArchive,
               lambda: model_store.extract_package(
                   root, rec["package_id"], limits={"max_compress_ratio": 1.0}),
               "异常压缩比（zip bomb）必须拒绝")


def test_non_zip_rejected():
    with tempfile.TemporaryDirectory() as ws:
        tar = os.path.join(ws, "s", "kit.tar")
        os.makedirs(os.path.dirname(tar), exist_ok=True)
        with open(tar, "wb") as f:
            f.write(b"ustar" + b"\0" * 100)
        raises(model_store.UnknownPackage,
               lambda: model_store.save_archive(ws, "kit.tar", source_path=tar),
               "首版只支持 ZIP，其它格式必须明确拒绝而不是当二进制存")


def test_extract_preserves_structure_and_skips_scripts():
    with tempfile.TemporaryDirectory() as ws:
        path = make_zip(os.path.join(ws, "s", "m.zip"), {
            "kit/setup.py": "import os\nos.system('rm -rf /')\n",
            "kit/sub/data.s2p": TOUCHSTONE_HEADER + "1.0 0.5 0.9 0.9 0.5\n",
        })
        rec = model_store.save_archive(ws, "m.zip", source_path=path)
        root = model_store.store_root(ws)
        out = model_store.extract_package(root, rec["package_id"])
        base = os.path.join(ws, out["extract_relpath"], "kit", "sub")
        ok(os.path.isfile(os.path.join(base, "data.s2p")),
           "必须保持原厂内部目录结构")
        ne(out.get("state"), model_store.STATE_READY,
           "解压结束不得标记模型就绪 —— 还没在 ADS 里验证过")
        ok(out.get("state") in (model_store.STATE_PENDING_IMPORT,
                                model_store.STATE_PENDING_VERIFY),
           f"解压结束应停在待导入/待验证，实际 {out.get('state')}")


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

def test_state_machine_rejects_illegal_transitions():
    with tempfile.TemporaryDirectory() as ws:
        path = sample_touchstone(os.path.join(ws, "s", "m.zip"))
        rec = model_store.save_archive(ws, "m.zip", source_path=path)
        root = model_store.store_root(ws)
        model_store.set_state(root, rec["package_id"], model_store.STATE_PENDING_IMPORT)
        # saved -> ready 跳步必须被拒：没导入没验证就宣布就绪是骗用户
        raises(model_store.IllegalStateTransition,
               lambda: model_store.set_state(root, rec["package_id"],
                                             model_store.STATE_READY),
               "未导入未验证不得直接标已就绪")


def test_failed_extraction_keeps_zip_and_cleans_temp():
    with tempfile.TemporaryDirectory() as ws:
        path = _zip_with_raw_name(os.path.join(ws, "s", "evil.zip"),
                                  "../escape.txt")
        rec = model_store.save_archive(ws, "evil.zip", source_path=path)
        root = model_store.store_root(ws)
        raises(model_store.UnsafeArchive,
               lambda: model_store.extract_package(root, rec["package_id"]))
        after = model_store.get_package(root, rec["package_id"])
        eq(after.get("state"), model_store.STATE_FAILED, "失败状态要写回清单")
        ok(os.path.isfile(os.path.join(ws, after["archive_relpath"])),
           "失败时必须保留原始 ZIP")
        tmp = os.path.join(root, ".tmp")
        leftovers = os.listdir(tmp) if os.path.isdir(tmp) else []
        eq(leftovers, [], f"失败后应清理临时产物，实际残留 {leftovers}")


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------

def test_concurrent_manifest_writes():
    """并发保存不许写坏 JSON，也不许丢更新。

    注意这里走的是 :func:`model_store.update_package`（内部在清单锁内做
    读-改-写），而不是 ``load_manifest()`` 之后再 ``save_manifest()`` ——
    后者横跨了两把锁，中间存在窗口，并发下必然丢记录。模块 docstring 里
    把这条纪律写明了，本测试就是守着它。
    """
    import threading

    with tempfile.TemporaryDirectory() as ws:
        root = model_store.store_root(ws)
        os.makedirs(root, exist_ok=True)
        errors: list = []

        def worker(i):
            try:
                for k in range(6):
                    # 走真实上传路径：每个线程上传**不同内容**的包
                    # （内容相同会按哈希复用成一条，那是另一个测试）。
                    src = os.path.join(ws, "src", f"p{i}_{k}.zip")
                    make_zip(src, {f"kit{i}_{k}/a.s2p": TOUCHSTONE_HEADER})
                    model_store.save_archive(ws, f"p{i}_{k}.zip", source_path=src)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        eq(errors, [], f"并发上传不应抛异常：{errors[:3]}")
        final = model_store.load_manifest(root)
        got = final.get("packages", {})
        eq(len(got), 24,
           f"并发写入后每条记录都应保留（不允许丢更新），实际只有 {len(got)} 条")
        # 内容不同的包必须各有自己的记录与存档目录，不能互相顶掉
        archives = os.path.join(root, "archives")
        if os.path.isdir(archives):
            eq(len(os.listdir(archives)), 24,
               "每个内容不同的包都应有自己的存档目录")


def test_concurrent_duplicate_upload_single_asset():
    import threading

    with tempfile.TemporaryDirectory() as ws:
        src = sample_touchstone(os.path.join(ws, "s", "m.zip"))
        results: list = []
        lock = threading.Lock()

        def worker():
            try:
                rec = model_store.save_archive(ws, "m.zip", source_path=src)
                with lock:
                    results.append(rec["package_id"])
            except Exception as e:  # noqa: BLE001
                with lock:
                    results.append(f"ERR:{type(e).__name__}:{e}")

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        errs = [r for r in results if isinstance(r, str) and r.startswith("ERR:")]
        eq(errs, [], f"并发重复上传不应报错：{errs[:2]}")
        eq(len(set(results)), 1, "并发上传同一文件必须收敛到同一个 package_id")


# ---------------------------------------------------------------------------
# 迁移与引用核对
# ---------------------------------------------------------------------------

def test_relocate_after_workspace_moved():
    with tempfile.TemporaryDirectory() as base:
        old_ws = os.path.join(base, "old_ws")
        os.makedirs(old_ws)
        path = sample_touchstone(os.path.join(base, "s", "m.zip"))
        rec = model_store.save_archive(old_ws, "m.zip", source_path=path)
        root = model_store.store_root(old_ws)
        model_store.extract_package(root, rec["package_id"])

        new_ws = os.path.join(base, "new_ws")
        os.makedirs(new_ws)
        import shutil
        shutil.move(os.path.join(old_ws, model_store.STORE_DIRNAME),
                    os.path.join(new_ws, model_store.STORE_DIRNAME))
        # 旧工作区已不在
        report = model_store.relocate(root, new_ws)
        ok(report, "迁移后应能重新定位")
        verify = model_store.verify_references(model_store.store_root(new_ws))
        ok(verify.get("ok"), f"迁移后引用应全部可核对：{verify}")


def test_verify_references_detects_missing():
    with tempfile.TemporaryDirectory() as ws:
        path = sample_touchstone(os.path.join(ws, "s", "m.zip"))
        rec = model_store.save_archive(ws, "m.zip", source_path=path)
        root = model_store.store_root(ws)
        ok(model_store.verify_references(root).get("ok"), "刚保存时引用应完整")
        os.unlink(os.path.join(ws, rec["archive_relpath"]))
        report = model_store.verify_references(root)
        eq(report.get("ok"), False, "资产丢失必须被核对出来")
        ok(report.get("missing"), "应指出缺失了哪些资产")


# ---------------------------------------------------------------------------
# 模型索引
# ---------------------------------------------------------------------------

def test_touchstone_index_reads_real_header():
    with tempfile.TemporaryDirectory() as ws:
        path = make_zip(os.path.join(ws, "s", "m.zip"), {
            "models/part_a.s2p": TOUCHSTONE_HEADER + "1.0 0.5 0.9 0.9 0.5\n"
                                               "2.0 0.4 0.8 0.8 0.4\n",
        })
        rec = model_store.save_archive(ws, "m.zip", source_path=path)
        root = model_store.store_root(ws)
        model_store.extract_package(root, rec["package_id"])
        models = model_store.index_models(root, rec["package_id"])
        ok(models, "应建立型号索引")
        first = models[0]
        eq(first.get("ports"), 2, "端口数应从文件名真实解析")
        eq(first.get("ports_source"), "filename", "端口数要标明来源")
        ok(first.get("freq_start_hz") is not None,
           "频率下限应从 Touchstone 文件真实解析")
        ok(abs(float(first["freq_start_hz"]) - 1e9) < 1e3,
           f"频率单位应按 # GHz 换算，实际 {first.get('freq_start_hz')}")
        eq(first.get("reference_impedance_ohm"), 50.0,
           "参考阻抗应从 R 50 真实解析")
        eq(first.get("reference_impedance_source"), "option_line",
           "参考阻抗要标明是从选项行读的，不是猜的")
        ok(first.get("relpath"), "应记录文件相对路径")
        ok(first.get("relpath", "").startswith("ads_agent_models/"),
           f"模型文件相对路径必须相对工作区，实际 {first.get('relpath')}")


def test_design_kit_index_uses_real_part_names():
    with tempfile.TemporaryDirectory() as ws:
        path = sample_design_kit(os.path.join(ws, "s", "k.zip"))
        rec = model_store.save_archive(ws, "kit.zip", source_path=path)
        root = model_store.store_root(ws)
        model_store.extract_package(root, rec["package_id"])
        models = model_store.index_models(root, rec["package_id"])
        names = {m.get("part") for m in models}
        ok("VENDOR_PART1" in names,
           f"型号应来自真实的元件文件名，实际索引到 {sorted(names)[:5]}")


def test_no_fabricated_vendor():
    """文件名里带厂商字样但包内无明确声明时，不得直接采信为已识别供应商。"""
    with tempfile.TemporaryDirectory() as ws:
        path = make_zip(os.path.join(ws, "s", "m.zip"), {
            "Murata_Stuff/some.s2p": TOUCHSTONE_HEADER + "1.0 0.5 0.9 0.9 0.5\n",
        })
        rec = model_store.save_archive(ws, "Murata_models.zip", source_path=path)
        root = model_store.store_root(ws)
        out = model_store.scan_archive(root, rec["package_id"])
        ok(out.get("vendor") in (None, ""),
           f"厂商不能只按目录名认定（实际 {out.get('vendor')!r}）——"
           f"要么包里明确声明，要么标未知")


def test_deleted_chat_does_not_delete_assets():
    """删聊天/清空会话不得顺带删除模型资产 —— 资产层不提供这种联动。"""
    with tempfile.TemporaryDirectory() as ws:
        path = sample_touchstone(os.path.join(ws, "s", "m.zip"))
        rec = model_store.save_archive(ws, "m.zip", source_path=path)
        root = model_store.store_root(ws)
        public = [n for n in dir(model_store) if not n.startswith("_")]
        for name in public:
            fn = getattr(model_store, name)
            if callable(fn) and ("delete" in name or "remove" in name
                                 or "purge" in name or "clean" in name):
                raise AssertionError(
                    f"模型资产层不应提供删除入口 {name} —— "
                    f"删聊天/清空会话不得顺带删除模型资产")
        ok(os.path.isfile(os.path.join(ws, rec["archive_relpath"])),
           "资产必须仍在")


# ---------------------------------------------------------------------------
# 原生元件列表（native_list）静态检测 —— 契约 §3.4
# ---------------------------------------------------------------------------

def sample_native_kit(path, root="AcmeKit_v1.0"):
    """一个齐全的原生套件：cfg 在 <root>/AcmeKit/，boot/palette 在 <root>/de/ael/。

    ``BOOT_AEL`` 值故意**不带扩展名**（真实 ADS 套件就是这样，
    如 DemoKit_mmWave 的 ``BOOT_AEL=../de/ael/boot``），用于验证补扩展名解析。
    """
    return make_zip(path, {
        f"{root}/AcmeKit/eesof_lib.cfg": (
            "DESIGN_KIT_NAME=AcmeKit\n"
            "VERSION=1.0\n"
            "TECH_DESC=Acme MMIC\n"
            "BOOT_AEL=../de/ael/boot\n"
            "INPUT_DATA_PATH=../circuit/models;../circuit/data\n"
            "LIB_BROWSER_CTL=../circuit/records/acme_library.ctl\n"
            "TEMPLATES_DIRECTORY=../circuit/templates/library\n"
        ),
        f"{root}/de/ael/boot.ael": "load('palette.ael')\n",
        f"{root}/de/ael/boot.atf": "atf-boot",
        f"{root}/de/ael/palette.ael": "de_define_library_palette(design, lib, cell)\n",
        f"{root}/de/ael/palette.atf": "atf-palette",
        f"{root}/circuit/bitmaps/acme_cap.bmp": b"BMP",
        f"{root}/circuit/records/acme_library.ctl":
            "<CATEGORY><NAME>Caps</NAME></CATEGORY>",
        f"{root}/circuit/models/acme_hbt.s2p":
            TOUCHSTONE_HEADER + "1.0 0.5 0.9 0.9 0.5\n",
        f"{root}/circuit/data/acme_notes.txt": "data",
        f"{root}/lib.defs": "DEFINE AcmeKit ./AcmeKit\n",
    })


def test_native_list_full_kit_resolves_paths():
    with tempfile.TemporaryDirectory() as ws:
        rec = model_store.save_archive(
            ws, "acme.zip",
            source_path=sample_native_kit(os.path.join(ws, "s", "a.zip")))
        root = model_store.store_root(ws)
        out = model_store.scan_archive(root, rec["package_id"])
        nl = out.get("native_list") or {}
        ok(isinstance(nl, dict) and nl,
           "scan 结果必须含 native_list（与 library_attach 同级）")
        eq(nl.get("design_kit_name"), "AcmeKit", "应读出 DESIGN_KIT_NAME")
        eq(nl.get("version"), "1.0", "应读出 VERSION")
        eq(nl.get("tech_desc"), "Acme MMIC", "应读出 TECH_DESC")
        eq(nl.get("eesof_lib_cfg"),
           ["AcmeKit_v1.0/AcmeKit/eesof_lib.cfg"], "应登记 cfg 的包根相对路径")
        # BOOT_AEL 值不带扩展名 → 必须补 .ael 找到真实 boot 文件
        ok("AcmeKit_v1.0/de/ael/boot.ael" in nl.get("boot_ael", []),
           f"BOOT_AEL(无扩展名)应解析出 boot.ael，实际 {nl.get('boot_ael')}")
        ok("AcmeKit_v1.0/de/ael/palette.ael" in nl.get("palette_ael", []),
           f"应登记 palette.ael，实际 {nl.get('palette_ael')}")
        ok((nl.get("palette_ael") or [""])[0].lower().endswith(".ael"),
           f".ael 与 .atf 并存时 .ael 应排在前，实际 {nl.get('palette_ael')}")
        ok("AcmeKit_v1.0/circuit/bitmaps" in nl.get("bitmaps", []),
           f"应登记 bitmaps 目录，实际 {nl.get('bitmaps')}")
        ok("AcmeKit_v1.0/circuit/records/acme_library.ctl"
           in nl.get("browser_ctl", []),
           f"LIB_BROWSER_CTL 指向的 .ctl 应出现在 browser_ctl，"
           f"实际 {nl.get('browser_ctl')}")
        ok("AcmeKit_v1.0/circuit/models" in nl.get("data_paths", [])
           and "AcmeKit_v1.0/circuit/data" in nl.get("data_paths", []),
           f"INPUT_DATA_PATH 的两个目录都应解析出，实际 {nl.get('data_paths')}")
        ok("AcmeKit_v1.0/lib.defs" in nl.get("lib_defs", []),
           "应登记 lib.defs")
        ok(nl.get("has_palette_assets") is True,
           "boot + palette 资产齐全时 has_palette_assets 应为 True")
        ok(nl.get("evidence"), "每个结论都要有可追溯证据")
        ok(any("静态存在" in s for s in (nl.get("limits") or [])),
           "limits 必须保留默认诊断「静态存在≠运行时加载成功」")


def test_native_list_missing_palette_diagnosed():
    """有 boot 但没有 palette.ael / bitmaps → has_palette_assets=False 且有诊断。"""
    with tempfile.TemporaryDirectory() as ws:
        path = make_zip(os.path.join(ws, "s", "b.zip"), {
            "Kit/eesof_lib.cfg": "DESIGN_KIT_NAME=Kit\nBOOT_AEL=de/ael/boot\n",
            "Kit/de/ael/boot.ael": "x",
            "Kit/lib.defs": "DEFINE Kit ./Kit\n",
        })
        rec = model_store.save_archive(ws, "b.zip", source_path=path)
        root = model_store.store_root(ws)
        nl = (model_store.scan_archive(root, rec["package_id"])
              .get("native_list") or {})
        ok(nl.get("boot_ael"), "boot 存在应被登记")
        ok(nl.get("has_palette_assets") is False,
           "缺 palette 资产时 has_palette_assets 必须为 False")
        blob = " ".join(nl.get("limits") or [])
        contains(blob.lower(), "palette",
                 f"limits 应给出缺 palette 资产的诊断，实际 {blob!r}")


def test_native_list_missing_boot_diagnosed():
    """有 palette 资产但没有 boot 文件 → has_palette_assets=False 且有诊断。"""
    with tempfile.TemporaryDirectory() as ws:
        path = make_zip(os.path.join(ws, "s", "c.zip"), {
            "Kit/eesof_lib.cfg": "DESIGN_KIT_NAME=Kit\nBOOT_AEL=de/ael/boot\n",
            "Kit/circuit/bitmaps/cap.bmp": b"BMP",
            "Kit/de/ael/palette.ael": "palette",
            "Kit/lib.defs": "DEFINE Kit ./Kit\n",
        })
        rec = model_store.save_archive(ws, "c.zip", source_path=path)
        root = model_store.store_root(ws)
        nl = (model_store.scan_archive(root, rec["package_id"])
              .get("native_list") or {})
        ok(not nl.get("boot_ael"),
           f"boot 文件缺失时 boot_ael 应为空，实际 {nl.get('boot_ael')}")
        ok(nl.get("has_palette_assets") is False,
           "缺 boot 时 has_palette_assets 必须为 False")
        blob = " ".join(nl.get("limits") or [])
        contains(blob.lower(), "boot", "limits 应给出缺启动脚本的诊断")


def test_native_list_palette_atf_fallback():
    """只有 .atf（无任何 .ael、无 bitmaps）的包不得被判缺 palette 资产。

    实测 TDK v2019.10 全包 .ael 条目数=0：boot 与 palette 都是编译产物
    （de/ael/boot.atf、de/ael/palette.atf）。palette 检测必须像 BOOT_AEL 一样
    有 .atf 回退，否则这类真实包会被误判成"缺 palette 资产"。
    """
    with tempfile.TemporaryDirectory() as ws:
        path = make_zip(os.path.join(ws, "s", "f.zip"), {
            "Kit/eesof_lib.cfg":
                "DESIGN_KIT_NAME=Kit\nBOOT_AEL=de/ael/boot\n",
            "Kit/de/ael/boot.atf": "atf-boot",
            "Kit/de/ael/palette.atf": "atf-palette",
            "Kit/lib.defs": "DEFINE Kit ./Kit\n",
        })
        rec = model_store.save_archive(ws, "f.zip", source_path=path)
        root = model_store.store_root(ws)
        nl = (model_store.scan_archive(root, rec["package_id"])
              .get("native_list") or {})
        ok("Kit/de/ael/boot.atf" in nl.get("boot_ael", []),
           f"boot.atf（无 .ael）应经回退命中，实际 {nl.get('boot_ael')}")
        ok("Kit/de/ael/palette.atf" in nl.get("palette_ael", []),
           f"palette.atf 应作为 palette 资产登记，实际 {nl.get('palette_ael')}")
        ok(nl.get("has_palette_assets") is True,
           "只有 palette.atf（无 .ael / 无 bitmaps）时不得误判为缺 palette 资产")


def test_native_list_external_boot_path_not_guessed():
    """BOOT_AEL 指向包外（环境变量）时不得瞎解析成包内文件，要如实记未解析。"""
    with tempfile.TemporaryDirectory() as ws:
        path = make_zip(os.path.join(ws, "s", "e.zip"), {
            "Kit/eesof_lib.cfg":
                "DESIGN_KIT_NAME=Kit\nBOOT_AEL=$HPEESOF_DIR/ads/ael/boot\n",
            "Kit/de/ael/boot.ael": "x",
            "Kit/lib.defs": "DEFINE Kit ./Kit\n",
        })
        rec = model_store.save_archive(ws, "e.zip", source_path=path)
        root = model_store.store_root(ws)
        nl = (model_store.scan_archive(root, rec["package_id"])
              .get("native_list") or {})
        ok(not nl.get("boot_ael"),
           "BOOT_AEL 指向环境变量(包外)时不得解析成包内文件（不许猜）")
        blob = " ".join(nl.get("limits") or []).lower()
        contains(blob, "包外", "limits 应说明该路径指向包外、未解析")


def test_native_list_special_kit_name_no_crash():
    """库名含 '#' / 中文 / 空格 / 斜杠也不能崩，值原样读出。"""
    with tempfile.TemporaryDirectory() as ws:
        name = "厂家#套件 A/B"
        path = make_zip(os.path.join(ws, "s", "d.zip"), {
            "特殊 套件/eesof_lib.cfg":
                f"DESIGN_KIT_NAME={name}\nVERSION=2.0#beta\nBOOT_AEL=de/ael/boot\n",
            "特殊 套件/de/ael/boot.ael": "x",
            "特殊 套件/de/ael/palette.ael": "p",
            "特殊 套件/lib.defs": "DEFINE 特殊 套件 ./特殊 套件\n",
        })
        rec = model_store.save_archive(ws, "d.zip", source_path=path)
        root = model_store.store_root(ws)
        out = model_store.scan_archive(root, rec["package_id"])
        nl = out.get("native_list") or {}
        eq(nl.get("design_kit_name"), name,
           "含 # / 中文 / 空格 / 斜杠的库名应原样读出不崩")
        eq(nl.get("version"), "2.0#beta",
           "值里的 # 不应被误当整行注释切掉")
        ok(nl.get("boot_ael"),
           "带特殊字符的目录下 BOOT_AEL 仍应能解析到 boot 文件")


def test_native_list_absent_old_structure_compatible():
    """老包/老清单没有 native_list：读取方按 (record.get('native_list') or {}) 不能崩。"""
    with tempfile.TemporaryDirectory() as ws:
        rec = model_store.save_archive(
            ws, "models.zip",
            source_path=sample_touchstone(os.path.join(ws, "s", "m.zip")))
        root = model_store.store_root(ws)
        # 未 scan 前记录里就没有 native_list（等价于老清单）
        fresh = model_store.get_package(root, rec["package_id"])
        nl0 = fresh.get("native_list") or {}
        eq(nl0, {}, "老清单里读不到 native_list 时取到空 dict，不抛异常")
        # Touchstone 包不含 cfg → 各列表为空但键存在，不崩
        out = model_store.scan_archive(root, rec["package_id"])
        nl = out.get("native_list")
        ok(isinstance(nl, dict), "scan 后 native_list 必须是 dict（向后兼容）")
        eq(nl.get("eesof_lib_cfg"), [], "无 cfg 时 eesof_lib_cfg 为空")
        ok(nl.get("has_palette_assets") is False,
           "无原生资源时 has_palette_assets 为 False")
        contains(" ".join(nl.get("limits") or []), "静态存在",
                 "即使无 cfg，limits 也要保留默认诊断")


if __name__ == "__main__":
    raise SystemExit(run(globals()))