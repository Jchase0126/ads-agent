import json

out = []
def P(*a):
    out.append(" ".join(str(x) for x in a))
def safe(fn):
    try:
        return fn()
    except Exception as e:
        return f"<ERR {type(e).__name__}: {e}>"

LIB, CELL = "AI_lib", "_lay_probe"

P("=== create scratch layout ===")
lay = safe(lambda: db_uu.create_layout(f"{LIB}:{CELL}:layout"))
P("create_layout ->", type(lay).__name__, str(lay)[:80])
if lay and not isinstance(lay, str):
    P("add_* methods:", [n for n in dir(lay) if n.startswith("add_")][:25])
    cond = safe(lambda: lay.create_layer_id("cond"))
    gnd = safe(lambda: lay.create_layer_id("cond2"))
    P("layer ids:", cond, gnd)
    P("layer id->name api:", [n for n in dir(lay) if "layer" in n.lower()][:15])
    P("add_path sig:", safe(lambda: str(sig(lay, "add_path"))[:250]))
    P("add_rectangle sig:", safe(lambda: str(sig(lay, "add_rectangle"))[:250]))

    # 几何：一条直角折线（该切角未切）、两个重叠矩形、一条斜线、接地矩形
    P("add_path ->", safe(lambda: lay.add_path(cond, [(0.0, 50.0), (50.0, 50.0), (50.0, 100.0)], 2.0)))
    P("add_path2 ->", safe(lambda: lay.add_path(cond, [(200.0, 50.0), (250.0, 50.0), (250.0, 100.0)], 2.0)))
    P("rect A ->", safe(lambda: lay.add_rectangle(cond, (0.0, 0.0), (100.0, 10.0))))
    P("rect B (overlap A) ->", safe(lambda: lay.add_rectangle(cond, (90.0, 5.0), (150.0, 20.0))))
    P("gnd rect ->", safe(lambda: lay.add_rectangle(gnd, (-10.0, -10.0), (60.0, 40.0))))
    net = safe(lambda: lay.find_or_add_net("P1"))
    term = safe(lambda: lay.add_term(net, "P1"))
    dot = safe(lambda: lay.add_dot(cond, (0.0, 5.0)))
    P("pin ->", safe(lambda: lay.add_pin(term, dot)))
    P("save ->", safe(lambda: lay.save_design()))

P("=== reopen read-only & introspect ===")
d = safe(lambda: odesign(LIB, CELL, view="layout", write=False))
P("reopen ->", type(d).__name__)
if d and not isinstance(d, str):
    try:
        shapes = list(d.shapes)
        P("len(shapes):", len(shapes))
        for s in shapes:
            P("  type:", type(s).__name__)
            for attr in ("layer_id", "bbox", "path_end_style", "width"):
                v = safe(lambda a=attr, s=s: getattr(s, a))
                if not (isinstance(v, str) and v.startswith("<ERR")):
                    P(f"    .{attr} =", str(v)[:130])
            ol = safe(lambda s=s: s.get_outline())
            P("    outline:", str(ol)[:220])
            P("    net:", safe(lambda s=s: str(getattr(s, "net", None))[:60]))
    except Exception as e:
        P("shapes ERR:", type(e).__name__, str(e)[:200])
    P("layer name lookup:", safe(lambda: [(i, str(d.layer_id_to_name(i)) if hasattr(d, 'layer_id_to_name') else '?') for i in range(1, 4)]))
    P("terms:", safe(lambda: str([(t.name if hasattr(t, 'name') else str(t))[:40] for t in d.terms])[:200]))
    P("nets:", safe(lambda: str([str(n)[:40] for n in d.nets])[:200]))

print("\n".join(out))
