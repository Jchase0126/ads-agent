d = odesign('AI_lib', 'Wilkinson_2G4_v2')
try:
    print('instances', len(list(d.instances)))
    for i in list(d.instances)[:18]:
        master = getattr(i, 'master_cell', None)
        print(i.name, 'origin=', i.origin, 'master=', master,
              'bbox=', getattr(i, 'bbox', None), 'is_valid=', getattr(i, 'is_valid', None))
    i = next(iter(d.instances))
    print('inst display attrs', [x for x in dir(i) if any(k in x.lower() for k in ('bound','box','master','symbol','view','display','origin'))])
finally:
    d.close_design()
