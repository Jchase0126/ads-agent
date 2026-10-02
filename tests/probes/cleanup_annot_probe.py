# -*- coding: utf-8 -*-
"""删除 AI_annot_probe scratch cell 并只读确认。"""
import ads_ops

de = ads_ops._de()
lib_obj = de.get_open_library("BFR106_lib")
lib_obj.delete_cell("AI_annot_probe")
try:
    ads_ops._open_design("BFR106_lib", "AI_annot_probe", "schematic", write=False)
    print("CLEANUP-FAIL: cell 仍可打开")
except Exception:
    print("CLEANUP-OK: AI_annot_probe 已删除且不可再打开")
