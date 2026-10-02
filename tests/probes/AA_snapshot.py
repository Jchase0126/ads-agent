import json


def netname(p):
    n = p.net
    s = str(getattr(n, 'name', '') or '') if n is not None else ''
    return s


import keysight.ads.de.db_uu as db_uu
from keysight.ads.de.db_uu import DesignMode

for LIB, CELL in (("AI_lib", "Wilkinson_5G9"), ("AI_lib", "Wilkinson_2G4_v2")):
    d = db_uu.open_design(f"{LIB}:{CELL}:schematic", mode=DesignMode.READ_ONLY)
    try:
        out = []
        for i in d.instances:
            master = i.master_cell
            row = {
                'name': i.name,
                'master': f'{master.library.name}:{master.name}',
                'origin': [round(i.origin.x, 3), round(i.origin.y, 3)],
                'angle': getattr(i, 'angle', None),
                'params': {p.name: str(p.value) for p in i.parameters},
                'pins': [(str(p.inst_term.term_number),
                          (round(p.position.x, 3), round(p.position.y, 3)) if getattr(p, 'position', None) is not None else None,
                          netname(p)) for p in i.inst_pins],
            }
            out.append(row)
        print(f"===== {CELL} ({len(out)} instances) =====")
        print(json.dumps(out, ensure_ascii=False, indent=1))
    finally:
        d.close_design()
