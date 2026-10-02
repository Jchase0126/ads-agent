import json, traceback

out = []
def P(*a):
    out.append(" ".join(str(x) for x in a))

def safe(fn):
    try:
        return fn()
    except Exception as e:
        return f"<ERR {type(e).__name__}: {e}>"

P("=== 1. workspace ===")
P("ws:", ws())

P("=== 2. cell & views (Wilkinson_5G9) ===")
cell = safe(lambda: cells("AI_lib", "Wilkinson_5G9"))
P("cells ->", type(cell).__name__, cell if isinstance(cell, str) else "")
def _views():
    vs = []
    for name in ("views", "view_names", "list_views"):
        if hasattr(cell, name):
            v = getattr(cell, name)
            vs.append(f"{name}={v}")
    lv = safe(lambda: cell.view("layout"))
    vs.append(f"view('layout')->{type(lv).__name__}")
    gv = safe(lambda: cell.get_view_if_exists("layout"))
    vs.append(f"get_view_if_exists('layout')->{type(gv).__name__}")
    return vs
P(*safe(_views) if isinstance(_views(), list) else [_views()])

P("=== 3. open layout read-only ===")
dl = safe(lambda: odesign("AI_lib", "Wilkinson_5G9", view="layout", write=False))
P("odesign(view='layout') ->", type(dl).__name__, str(dl)[:200])
if isinstance(dl, str):
    P("retry without view kw:")
    dl2 = safe(lambda: odesign("AI_lib", "Wilkinson_5G9", write=False))
    P("  ->", type(dl2).__name__)
    P("  design.viewName?", safe(lambda: getattr(dl2, "view_name", None)) if dl2 else "")

P("=== 4. layout design members of interest ===")
if dl and not isinstance(dl, str):
    names = [n for n in dir(dl) if any(k in n.lower() for k in
             ("shape", "rect", "path", "poly", "wire", "pin", "port", "inst", "layer", "em", "bound"))]
    P("matched members:", names)

P("=== 5. shape iteration candidates ===")
if dl and not isinstance(dl, str):
    for attr in ("shapes", "each_shape", "get_shapes", "shape_iter"):
        P(attr, "->", safe(lambda a=attr: type(getattr(dl, a)).__name__))
    try:
        it = dl.shapes
        seq = list(it)
        P("len(shapes) =", len(seq))
        for s in seq[:6]:
            P("  shape:", type(s).__name__, safe(lambda s=s: str(s)[:120]))
            P("    attrs:", [n for n in dir(s) if any(k in n.lower() for k in
              ("layer", "geo", "type", "bound", "corner", "width", "end", "path"))][:20])
    except Exception as e:
        P("iter shapes ERR:", type(e).__name__, str(e)[:150])

P("=== 6. generate/update layout API ===")
if cell is not None and not isinstance(cell, str):
    lay_names = [n for n in dir(cell) if "layout" in n.lower() or "generate" in n.lower()]
    P("cell members:", lay_names)
    for n in lay_names:
        try:
            P("  sig", n, "->", str(sig(cell, n))[:200])
        except Exception as e:
            P("  sig", n, "ERR:", str(e)[:100])
if dl and not isinstance(dl, str):
    dlay = [n for n in dir(dl) if "generate" in n.lower() or "schematic" in n.lower()]
    P("design generate/schematic members:", dlay)

P("=== 7. LineCalc / synthesis API ===")
import pkgutil
try:
    import keysight.ads as ka
    mods = [m.name for m in pkgutil.iter_modules(ka.__path__)]
    P("keysight.ads submodules:", mods[:40])
    hits = [m for m in mods if any(k in m.lower() for k in ("line", "calc", "synth", "em", "geo"))]
    P("interesting:", hits)
except Exception as e:
    P("pkgutil ERR:", e)
for cand in ("linecalc", "mlinecalc", "line_calc", "synthesis"):
    P(f"de has {cand}?", hasattr(de, cand))
denames = [n for n in dir(de) if any(k in n.lower() for k in ("line", "calc", "synth"))]
P("de members of interest:", denames)

P("=== 8. EM setup API ===")
if dl and not isinstance(dl, str):
    ems = [n for n in dir(dl) if n.lower().startswith("em") or "em_" in n.lower()]
    P("design EM members:", ems)

P("=== 9. layers / tech ===")
if dl and not isinstance(dl, str):
    P("layer members:", [n for n in dir(dl) if "layer" in n.lower()][:15])
    try:
        it = dl.shapes
        s0 = next(iter(it), None)
        if s0 is not None:
            P("first shape type:", type(s0).__name__)
            P("  .layer ->", safe(lambda: getattr(s0, "layer", None)))
            P("  .layer_name ->", safe(lambda: getattr(s0, "layer_name", None)))
    except Exception as e:
        P("shape0 ERR:", e)

print("\n".join(out))
