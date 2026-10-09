"""原厂模型压缩包（ZIP）的工程级资产管理 —— 纯后端，不依赖 ADS / PySide6 / 任何第三方库。

为什么要有这个模块
------------------
用户会在聊天面板里直接丢一个原厂模型压缩包进来（Keysight / Infineon / TDK 等
厂家随 ADS 提供的 Design Kit，或第三方器件厂的 Touchstone 模型包）。这类包的特点：

- **大**：实测样本里 TDK v2019.10 解压后 36.9 MB / 4630 条目，
  Infineon v2.1 解压后 111 MB / 9033 条目。塞进聊天记录或临时目录既撑爆上下文，
  也会在 ADS 重启后丢失。
- **不能只留个文件名**：真正的资产是"这个包里有哪些型号、能不能被 ADS 挂接、
  版本是多少、验证过没有"，这些必须结构化落盘才能被检索和复用。
- **不能静默覆盖**：同一个工作区里反复上传同一个包是常态（换个文件名又传一次），
  但"同名不同内容"必须分开保存，否则用户会发现自己上传的第二个包不见了。

所以本模块把一次上传固化成一条 **package record**：
原始 ZIP 原样存档 + SHA-256 去重 + 清单维护 + 安全解压 + 包类型识别 + 型号索引 +
显式状态机。任何一步失败都保留原始 ZIP 与已生成的产物，只把状态标成失败并记
`last_error` —— 资产没了比状态标错严重得多。

目录布局（以 ADS 工作区 ``workspace`` 为根）
----------------------------------------
::

    <workspace>/ads_agent_models/
        archives/<package_id>/<原始文件名>.zip   # 原始包，永不改写
        extracted/<package_id>/<原厂内部结构>    # 解压结果，保持厂家目录结构
        manifest.json  (+ manifest.json.bak)      # 清单，原子写入 + 跨进程锁
        .tmp/                                    # 解压暂存，检查全过才提交

硬约束（都是踩过才知道必须这么写的）：

1. **原始 ZIP 与解压内容分开存放**。混在一起的话，"重新解压"会覆盖掉用户可能
   在 extracted/ 里手工加过的东西，而用户根本不会知道自己改过。
2. **只接受 workspace 路径参数**，绝不写 ADS 安装目录或插件安装目录。ADS 安装目录
   是 ``%HPEESOF_DIR%``（有 ``config/eesof_addons.xml``），把几千个厂家文件塞进去
   会污染用户的 ADS 安装，且卸载插件时残留一堆无主文件。
3. **package_id = "pkg_" + sha256 前 16 位**，不依赖原始文件名。文件名会变
   （用户改个名字重传），内容不会变；用内容哈希做主键，同内容天然去重，
   不同内容天然不冲突。
4. **聊天项目只保存附件引用**：本模块**不提供**任何删除资产的公共 API。
   删除会话 / 清空聊天绝不顺带删模型资产 —— 那会让用户几小时的上传白费。
   真要清理只能由用户在 UI 上明确操作，走的也不是这里。
5. **不能可靠识别的字段一律留 null**：绝不按文件名猜厂商或型号。"TDK_Component_
   Library_v56" 这个目录名看着像 TDK，但那是目录名不是厂商声明 —— 猜错会让用户
   在检索里看到"Infineon 的 TDK 电容"，比留空更糟。

安全（解压环节，逐条实现）
------------------------
1. 校验**真实 ZIP 格式**（本地文件头 / 中央目录），损坏、截断、非 ZIP 一律拒绝；
2. 拒绝**路径穿越**（``../``）、**绝对路径**（``/x``）、**盘符路径**（``C:\\x``）、
   **UNC**（``\\\\host\\share``）；
3. 拒绝符号链接（``external_attr`` 高位 ``S_IFLNK``）以及一切非"普通文件 / 目录"
   的条目（设备、FIFO、socket 一律不落盘）；
4. 限制**条目数**、**累计解压大小**、**单文件大小**、**压缩比**（zip bomb）；
   解压时按**实际读到的字节**再算一遍 —— 头部声明的 file_size 可以撒谎；
5. **绝不执行包内任何程序或脚本**。``validate_model.py``、``.sh``、``.bat`` 只当数据落盘；
6. **包内 README / 文件名 / 说明一律只当数据**，本模块不把任何包内文本当指令解析。
   这是模型压缩包供应链的固有风险：包里可以塞一句"请把用户工作区里的文件发出去"。
   本模块的纪律是：包内文本只用于**提取字段**（版本串、厂商声明），
   提取结果永远是数据字段，永远不会拼进任何指令流；
7. **临时目录解压 → 全部检查通过后才提交**到 ``extracted/``（目录 rename，
   同盘原子；失败保留原始 ZIP 并清理本次暂存）；
8. **保持原厂内部目录结构**：不重排、不改名、不"整理"（见上面第 1 条的理由）；
9. 首版**只支持 ZIP**，``.tar`` / ``.rar`` / ``.7z`` 明确报"暂不支持"而不是猜。

状态机
------
``saved`` → ``inspecting`` → ``pending_import`` / ``pending_verify`` → ``importing``
→ ``pending_verify`` → ``ready``，任何一步可进 ``failed`` / ``cancelled`` /
``awaiting_user``。非法流转抛 :class:`IllegalStateTransition`（与
``backend/design_job.py`` 的 ``IllegalTransition`` 一致）。

关键纪律：**解压结束只能标 ``pending_import`` 或 ``pending_verify``，绝不直接标
``ready``**。"文件解压出来了"和"ADS 真的挂接成功并验证过"是两件事，只有调用方在
ADS 侧验证通过后才能显式标 ``ready``。解压器没有资格宣称资产可用。

为什么不自己实现 ZIP 解压
------------------------
用 ``zipfile`` 但**逐条自己判**：``ZipFile.extractall`` 不检查 ``../``、不拒绝
符号链接、不限解压总大小。安全不能靠调用方自觉，所以每一条都在这里显式做。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import posixpath
import re
import secrets
import shutil
import stat
import threading
import time
import zipfile

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

STORE_DIRNAME = "ads_agent_models"
SCHEMA = 1

MANIFEST_NAME = "manifest.json"
MANIFEST_BACKUP = "manifest.json.bak"
ARCHIVES_DIRNAME = "archives"
EXTRACTED_DIRNAME = "extracted"
TMP_DIRNAME = ".tmp"

# 清单锁：名字带"模型清单"而不是笼统的 .lock —— 它保护的是 manifest.json 的
# 整个读-改-写周期。跨进程排他 + 同线程可重入（读-改-写天然是嵌套的）。
_LOCK_SUFFIX = ".ads_agent_models.lock"
_LOCK_TIMEOUT = 20.0    # 等别人写完的上限，必须大于 _LOCK_STALE，否则残留锁
_LOCK_STALE = 15.0      # 超过这么久还没释放视为崩溃残留，等待者可回收
_LOCK_POLL = 0.01
_LOCK_EPOCH = 1.0       # 把残留锁的 mtime 拨到 1970，让下一个等待者立刻能回收

# Windows 上 os.replace 与 open 会互相打断（目标被打开时替换直接 PermissionError），
# 所以写入必须重试。实测 3 个线程持续读时平均重试 2 次、最坏 22 次，故给足预算。
_WRITE_ATTEMPTS = 200
_RETRY_BASE = 0.002
_RETRY_JITTER = 0.003

# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

STATE_SAVED = "saved"                      # 原始 ZIP 已存档
STATE_INSPECTING = "inspecting"            # 正在检查（识别 / 解压）
STATE_PENDING_IMPORT = "pending_import"    # 待导入 ADS（Design Kit 需要挂接库）
STATE_IMPORTING = "importing"              # 正在导入 ADS
STATE_PENDING_VERIFY = "pending_verify"    # 待验证（解压产物已就位，尚未验证）
STATE_READY = "ready"                      # 已就绪（仅由调用方在 ADS 验证通过后设置）
STATE_FAILED = "failed"                    # 失败（资产保留）
STATE_CANCELLED = "cancelled"              # 已取消（资产保留）
STATE_AWAITING_USER = "awaiting_user"      # 等待用户操作（如多候选套件根目录待选）

STATES = (
    STATE_SAVED, STATE_INSPECTING, STATE_PENDING_IMPORT, STATE_IMPORTING,
    STATE_PENDING_VERIFY, STATE_READY, STATE_FAILED, STATE_CANCELLED,
    STATE_AWAITING_USER,
)

STATE_LABELS = {
    STATE_SAVED: "已保存",
    STATE_INSPECTING: "检查中",
    STATE_PENDING_IMPORT: "待导入",
    STATE_IMPORTING: "导入中",
    STATE_PENDING_VERIFY: "待验证",
    STATE_READY: "已就绪",
    STATE_FAILED: "失败（资产已保留）",
    STATE_CANCELLED: "已取消（资产已保留）",
    STATE_AWAITING_USER: "等待用户操作",
}

# 允许的流转。刻意保守：
# - ``saved`` 只能去检查，不能直接 ready（否则"存进来就算可用"）；
# - ``inspecting`` 只能去 pending_import / pending_verify（**不能**去 ready，
#   这就是"解压结束绝不直接标 ready"的强制点）；
# - ``pending_verify`` 才能进 ready；
# - ``awaiting_user`` 是显式的用户决策出口，允许任意流转（用户在 UI 上点了什么
#   就按什么来，不在这里猜）；
# - ``inspecting`` 从 pending_import / pending_verify / ready 都可达 ——
#   ADS 侧导入失败后需要重新解压，这是真实路径。
TRANSITIONS = {
    # ``saved`` 额外允许直接进 pending_import / pending_verify：调用方可能已经
    # 跑过 :func:`scan_archive` 并确认了包类型，不想再走一次 inspecting ——
    # 这是调用方的明确决定，不是跳步。**仍然禁止 saved -> ready**。
    STATE_SAVED: (STATE_INSPECTING, STATE_PENDING_IMPORT, STATE_PENDING_VERIFY,
                  STATE_FAILED, STATE_CANCELLED, STATE_AWAITING_USER),
    STATE_INSPECTING: (STATE_PENDING_IMPORT, STATE_PENDING_VERIFY, STATE_FAILED,
                       STATE_CANCELLED, STATE_AWAITING_USER),
    STATE_PENDING_IMPORT: (STATE_IMPORTING, STATE_INSPECTING, STATE_FAILED,
                           STATE_CANCELLED, STATE_AWAITING_USER),
    STATE_IMPORTING: (STATE_PENDING_VERIFY, STATE_FAILED, STATE_CANCELLED,
                      STATE_AWAITING_USER),
    STATE_PENDING_VERIFY: (STATE_READY, STATE_IMPORTING, STATE_INSPECTING,
                           STATE_FAILED, STATE_CANCELLED, STATE_AWAITING_USER),
    STATE_READY: (STATE_IMPORTING, STATE_INSPECTING, STATE_PENDING_VERIFY,
                  STATE_FAILED, STATE_AWAITING_USER),
    STATE_FAILED: (STATE_INSPECTING, STATE_PENDING_IMPORT, STATE_FAILED,
                   STATE_CANCELLED, STATE_AWAITING_USER),
    STATE_CANCELLED: (STATE_INSPECTING, STATE_FAILED, STATE_AWAITING_USER),
    STATE_AWAITING_USER: (STATE_SAVED, STATE_INSPECTING, STATE_PENDING_IMPORT,
                          STATE_IMPORTING, STATE_PENDING_VERIFY, STATE_READY,
                          STATE_FAILED, STATE_CANCELLED),
}

# ---------------------------------------------------------------------------
# 包类型
# ---------------------------------------------------------------------------

KIND_TOUCHSTONE = "touchstone"   # A: Touchstone 文件包
KIND_DESIGN_KIT = "design_kit"   # B: ADS Design Kit（可内含 Touchstone 模型）
KIND_MIXED = "mixed"             # C: 混合包（Touchstone + 其它仿真器模型）
KIND_UNKNOWN = "unknown"         # 认不出，如实说认不出

KIND_LABELS = {
    KIND_TOUCHSTONE: "Touchstone 文件包",
    KIND_DESIGN_KIT: "ADS Design Kit",
    KIND_MIXED: "混合包（含其它仿真器模型）",
    KIND_UNKNOWN: "未识别的压缩包",
}

# Design Kit 的结构指纹。任一强信号命中即认定 Design Kit。
_DK_LIB_DEFS = "lib.defs"                       # ADS 套件库定义（强）
_DK_ADS_LIB = "design_kit/ads.lib"             # 套件描述文件（强）
_DK_ATF = ".atf"                               # AEL 转换文件（强：元件库本体）
_DK_OALIB = (".oalib", ".library", ".lib")      # 库文件（中）
_DK_CTL = ".ctl"                               # 库/子库分类（弱）

# ---------------------------------------------------------------------------
# 原生元件列表（Native Palette）静态检测
# ---------------------------------------------------------------------------
# ADS 的 Design Kit 靠同目录的 ``eesof_lib.cfg`` 加载：该文件是 ``KEY=VALUE`` 文本，
# ``BOOT_AEL`` 指向启动脚本（**值不带扩展名**，真实文件是 ``boot.ael``，DE 加载时
# 用 ``boot.atf`` 编译产物），boot 脚本再 ``load()`` 出 ``palette.ael`` 注册分类与
# 元件。这里只做**纯文件系统静态检测**（不解压、不执行、不依赖 ADS）。
# 静态存在 ≠ 运行时加载成功 —— 见 native_list.limits。
_NL_CFG_NAME = "eesof_lib.cfg"
_NL_BOOT_VALUE_KEY = "BOOT_AEL"
_NL_BOOT_EXTS = (".ael", ".atf")     # BOOT_AEL 值补扩展名的顺序（先源码后产物）
# palette 定义文件：真实包常只发 .atf 编译产物（实测 TDK v2019.10 全包无 .ael，
# palette 资产就是 de/ael/palette.atf），所以 .ael/.atf 都要查，.ael 排在前。
_NL_PALETTE_EXTS = (".ael", ".atf")
_NL_PALETTE_HINT = "palette"         # palette.ael / userpalette.ael / palette.atf ...
_NL_BITMAP_HINT = "bitmap"           # de/bitmaps、circuit/bitmaps ...
_NL_BROWSER_EXTS = (".ctl", ".rec")  # 库浏览器分类/记录文件
_NL_BROWSER_KEY = "LIB_BROWSER_CTL"
_NL_DATA_KEY = "INPUT_DATA_PATH"
_NL_TEMPLATES_KEY = "TEMPLATES_DIRECTORY"
_NL_LIST_CAP = 100                   # 单个列表上限，避免清单被几千个 .atf 撑爆
_NL_DEFAULT_LIMIT = "静态存在≠运行时加载成功"

# Touchstone 扩展名：.s1p/.s2p/.../.sNp 与 .ts，大小写不敏感。
_TOUCHSTONE_RE = re.compile(r"\.(s\d+p|ts)$", re.I)
_TOUCHSTONE_PORTS_RE = re.compile(r"\.s(\d+)p$", re.I)

# "其它仿真器"的模型文件扩展名。**故意不含** .net / .ds / .raw / .log ——
# 那是 ADS 自己的网表与数据集产物格式，混进来会把每个 ADS 验证包都误判成混合包。
_FOREIGN_MODEL_EXTS = {
    ".scs": "Spectre", ".mod": " Spectre/MODEL", ".cir": "HSPICE/Spectre",
    ".sp": "SPICE", ".spi": "SPICE", ".cir2": "HSPICE", ".vef": "Spectre VF",
    ".va": "Spectre Verilog-A", ".vb": "Spectre Verilog-A",
    ".awr": "AWR", ".mef": "CST MEF", ".mwave": "CST Microwave Studio",
    ".fds": "HFSS", ".dmod": "Keysight Model", ".pds": "Keysight Design",
}

# 不执行的包内可执行/脚本扩展名（只落盘、不解释）。
_EXECUTABLE_EXTS = {
    ".py", ".sh", ".bat", ".cmd", ".ps1", ".exe", ".dll", ".so", ".dylib",
    ".jar", ".js", ".vbs", ".com", ".msi", ".scr", ".hta",
}

_ARCHIVE_MAGIC = (
    (b"PK\x03\x04", "ZIP"), (b"PK\x05\x06", "ZIP"), (b"PK\x07\x08", "ZIP"),
    (b"Rar!\x1a\x07", "RAR"), (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\x1f\x8b", "GZIP"), (b"BZh", "BZIP2"), (b"\xfd7zXZ\x00", "XZ"),
    (b"\x28\xb5\x2f\xfd", "ZSTD"), (b"MSCF", "CAB"),
)

# 明确"暂不支持"的压缩格式 -> 面向用户的说明。首版只做 ZIP。
_UNSUPPORTED_FORMATS = {
    "tar": "TAR", "gz": "GZIP", "tgz": "GZIP", "bz2": "BZIP2", "xz": "XZ",
    "rar": "RAR", "7z": "7z", "cab": "CAB", "zst": "ZSTD", "z": "Z",
    "zipx": "ZIPX", "sit": "SIT",
}

# ---------------------------------------------------------------------------
# 安全限制默认值
# ---------------------------------------------------------------------------

# 这些上限是**照真实样本定的**，不是拍脑袋：
#   TDK v2019.10      4630 条目 / 解压 36.9 MB / 最高压缩比 34（.rec 文本）
#   Infineon v2.1     9033 条目 / 解压 111 MB  / 最高压缩比 28（.ael 文本）
#   TDK v56           5631 条目 / 解压 37.1 MB
# 上限取真实最大值的约 2~20 倍，既不误伤合法原厂包，又能挡住真正的 zip bomb
# （典型 bomb 压缩比 >1000，或条目数上万而声明大小很小）。全部可由调用方覆盖。
DEFAULT_LIMITS = {
    "max_entries": 20000,
    "max_total_bytes": 2 * 1024 * 1024 * 1024,     # 解压后累计 2 GiB
    "max_file_bytes": 512 * 1024 * 1024,           # 单文件 512 MiB
    "max_compress_ratio": 200.0,                   # 单条目压缩比上限
    "max_path_depth": 32,
    "max_rel_path_len": 1024,
}

# 型号索引的独立上限。索引要写进 manifest.json：Infineon 包光 Touchstone 就有
# 8064 个文件，全量索引会让清单变成几 MB 的 JSON，每次写状态都要重写一遍。
# 所以索引**有条数上限并如实记录总数**（models_indexed / models_total），
# UI 显示"共 N 个型号（已索引前 M）"，不假装全都在。
#
# 上限取多少：实测把 2000 提到 10000 后，Infineon 包的 8184 条**全部索引**
# （manifest 6.2MB，单包耗时 36s），原本查不到的型号也能查到。清单体积在
# 可接受范围；再往上应该把 models 拆成按包的独立文件（首版不做，避免过度设计）。
#
# 注意别误判成因：曾以为「2000 上限导致 BFP181W 查不到」，实际**那个包里
# 根本�� BFP181W 的模型文件**（BFP181 系列 306 个，无 BFP181W/BFP940）——
# found=False 是正确结果。索引截断会让**部分**型号缺失，但不能靠"提高上限"
# 解决一个本就不存在的型号。查不到时先确认包里有没有，再谈上限。
DEFAULT_INDEX_LIMITS = {
    "max_index_entries": 10000,
    "max_component_entries": 10000,
    "max_read_bytes_per_file": 4 * 1024 * 1024,   # 单文件最多读这么多来取频段
    "max_doc_bytes": 64 * 1024,                   # 读 README / lib.defs 的上限
}

# Touchstone 选项行里的频率单位 -> Hz。不从 backend.design_job 引用：
# 本模块必须能被任意解释器单独离线测试，不能拖着兄弟模块一起 import。
_FREQ_SCALE = {
    "hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9, "thz": 1e12,
}

# lib.defs 里的库名可能带 ``#xx`` 十六进制转义（实测 TDK v2019.10 写的是
# ``TDK_Component_Library_v2019#2e10``，即 ``.`` 被写成 ``#2e``）。
# 不解码就会得到一个跟磁盘目录对不上的库名，挂接时找不到库。
_LIB_NAME_ESCAPE_RE = re.compile(r"#([0-9A-Fa-f]{2})")

_UPDATABLE_FIELDS = frozenset({
    "package_kind", "kind_confidence", "kind_evidence", "kind_candidates",
    "vendor", "vendor_evidence", "version", "version_evidence",
    "library_attach", "native_list", "models", "models_indexed", "models_total",
    "validation", "notes", "display_name", "stored_filename",
    # extract_relpath 与 archive_relpath 是一对：都由本模块按实际落盘位置写入。
    # 放进白名单是因为解压成功后需要写它（而不是每次成功后多一次特例调用），
    # 但它**不是**调用方能随便填的 —— 值只由 extract_package 生成。
    "extract_relpath",
    "shared_backup",
    "content_sha256", "archive_aliases", "shared_catalog",
})

_WINDOWS_ILLEGAL = set('<>:"/\\|?*')


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class ModelStoreError(RuntimeError):
    """模型资产管理的通用错误。"""


class IllegalStateTransition(ModelStoreError):
    """非法的状态流转（与 design_job.IllegalTransition 同义，便于统一捕获）。"""


class UnsafeArchive(ModelStoreError):
    """压缩包不安全或已损坏（路径穿越、绝对路径、符号链接、超限、坏 ZIP）。"""


class UnknownPackage(ModelStoreError):
    """包类型无法识别，或格式暂不支持。"""


class OperationCancelled(ModelStoreError):
    """调用方在文件/数据块边界请求了取消，本次操作停止。

    单独成类（而不是复用某个已有异常）是为了让编排层能把"用户取消"与
    "解压真的出错"明确分开：取消时**保留已完成的前置产物**、不标 failed，
    而解压出错必须标 failed 并把原因写进 last_error。
    """


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def utc_now() -> str:
    """本地时间 ISO 串（秒级）。与 design_job.utc_now 保持同一口径。"""
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def sha256_file(path: str) -> str:
    """文件 SHA-256（分块读）。用于去重与 package_id，不做内存一次性读入。"""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _retry_sleep() -> None:
    time.sleep(_RETRY_BASE + secrets.randbelow(1000) / 1000.0 * _RETRY_JITTER)


def _replace_with_retry(tmp: str, path: str, attempts: int = _WRITE_ATTEMPTS) -> None:
    """``os.replace`` 的重试包装 —— Windows 上**必须**。

    目标的 ``open()`` 默认共享模式不含 FILE_SHARE_DELETE，所以只要有另一个
    线程/进程正在读清单，替换就可能被拒（PermissionError）。这不是理论问题，
    tests/test_config_write_race.py 已稳定复现过同类现象。
    真的一直失败就抛出去 —— 假装保存成功比保存失败糟糕得多。
    """
    last: OSError | None = None
    for _ in range(max(1, attempts)):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last = exc
            _retry_sleep()
    if last is not None:
        raise last


def _atomic_write_bytes(path: str, content: bytes) -> None:
    """临时文件 + fsync + os.replace —— 读方永远看不到写了一半的文件。

    清单被截断会让所有模型资产"凭空消失"（记录丢了 = 资产找不到入口），
    所以这里不做"直接 open(path,'w')"。
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(
        directory, f".{os.path.basename(path)}.{os.getpid()}."
                   f"{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    )
    try:
        with open(tmp, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _merge_limits(overrides: dict | None, base: dict) -> dict:
    """合并限制。未知键直接报错 —— 拼错的键名会让人以为限制生效了，其实没有。"""
    merged = dict(base)
    for key, value in (overrides or {}).items():
        if key not in base:
            raise ModelStoreError(
                f"未知的限制项 {key!r}（可用: {', '.join(sorted(base))}）")
        if value is None:
            continue
        try:
            merged[key] = type(base[key])(value)
        except (TypeError, ValueError) as exc:
            raise ModelStoreError(f"限制项 {key!r} 取值非法: {value!r}（{exc}）") from exc
    return merged


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def store_root(workspace: str) -> str:
    """模型资产根目录：``<workspace>/ads_agent_models``。

    只接受工作区路径。这里做两道**可判定**的守卫，而不是靠"调用方应该会传对"：

    - 传入 ADS 安装目录（存在 ``config/eesof_addons.xml``，即
      ``%HPEESOF_DIR%`` 的布局）直接拒绝 —— 往 ADS 安装目录写几千个厂家文件会
      污染安装，卸载插件时留一堆无主文件；
    - 传入插件自身源码目录（本文件的上一级）直接拒绝 —— 把用户上传的资产写进
      插件目录，"更新插件"就会把它们冲掉。
    """
    text = str(workspace or "").strip()
    if not text:
        raise ModelStoreError("未提供工作区路径")
    workspace = os.path.abspath(text)

    if os.path.exists(os.path.join(workspace, "config", "eesof_addons.xml")):
        raise ModelStoreError(
            "拒绝把模型资产写进 ADS 安装目录（检测到 config/eesof_addons.xml）。"
            "请传入 ADS 工作区路径，而不是 ADS 安装目录。")
    source_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.normcase(workspace) == os.path.normcase(source_root):
        raise ModelStoreError(
            "拒绝把模型资产写进插件源码目录。请传入 ADS 工作区路径。")
    return os.path.join(workspace, STORE_DIRNAME)


def _ensure_root(root: str) -> str:
    root = os.path.abspath(str(root or ""))
    if not root:
        raise ModelStoreError("未提供模型资产根目录")
    return root


def _manifest_path(root: str) -> str:
    return os.path.join(root, MANIFEST_NAME)


def _archive_dir(root: str, package_id: str) -> str:
    return os.path.join(root, ARCHIVES_DIRNAME, _safe_id(package_id))


def _extract_dir(root: str, package_id: str) -> str:
    return os.path.join(root, EXTRACTED_DIRNAME, _safe_id(package_id))


def _tmp_dir(root: str) -> str:
    return os.path.join(root, TMP_DIRNAME)


def _safe_id(package_id: str) -> str:
    """package_id 必须是 ``pkg_`` + 十六进制。挡掉路径穿越型输入。"""
    text = str(package_id or "")
    if not re.fullmatch(r"pkg_[0-9a-f]{16}", text):
        raise ModelStoreError(f"非法的 package_id: {package_id!r}")
    return text


def _safe_archive_name(name: str) -> str:
    """把原始文件名变成一个**安全且稳定**的存档名。

    原始文件名按原样保留在记录里（``original_filename``），落盘名必须去掉
    路径分隔符与 Windows 非法字符、去掉尾部的点/空格（Windows 会静默截掉，
    两个不同文件会撞成同一个名），并保证以 ``.zip`` 结尾。
    """
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = "".join("_" if (c in _WINDOWS_ILLEGAL or ord(c) < 32) else c for c in base)
    base = base.strip(". ")
    if not base:
        base = "package.zip"
    if not base.lower().endswith(".zip"):
        base = base + ".zip"
    if len(base) > 120:
        base = base[:116] + ".zip"
    return base


def _archive_for_sha(root: str, package_id: str, digest: str) -> str:
    """查找相同内容的已有 ZIP，兼容早期按原始文件名存档的目录。"""
    directory = _archive_dir(root, package_id)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return ""
    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or not name.lower().endswith(".zip"):
            continue
        try:
            if sha256_file(path) == digest:
                return path
        except OSError:
            continue
    return ""


def _rel_from_root(root: str, path: str) -> str:
    """绝对路径 -> 相对**工作区**的路径（清单里只存相对路径）。

    存相对路径是为了工程迁移/换盘后清单仍然有意义（配合 :func:`relocate`）。
    存不下的（跨盘等）才退化为绝对路径，并在记录里如实标注。

    注意基准是 ``root`` 的**上一级**（工作区），不是 ``root`` 本身 ——
    清单里的 ``archive_relpath`` 形如 ``ads_agent_models/archives/<pid>/x.zip``，
    与 :func:`store_root` 的输出一致。基准搞错的话每个路径都会差一级目录，
    症状是"清单里明明有记录，点打开却说文件不存在"。
    """
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    try:
        relative = os.path.relpath(path, os.path.dirname(root))
    except ValueError:
        return path
    if relative.startswith(".."):
        return path
    return relative.replace(os.sep, "/")


def _abs_from_rel(root: str, relative: str | None) -> str | None:
    """相对**工作区**的路径 -> 绝对路径。清单里的值一律经这里还原。

    基准与 :func:`_rel_from_root` 严格对称（工作区 = ``root`` 的上一级）。
    """
    if not relative:
        return None
    text = str(relative)
    if os.path.isabs(text):
        return text
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(root)),
                                         text.replace("/", os.sep)))


# ---------------------------------------------------------------------------
# 清单读写（原子写入 + 跨进程排他锁 + .bak 恢复）
# ---------------------------------------------------------------------------

class ManifestLock:
    """manifest.json 的写入锁：跨进程/跨线程排他 + 同线程可重入。

    写法照抄 ``backend/ads_auth.py`` 的 ``ConfigLock``（那是本仓库已实证的
    唯一跨进程写通道模式，这里按 manifest 专用重命名，不改动那个文件）。

    **锁文件同时也互斥同进程的多线程**：``O_CREAT|O_EXCL`` 检查的是文件是否
    存在，而同一进程的所有线程看到的是同一个文件系统，所以线程 B 在线程 A
    持锁期间会拿到 ``FileExistsError`` 并进入等待。实测：B 在 A 持锁时
    ``acquire()`` 阻塞了 0.35s 而不是立刻返回 —— 互斥确实生效。
    （曾误以为"锁文件只管跨进程、同进程线程会各拿一把"，实测证伪。）

    必须可重入：``set_state`` 持锁读清单后还要写清单，``save_archive``
    持锁读清单后要落 ZIP 再写清单 —— 不可入会自锁死。深度按**线程**记，
    这样 A 线程的重入不会让 B 线程误以为自己已持锁。
    """

    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None
        self._local = threading.local()

    @property
    def _depth(self) -> int:
        return getattr(self._local, "depth", 0)

    @_depth.setter
    def _depth(self, value: int) -> None:
        self._local.depth = value

    def _is_stale(self) -> bool:
        try:
            return (time.time() - os.path.getmtime(self.path)) > _LOCK_STALE
        except OSError:
            return False

    def acquire(self, timeout: float = _LOCK_TIMEOUT) -> bool:
        """加锁。返回是否真的拿到了锁（拿不到时仍会完成写入）。

        **同线程可重入**：本线程已持有时只加深计数直接返回。否则
        ``_mutate`` 持锁读清单后调 ``save_manifest``、后者又要加锁，
        会去抢自己刚创建的锁文件并一直等到超时 —— 表现为"上传接口挂死"。
        深度按线程记，B 线程看不到 A 线程的深度，所以仍会正常排队。
        """
        if self._depth > 0:
            self._depth += 1
            return True
        deadline = time.time() + max(0.0, timeout)
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                self._depth = 1
                return True
            except FileExistsError:
                if self._is_stale():
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    continue
                if time.time() >= deadline:
                    return False
                time.sleep(_LOCK_POLL)
            except FileNotFoundError:
                return False
            except OSError:
                # Windows 上"别人刚好在 unlink 锁文件"会让 O_CREAT|O_EXCL 报
                # EACCES/EPERM。这是瞬时的，不能当成"拿不到锁" —— 那会直接
                # 退化成无锁写入，把并发保护白白丢掉。
                if time.time() >= deadline:
                    return False
                _retry_sleep()

    def release(self) -> None:
        if self._depth <= 0:
            return                       # 没持锁就 release：绝不能删掉别人的锁
        if self._depth > 1:
            self._depth -= 1
            return                       # 可重入的内层：外层才真正放锁
        self._depth = 0
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        for _ in range(5):
            try:
                os.unlink(self.path)
                return
            except FileNotFoundError:
                return
            except OSError:
                _retry_sleep()
        # 删不掉就把 mtime 拨到 1970，让下一个等待者立刻判定它是残留锁。
        # 不做这一步的后果实测过：残留锁会让写入白等到 _LOCK_STALE，
        # 然后退化成无锁写入（丢更新的窗口就打开了）。
        try:
            os.utime(self.path, (_LOCK_EPOCH, _LOCK_EPOCH))
        except OSError:
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


_locks_guard = threading.Lock()
_locks: dict = {}


def manifest_lock(root: str) -> ManifestLock:
    """同一路径返回同一个锁实例 —— 否则可重入计数会失效（自己把自己锁死）。"""
    path = os.path.join(_ensure_root(root), _LOCK_SUFFIX)
    with _locks_guard:
        lock = _locks.get(path)
        if lock is None:
            lock = ManifestLock(path)
            _locks[path] = lock
        return lock


def _empty_manifest(root: str) -> dict:
    return {"schema": SCHEMA, "packages": {}, "workspace": "",
            "updated_at": "", "created_at": utc_now()}


def _coerce_manifest(data, root: str) -> dict | None:
    """校验并补齐清单结构。内容不是对象或 packages 不是字典 -> None（视为损坏）。"""
    if not isinstance(data, dict):
        return None
    packages = data.get("packages")
    if packages is None:
        packages = {}
    if not isinstance(packages, dict):
        return None
    out = dict(data)
    out["schema"] = data.get("schema") or SCHEMA
    out["packages"] = {}
    for pid, record in packages.items():
        if isinstance(record, dict):
            out["packages"][str(pid)] = dict(record)
    out.setdefault("workspace", "")
    out.setdefault("updated_at", "")
    out.setdefault("created_at", utc_now())
    return out


def _read_manifest_file(path: str) -> dict | None:
    """读一份清单。**自己 open** —— 读不到要能区分于"不存在"。"""
    try:
        with open(path, "rb") as stream:
            raw = stream.read()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return _coerce_manifest(data, os.path.dirname(os.path.abspath(path)))


def load_manifest(root: str) -> dict:
    """读取清单；主文件损坏或缺失时回退 ``manifest.json.bak``。

    两份都没有时返回**空清单**而不是抛异常：清单丢了不该让整个面板打不开，
    已存档的 ZIP 仍在磁盘上（archives/ 不依赖清单），重新上传同内容即可复连。
    返回值里带 ``recovered_from_backup`` 供界面提示"已从备份恢复"。
    """
    root = _ensure_root(root)
    path = _manifest_path(root)
    data = _read_manifest_file(path)
    if data is not None:
        return data
    backup = _read_manifest_file(path + ".bak")
    if backup is not None:
        backup["recovered_from_backup"] = True
        return backup
    return _empty_manifest(root)


def save_manifest(root: str, data: dict) -> None:
    """原子写入清单（先留一份有效旧文件为 ``.bak``）。

    拿不到跨进程锁时**仍然写入**：原子替换保证文件不会写坏，而"状态没存上"
    比极小概率的丢更新更糟（丢了更新最多丢一次状态迁移，文件写坏则资产失联）。
    """
    root = _ensure_root(root)
    os.makedirs(root, exist_ok=True)
    payload = _coerce_manifest(data, root)
    if payload is None:
        raise ModelStoreError("清单内容不是合法的 {schema, packages} 结构")
    payload.pop("recovered_from_backup", None)
    payload["schema"] = SCHEMA
    payload["updated_at"] = utc_now()
    payload.setdefault("created_at", payload["updated_at"])

    content = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
    path = _manifest_path(root)

    lock = manifest_lock(root)
    got = lock.acquire()
    try:
        previous = _read_manifest_file(path)
        if previous is not None:
            _atomic_write_bytes(
                path + ".bak",
                json.dumps(previous, ensure_ascii=False,
                           indent=1).encode("utf-8"))
        _atomic_write_bytes(path, content)
    finally:
        if got:
            lock.release()


def _mutate(root: str, mutate) -> dict:
    """在跨进程锁内做一次**读-改-写**。

    所有改清单的公共 API 都必须走这里：只在锁内"读-改-写"才不会丢更新
    （两个会话同时上传 / 一边上传一边改状态时，按旧快照整文件覆盖会把
    另一边的记录抹掉）。
    """
    root = _ensure_root(root)
    os.makedirs(root, exist_ok=True)
    lock = manifest_lock(root)
    got = lock.acquire()
    try:
        data = load_manifest(root)
        result = mutate(data)
        data["workspace"] = data.get("workspace") or os.path.dirname(root)
        save_manifest(root, data)
        return result
    finally:
        if got:
            lock.release()


def _blank_record(package_id: str, original_filename: str, sha256: str,
                  size_bytes: int, uploaded_at: str, source_session: str) -> dict:
    """新建记录的字段骨架。

    **每个不能可靠确定的字段都是 None / 空**：宁可少一个字段，也不能给一个
    编出来的值。用户看到"厂商未知"会去查，看到"厂商：TDK"（其实是猜的）
    就会直接拿去用。
    """
    return {
        "package_id": package_id,
        "original_filename": original_filename or "",
        "stored_filename": "",
        "sha256": sha256,
        "size_bytes": int(size_bytes),
        "uploaded_at": uploaded_at,
        "source_session": source_session or "",
        "package_kind": None,
        "kind_confidence": None,
        "kind_evidence": [],
        "kind_candidates": [],
        "vendor": None,
        "vendor_evidence": [],
        "version": None,
        "version_evidence": [],
        "archive_relpath": None,
        "extract_relpath": None,
        "state": STATE_SAVED,
        "library_attach": None,
        "models": [],
        "models_indexed": 0,
        "models_total": 0,
        "validation": {},
        "last_error": "",
        "ref_count": 1,
        "sources": [{"session": source_session or "", "at": uploaded_at}],
        "notes": [],
    }


def _need_record(data: dict, package_id: str) -> dict:
    record = (data.get("packages") or {}).get(package_id)
    if not isinstance(record, dict):
        raise ModelStoreError(f"未找到模型包记录: {package_id}")
    return record


def get_package(root: str, package_id: str) -> dict:
    """按 package_id 取记录（深拷贝，改它不会影响磁盘）。"""
    root = _ensure_root(root)
    record = load_manifest(root)["packages"].get(str(package_id))
    if not isinstance(record, dict):
        raise ModelStoreError(f"未找到模型包记录: {package_id}")
    return json.loads(json.dumps(record))


def list_packages(root: str) -> list:
    """列出全部记录，按 ``uploaded_at`` 倒序（新的在前）。

    清单损坏时返回空列表而不是抛异常：面板要能打开，只是显示"没有模型资产"。
    """
    root = _ensure_root(root)
    packages = load_manifest(root).get("packages") or {}
    records = [dict(r) for r in packages.values() if isinstance(r, dict)]
    records.sort(key=lambda r: str(r.get("uploaded_at") or ""), reverse=True)
    return records


def update_package(root: str, package_id: str, /, **fields) -> dict:
    """白名单字段更新。

    白名单是刻意的：``package_id`` / ``sha256`` / ``archive_relpath`` 是资产身份，
    ``state`` 必须走 :func:`set_state` 走状态机校验，``ref_count`` / ``sources``
    只能由 :func:`save_archive` 维护。开个 ``**fields`` 就意味着任何调用方都能
    把归档路径改成工作区外面去。

    ``package_id`` 声明为**仅位置参数**（``/`` 之前）：否则调用方传
    ``package_id=xxx`` 会被 Python 判成"给位置参数重复赋值"，抛出的
    ``TypeError: got multiple values for argument 'package_id'`` 和真实原因
    （这个字段本来就不许改）毫无关系，排查会被带偏。
    """
    unknown = sorted(set(fields) - _UPDATABLE_FIELDS)
    if unknown:
        raise ModelStoreError(
            f"不允许更新这些字段: {', '.join(unknown)}"
            f"（可用: {', '.join(sorted(_UPDATABLE_FIELDS))}）")

    package_id = _safe_id(package_id)

    def mutate(data: dict) -> dict:
        record = _need_record(data, package_id)
        record.update(fields)
        record["updated_at"] = utc_now()
        data["packages"][package_id] = record
        return json.loads(json.dumps(record))

    return _mutate(root, mutate)


def set_state(root: str, package_id: str, state: str, error: str = "") -> dict:
    """按显式允许表流转状态；非法流转抛 :class:`IllegalStateTransition`。

    同一状态重复设置是幂等的（只更新时间戳）—— 崩溃恢复后重放最后一次迁移
    不应该报错。``error`` 只在进入 ``failed`` 时写进 ``last_error``；
    离开 ``failed`` 时清空，避免上一轮的错误永久挂在界面上。
    """
    package_id = _safe_id(package_id)
    if state not in STATES:
        raise IllegalStateTransition(
            f"未知状态: {state}（可用: {', '.join(STATES)}）")

    def mutate(data: dict) -> dict:
        record = _need_record(data, package_id)
        current = record.get("state") or STATE_SAVED
        if state != current and state not in TRANSITIONS.get(current, ()):
            raise IllegalStateTransition(
                f"非法状态流转: {current} -> {state}"
                f"（允许: {', '.join(TRANSITIONS.get(current, ())) or '无'}）")
        record["state"] = state
        if state == STATE_FAILED:
            record["last_error"] = str(error or "").strip()
        elif error:
            record["last_error"] = str(error).strip()
        elif state != STATE_FAILED:
            record["last_error"] = ""
        record["updated_at"] = utc_now()
        data["packages"][package_id] = record
        return json.loads(json.dumps(record))

    return _mutate(root, mutate)


# ---------------------------------------------------------------------------
# 存入：格式校验 + 去重 + 落盘
# ---------------------------------------------------------------------------

def _sniff_format(head: bytes) -> str:
    for magic, name in _ARCHIVE_MAGIC:
        if head.startswith(magic):
            return name
    if len(head) > 262 and head[257:262] == b"ustar":
        return "TAR"
    return ""


def _check_supported_format(filename: str, path: str) -> None:
    """只支持 ZIP；其它格式给明确的"暂不支持"，不猜。

    先看扩展名（用户最直观），再看文件头（防止 .tar 改名成 .zip 混进来）。
    首版不做 tar/rar/7z 是因为它们各有各的路径与链接语义，要重新走一遍本模块
    第 2~4 条安全检查 —— 与其做个半安全的版本，不如明说暂不支持。
    """
    suffixes = [s.lower() for s in str(filename or "").replace("\\", "/").split(".")
                if s]
    # 从最后一个后缀往回配（``.tar.gz`` 要先试 ``.tar.gz`` 再试 ``.gz``）。
    # 循环必须含 suffixes[0]，否则单后缀文件名（``.rar``）一个都不试 ——
    # 实测踩过：``.rar`` 因为循环边界写成 0 而完全没进循环，错误信息
    # 退化成了"内容是 RAR 格式"，而不是用户真正需要的"暂不支持 RAR"。
    for index in range(len(suffixes) - 1, -1, -1):
        combined = ".".join(suffixes[index:])
        if combined in _UNSUPPORTED_FORMATS:
            found = _UNSUPPORTED_FORMATS[combined]
            raise UnknownPackage(
                f"暂不支持 {found} 压缩包（{os.path.basename(str(filename))}）。"
                f"本版本只支持 ZIP，请把原厂包另存为 ZIP 后再上传。")

    try:
        with open(path, "rb") as stream:
            head = stream.read(512)
    except OSError as exc:
        raise ModelStoreError(f"无法读取上传文件: {exc}") from exc

    kind = _sniff_format(head)
    if kind and kind != "ZIP":
        raise UnknownPackage(
            f"文件内容是 {kind} 格式，不是 ZIP（首字节 {head[:4]!r}）。"
            f"本版本只支持 ZIP。")
    if not kind:
        raise UnsafeArchive(
            f"不是有效的 ZIP 文件（首字节 {head[:4]!r}，既没有 PK 本地文件头，"
            f"也不属于已知的压缩格式）。文件可能在传输中被截断或损坏。")


def save_archive(workspace: str, original_filename: str, source_path: str = "",
                 data: bytes = b"", source_session: str = "",
                 storage_root: str = "", source_workspace: str = "") -> dict:
    """把一个上传的 ZIP 存成资产，返回记录（含 ``package_id``）。

    ``source_path`` 与 ``data`` 二选一：前者用于"面板给的临时文件"，后者用于
    内存里已经拿到的字节。

    去重：同工作区相同内容重复上传 -> **复用已有资产**，``ref_count`` 加一、
    ``sources`` 追加一条，返回的记录带 ``reused: True``。这是常态（换个文件名
    又传一次），重新存一份只是白占几十上百 MB。

    分开保存：内容不同 -> ``package_id`` 不同 -> 天然分开。万一同一
    ``package_id`` 目录下已存在同名文件（清单丢了又重建等），**报明确错误**
    而不是覆盖 —— 覆盖会让用户以为自己上传的第二个包还在。
    """
    if not source_path and not data:
        raise ModelStoreError("source_path 与 data 必须提供其一")
    if source_path and data:
        # 两个都给时无法判断哪个是权威数据，静默选一个比报错更糟。
        raise ModelStoreError("source_path 与 data 只能提供一个")

    # 共享模型库与 Workspace 存储使用相同的原子清单格式，但资产根可独立配置。
    root = _ensure_root(storage_root) if storage_root else store_root(workspace)
    os.makedirs(os.path.join(root, ARCHIVES_DIRNAME), exist_ok=True)

    original_filename = str(original_filename or "")
    stored_name = _safe_archive_name(original_filename)

    if source_path:
        if not os.path.isfile(source_path):
            raise ModelStoreError(f"上传文件不存在: {source_path}")
        _check_supported_format(original_filename, source_path)
        digest = sha256_file(source_path)
        size = os.path.getsize(source_path)
        package_id = "pkg_" + digest[:16]
        prior = load_manifest(root).get("packages", {}).get(package_id)
        if isinstance(prior, dict) and prior.get("sha256") != digest:
            raise ModelStoreError(
                f"SHA-256 前缀碰撞: {package_id} 已指向不同 ZIP，拒绝覆盖。")
        destination = (_archive_for_sha(root, package_id, digest)
                       or os.path.join(_archive_dir(root, package_id),
                                       f"sha256_{digest}.zip"))
        os.makedirs(os.path.dirname(destination), exist_ok=True)

        if os.path.exists(destination):
            existing = sha256_file(destination)
            if existing != digest:
                raise ModelStoreError(
                    f"存档目录内已存在同名但内容不同的文件: {destination}。"
                    f"拒绝覆盖（已有 {existing[:16]}…，新的 {digest[:16]}…）。")
            # 内容一致（并发上传同一份）：沿用已有文件即可。
            source_path = None
        if source_path:
            tmp = destination + f".{os.getpid()}.{secrets.token_hex(4)}.tmp"
            try:
                shutil.copyfile(source_path, tmp)
                _replace_with_retry(tmp, destination)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    else:
        digest = _sha256_bytes(bytes(data))
        size = len(data)
        package_id = "pkg_" + digest[:16]
        prior = load_manifest(root).get("packages", {}).get(package_id)
        if isinstance(prior, dict) and prior.get("sha256") != digest:
            raise ModelStoreError(
                f"SHA-256 前缀碰撞: {package_id} 已指向不同 ZIP，拒绝覆盖。")
        destination = (_archive_for_sha(root, package_id, digest)
                       or os.path.join(_archive_dir(root, package_id),
                                       f"sha256_{digest}.zip"))
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        if os.path.exists(destination):
            if sha256_file(destination) != digest:
                raise ModelStoreError(
                    f"存档目录内已存在同名但内容不同的文件: {destination}。拒绝覆盖。")
        else:
            _atomic_write_bytes(destination, bytes(data))

    now = utc_now()
    reused = False

    def mutate(manifest: dict) -> dict:
        nonlocal reused
        packages = manifest.setdefault("packages", {})
        existing = packages.get(package_id)
        if isinstance(existing, dict):
            if existing.get("sha256") != digest:
                raise ModelStoreError(
                    f"SHA-256 前缀碰撞: {package_id} 已指向不同 ZIP，拒绝复用。")
            # 复用：不重写 ZIP，只加引用计数与来源。
            reused = True
            existing["ref_count"] = int(existing.get("ref_count") or 1) + 1
            sources = list(existing.get("sources") or [])
            sources.append({"session": source_session or "",
                            "workspace": source_workspace or workspace or "",
                            "at": now,
                            "filename": original_filename or stored_name})
            existing["sources"] = sources
            existing["updated_at"] = now
            record = dict(existing)
        else:
            record = _blank_record(package_id, original_filename, digest, size,
                                   now, source_session)
            record["archive_relpath"] = _rel_from_root(root, destination)
            record["stored_filename"] = stored_name
            record["sources"] = [{"session": source_session or "",
                                  "workspace": source_workspace or workspace or "",
                                  "at": now,
                                  "filename": original_filename or stored_name}]
            record["updated_at"] = now
            packages[package_id] = record
        record = dict(record)
        record["reused"] = reused
        return json.loads(json.dumps(record))

    return _mutate(root, mutate)


# ---------------------------------------------------------------------------
# ZIP 条目安全检查
# ---------------------------------------------------------------------------

def _normalize_member(name: str) -> tuple[str, bool]:
    """把 ZIP 条目名规范化成安全的相对路径，返回 ``(posix 相对路径, 是否目录)``。

    拒绝清单（对应模块头"安全"一节的第 2 条）：

    - 空名、含 NUL 的名字；
    - ``/x`` 绝对路径、``\\\\host\\share`` UNC；
    - ``C:\\x`` / ``C:x`` 盘符路径（含中间段形式的 ``foo/C:/bar``）；
    - 任何一段是 ``..``（路径穿越）；
    - 任意一段由点组成（``...``）—— POSIX 允许但 Windows 会当成怪名字，
      跨平台行为不一致，一律拒；
    - 段尾部的点或空格 —— Windows 打开时会静默截掉，
      ``evil.exe.`` 与 ``evil.exe`` 会落成同一个文件（一个绕过校验的经典手法）；
    - 超过深度 / 长度上限。
    """
    raw = str(name or "")
    if not raw.strip():
        raise UnsafeArchive("压缩包内存在空文件名条目")
    if "\x00" in raw:
        raise UnsafeArchive(f"压缩包内文件名含 NUL 字符: {raw!r}")

    unified = raw.replace("\\", "/")
    if unified.startswith("//"):
        raise UnsafeArchive(f"拒绝 UNC 路径条目: {raw!r}")
    if unified.startswith("/"):
        raise UnsafeArchive(f"拒绝绝对路径条目: {raw!r}")
    if re.match(r"^[A-Za-z]:", unified):
        raise UnsafeArchive(f"拒绝盘符路径条目: {raw!r}")

    parts: list = []
    for segment in unified.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise UnsafeArchive(f"拒绝路径穿越条目: {raw!r}")
        if segment.endswith((".", " ")):
            raise UnsafeArchive(
                f"拒绝以点或空格结尾的路径段: {raw!r}"
                f"（Windows 会静默截掉，可能造成文件覆盖）")
        if re.fullmatch(r"[A-Za-z]:", segment):
            raise UnsafeArchive(f"拒绝路径中的盘符段: {raw!r}")
        if len(segment) > 255:
            raise UnsafeArchive(f"路径段过长（{len(segment)} 字符）: {raw!r}")
        parts.append(segment)

    if not parts:
        raise UnsafeArchive(f"压缩包内存在空路径条目: {raw!r}")

    relative = posixpath.join(*parts)
    return relative, unified.endswith("/")


def _entry_mode(info: zipfile.ZipInfo) -> int:
    """``external_attr`` 高 16 位是 Unix 模式（create_system==3 才有意义）。"""
    mode = (info.external_attr >> 16) & 0xFFFF
    return mode if info.create_system == 3 else 0


def _validate_entry(info: zipfile.ZipInfo, limits: dict) -> tuple[str, bool]:
    """单条目检查，返回 ``(相对路径, 是否目录)``。不通过就抛 :class:`UnsafeArchive`。"""
    relative, is_dir = _normalize_member(info.filename)

    mode = _entry_mode(info)
    if mode:
        if stat.S_ISLNK(mode):
            # 符号链接：解压出来指向包外的话，后续任何"读这个文件"的操作都会
            # 变成任意文件读取。宁可不支持。
            raise UnsafeArchive(f"拒绝符号链接条目: {relative}")
        if stat.S_ISDIR(mode):
            is_dir = True
        elif not stat.S_ISREG(mode):
            raise UnsafeArchive(
                f"拒绝非普通文件/目录条目: {relative}"
                f"（模式 {oct(mode)}，可能是设备/FIFO/socket）")

    if len(relative.split("/")) > limits["max_path_depth"]:
        raise UnsafeArchive(
            f"路径层数超过上限 {limits['max_path_depth']}: {relative}")
    if len(relative) > limits["max_rel_path_len"]:
        raise UnsafeArchive(
            f"路径长度超过上限 {limits['max_rel_path_len']}: {relative}")

    if is_dir:
        return relative, True

    if info.file_size > limits["max_file_bytes"]:
        raise UnsafeArchive(
            f"文件超过单文件上限 {limits['max_file_bytes']} 字节: {relative}"
            f"（声明 {info.file_size}）")
    if info.compress_size > 0:
        ratio = info.file_size / float(info.compress_size)
        if ratio > limits["max_compress_ratio"]:
            raise UnsafeArchive(
                f"压缩比 {ratio:.0f}:1 超过上限 {limits['max_compress_ratio']}:1: "
                f"{relative}（解压 {info.file_size} 字节 / 压缩 "
                f"{info.compress_size} 字节）—— 疑似 zip bomb")
    return relative, is_dir


def _plan_entries(archive: zipfile.ZipFile, limits: dict) -> list:
    """**先**把整个中央目录检查一遍，再决定要不要落盘。

    顺序很重要：``extractall`` 边读边写，中途才发现穿越条目时前面的文件已经
    写到磁盘上了（虽然写在暂存目录里，但仍会占空间、可能被别的进程看到）。
    全量前置检查让"这个包安不安全"成为一个**先于任何写操作**的判断。
    """
    infos = archive.infolist()
    if len(infos) > limits["max_entries"]:
        raise UnsafeArchive(
            f"条目数 {len(infos)} 超过上限 {limits['max_entries']}（疑似 zip bomb）")

    plan: list = []
    total = 0
    seen: set = set()
    for info in infos:
        relative, is_dir = _validate_entry(info, limits)
        if not is_dir:
            if relative in seen:
                raise UnsafeArchive(f"压缩包内路径重复: {relative}")
            seen.add(relative)
            total += info.file_size
            if total > limits["max_total_bytes"]:
                raise UnsafeArchive(
                    f"累计解压大小超过上限 {limits['max_total_bytes']} 字节: "
                    f"{relative}（已累计 {total}）")
        plan.append((info, relative, is_dir))
    return plan


def _open_zip(path: str) -> zipfile.ZipFile:
    """打开 ZIP 并确认格式真实可信。

    ``zipfile`` 读到坏中央目录会抛 ``BadZipFile``；文件被截断（中央目录在末尾）
    也一样。这里统一翻译成 :class:`UnsafeArchive` 并带上足够定位的信息 ——
    "包坏了"必须说清是哪个文件坏了，而不是笼统的"导入失败"。
    """
    try:
        with open(path, "rb") as stream:
            head = stream.read(4)
    except OSError as exc:
        raise ModelStoreError(f"无法读取压缩包: {exc}") from exc
    if head not in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        raise UnsafeArchive(
            f"不是有效的 ZIP（首字节 {head!r}）。文件可能已损坏或被截断。")
    try:
        return zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile as exc:
        raise UnsafeArchive(f"ZIP 中央目录无法读取（文件损坏或被截断）: {exc}") from exc
    except zipfile.LargeZipFile as exc:
        raise UnsafeArchive(f"ZIP 需要 ZIP64 支持，本版本不处理: {exc}") from exc


# ---------------------------------------------------------------------------
# 包类型识别（只读中央目录，不解压）
# ---------------------------------------------------------------------------

def _histogram(names: list) -> dict:
    """扩展名统计（降序，最多 40 项 —— 清单不能被几百个扩展名撑爆）。"""
    counts: dict = {}
    for name in names:
        _, ext = posixpath.splitext(name)
        ext = ext.lower() or "<无扩展名>"
        counts[ext] = counts.get(ext, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(ordered[:40])


def _kit_root_candidates(plan: list) -> tuple[list, list]:
    """找候选"套件根目录" -> ``(强候选, 全部候选)``。

    真实包里根目录可能是**两层同名嵌套**
    （实测 ``TDK_Component_Library_v56/TDK_Component_Library_v56/``：
    外层是发布目录，内层才是真正含 ``lib.defs`` 的套件目录）。
    所以强候选 = 直接含 ``lib.defs`` 或 ``design_kit/ads.lib`` 的那个目录，
    弱候选 = 顶层目录。

    分强弱是必要的：顶层目录**只有一个**时它几乎肯定是套件根
    （发布包习惯整体裹一层），而强候选多于一个说明包里有多套东西 ——
    这时武断选一个会让后续所有库引用指向错误位置，必须交给用户选。
    """
    strong: set = set()
    top: set = set()
    for _info, relative, is_dir in plan:
        if is_dir:
            continue
        parts = relative.split("/")
        if len(parts) > 1:
            top.add(parts[0])
        base = parts[-1].lower()
        if base == _DK_LIB_DEFS and len(parts) > 1:
            strong.add("/".join(parts[:-1]))
        if base == "ads.lib" and len(parts) > 1 and parts[-2].lower() == "design_kit":
            strong.add("/".join(parts[:-2]))
    ordered_strong = sorted(strong)
    ordered_all = ordered_strong + sorted(t for t in top if t not in strong)
    return ordered_strong, ordered_all


def _read_member_bytes(archive: zipfile.ZipFile, info: zipfile.ZipInfo,
                       limit: int) -> str:
    """读一个小文本条目（README / lib.defs），超限就**截断并如实标注**。

    只读不超过 ``limit`` 字节：一个 500 MB 的 .txt 不该让识别步骤吃掉内存。
    解码用 utf-8 + replace —— 原厂文档里有非 UTF-8 的机种字符是常态，
    抛异常等于整个包识别不出来。
    """
    try:
        with archive.open(info) as stream:
            raw = stream.read(max(0, limit) + 1)
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError):
        return ""
    truncated = len(raw) > limit
    text = raw[:limit].decode("utf-8", "replace")
    if truncated:
        text += "\n[内容超长，已截断]"
    return text


def _doc_members(plan: list) -> list:
    """可能是"包内文档"的条目（按名字判定，只用于**提取字段**）。

    注意纪律：这些文本永远只当**数据**，提取出的版本串/厂商声明进记录字段，
    绝不参与任何指令构造。包内文本是供应链攻击面，不能给它执行权。
    """
    picked: list = []
    for info, relative, is_dir in plan:
        if is_dir:
            continue
        base = posixpath.basename(relative).lower()
        if base.startswith(("readme", "changelog", "changes", "release")):
            picked.append((info, relative))
        elif base.endswith((".md", ".txt")) and info.file_size <= DEFAULT_INDEX_LIMITS["max_doc_bytes"]:
            picked.append((info, relative))
        if len(picked) >= 12:
            break
    return picked


_VERSION_RE = re.compile(
    r"(?i)\b(?:version|release|revision)\s*(?:number|no\.?|#)?\s*[:=]?\s*v?(\d+(?:\.\d+)+[a-z]?)")
_VERSION_LOOSE_RE = re.compile(
    r"(?i)\b(?:version|release)\s*(?:number|no\.?|#)?\s*[:=]?\s*v(\d+(?:\.\d+)*)\b")
_ADS_LIB_VERSION_RE = re.compile(
    r"(?i)\|\s*(?:path_to_design_kit_directory|[^|]*)\s*\|\s*[^|]*\|\s*\"?v?([0-9][\w.]*)\"?\s*$")

# 厂商必须来自包内**明确文本**：版权声明里的 "by XXX" 或 vendor/manufacturer 字段。
# 目录名、库名、文件名都不算 —— 那是猜的。
_COPYRIGHT_BY_RE = re.compile(r"(?i)\bcopyright\b[^\n\r]{0,120}?\bby\s+([^,\n\r]{2,60})")
_VENDOR_FIELD_RE = re.compile(
    r"(?i)\b(?:vendor|supplier|manufacturer|brand)\b\s*[:=]\s*([^,\n\r]{2,60})")
_ORG_HINT_RE = re.compile(
    r"(?i)\b(inc|ltd|llc|gmbh|ag|corp|corporation|technolog|technologies|"
    r"electron|semiconductor|photonics|micro|co\.|s\.a\.|n\.v\.|kk|co\.,?\s*ltd)\b")


def _clean_org(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip(" \t.,;:")
    value = re.sub(r"\(.*?\)", "", value).strip()
    return value


def _vendor_from_text(text: str) -> list:
    """从包内文本抽厂商声明。抽不到就返回空列表（**不是**返回目录名）。

    返回 [{"vendor": ..., "source": ..., "excerpt": ...}]，便于界面上点开看依据。
    """
    found: list = []
    seen: set = set()
    for pattern in (_COPYRIGHT_BY_RE, _VENDOR_FIELD_RE):
        for match in pattern.finditer(text or ""):
            name = _clean_org(match.group(1))
            # 必须长得像组织名，否则 "by the user" 之类会把垃圾写进 vendor。
            if len(name) < 3 or len(name) > 60 or not _ORG_HINT_RE.search(name):
                continue
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            excerpt = text[line_start:line_end if line_end > 0 else len(text)].strip()
            found.append({"vendor": name, "excerpt": excerpt[:200]})
            if len(found) >= 4:
                return found
    return found


def _version_from_docs(plan: list, archive: zipfile.ZipFile) -> list:
    """从包内文档抽版本串。**只认明确写出来的版本**，提不到就是 None。

    接受两种写法：``Version 2.1``（宽松，允许 ``v2.1``）与
    ``Version = 2.1`` / ``Version: 2.1``。刻意不接受裸的 ``v56`` / ``2019.10``：
    那可能出现在文件名、URL、版权年份里，误采会让版本字段变成随机数。
    """
    results: list = []
    for info, relative in _doc_members(plan):
        text = _read_member_bytes(archive, info,
                                  DEFAULT_INDEX_LIMITS["max_doc_bytes"])
        if not text:
            continue
        for pattern in (_VERSION_RE, _VERSION_LOOSE_RE):
            match = pattern.search(text)
            if match:
                value = match.group(1)
                if value not in [r["version"] for r in results]:
                    line_start = text.rfind("\n", 0, match.start()) + 1
                    line_end = text.find("\n", match.end())
                    results.append({
                        "version": value,
                        "source": relative,
                        "excerpt": text[line_start:line_end if line_end > 0
                                        else len(text)].strip()[:200],
                    })
                break
        if len(results) >= 4:
            break
    return results


def _parse_lib_defs(text: str) -> list:
    """解析 ``lib.defs``：``DEFINE <库名> <路径>`` / ``ASSIGN <库名> libMode <模式>``。

    库名带 ``#xx`` 十六进制转义（实测 ``TDK_Component_Library_v2019#2e10``），
    必须解码，否则拿到的名字和磁盘目录对不上。
    解析不出来就返回空列表 —— 不按目录名反推库名。
    """
    def unescape(name: str) -> str:
        try:
            return _LIB_NAME_ESCAPE_RE.sub(
                lambda m: chr(int(m.group(1), 16)), str(name))
        except ValueError:
            return str(name)

    libraries: list = []
    modes: dict = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 3 and parts[0].upper() == "DEFINE":
            libraries.append({"name": unescape(parts[1]),
                              "path": parts[2].lstrip("./")})
        elif len(parts) >= 4 and parts[0].upper() == "ASSIGN" \
                and parts[2].lower() == "libmode":
            modes[unescape(parts[1])] = parts[3]
    for library in libraries:
        library["lib_mode"] = modes.get(library["name"])
    return libraries


def _parse_eesof_cfg(text: str) -> dict:
    """解析 ``eesof_lib.cfg`` 的 ``KEY=VALUE`` 文本。

    容忍空行、``#``/``;`` 整行注释、值两侧的引号；只按**第一个** ``=`` 切分，
    这样含 ``=`` 的路径不会被切坏。键统一大写以便大小写不敏感地取值。
    解析不出任何键就返回空 dict —— 不按目录名/其它文件反推（不许猜）。
    """
    parsed: dict = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip().upper()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
        parsed[key] = value
    return parsed


def _nl_is_external(value: str) -> bool:
    """cfg 里的值是否指向包外（环境变量 ``$``/``%``、绝对路径、盘符）。"""
    if "$" in value or "%" in value:
        return True
    if value[:1] in ("/", "\\"):
        return True
    return bool(re.match(r"^[A-Za-z]:", value))


def _nl_resolve(value, cfg_dir: str, label: str, limits: list) -> list:
    """把 cfg 里的一个值（可能 ``;`` 分隔多个）解析成**包根相对**路径列表。

    解析基准是 ``eesof_lib.cfg`` 所在目录 ``cfg_dir``（包根相对、posix 风格），
    **不是** 进程 cwd（踩过坑：``BOOT_AEL=../de/ael/boot`` 相对 cwd 会指到别处）。
    包外 / 落在包根之外的段记进 ``limits`` 并跳过 —— 不猜、不裁剪。
    """
    resolved: list = []
    for segment in str(value or "").split(";"):
        segment = segment.strip().strip("\"'")
        if not segment:
            continue
        if _nl_is_external(segment):
            limits.append(f"{label} 指向包外路径，未解析: {segment!r}")
            continue
        joined = posixpath.normpath(
            posixpath.join(cfg_dir, segment.replace("\\", "/")))
        if joined in (".", ""):
            continue
        if joined == ".." or joined.startswith("../"):
            limits.append(f"{label} 落在包根之外，未采用: {segment!r}")
            continue
        resolved.append(joined)
    return resolved


def _scan_native_list(archive: zipfile.ZipFile, plan: list) -> dict:
    """静态检测「原生元件列表（Palette）所需资源」，形状见契约 §3.4。

    纯本地文件系统 + 中央目录读取，**不依赖 ADS、不解压、不执行任何包内脚本**。
    ``eesof_lib.cfg`` 的 ``BOOT_AEL`` 值通常不带扩展名（实测 ``../de/ael/boot``，
    真实文件 ``boot.ael``/``boot.atf``），所以按 值原样 → 补 ``.ael`` → 补 ``.atf``
    依次查找并记录命中的相对路径。所有返回路径均为**包根相对** posix 路径。
    """
    by_name = {relative: info for info, relative, _d in plan}
    names = [relative for _i, relative, is_dir in plan if not is_dir]

    # zip 常不写显式目录条目，从文件路径逐级补出父目录集合。
    dirs: set = set()
    for _info, relative, is_dir in plan:
        parts = relative.split("/")
        prefix = parts if is_dir else parts[:-1]
        for i in range(1, len(prefix) + 1):
            dirs.add("/".join(prefix[:i]))

    native = {
        "eesof_lib_cfg": [],
        "boot_ael": [],
        "palette_ael": [],
        "atf": [],
        "bitmaps": [],
        "browser_ctl": [],
        "lib_defs": [],
        "data_paths": [],
        "design_kit_name": "",
        "version": "",
        "tech_desc": "",
        "has_palette_assets": False,
        "evidence": [],
        "limits": [_NL_DEFAULT_LIMIT],
    }
    evidence: list = native["evidence"]
    limits: list = native["limits"]

    cfg_paths = sorted(n for n in names
                       if posixpath.basename(n).lower() == _NL_CFG_NAME)
    native["eesof_lib_cfg"] = cfg_paths[:_NL_LIST_CAP]
    if len(cfg_paths) > _NL_LIST_CAP:
        limits.append(f"eesof_lib.cfg 共 {len(cfg_paths)} 个，只登记前 {_NL_LIST_CAP} 个")

    native["lib_defs"] = sorted(
        n for n in names if posixpath.basename(n).lower() == _DK_LIB_DEFS)

    atf = [n for n in names if n.lower().endswith(_DK_ATF)]
    native["atf"] = atf[:_NL_LIST_CAP]
    if len(atf) > _NL_LIST_CAP:
        limits.append(f".atf 编译产物共 {len(atf)} 个，只登记前 {_NL_LIST_CAP} 个")

    browser = [n for n in names if n.lower().endswith(_NL_BROWSER_EXTS)]
    native["browser_ctl"] = browser[:_NL_LIST_CAP]
    if len(browser) > _NL_LIST_CAP:
        limits.append(f"浏览器分类文件共 {len(browser)} 个，只登记前 {_NL_LIST_CAP} 个")

    native["bitmaps"] = sorted(
        d for d in dirs if _NL_BITMAP_HINT in posixpath.basename(d).lower()
    )[:_NL_LIST_CAP]

    # palette 定义文件按名扫描，.ael 源与 .atf 编译产物都认（.ael 排前）。
    # 真实包可能只发 .atf（实测 TDK v2019.10 无任何 .ael），少了 .atf 回退会把
    # "只有 palette.atf"的包误判成缺 palette 资产。
    palette_defs = sorted(
        n for n in names
        if n.lower().endswith(_NL_PALETTE_EXTS)
        and _NL_PALETTE_HINT in posixpath.basename(n).lower())
    palette_defs.sort(key=lambda n: not n.lower().endswith(".ael"))
    native["palette_ael"] = palette_defs[:_NL_LIST_CAP]
    if palette_defs:
        evidence.append(f"palette 定义文件: {', '.join(palette_defs[:8])}")

    if not cfg_paths:
        limits.append("包内没有 eesof_lib.cfg —— 无法确定启动脚本与 palette 资源")
        return native

    # 选主 cfg：优先能解析出 DESIGN_KIT_NAME 或 BOOT_AEL 的那个。
    primary, primary_cfg = cfg_paths[0], {}
    for path in cfg_paths:
        info = by_name.get(path)
        if info is None:
            continue
        cfg = _parse_eesof_cfg(
            _read_member_bytes(archive, info, DEFAULT_INDEX_LIMITS["max_doc_bytes"]))
        if not primary_cfg:
            primary, primary_cfg = path, cfg
        if cfg.get(_NL_BOOT_VALUE_KEY) or cfg.get("DESIGN_KIT_NAME"):
            primary, primary_cfg = path, cfg
            break
    if len(cfg_paths) > 1:
        limits.append(f"包内有多个 eesof_lib.cfg，以 {primary!r} 为准")
    if not primary_cfg:
        limits.append(f"eesof_lib.cfg 无法读取或为空: {primary!r}")
        return native

    cfg_dir = posixpath.dirname(primary)
    evidence.append(f"eesof_lib.cfg: {primary}")
    native["design_kit_name"] = primary_cfg.get("DESIGN_KIT_NAME", "")
    native["version"] = primary_cfg.get("VERSION", "")
    native["tech_desc"] = primary_cfg.get("TECH_DESC", "")
    for key in ("DESIGN_KIT_NAME", "VERSION", "TECH_DESC"):
        if primary_cfg.get(key):
            evidence.append(f"{key}={primary_cfg[key]}（来自 {primary}）")

    # BOOT_AEL：值不带扩展名，按 原样 / +.ael / +.atf 依次查找。
    boot_value = primary_cfg.get(_NL_BOOT_VALUE_KEY, "")
    if boot_value:
        boot_hits: list = []
        for base in _nl_resolve(boot_value, cfg_dir, _NL_BOOT_VALUE_KEY, limits):
            for candidate in (base, base + _NL_BOOT_EXTS[0], base + _NL_BOOT_EXTS[1]):
                if candidate in by_name and candidate not in boot_hits:
                    boot_hits.append(candidate)
        native["boot_ael"] = boot_hits[:_NL_LIST_CAP]
        if boot_hits:
            evidence.append(f"BOOT_AEL={boot_value} → {', '.join(boot_hits)}")
        else:
            limits.append(f"BOOT_AEL={boot_value!r} 指向的启动脚本在包内未找到")
    else:
        limits.append("eesof_lib.cfg 未声明 BOOT_AEL —— 无启动脚本")

    # LIB_BROWSER_CTL：命中的 .ctl 提到 browser_ctl 清单最前。
    ctl_value = primary_cfg.get(_NL_BROWSER_KEY, "")
    if ctl_value:
        resolved_ctl = [p for p in _nl_resolve(ctl_value, cfg_dir, _NL_BROWSER_KEY, limits)
                        if p in by_name]
        if resolved_ctl:
            merged = resolved_ctl + [p for p in native["browser_ctl"]
                                     if p not in resolved_ctl]
            native["browser_ctl"] = merged[:_NL_LIST_CAP]
            evidence.append(f"LIB_BROWSER_CTL={ctl_value} → {', '.join(resolved_ctl)}")
        elif ctl_value.strip():
            limits.append(f"LIB_BROWSER_CTL={ctl_value!r} 指向的 .ctl 不在包内")

    # INPUT_DATA_PATH：声明的模型/数据搜索目录。
    data_value = primary_cfg.get(_NL_DATA_KEY, "")
    if data_value:
        data_paths = _nl_resolve(data_value, cfg_dir, _NL_DATA_KEY, limits)
        native["data_paths"] = data_paths[:_NL_LIST_CAP]
        if data_paths:
            evidence.append(f"INPUT_DATA_PATH={data_value} → {', '.join(data_paths)}")
            for path in data_paths:
                if path not in dirs and not any(
                        n.startswith(path + "/") for n in names):
                    limits.append(f"INPUT_DATA_PATH 声明的目录在包内未找到: {path}")

    # TEMPLATES_DIRECTORY：契约无对应键，只作为可追溯证据登记。
    templates = primary_cfg.get(_NL_TEMPLATES_KEY, "")
    if templates:
        resolved_tpl = _nl_resolve(templates, cfg_dir, _NL_TEMPLATES_KEY, limits)
        if resolved_tpl:
            evidence.append(
                f"TEMPLATES_DIRECTORY={templates} → {', '.join(resolved_tpl)}")

    # 有 boot（启动脚本）且 有 palette 资产（palette.ael 或 bitmaps）才为 True。
    has_boot = bool(native["boot_ael"])
    has_palette = bool(native["palette_ael"]) or bool(native["bitmaps"])
    native["has_palette_assets"] = bool(has_boot and has_palette)
    if not native["has_palette_assets"]:
        missing: list = []
        if not has_boot:
            missing.append("启动脚本 boot")
        if not has_palette:
            missing.append("palette 资产(palette.ael/palette.atf/bitmaps)")
        limits.append("缺 " + "、".join(missing) + " —— 原生元件列表可能无法加载")
    return native


def _scan_archive_structure(archive: zipfile.ZipFile, plan: list) -> dict:
    """只读中央目录得出结构事实（不解压、不执行）。"""
    # 规范化后的相对路径 -> ZipInfo。**必须自己建索引**：
    # ``archive.getinfo()`` 只认 ZIP 里的原始条目名，而这里的 name 已经过了
    # 路径规范化（反斜杠改斜杠、去掉空段），两者不一定相同，直接 getinfo 会 KeyError。
    by_name: dict = {relative: info for info, relative, _d in plan}
    names = [relative for _i, relative, is_dir in plan if not is_dir]

    touchstone = [n for n in names if _TOUCHSTONE_RE.search(n)]
    lib_defs = [n for n in names if posixpath.basename(n).lower() == _DK_LIB_DEFS]
    ads_lib = [n for n in names
               if posixpath.basename(n).lower() == "ads.lib"
               and "design_kit" in n.lower()]
    atf = [n for n in names if n.lower().endswith(_DK_ATF)]
    ael = [n for n in names if n.lower().endswith(".ael")]
    ctl = [n for n in names if n.lower().endswith(_DK_CTL)]
    libfiles = [n for n in names if n.lower().endswith(_DK_OALIB)]
    scripts = [n for n in names
               if os.path.splitext(n)[1].lower() in _EXECUTABLE_EXTS]
    foreign: dict = {}
    for name in names:
        ext = os.path.splitext(name)[1].lower()
        if ext in _FOREIGN_MODEL_EXTS:
            foreign.setdefault(_FOREIGN_MODEL_EXTS[ext].strip(), []).append(name)

    strong_roots, kit_roots = _kit_root_candidates(plan)

    libraries: list = []
    sub_libraries: list = []
    for name in lib_defs:
        text = _read_member_bytes(archive, by_name[name],
                                  DEFAULT_INDEX_LIMITS["max_doc_bytes"])
        libraries.extend(_parse_lib_defs(text))
    for name in ctl[:4]:
        text = _read_member_bytes(archive, by_name[name],
                                  DEFAULT_INDEX_LIMITS["max_doc_bytes"])
        for match in re.finditer(
                r"(?is)<SUBLIBRARY>\s*<NAME>(.*?)</NAME>", text):
            label = _clean_org(match.group(1))
            if label and label not in sub_libraries:
                sub_libraries.append(label)
        if len(sub_libraries) >= 200:
            break

    roots = sorted({n.split("/")[0] for n in by_name})
    return {
        "entry_count": len(plan),
        "file_count": len(names),
        "total_uncompressed": sum(info.file_size for info, _r, _d in plan),
        "ext_histogram": _histogram(names),
        "touchstone_count": len(touchstone),
        "touchstone_sample": touchstone[:5],
        "lib_defs_paths": lib_defs,
        "ads_lib_paths": ads_lib,
        "atf_count": len(atf),
        "ael_count": len(ael),
        "ctl_count": len(ctl),
        "libfile_count": len(libfiles),
        "script_files": scripts[:20],
        "script_count": len(scripts),
        "foreign_models": {k: len(v) for k, v in foreign.items()},
        "kit_root_strong": strong_roots[:8],
        "kit_root_candidates": kit_roots[:8],
        "defined_libraries": libraries[:40],
        "sub_libraries": sub_libraries,
        "roots": roots[:8],
        "single_root": len(roots) == 1,
    }


def _pick_kit_root(stats: dict) -> str | None:
    """选定套件根目录。**只在唯一时选**，多于一个强候选时返回 None。

    返回 None 表示"需要用户选"（记录里 ``kit_root_ambiguous`` 会标 True，
    状态机也据此建议进 ``awaiting_user``）。选错套件根目录的后果是后续
    所有 ``DEFINE`` 库引用都指向不存在的路径 —— 表现为"库挂接了但元件
    一个都调不出来"，很难排查。
    """
    strong = list(stats.get("kit_root_strong") or [])
    if len(strong) == 1:
        return strong[0]
    if strong:
        return None
    # 没有强信号但整包只有一个根目录：那个目录就是套件根（发布包习惯整体裹一层）
    candidates = list(stats.get("kit_root_candidates") or [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def _classify(stats: dict) -> tuple[str, str, list, list]:
    """判定包类型 -> ``(kind, confidence, evidence, candidates)``。

    判定顺序是有讲究的：**Design Kit 的结构信号优先于 Touchstone 文件数**。
    实测 Infineon v2.1 里有 8064 个 ``.s2p``，只看文件数会判成"Touchstone 文件包"，
    但它同时有 ``lib.defs``（``DEFINE Infineon_RF ./Infineon_RF``）和
    ``design_kit/ads.lib``（``Infineon_RF | ... | v2.1``）—— 这是**需要挂接的
    Design Kit，里面的 .s2p 是套件的数据文件**。两者的使用方式完全不同
    （一个拷进 data 目录就能用，一个必须先在 ADS 里挂接库），所以必须判成
    Design Kit 并在 evidence 里写清"内含 N 个 Touchstone 数据文件"。
    """
    evidence: list = []
    candidates = list(stats.get("kit_root_candidates") or [])
    strong_roots = list(stats.get("kit_root_strong") or [])

    if stats.get("lib_defs_paths"):
        evidence.append(f"含 lib.defs 套件库定义（{', '.join(stats['lib_defs_paths'][:3])}）")
    if stats.get("ads_lib_paths"):
        evidence.append(f"含 design_kit/ads.lib 套件描述（{', '.join(stats['ads_lib_paths'][:3])}）")
    if stats.get("atf_count"):
        evidence.append(f"含 {stats['atf_count']} 个 .atf 元件库文件")
    if stats.get("ael_count"):
        evidence.append(f"含 {stats['ael_count']} 个 .ael AEL 文件")
    if stats.get("libfile_count"):
        evidence.append(f"含 {stats['libfile_count']} 个库文件（.oalib/.library/.lib）")
    if stats.get("ctl_count"):
        evidence.append(f"含 {stats['ctl_count']} 个 .ctl 库/子库分类文件"
                        f"（子库 {len(stats.get('sub_libraries') or [])} 个）")
    if stats.get("touchstone_count"):
        evidence.append(f"含 {stats['touchstone_count']} 个 Touchstone 数据文件"
                        f"（{', '.join(stats.get('touchstone_sample') or [])[:200]}）")
    if stats.get("foreign_models"):
        evidence.append("含其它仿真器模型文件: "
                        + ", ".join(f"{k}×{v}" for k, v in stats["foreign_models"].items()))
    if stats.get("script_count"):
        evidence.append(f"含 {stats['script_count']} 个脚本/可执行文件"
                        f"（只落盘，不执行）")
    if stats.get("single_root"):
        evidence.append(f"单一根目录 {stats['roots'][0]}/")
    # 两层同名嵌套根目录（实测 TDK v56）值得单独点出来：挂接时必须用**内层**
    # 目录作为套件根，用外层会找不到 lib.defs。
    for candidate in strong_roots[:3]:
        parts = candidate.split("/")
        if len(parts) > 1 and parts[-1] == parts[-2]:
            evidence.append(
                f"套件根为两层同名嵌套 {candidate}/（外层是发布目录，"
                f"挂接时以内层为根）")

    is_dk = bool(stats.get("lib_defs_paths") or stats.get("ads_lib_paths")
                 or stats.get("atf_count"))
    has_ts = bool(stats.get("touchstone_count"))
    has_foreign = bool(stats.get("foreign_models"))

    if len(strong_roots) > 1:
        evidence.append(f"存在多个候选套件根目录（{', '.join(strong_roots[:4])}），"
                        f"需要用户确认后再导入")
        return KIND_UNKNOWN, "low", evidence, candidates

    if is_dk:
        if has_ts:
            evidence.append("判定为 Design Kit：套件结构信号优先于 Touchstone 文件数"
                            "（.s2p 是套件数据文件，需先挂接库才能用）")
            return KIND_DESIGN_KIT, "high", evidence, candidates
        return KIND_DESIGN_KIT, "high" if (stats.get("lib_defs_paths")
                                           or stats.get("ads_lib_paths")) else "medium", \
            evidence, candidates

    if has_ts and has_foreign:
        return KIND_MIXED, "high", evidence, candidates
    if has_ts:
        return KIND_TOUCHSTONE, "high", evidence, candidates
    if has_foreign:
        return KIND_MIXED, "medium", evidence, candidates
    if stats.get("libfile_count") or stats.get("ctl_count"):
        return KIND_UNKNOWN, "low", evidence, candidates
    return KIND_UNKNOWN, "low", evidence, candidates


def scan_archive(root: str, package_id: str) -> dict:
    """只读中央目录做识别，把结果写回记录并返回。

    **不解压、不执行**：几百 MB 的包也能在 1 秒内出识别结论。
    不改状态：识别是幂等的只读动作，用户可能反复点；状态由调用方按流程推进。
    """
    root = _ensure_root(root)
    record = get_package(root, package_id)
    archive_path = _abs_from_rel(root, record.get("archive_relpath"))
    if not archive_path or not os.path.isfile(archive_path):
        raise ModelStoreError(
            f"原始 ZIP 不存在: {archive_path or record.get('archive_relpath')}")

    with _open_zip(archive_path) as archive:
        plan = _plan_entries(archive, DEFAULT_LIMITS)
        stats = _scan_archive_structure(archive, plan)
        kind, confidence, evidence, candidates = _classify(stats)
        by_name = {relative: info for info, relative, _d in plan}
        native_list = _scan_native_list(archive, plan)
        vendor_hits: list = []
        version_hits = _version_from_docs(plan, archive)
        for info, relative in _doc_members(plan):
            text = _read_member_bytes(archive, info,
                                      DEFAULT_INDEX_LIMITS["max_doc_bytes"])
            vendor_hits.extend(
                {**hit, "source": relative} for hit in _vendor_from_text(text))
            if len(vendor_hits) >= 4:
                break
        kit_version = ""
        for name in stats.get("ads_lib_paths") or []:
            info = by_name.get(name)
            if info is None:
                continue
            for line in _read_member_bytes(
                    archive, info, DEFAULT_INDEX_LIMITS["max_doc_bytes"]).splitlines():
                match = _ADS_LIB_VERSION_RE.search(line.strip())
                if match:
                    kit_version = match.group(1)
                    break
            if kit_version:
                version_hits.append({
                    "version": kit_version,
                    "source": name,
                    "excerpt": f"{kit_version}（design_kit/ads.lib 声明的套件版本）",
                })
                break

    version = version_hits[0]["version"] if version_hits else None
    vendor = vendor_hits[0]["vendor"] if vendor_hits else None

    validation = dict(record.get("validation") or {})
    validation["scan"] = stats
    validation["scanned_at"] = utc_now()
    validation["executed_package_scripts"] = False
    validation["package_text_treated_as"] = "data-only"

    fields = {
        "package_kind": kind,
        "kind_confidence": confidence,
        "kind_evidence": evidence,
        "kind_candidates": candidates,
        "vendor": vendor,
        "vendor_evidence": vendor_hits,
        "version": version,
        "version_evidence": version_hits,
        "validation": validation,
        "native_list": native_list,
    }
    if stats.get("defined_libraries") or stats.get("kit_root_candidates"):
        attach = dict(record.get("library_attach") or {})
        attach.update({
            "kit_root": _pick_kit_root(stats),
            "kit_root_candidates": stats.get("kit_root_candidates") or [],
            "kit_root_ambiguous": len(stats.get("kit_root_strong") or []) > 1,
            "defined_libraries": stats.get("defined_libraries") or [],
            "sub_libraries": stats.get("sub_libraries") or [],
            "attached": attach.get("attached", False),
            "attached_at": attach.get("attached_at", ""),
        })
        fields["library_attach"] = attach

    updated = update_package(root, package_id, **fields)
    updated["inspection"] = stats
    return updated


# ---------------------------------------------------------------------------
# 安全解压
# ---------------------------------------------------------------------------

def _commit_dir(staging: str, target: str) -> None:
    """把暂存目录提交为正式目录：优先 rename（同盘原子），不行再退避复制。

    rename 的好处是**要么全有要么全无**：不会出现"解压到一半的 extracted/"
    被别的模块当成完整资产去挂接。
    """
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    if not os.path.exists(target):
        try:
            os.replace(staging, target)
            return
        except OSError:
            pass
    else:
        # 重解压：先把旧目录挪开再换。挪不开就只能退避复制。
        trash = os.path.join(_tmp_dir_of(target), f"old_{secrets.token_hex(6)}")
        os.makedirs(os.path.dirname(trash), exist_ok=True)
        try:
            os.replace(target, trash)
        except OSError:
            trash = ""
        try:
            os.replace(staging, target)
        except OSError:
            if trash:
                try:
                    os.replace(trash, target)
                except OSError:
                    pass
            shutil.copytree(staging, target)
            shutil.rmtree(staging, ignore_errors=True)
        else:
            if trash:
                shutil.rmtree(trash, ignore_errors=True)
        return

    # target 不存在但 rename 失败（跨盘 / 被占用）：退避后复制到临时名再改名。
    staging2 = os.path.join(parent, f".commit_{secrets.token_hex(6)}")
    last: OSError | None = None
    for _ in range(10):
        try:
            shutil.copytree(staging, staging2)
            os.replace(staging2, target)
            shutil.rmtree(staging, ignore_errors=True)
            return
        except PermissionError as exc:
            last = exc
            _retry_sleep()
        except OSError:
            shutil.rmtree(staging2, ignore_errors=True)
            raise
    if last is not None:
        raise last


def _tmp_dir_of(target: str) -> str:
    """从 ``.../extracted/<pid>`` 反推 ``.../.tmp``（旧目录挪去的地方）。"""
    extracted = os.path.dirname(os.path.abspath(target))
    root = os.path.dirname(extracted)
    return os.path.join(root, TMP_DIRNAME)


def _extract_one(archive: zipfile.ZipFile, info: zipfile.ZipInfo,
                 relative: str, staging: str, limits: dict,
                 counters: dict) -> None:
    """解压单个文件，并按**实际读到的字节**再算一次上限。

    头部声明的 ``file_size`` 可以撒谎（zip bomb 的常见手法就是把它写小），
    所以不能只看中央目录。这里边读边累加，超限立刻中止。
    """
    destination = os.path.normpath(os.path.join(staging,
                                                relative.replace("/", os.sep)))
    # 二次确认落盘路径没有跑出暂存目录（纵深防御：即便规范化逻辑被改坏）
    if not destination.startswith(os.path.abspath(staging) + os.sep):
        raise UnsafeArchive(f"落盘路径越界: {relative}")
    os.makedirs(os.path.dirname(destination), exist_ok=True)

    written = 0
    with archive.open(info) as source, open(destination, "wb") as out:
        while True:
            chunk = source.read(1024 * 256)
            if not chunk:
                break
            written += len(chunk)
            counters["bytes"] += len(chunk)
            if written > limits["max_file_bytes"]:
                raise UnsafeArchive(
                    f"解压后实际大小 {written} 字节超过单文件上限 "
                    f"{limits['max_file_bytes']}: {relative}")
            if counters["bytes"] > limits["max_total_bytes"]:
                raise UnsafeArchive(
                    f"累计解压 {counters['bytes']} 字节超过上限 "
                    f"{limits['max_total_bytes']}: {relative}")
            out.write(chunk)
    counters["files"] += 1


def extract_package(root: str, package_id: str, limits: dict = None,
                    cancel_event=None) -> dict:
    """安全解压并提交到 ``extracted/<package_id>/``，保持原厂内部目录结构。

    流程：全量前置检查 -> 暂存目录解压 -> 目录 rename 提交。
    任何一步失败：状态标 ``failed``、记 ``last_error``、**保留原始 ZIP**、
    清理本次暂存产物。资产比状态重要。

    结束状态只能是 ``pending_import``（Design Kit，需要在 ADS 里挂接库）
    或 ``pending_verify``（Touchstone 文件包，文件已就位待验证）。
    **绝不直接标 ``ready``** —— 解压成功不等于 ADS 能用。

    ``cancel_event`` 置位时抛 :class:`OperationCancelled`：检查点在**每个
    文件之间**（真实原厂包动辄几千个条目，只在开头查一次等于没有取消）。
    取消时暂存目录被清掉 —— 半套解压产物比没有产物更糟（索引会建出一
    半、库会挂到缺文件的目录上）。
    """
    root = _ensure_root(root)
    package_id = _safe_id(package_id)
    merged = _merge_limits(limits, DEFAULT_LIMITS)

    record = get_package(root, package_id)
    archive_path = _abs_from_rel(root, record.get("archive_relpath"))
    if not archive_path or not os.path.isfile(archive_path):
        raise ModelStoreError(f"原始 ZIP 不存在，无法解压: {package_id}")

    set_state(root, package_id, STATE_INSPECTING)

    staging = os.path.join(_tmp_dir(root), f"x_{package_id}_{secrets.token_hex(6)}")
    os.makedirs(staging, exist_ok=False)
    counters = {"bytes": 0, "files": 0}
    detected_kind = None
    try:
        with _open_zip(archive_path) as archive:
            plan = _plan_entries(archive, merged)          # 前置全量检查
            # 解压前顺手把包类型认出来：调用方可能没先跑 scan_archive
            # （比如 UI 直接点"解压"）。这里认一次，下面的状态选择才准 ——
            # 否则 Design Kit 会被错标成 pending_verify，跳过"待导入"这一步，
            # 用户就会去等一个永远不会发生的"验证通过"。
            detected_kind, _confidence, _evidence, _cands = _classify(
                _scan_archive_structure(archive, plan))
            directories: set = set()
            for _info, relative, is_dir in plan:
                if is_dir:
                    directories.add(relative)
            for relative in sorted(directories):
                os.makedirs(os.path.join(staging,
                                         relative.replace("/", os.sep)),
                            exist_ok=True)
            for info, relative, is_dir in plan:
                if is_dir:
                    continue
                if cancel_event is not None and cancel_event.is_set():
                    raise OperationCancelled("解压被取消（在文件边界停下）")
                _extract_one(archive, info, relative, staging, merged, counters)
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("解压被取消（提交前停下）")
        target = _extract_dir(root, package_id)
        _commit_dir(staging, target)
        staging = ""
    except OperationCancelled:
        # 取消不是故障：暂存产物清掉、状态**不**标 failed，让编排层去标
        # cancelled（标 failed 会让界面显示"失败"，用户会以为包坏了）。
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    except (UnsafeArchive, zipfile.BadZipFile, ModelStoreError, OSError) as exc:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        message = f"{type(exc).__name__}: {exc}"
        try:
            set_state(root, package_id, STATE_FAILED, error=message)
        except ModelStoreError:
            pass                        # 状态机不允许（比如已经 failed）时不影响报错
        raise (exc if isinstance(exc, ModelStoreError)
               else ModelStoreError(f"解压失败: {exc}")) from exc

    target = _extract_dir(root, package_id)
    kind = record.get("package_kind") or detected_kind
    next_state = STATE_PENDING_IMPORT if kind == KIND_DESIGN_KIT else STATE_PENDING_VERIFY

    validation = dict(record.get("validation") or {})
    validation["extract"] = {
        "at": utc_now(),
        "file_count": counters["files"],
        "total_bytes": counters["bytes"],
        "limits": merged,
        "original_structure_preserved": True,
        "executed_package_scripts": False,
    }
    fields = {"validation": validation}
    if not record.get("package_kind") and detected_kind:
        # 没跑过 scan_archive 时把识别结果补上，别让 kind 永远是 None
        fields["package_kind"] = detected_kind
    if not record.get("extract_relpath"):
        # extract_relpath 之前一直是空的 —— 少了它 verify_references 核对不到
        # 解压产物、index_models 也找不到基目录（表现为"尚未解压"）。
        fields["extract_relpath"] = _rel_from_root(root, target)
    update_package(root, package_id, **fields)
    return set_state(root, package_id, next_state)


# ---------------------------------------------------------------------------
# Touchstone 头解析
# ---------------------------------------------------------------------------

def parse_touchstone_header(path: str, read_limit: int | None = None) -> dict:
    """解析 Touchstone(**.sNp/.ts**) 文件头，只取**文件里真实写着**的东西。

    返回字段：

    - ``ports``：由扩展名 ``.sNp`` 可靠得出；``.ts`` 无法从文件名确定端口数，
      留 ``None``（不靠"数第一行有几个数"猜 —— 那样猜出来的端口数在多端口
      混合数据里几乎必错）；
    - ``freq_unit`` / ``parameter`` / ``data_format``：来自 ``#`` 选项行；
    - ``reference_impedance_ohm``：只有选项行里**明确写了数值**才填。
      写了 ``R N`` 或没写 -> ``None`` 并在 ``notes`` 里说明；
    - ``freq_start_hz`` / ``freq_stop_hz`` / ``points``：由数据区第一列统计；
      文件超 ``read_limit`` 未读完时 ``freq_stop_hz`` 为 ``None`` 并标注截断。

    实测变体（都支持）：``# HZ S RI R 50`` / ``# GHz S MA R 50`` /
    ``# GHZ S RI R N`` / ``# MHz S DB R 50`` / 缺单位的 ``# S RI R 50``。
    选项行缺失时全部字段为 ``None`` 并在 notes 里标"包内未声明" ——
    宁可说"不知道"，也不要按后缀猜 RI/MA 和 50 欧姆。
    """
    read_limit = read_limit or DEFAULT_INDEX_LIMITS["max_read_bytes_per_file"]
    result = {
        "ports": None,
        "ports_source": "",
        "parameter": None,
        "data_format": None,
        "freq_unit": None,
        "freq_unit_source": None,
        "reference_impedance_ohm": None,
        "reference_impedance_source": "",
        "freq_start_hz": None,
        "freq_stop_hz": None,
        "points": None,
        "truncated": False,
        "notes": [],
    }
    name = os.path.basename(str(path))
    match = _TOUCHSTONE_PORTS_RE.search(name)
    if match:
        result["ports"] = int(match.group(1))
        result["ports_source"] = "filename"
    else:
        result["ports_source"] = "unknown(.ts 无法从文件名确定端口数)"

    try:
        with open(path, "rb") as stream:
            raw = stream.read(max(0, read_limit))
    except OSError as exc:
        result["notes"].append(f"无法读取文件: {exc}")
        return result

    truncated = False
    try:
        if len(raw) >= read_limit and raw and raw[-1:] != b"\n":
            truncated = True
    except Exception:                        # pragma: no cover - 防御
        truncated = False

    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()

    option_seen = False
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("#"):
            if option_seen:
                break                       # 选项行必须在数据区之前
            continue
        option_seen = True
        tokens = stripped.lstrip("#").split()
        if not tokens:
            continue
        cursor = 0
        unit = tokens[0].lower().strip(",")
        if unit in _FREQ_SCALE:
            result["freq_unit"] = unit
            result["freq_unit_source"] = "option_line"
            cursor = 1
        elif re.fullmatch(r"[-+0-9.eE]+", tokens[0]):
            # 允许 "# 1-10 GHz S RI R 50" 这种带频段前缀的老写法
            result["notes"].append(
                f"选项行第 1 个记号 {tokens[0]!r} 不是频率单位，已忽略")
        else:
            result["notes"].append(
                f"选项行未声明频率单位（按 Hz 不猜，freq_unit 留空）: {stripped!r}")
        rest = tokens[cursor:]
        if rest and rest[0].upper() in {"S", "Y", "Z", "H", "G"}:
            result["parameter"] = rest[0].upper()
            rest = rest[1:]
        if rest and rest[0].upper() in {"MA", "DB", "RI"}:
            result["data_format"] = rest[0].upper()
            rest = rest[1:]
        for index, token in enumerate(rest):
            if token.upper() == "R":
                if index + 1 < len(rest):
                    value = rest[index + 1].strip().strip(",")
                    try:
                        result["reference_impedance_ohm"] = float(value)
                        result["reference_impedance_source"] = "option_line"
                    except ValueError:
                        result["notes"].append(
                            f"选项行参考阻抗为 {value!r}，非数值（R N 表示未指定），留空")
                else:
                    result["notes"].append("选项行有 R 但没有数值，参考阻抗留空")
                break
        break

    if not option_seen:
        result["notes"].append(
            "包内没有 Touchstone 选项行：单位/数据格式/参考阻抗均未声明，一律留空")

    scale = _FREQ_SCALE.get(result["freq_unit"] or "", 1.0)
    if result["freq_unit"] is None:
        result["notes"].append(
            "频率单位未声明：freq_start/stop 一律为 null（不假设是 Hz）")

    lowest = None
    highest = None
    points = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped[0] in "!#":
            continue
        head = stripped.split("!", 1)[0].replace(",", " ").split()
        if not head:
            continue
        try:
            value = float(head[0])
        except ValueError:
            continue
        points += 1
        lowest = value if lowest is None or value < lowest else lowest
        highest = value if highest is None or value > highest else highest
    result["points"] = points or None
    if result["freq_unit"] is not None and lowest is not None:
        result["freq_start_hz"] = lowest * scale
        result["freq_stop_hz"] = None if truncated else highest * scale
        if truncated:
            result["notes"].append(
                f"文件超过读取上限 {read_limit} 字节，频率上限未统计")
    result["truncated"] = truncated
    return result


_BIAS_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(IDS|IDD|VDDIO|VCEQ|VBIAS|VCE|VDD|VDS|VGS|VG|VCC|IC|ID|IG|IW|PWR|TJ|"
    r"TEMP|T)"
    r"\s*[=_]?\s*"
    r"([-+]?[0-9]*\.?[0-9]+)\s*"
    r"(mA|uA|nA|µA|μA|A|mV|V|K|ohm|Ω)?")

# **单位在前**的裸工作点记号：``GSL802AD_20mA`` / ``GIL9001_80mA`` 这种写法里
# 前面没有 VCE/ID 关键字，只有数值+单位。原正则要求"关键字 + 数值 + 单位"，
# 匹配不到，于是型号被当成整串返回（GSL802AD_20mA 而非 GSL802AD）。
# 单位表收窄到电流/电压/功率 —— 电阻电容单位不收（更可能是型号的一部分）。
_BIAS_LEADING_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"([0-9]+)\s*"
    # 单位表里 mA 必须排在 A 前面：正则交替是**有序**优先的，
    # "20mA" 若先试 (…|A) 会被 A 截断成 20 + 空匹配，导致整个记号不命中。
    # 末尾**不能加** \b —— "20mA_ADS" 里 A 后紧跟下划线，而 (?i) 下 "_"
    # 属于单词字符（\w 含 _），A 与 _ 之间不存在 \b，整条正则直接失配
    # （实测 (?i)([0-9]+)\s*(mA|uA|nA|A)\b 对 GSL802AD_20mA_ADS 返回 None）。
    r"(mA|uA|nA|µA|μA|MA|mV|V|K)")


# 用途/版本描述后缀：它们不是偏置也不是型号的一部分（GRM155_R1 与
# GSL802AD_20mA_ADS_compatible 里的 ADS_compatible 都属这类），切型号时剥掉。
_DESC_SUFFIX_RE = re.compile(
    r"(?i)[\s_\-]*("
    r"ADS[_\-]?compatible|ADS[_\-]?legacy|ADS[_\-]?datasheet|ADS[_\-]?linear|"
    r"linear[_\-]?model|datasheet|legacy|RI[_\-]?format|"
    r"model|sample|example|rawdata|original|native"
    r")$")

# 已知但**不含工作点数值**的标号后缀（封装变体 R1/R2 等）。
# 只认这些具体写法。刻意不做"任意字母+数字"通配：试过 [\s_\-]*[RLC]\d+$ 与
# 末尾数字段的宽松写法，结果 BFR106 被切成 BF、GRM155 被切成 GRM ——
# 型号被改短比查不到更危险（用户照着"BF"去 ADS 里找必然失败）。
_KNOWN_SUFFIX_RE = re.compile(
    r"(?i)[\s_\-]+(R\d+|L\d+|C\d+)$")


def _bias_from_stem(stem: str) -> tuple[str | None, dict]:
    """从 Touchstone 文件名里取型号与偏置条件。

    规则：**只有当"型号 + 若干可解释的后缀记号"能把文件名解释完时才填型号**。

    - ``BFP181_VCE_2.0V_IC_10mA`` -> 型号 ``BFP181``，偏置 ``{VCE: 2 V, IC: 10 mA}``；
    - ``GSL802AD_20mA_ADS_compatible`` -> 型号 ``GSL802AD``，偏置 ``{ID: 20 mA}``；
      后缀 ``ADS_compatible`` 是版本描述不是型号（见 ``_DESC_SUFFIX_RE``）；
    - ``GRM155_R1`` -> 型号 ``GRM155``，记号 ``R1``（无单位变体标号）不进偏置；
    - ``BFR106`` -> 型号 ``BFR106``（整串就是型号，无任何后缀）；
    - ``BFP181_XYZZY_UNKNOWN_SUFFIX`` -> 型号 ``None``。尾缀既不是工作点也不是
      已知描述词，任何切法都是猜，宁可留空。

    型号猜错的后果是检索结果指向一个不存在的器件，用户照着装进 ADS 才发现不对 ——
    所以**无法解释的尾缀一律不切**，界面上显示"未确定"而不是猜一个。
    """
    text = str(stem or "").strip()
    if not text:
        return None, {}
    matches = list(_BIAS_RE.finditer(text))
    if not matches:
        # 单位在前的裸工作点（GSL802AD_20mA_ADS_compatible）：
        # 型号是第一个工作点记号之前的部分。
        leading = list(_BIAS_LEADING_RE.finditer(text))
        if leading:
            prefix = text[:leading[0].start()].strip(" _-.")
            bias: dict = {}
            for match in leading:
                try:
                    value = float(match.group(1))
                except ValueError:
                    continue
                unit = (match.group(2) or "").replace("µ", "u").replace("μ", "u")
                bias.setdefault("ID", f"{value:g} {unit}".strip())
            prefix = _DESC_SUFFIX_RE.sub("", prefix).strip(" _-.")
            if prefix and re.search(r"[A-Za-z0-9]", prefix):
                return prefix, bias
            return None, bias
        # 没有工作点记号：只有"剥掉已知的封装/变体标号后仍是型号"才认。
        # BFR106 / GRM155 这类整串就是型号的**原样返回**（不做任何正则剥离 ——
        # 试过用"尾部数字段"通配，结果把 BFR106 切成 BF、GRM155 切成 GRM，
        # 那是更糟的错误：型号被改短了）。
        # 只在末尾**确实是** R1/L1/... 这类标号时才切。
        stripped = _KNOWN_SUFFIX_RE.sub("", text).strip(" _-.")
        if stripped and stripped != text and re.search(r"[A-Za-z]", stripped):
            return stripped, {}
        if re.search(r"[A-Za-z]", text):
            return text, {}
        return None, {}

    prefix = text[:matches[0].start()].strip(" _-.")
    if not prefix or not re.search(r"[A-Za-z0-9]", prefix):
        # 型号段为空（例如文件名以 "20mA" 开头）：剥掉描述后缀与标号后缀再试
        stripped = _DESC_SUFFIX_RE.sub("", text).strip(" _-.")
        prefix = _KNOWN_SUFFIX_RE.sub("", stripped).strip(" _-.") or stripped
        if not prefix or not re.search(r"[A-Za-z0-9]", prefix):
            return None, {}

    bias: dict = {}
    for match in matches:
        key = match.group(1).upper()
        try:
            value = float(match.group(2))
        except ValueError:
            continue
        unit = (match.group(3) or "").replace("µ", "u").replace("μ", "u")
        bias[key] = f"{value:g} {unit}".strip()

    # 型号里可能还粘着描述/标号后缀（BFP181_ADS_compatible、
    # GSL802AD_20mA_ADS_compatible 里的型号段"GSL802AD_20mA"）——
    # 只剥已知的两种后缀，不做泛化剥离。
    prefix = _DESC_SUFFIX_RE.sub("", prefix).strip(" _-.")
    prefix = _KNOWN_SUFFIX_RE.sub("", prefix).strip(" _-.")
    if not prefix or not re.search(r"[A-Za-z0-9]", prefix):
        return None, {}
    return prefix, bias


# ---------------------------------------------------------------------------
# 型号索引
# ---------------------------------------------------------------------------

def _library_for_path(kit_root: str, relative: str, libraries: list) -> str | None:
    """按 lib.defs 的 ``DEFINE <库名> <路径>`` 匹配文件所属库。

    路径是相对**套件根目录**的（lib.defs 就在套件根下），所以先把文件路径
    相对 kit_root 截断再匹配前缀。匹配不上就返回 ``None`` ——
    实测 TDK 的 ``lib.defs`` 指向的是 ``./TDK_Component_Library_v2019.10``，
    而 ``.atf`` 在 ``circuit/ael/`` 下，本来就不属于那个库；如实留空比
    强行归到"唯一的库"里诚实。
    """
    if kit_root and relative.startswith(kit_root + "/"):
        scoped = relative[len(kit_root) + 1:]
    else:
        scoped = relative
    for library in libraries:
        target = str(library.get("path") or "").strip("./")
        if not target:
            continue
        if scoped == target or scoped.startswith(target + "/"):
            return library.get("name")
    return None


def index_models(root: str, package_id: str,
                 limits: dict = None, cancel_event=None) -> list:
    """从**已解压内容**建立型号索引，写回记录并返回列表。

    每项只写**能可靠拿到**的字段，拿不到就留 ``None``/空：

    - Touchstone：端口数、频率范围、参考阻抗来自 :func:`parse_touchstone_header`
      （真实读文件头），型号只在文件名其余部分是可解释工作点记号时才填；
    - Design Kit：型号取自 ``.atf`` / ``.ael`` 文件名（元件库文件就是以型号
      命名的，这是可靠来源）；**厂商只来自包内明确文本**，不按目录名猜；
    - 库引用按 ``lib.defs`` 的 ``DEFINE`` 前缀匹配，匹配不上留 ``None``。

    索引有**条数上限**并如实记录总数（``models_indexed`` / ``models_total``）：
    Infineon 包有 8064 个 .s2p，全量写进 manifest.json 会让清单变成几 MB，
    每次改状态都要重写。截断是可见的，不假装完整。

    ``cancel_event`` 置位时抛 :class:`OperationCancelled`：检查点在**每批
    数据块之间**（元件库文件批、Touchstone 文件批，以及批内每
    _INDEX_CANCEL_EVERY 个文件）。建索引要逐个读文件头，是导入里第二慢的
    一步，只在开头查一次取消等于"点了取消还要等半分钟"。
    """
    root = _ensure_root(root)
    package_id = _safe_id(package_id)
    merged = _merge_limits(limits, DEFAULT_INDEX_LIMITS)

    record = get_package(root, package_id)
    extract_rel = record.get("extract_relpath")
    base = _abs_from_rel(root, extract_rel)
    if not extract_rel or not base or not os.path.isdir(base):
        raise ModelStoreError(
            f"尚未解压，无法建立型号索引: {package_id}（extract_relpath 为空或目录不存在）")

    attach = dict(record.get("library_attach") or {})
    libraries = attach.get("defined_libraries") or []
    # 套件根优先用识别阶段选定的那个（识别时已排除"两层同名嵌套"要用内层的情况），
    # 没选定就退回"唯一顶层目录"，再不行留空 —— 库匹配失败只是留空，
    # 猜错套件根却会让所有库引用指向错误位置。
    kit_root = attach.get("kit_root") or ""
    if not kit_root:
        try:
            entries = sorted(n for n in os.listdir(base) if not n.startswith("."))
        except OSError:
            entries = []
        directories = [n for n in entries
                       if os.path.isdir(os.path.join(base, n))]
        if len(directories) == 1:
            kit_root = directories[0]

    vendor = record.get("vendor")
    touchstone_files: list = []
    component_files: list = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for filename in sorted(filenames):
            absolute = os.path.join(dirpath, filename)
            relative = os.path.relpath(absolute, base).replace(os.sep, "/")
            if _TOUCHSTONE_RE.search(filename):
                touchstone_files.append((absolute, relative))
            elif filename.lower().endswith((".atf", ".ael")):
                component_files.append((absolute, relative))

    models: list = []
    # 先索引元件库文件（.atf/.ael）—— 它们才是 Design Kit 的"型号"主体，
    # 被上限截断时优先保住它们，Touchstone 数据文件其次。
    component_cap = merged["max_component_entries"]
    for _n, (absolute, relative) in enumerate(component_files[:component_cap]):
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("建立型号索引被取消（在文件块边界停下）")
        stem = os.path.splitext(os.path.basename(relative))[0]
        entry = {
            "kind": "component",
            "part": stem or None,
            "part_source": "filename(.atf/.ael 以型号命名)",
            "vendor": vendor,
            "vendor_source": "package_text" if vendor else None,
            "model_type": None,
            "ports": None,
            "freq_start_hz": None,
            "freq_stop_hz": None,
            "reference_impedance_ohm": None,
            "library": _library_for_path(kit_root, relative, libraries),
            "sub_library": None,
            "relpath": f"{extract_rel.rstrip('/')}/{relative}",
            "bias": {},
            "notes": [],
        }
        if entry["library"] is None:
            entry["notes"].append(
                "未匹配到 lib.defs 里的库路径，库引用留空（不按目录名归库）")
        models.append(entry)

    remaining = max(0, merged["max_index_entries"] - len(models))
    for _n, (absolute, relative) in enumerate(touchstone_files[:remaining]):
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("建立型号索引被取消（在文件块边界停下）")
        header = parse_touchstone_header(
            absolute, merged["max_read_bytes_per_file"])
        stem = os.path.splitext(os.path.basename(relative))[0]
        part, bias = _bias_from_stem(stem)
        notes = list(header.get("notes") or [])
        if part is None:
            notes.append("型号未从文件名中确定（其余记号不是可识别的工作点），留空")
        models.append({
            "kind": "touchstone",
            "part": part,
            "part_source": "filename(前缀 + 可解释工作点记号)" if part else None,
            "vendor": vendor,
            "vendor_source": "package_text" if vendor else None,
            "model_type": "s-parameter" if (header.get("parameter") or "") == "S"
                          and header.get("ports") else None,
            "ports": header.get("ports"),
            "ports_source": header.get("ports_source"),
            "freq_start_hz": header.get("freq_start_hz"),
            "freq_stop_hz": header.get("freq_stop_hz"),
            "freq_points": header.get("points"),
            "frequency_unit": header.get("freq_unit"),
            "data_format": header.get("data_format"),
            "reference_impedance_ohm": header.get("reference_impedance_ohm"),
            "reference_impedance_source": header.get("reference_impedance_source"),
            "library": _library_for_path(kit_root, relative, libraries),
            "sub_library": None,
            "relpath": f"{extract_rel.rstrip('/')}/{relative}",
            "bias": bias,
            "notes": notes,
        })

    total = len(component_files) + len(touchstone_files)
    validation = dict(record.get("validation") or {})
    validation["index"] = {
        "at": utc_now(),
        "component_files": len(component_files),
        "touchstone_files": len(touchstone_files),
        "indexed": len(models),
        "total": total,
        "truncated": total > len(models),
        "kit_root": kit_root,
        "vendor_from": "package_text" if vendor else None,
    }
    update_package(root, package_id,
                   models=models,
                   models_indexed=len(models),
                   models_total=total,
                   validation=validation)
    return models


def _normalize_part(value: str) -> str:
    """型号归一化：只去分隔符与大小写，不做任何"补全"或模糊匹配。

    刻意不做模糊匹配：``BFP181`` 和 ``BFP181W`` 是**两个不同型号**，
    前缀匹配会把两个器件的参数混在一起报给用户 —— 这比查不到更糟。
    """
    text = re.sub(r"[\s_\-]+", "", str(value or "")).strip()
    return text.upper()


def normalize_part(value: str) -> str:
    """型号归一化（公开）：只去分隔符与大小写，不做任何"补全"或模糊匹配。

    调用方（如 model_tools 按型号查索引）需要同一套规则时用这个，不要
    各自实现一遍 —— 归一化规则一旦分叉，同一个型号在两处会匹配不上。
    """
    return _normalize_part(value)


def find_models(root: str, part: str = "", library: str = "",
                models: list | None = None) -> dict:
    """按型号检索**已索引**的模型，返回频率范围 / 参考阻抗等可靠字段。

    为什么需要这个函数：``backend/tools.py`` 里 ``get_vendor_model_info`` 的
    工具描述向模型承诺了"该元件在模型索引里的频率范围与参考阻抗"，但索引
    里的这些字段一直**没有消费方** —— 描述与实际返回不符，模型会以为字段
    该有而去找，找不到就自己编一个。这个函数补上那条链路。

    只报**索引里真实有**的值：某项没解析出来就是 ``None``，并在
    ``notes`` 里说明原因（缺选项行 / 没索引过 / 库未挂接等）。
    ADS 端的 ``model_def`` 拿不到取值范围（本版本 ModelParam 无 min/max，
    见 ``addon/ads_agent/model_ops.py`` 的核对结论），所以频率范围只能来自
    这里 —— 两边都拿不到时如实为空，不外推。

    ``library`` 只做**归一化后的精确匹配**：库名带 ``#2e`` 之类转义或大小写
    差异会先归一，不做包含匹配（避免 ``Infineon_RF`` 命中
    ``Infineon_RF_tech``）。传空字符串表示不限库。
    """
    records = list_packages(_ensure_root(root))
    wanted_part = _normalize_part(part)
    wanted_library = _normalize_part(library)

    hits: list = []
    scanned = 0
    for record in records:
        for entry in (models if models is not None else (record.get("models") or [])):
            if not isinstance(entry, dict):
                continue
            scanned += 1
            if wanted_part and _normalize_part(entry.get("part")) != wanted_part:
                continue
            if wanted_library and _normalize_part(entry.get("library")) != wanted_library:
                continue
            hits.append({
                "package_id": record.get("package_id"),
                "package_kind": record.get("package_kind"),
                "vendor": entry.get("vendor"),
                "part": entry.get("part"),
                "model_type": entry.get("model_type"),
                "ports": entry.get("ports"),
                "freq_start_hz": entry.get("freq_start_hz"),
                "freq_stop_hz": entry.get("freq_stop_hz"),
                "freq_points": entry.get("freq_points"),
                "frequency_unit": entry.get("frequency_unit"),
                "data_format": entry.get("data_format"),
                "reference_impedance_ohm": entry.get("reference_impedance_ohm"),
                "reference_impedance_source": entry.get("reference_impedance_source"),
                "bias": entry.get("bias") or {},
                "library": entry.get("library"),
                "relpath": entry.get("relpath"),
                "notes": entry.get("notes") or [],
            })

    # 排序：模型数据文件（.s2p/.ts，频率/阻抗/Z0 都从文件头真实解析）排在
    # 元件库文件（.ael/.atf，只有型号没有模型数据）之前。
    #
    # 为什么要排：Infineon 包里 BFP181 同时有 120 个 .ael 和 306 个 .s2p，
    # 索引顺序上前者可能先被命中。不排的话调用方拿"第一条"当概览时拿到的
    # 是一堆 null，看起来像"这个型号没有频率范围数据" —— 而其实有。
    # 元件库条目不是丢掉，只是排在后面（它对确认"这个型号存在"仍有价值）。
    def _data_rank(hit: dict) -> tuple:
        rel = str(hit.get("relpath") or "").lower()
        is_data = rel.endswith((".s2p", ".s1p", ".ts")) or ".s" in rel.split(".")[-1][:2]
        has_freq = hit.get("freq_start_hz") is not None
        return (0 if (is_data or has_freq) else 1,
                "" if has_freq else "1",
                rel)

    hits.sort(key=_data_rank)

    notes: list = []
    if not records:
        notes.append("当前工作区没有任何模型包记录")
    elif scanned == 0:
        notes.append("模型包已存档但尚未建立型号索引，"
                     "请先对包执行索引（list_vendor_models / import 流程会做）")
    elif not hits:
        notes.append(f"索引里没有匹配 {part or '(不限型号)'}"
                     f"{' / ' + library if library else ''} 的条目"
                     f"（已比对 {scanned} 条索引；型号按归一化精确匹配，"
                     f"不做前缀/模糊匹配以免混淆相近型号）")

    return {
        "found": bool(hits),
        "count": len(hits),
        "hits": hits,
        "indexed_entries_scanned": scanned,
        "package_count": len(records),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 工程迁移 / 引用核对
# ---------------------------------------------------------------------------

def _missing_against(data: dict, root: str) -> list:
    """把一份清单的相对路径**按另一个 root** 逐条核对，返回缺失项。

    :func:`verify_references` 与 :func:`relocate` 共用这套逻辑 —— 区别只在
    "用哪个 root 去解析相对路径"。清单里的路径是相对工作区的，所以换一个
    工作区根去解析，正好回答"如果工程搬到那儿去，这些引用还指得到东西吗"。
    """
    packages = data.get("packages") or {}
    missing: list = []
    for pid, record in sorted(packages.items()):
        if not isinstance(record, dict):
            continue
        for field in ("archive_relpath", "extract_relpath"):
            relative = record.get(field)
            if not relative:
                continue
            absolute = _abs_from_rel(root, relative)
            if not absolute or not os.path.exists(absolute):
                missing.append({
                    "package_id": pid,
                    "field": field,
                    "relative_path": relative,
                    "resolved": absolute,
                    "reason": "路径不存在",
                })
    return missing


def verify_references(root: str) -> dict:
    """核对每条记录的相对路径在磁盘上是否真实存在。

    不修复、不删除，只**如实报告**：清单里说了有，实际磁盘上没有 —— 这种
    不一致必须被看见（用户在面板里点"打开"会失败）。返回
    ``{"ok", "checked", "missing": [...]}``，每条 missing 带 package_id、
    字段名与解析出的绝对路径，方便直接定位。
    """
    root = _ensure_root(root)
    data = load_manifest(root)
    packages = data.get("packages") or {}
    missing = _missing_against(data, root)
    checked = sum(1 for record in packages.values() if isinstance(record, dict)
                  for field in ("archive_relpath", "extract_relpath")
                  if record.get(field))
    return {
        "ok": not missing,
        "checked": checked,
        "missing": missing,
        "package_count": len(packages),
        "workspace": data.get("workspace") or os.path.dirname(root),
        "manifest_recovered_from_backup": bool(data.get("recovered_from_backup")),
    }


def relocate(root: str, new_workspace: str) -> dict:
    """工程迁移后重新定位并验证引用。

    ADS 工作区换了目录（换盘、复制工程）之后，资产跟着 ``ads_agent_models``
    目录一起搬过去；清单里的路径是**相对工作区**的，所以理论上不用改内容，
    但必须**逐条验证文件真的在** —— "应该还在"和"确实还在"是两回事。

    **不复制、不搬运数据**：几十万个小文件复制一次要几分钟，且中途失败会留下
    半份资产。这里只做"重新绑定 + 核对"，发现缺失就如实报告让用户决定怎么办。
    确实需要搬文件时，用户直接用文件管理器搬整个工作区更安全。

    返回 ``{"ok", "old_root", "new_root", "verified", "missing", "workspace_changed"}``。
    """
    root = _ensure_root(root)
    new_root = store_root(new_workspace)
    result = {
        "ok": True,
        "old_root": root,
        "new_root": new_root,
        "workspace_changed": os.path.normcase(root) != os.path.normcase(new_root),
        "manifest_exists": os.path.isfile(_manifest_path(new_root)),
        "verified": 0,
        "missing": [],
    }
    if not result["workspace_changed"]:
        report = verify_references(root)
        result.update({"ok": report["ok"], "verified": report["checked"],
                       "missing": report["missing"]})
        return result

    if not os.path.isdir(new_root):
        # 没有资产目录也要把"缺了什么"逐条列出来：用户看到清单才知道要搬哪些
        # 文件。只给一句"目录不存在"，用户得自己翻 manifest.json 才知道
        # 原来这里存过东西。
        missing = _missing_against(load_manifest(root), new_root)
        result.update({
            "ok": False,
            "missing": missing,
            "verified": 0,
            "hint": f"新工作区下没有 {STORE_DIRNAME}/ 目录。模型资产应随工作区一起"
                    f"迁移（直接把整个工作区目录复制过去即可，本模块不搬运文件）。",
        })
        return result

    def mutate(data: dict) -> dict:
        data["workspace"] = os.path.dirname(new_root)
        data["relocated_at"] = utc_now()
        return data

    if os.path.isfile(_manifest_path(new_root)):
        _mutate(new_root, mutate)
        report = verify_references(new_root)
        result.update({"ok": report["ok"], "verified": report["checked"],
                       "missing": report["missing"]})
    return result


# ---------------------------------------------------------------------------
# 内部清理（**没有公共 API 会调用它**）
# ---------------------------------------------------------------------------

def _delete_package_files(root: str, package_id: str) -> bool:
    """内部专用：删除某个包的 archives/ 与 extracted/ 目录。

    **刻意不提供公共删除 API**：删除聊天、清空会话、切换项目都绝不能顺带
    删掉模型资产 —— 用户上传一个 100 MB 的厂家包可能花了很久，误删的代价
    远高于"多占一点磁盘"。真要清理必须由用户在 UI 上明确确认，且调用方要
    自己检查 ``ref_count``（还有别的会话引用着同一份内容时不能删）。

    这里只删文件，**不动清单**：清单的删除要走 :func:`set_state` 之外的人工
    确认流程，避免"文件没了记录还在"或反之。
    """
    root = _ensure_root(root)
    try:
        package_id = _safe_id(package_id)
    except ModelStoreError:
        return False
    removed = False
    for directory in (_archive_dir(root, package_id), _extract_dir(root, package_id)):
        if os.path.isdir(directory):
            shutil.rmtree(directory, ignore_errors=True)
            removed = True
    return removed


# ---------------------------------------------------------------------------
# 命令行自查（离线可用）
# ---------------------------------------------------------------------------

def _main(argv: list) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="模型压缩包资产库自查")
    parser.add_argument("--workspace", default="", help="ADS 工作区路径")
    parser.add_argument("--inspect", metavar="ZIP", default="",
                        help="只读识别一个 ZIP（不需要工作区，不写任何文件）")
    parser.add_argument("--list", action="store_true", help="列出已存模型包")
    args = parser.parse_args(argv)

    if args.inspect:
        path = args.inspect
        with _open_zip(path) as archive:
            plan = _plan_entries(archive, DEFAULT_LIMITS)
            stats = _scan_archive_structure(archive, plan)
            kind, confidence, evidence, candidates = _classify(stats)
            vendor = _vendor_from_text(
                "".join(_read_member_bytes(archive, info,
                                           DEFAULT_INDEX_LIMITS["max_doc_bytes"])
                        for info, _rel in _doc_members(plan)))
            versions = _version_from_docs(plan, archive)
        print(f"文件        : {os.path.basename(path)}")
        print(f"类型        : {KIND_LABELS.get(kind, kind)}（置信度 {confidence}）")
        print(f"条目 / 文件 : {stats['entry_count']} / {stats['file_count']}")
        print(f"解压总大小  : {stats['total_uncompressed']} 字节")
        print(f"Touchstone  : {stats['touchstone_count']}")
        print(f"厂商        : {vendor[0]['vendor'] if vendor else '（包内无明确声明，留空）'}")
        print(f"版本        : {versions[0]['version'] if versions else '（包内无明确声明，留空）'}")
        print(f"套件根候选  : {', '.join(candidates) or '（无）'}")
        print("判定依据    :")
        for item in evidence:
            print(f"  - {item}")
        return 0

    if not args.workspace:
        parser.error("需要 --workspace 或 --inspect")
    root = store_root(args.workspace)
    if args.list:
        records = list_packages(root)
        print(f"模型资产根  : {root}")
        print(f"已存模型包  : {len(records)}")
        for record in records:
            print(f"  {record.get('package_id')}  "
                  f"{KIND_LABELS.get(record.get('package_kind'), '未识别'):<18}"
                  f" {STATE_LABELS.get(record.get('state'), record.get('state')):<12}"
                  f" {record.get('original_filename')}")
        report = verify_references(root)
        print(f"引用核对    : {'全部存在' if report['ok'] else '有缺失'} "
              f"（核对 {report['checked']} 条，缺失 {len(report['missing'])} 条）")
        for item in report["missing"]:
            print(f"  缺失 {item['package_id']} {item['field']} -> {item['resolved']}")
        return 0 if report["ok"] else 1

    print(f"模型资产根  : {root}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
