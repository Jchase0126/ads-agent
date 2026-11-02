# -*- coding: utf-8 -*-
"""PDF 解析子进程运行时。

后端解释器缺 pymupdf 时，可在 config.ini 配置：

    [datasheet]
    runtime_python = <带 pymupdf 的解释器路径>

pdf_extract.extract_document 会把原始字节落盘后调起本脚本，用与进程内
完全相同的解析代码（pdf_extract._pymupdf_parse）得到逐页结构化结果。

用法：python pdf_worker.py --backend <backend目录> <pdf路径>
输出：单个 JSON 对象（stdout）；失败时退出码非 0。
"""

from __future__ import annotations

import os
import sys


def main(argv: list) -> int:
    args = list(argv)
    backend_dir = None
    if "--backend" in args:
        pos = args.index("--backend")
        if pos + 1 >= len(args):
            print("missing value for --backend", file=sys.stderr)
            return 2
        backend_dir = args[pos + 1]
        del args[pos:pos + 2]
    if len(args) != 1:
        print("usage: pdf_worker.py [--backend DIR] PDF_PATH", file=sys.stderr)
        return 2
    pdf_path = args[0]
    if not os.path.isfile(pdf_path):
        print("pdf not found", file=sys.stderr)
        return 2
    if backend_dir and backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    try:
        import pdf_extract
    except Exception as exc:  # noqa: BLE001
        print(f"cannot import pdf_extract: {exc}", file=sys.stderr)
        return 3
    try:
        with open(pdf_path, "rb") as stream:
            data = stream.read()
        result = pdf_extract._pymupdf_parse(data)
    except Exception as exc:  # noqa: BLE001
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 4
    import json

    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
