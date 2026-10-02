import json
def netname(p):
    n = p.net
    return str(getattr(n, 'name', '') or '') if n is not None else ''
d = odesign('AI_lib', 'Wilkinson_2G4_v2')
try:
    out = []
    for i in d.instances:
        master = i.master_cell
        row = {'name': i.name, 'master': f'{master.library.name}:{master.name}',
               'origin': [i.origin.x, i.origin.y],
               'params': {p.name: str(p.value) for p in i.parameters},
               'vars': dict(i.vars) if getattr(i, 'is_var_instance', False) else {},
               'pins': [(str(p.inst_term.term_number), netname(p)) for p in i.inst_pins]}
        out.append(row)
    print(json.dumps(out, ensure_ascii=False))
finally:
    d.close_design()
