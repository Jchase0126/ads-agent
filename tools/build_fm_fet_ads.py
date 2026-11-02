"""Create a separate FET ADS workspace and simulate the FM amplifier."""
import argparse,json,sys,uuid
from pathlib import Path
import numpy as np
from build_j310_inverse_ads import spec_for
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'addon/ads_agent'))
OUT=ROOT/'output/FET_FM'

def make_spec(lib,cell,d):
    v=d['values'];p=d['parameters']
    core=spec_for(lib,cell,p,'package_delay')['instances'][1]
    instances=[{'master':'ads_simulation:TermG','name':'RF_IN','params':{'Num':'1','Z':'50 Ohm'}},core,
      {'master':'ads_simulation:TermG','name':'RF_OUT','params':{'Num':'2','Z':'50 Ohm'}},
      {'master':'ads_simulation:S_Param','name':'SP1','params':{'Start':'88 MHz','Stop':'1000 MHz','Step':'1 MHz','CalcS':'yes','CalcY':'yes','CalcNoise':'No'}}]
    connections=[]
    def add(name,master,param,value,unit):
        params={param:f'{value:.12g} {unit}'}
        if master in ('R','L'):params['Noise']='No'
        instances.append({'master':'ads_rflib:'+master,'name':name,'params':params})
    def connect(a,b):connections.append({'a':list(a),'b':list(b)})
    def ground(name,node):
        instances.append({'master':'ads_rflib:GROUND','name':name});connect(node,(name,1))
    def chain(nodes):
        for a,b in zip(nodes,nodes[1:]):connect(a,b)
    for n in ['g','d','s']:
        add('R'+n.upper()+'_PKG','R','R',p['R'+n+'_Ohm'],'Ohm')
        add('L'+n.upper()+'_PKG','L','L',p['L'+n+'_nH'],'nH')
        connect(('R'+n.upper()+'_PKG',2),('L'+n.upper()+'_PKG',1))
        connect(('L'+n.upper()+'_PKG',2),('JFET_LINEAR',{'g':1,'d':2,'s':3}[n]))
    for name,master,param,val,unit in [('C_BLOCK','C','C',1000,'pF'),('L_IN','L','L',v['Lin_nH'],'nH'),
        ('C_IN','C','C',v['Cin_pF'],'pF'),('R_GATE','R','R',v['Rgate_Ohm'],'Ohm'),
        ('R_SOURCE','R','R',v['Rsource_Ohm'],'Ohm'),('L_FEED_MATCH','L','L',v['Lout_nH'],'nH'),
        ('C_OUT','C','C',v['Cout_pF'],'pF'),('C_DAMP_BLOCK','C','C',1000,'pF'),
        ('R_DAMP','R','R',v['Rdrain_Ohm'],'Ohm'),('R_BIAS','R','R',1e6,'Ohm'),
        ('C_FB_BLOCK','C','C',1000,'pF'),('R_FEEDBACK','R','R',v['Rfeedback_Ohm'],'Ohm')]:add(name,master,param,val,unit)
    connect(('RF_IN',1),('C_BLOCK',1));connect(('C_BLOCK',2),('L_IN',1))
    connect(('L_IN',2),('R_GATE',1));connect(('R_GATE',2),('RG_PKG',1))
    connect(('RF_IN',1),('C_IN',1));ground('G_INPUT',('C_IN',2))
    connect(('R_GATE',1),('R_BIAS',1));ground('G_VBIAS_AC',('R_BIAS',2))
    connect(('RS_PKG',1),('R_SOURCE',1));ground('G_SOURCE',('R_SOURCE',2))
    connect(('RD_PKG',1),('C_OUT',1));connect(('C_OUT',2),('RF_OUT',1))
    connect(('RD_PKG',1),('L_FEED_MATCH',1));ground('G_VDD_AC',('L_FEED_MATCH',2))
    connect(('RD_PKG',1),('C_DAMP_BLOCK',1));connect(('C_DAMP_BLOCK',2),('R_DAMP',1));ground('G_DAMP',('R_DAMP',2))
    connect(('RD_PKG',1),('C_FB_BLOCK',1));connect(('C_FB_BLOCK',2),('R_FEEDBACK',1));connect(('R_FEEDBACK',2),('R_GATE',1))
    return {'library':lib,'cell':cell,'layout':'auto','recreate':False,'instances':instances,'connections':connections}

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    from keysight.ads import de,dataset
    import ads_ops
    parser=argparse.ArgumentParser()
    parser.add_argument('--workspace',type=Path,help='ADS workspace; defaults to recorded deployment location')
    args=parser.parse_args()
    deployment=OUT/'deployment.json'
    recorded=json.loads(deployment.read_text(encoding='utf-8')).get('workspace') if deployment.exists() else None
    d=json.loads((OUT/'design.json').read_text())
    wp=(args.workspace or (Path(recorded) if recorded else OUT/'FET_wrk')).resolve()
    lib='FET_lib';cell='FM_J310_CS'
    marker=wp/'fm_fet_owned.json'
    if wp.exists():
        if not marker.exists():raise RuntimeError('Existing workspace has no ownership marker')
        w=de.open_workspace(str(wp));cell+='_'+uuid.uuid4().hex[:8]
    else:
        w=de.create_workspace(str(wp));w.open()
        de.create_new_library(lib,str(wp/lib));w.add_library(lib,str(wp/lib),de.LibraryMode.NON_SHARED)
        marker.write_text(json.dumps({'purpose':'User requested FM J310 amplifier','library':lib}))
    r={'workspace':str(wp),'library':lib,'cell':cell,'status':'building','scope':'fixed-bias linear FM candidate; RF equivalents of bias returns'}
    try:
        spec=make_spec(lib,cell,d);(OUT/'build_request.json').write_text(json.dumps(spec,indent=2))
        r['build']=ads_ops.build_schematic(spec);print('Built',cell,flush=True)
        r['simulation']=ads_ops.run_simulation({'library':lib,'cell':cell});print('Simulated',flush=True)
        df=dataset.open(r['simulation']['dataset_path'])['SP1.SP'].to_dataframe().reset_index()
        df.to_csv(OUT/'ads_raw.csv',index=False)
        cols={}
        for c in df.columns:
            a=df[c].to_numpy()
            if np.iscomplexobj(a):cols[str(c)]={'real':a.real.tolist(),'imag':a.imag.tolist()}
            elif np.issubdtype(a.dtype,np.number):cols[str(c)]=a.tolist()
        (OUT/'ads_dataset.json').write_text(json.dumps(cols,indent=2));r['status']='simulated';r['points']=len(df)
    except Exception as ex:r['status']='failed';r['error']=str(ex);raise
    finally:
        (OUT/'ads_execution.json').write_text(json.dumps(r,indent=2,ensure_ascii=False),encoding='utf-8')
        if r['status']=='simulated':(OUT/'deployment.json').write_text(json.dumps(r,indent=2,ensure_ascii=False),encoding='utf-8')
        de.close_workspace()
if __name__=='__main__':main()
