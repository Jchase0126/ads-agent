import json

out = []
def P(*a):
    out.append(" ".join(str(x) for x in a))
def safe(fn):
    try:
        return fn()
    except Exception as e:
        return f"<ERR {type(e).__name__}: {e}>"

LIB, CELL = "AI_lib", "Wilkinson_1G7_2way"

P("=== open layout read-only ===")
d = safe(lambda: odesign(LIB, CELL, view="layout", write=False))
P("open ->", type(d).__name__, str(d)[:120])
if d and not isinstance(d, str):
    P("is_layout:", safe(lambda: d.is_layout))
    P("viewName:", safe(lambda: getattr(d, "view_name", "?")))

    P("=== shapes ===")
    try:
        shapes = list(d.shapes)
        P("len(shapes):", len(shapes))
        for s in shapes[:12]:
            t = type(s).__name__
            P("  ", t)
            for attr in ("layer_id", "layer", "bbox", "corners", "path_end_style", "width", "net"):
                v = safe(lambda a=attr, s=s: getattr(s, a))
                if not (isinstance(v, str) and v.startswith("<ERR")):
                    P(f"    .{attr} =", str(v)[:110])
            ol = safe(lambda s=s: s.get_outline())
            P("    outline:", str(ol)[:150])
    except Exception as e:
        P("shapes ERR:", type(e).__name__, str(e)[:200])

    P("=== instances ===")
    try:
        insts = list(d.instances)
        P("len(instances):", len(insts))
        for i in insts[:12]:
            nm = safe(lambda i=i: getattr(i, "instance_name", "?"))
            mst = safe(lambda i=i: str(getattr(i, "instance", lambda: None)() and "" ) or getattr(getattr(i, "instance", lambda: None)(), "name", "?"))
            P("  name:", nm, "| master:", mst if not isinstance(mst, str) else mst[:60])
            pa = [n for n in dir(i) if any(k in n.lower() for k in ("param", "pin", "bbox", "transf", "angle"))]
            P("    attrs:", pa[:18])
    except Exception as e:
        P("instances ERR:", e)

    P("=== pins / ports ===")
    for attr in ("pins", "ports", "layout_pins", "terms"):
        v = safe(lambda a=attr: getattr(d, a, None))
        P(attr, "->", type(v).__name__, str(v)[:100])
    try:
        pins = list(d.pins)
        P("len(pins):", len(pins))
        for p in pins[:8]:
            P("  pin:", type(p).__name__, safe(lambda p=p: str(p)[:120]))
    except Exception as e:
        P("pins ERR:", e)

    P("=== net shapes ===")
    try:
        nets = list(d.nets)
        P("len(nets):", len(nets))
        for n in nets[:5]:
            P("  net:", type(n).__name__, safe(lambda n=n: str(n)[:80]))
            sh = safe(lambda n=n: len(list(n.shapes)) if hasattr(n, "shapes") else "n/a")
            P("    shapes:", sh)
    except Exception as e:
        P("nets ERR:", e)

P("=== get_substrate_info ===")
import keysight.ads.emtools as emt
P("sig:", safe(lambda: str(sig(emt, "get_substrate_info"))[:300]))
P("sig create_empro_view:", safe(lambda: str(sig(emt, "create_empro_view"))[:300]))
P("sig em_update_layout:", safe(lambda: str(sig(emt, "em_update_layout"))[:300]))

P("=== schematic side instance names (for mapping) ===")
ds = safe(lambda: odesign(LIB, CELL, write=False))
if ds and not isinstance(ds, str):
    try:
        names = [safe(lambda i=i: getattr(i, "instance_name", "?")) for i in list(ds.instances)]
        P("schematic instances:", names)
    except Exception as e:
        P("ERR:", e)

print("\n".join(out))
