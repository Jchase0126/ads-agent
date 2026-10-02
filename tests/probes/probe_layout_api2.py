import os, glob

out = []
def P(*a):
    out.append(" ".join(str(x) for x in a))
def safe(fn):
    try:
        return fn()
    except Exception as e:
        return f"<ERR {type(e).__name__}: {e}>"

P("=== 1. all libs/cells/views ===")
def _iter(obj):
    if obj is None:
        return []
    if isinstance(obj, (list, tuple)):
        return list(obj)
    if hasattr(obj, "values"):
        try:
            return list(obj.values())
        except Exception:
            pass
    try:
        return list(obj)
    except Exception as e:
        return [f"<not iterable: {type(obj).__name__}: {e}>"]

def walk_views():
    rows = []
    libs = _iter(ws().libraries)
    for lib in libs:
        lname = getattr(lib, "name", "?")
        for cell in _iter(getattr(lib, "cells", None)):
            cname = getattr(cell, "name", "?")
            vs = []
            for v in _iter(getattr(cell, "views", None)):
                nm = getattr(v, "name", str(v))
                vt = getattr(v, "view_type", "")
                vs.append(f"{nm}({vt})")
            rows.append(f"{lname}:{cname}: {vs}")
    return rows
r = safe(walk_views)
P(*r if isinstance(r, list) else [r])

P("=== 2. LineCalc executables in install ===")
root = os.environ.get("HPEESOF_DIR", r"E:\ADS\Programfile\ADS2027")
P("HPEESOF_DIR:", root)
for pat in ("*line*", "*calc*", "*lcalc*"):
    hits = glob.glob(os.path.join(root, "bin", pat))
    P(pat, "->", [os.path.basename(h) for h in hits][:10])

P("=== 3. emtools module ===")
import keysight.ads.emtools as emt
P("emtools members:", [n for n in dir(emt) if not n.startswith("_")][:40])

P("=== 4. create_layout signature ===")
P("sig create_layout:", safe(lambda: str(sig(db_uu, "create_layout"))[:300]))

P("=== 5. layout Design introspection (first layout found) ===")
def first_layout():
    for lib in _iter(ws().libraries):
        for cell in _iter(getattr(lib, "cells", None)):
            for v in _iter(getattr(cell, "views", None)):
                if getattr(v, "name", "") == "layout":
                    return f"{getattr(lib, 'name', '?')}:{getattr(cell, 'name', '?')}"
    return None
target = safe(first_layout)
P("first layout cell:", target)
if target and not isinstance(target, str):
    lib, cname = target.split(":")
    d = safe(lambda: odesign(lib, cname, view="layout", write=False))
    P("open ->", type(d).__name__)
    if d and not isinstance(d, str):
        P("is_layout:", safe(lambda: d.is_layout))
        try:
            shapes = list(d.shapes)
            P("len(shapes):", len(shapes))
            for s in shapes[:8]:
                P("  ", type(s).__name__, safe(lambda s=s: str(s)[:100]))
                P("     attrs:", [n for n in dir(s) if not n.startswith("_")][:25])
        except Exception as e:
            P("shapes ERR:", type(e).__name__, str(e)[:200])
        try:
            insts = list(d.instances)
            P("len(instances):", len(insts))
            for i in insts[:8]:
                P("  inst:", safe(lambda i=i: (i.instance_name if hasattr(i, 'instance_name') else str(i))[:60]))
        except Exception as e:
            P("instances ERR:", e)

print("\n".join(out))
