# -*- coding: utf-8 -*-
"""删除 scratch 探针 cell _bfr_probe(用后即删惯例)。"""
import keysight.ads.de as de

ws = de.active_workspace()
lib = None
for lb in ws.libraries:
    if str(lb.name) == "BFR106_lib":
        lib = lb
        break
names = sorted(str(c.name) for c in lib.cells)
print("删除前:", names)
if "_bfr_probe" in names:
    lib.delete_cell("_bfr_probe")
    print("已删除 _bfr_probe")
else:
    print("无 _bfr_probe,跳过")
print("删除后:", sorted(str(c.name) for c in lib.cells))
print("done")
