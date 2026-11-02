"""Build an actual ADS linear FET core plus terminal package R/L, no S2P."""
from __future__ import annotations
import json,sys,uuid
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"addon"/"ads_agent"))

def spec_for(lib,cell,p,kind):
    core={"G":f"{p['gm_mS']:.12g} mS","T":f"{p.get('tau_ps',0):.12g} psec","F":"0 GHz",
       "Cgs":f"{p['Cgs_pF']:.12g} pF","Cdg":f"{p['Cgd_pF']:.12g} pF",
       "Cds":f"{p['Cds_pF']:.12g} pF","Cdc":"0 pF","Ggs":"0 S","Ri":"0 Ohm",
       "Rds":f"{1e6/p['gds_uS']:.12g} Ohm"}
    inst=[{"master":"ads_simulation:TermG","name":"P1_SOURCE","params":{"Num":"1","Z":"50 Ohm"}},
          {"master":"ads_rflib:FET","name":"JFET_LINEAR","params":core},
          {"master":"ads_simulation:TermG","name":"P2_DRAIN","params":{"Num":"2","Z":"50 Ohm"}},
          {"master":"ads_rflib:GROUND","name":"GATE_REF"},
          {"master":"ads_simulation:S_Param","name":"SP1","params":{"Start":"100 MHz","Stop":"1000 MHz",
              "Step":"5 MHz","CalcY":"yes","CalcS":"yes","CalcNoise":"No"}}]
    connections=[]
    # Native linear FET symbol: gate=1, drain=2, source=3; runtime pin positions verified.
    for n,endpoint,pin in (("s",["P1_SOURCE",1],3),("d",["P2_DRAIN",1],2),("g",["GATE_REF",1],1)):
        corepin=["JFET_LINEAR",pin]
        if kind=="package_delay":
            r,l="R"+n.upper(),"L"+n.upper()
            inst.extend([{"master":"ads_rflib:R","name":r,"params":{"R":f"{p['R'+n+'_Ohm']:.12g} Ohm","Noise":"No"}},
                         {"master":"ads_rflib:L","name":l,"params":{"L":f"{p['L'+n+'_nH']:.12g} nH","Noise":"No"}}])
            connections.extend([{"a":endpoint,"b":[r,1]},{"a":[r,2],"b":[l,1]},{"a":[l,2],"b":corepin}])
        else:connections.append({"a":endpoint,"b":corepin})
    return {"library":lib,"cell":cell,"layout":"auto","recreate":False,"instances":inst,"connections":connections}

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    out=ROOT/"output/datasheet/SMMBFJ310L/inverse"
    fitted=json.loads((out/"fit_results.json").read_text())
    if fitted["selected"]!="package_delay":raise RuntimeError("No accepted packaged active model")
    workspace=out.parent/"ADS_model_wrk"
    if not (workspace/"j310_manifest.json").exists():raise RuntimeError("Owned ADS workspace missing")
    from keysight.ads import de,dataset
    import ads_ops
    if de.workspace_is_open():raise RuntimeError("Run in isolated automation process")
    w=de.open_workspace(str(workspace));lib="J310_datasheet_lib";run_id=uuid.uuid4().hex[:8]
    result={"workspace":str(workspace),"library":lib,"run_id":run_id,"models":{},"status":"building",
            "scope":fitted["scope"],"not_nonlinear":True,"uses_s2p":False}
    try:
        for kind in ("intrinsic","package_delay"):
            p=fitted["candidates"][kind]["parameters"];cell=f"Inverse_{kind}_{run_id}"
            spec=spec_for(lib,cell,p,kind)
            (out/f"{kind}_build_request.json").write_text(json.dumps(spec,indent=2),encoding="utf-8")
            print("Building",cell,flush=True)
            built=ads_ops.build_schematic(spec)
            result["models"][kind]={"cell":cell,"build":built,"status":"built"}
            (out/"ads_execution.json").write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")
            print("Simulating",cell,flush=True)
            sim=ads_ops.run_simulation({"library":lib,"cell":cell})
            result["models"][kind]["simulation"]=sim
            data=dataset.open(sim["dataset_path"])["SP1.SP"].to_dataframe().reset_index()
            data.to_csv(out/f"{kind}_ads_raw.csv",index=False)
            values={}
            for name in data.columns:
                a=data[name].to_numpy()
                if np.iscomplexobj(a):values[str(name)]={"real":a.real.tolist(),"imag":a.imag.tolist()}
                elif np.issubdtype(a.dtype,np.number):values[str(name)]=a.tolist()
            (out/f"{kind}_ads_dataset.json").write_text(json.dumps(values,indent=2),encoding="utf-8")
            result["models"][kind]["status"]="simulated"
            print("Done",kind,len(data),"points",flush=True)
        result["status"]="simulated"
    except Exception as e:
        result["status"]="failed";result["error"]=f"{type(e).__name__}: {e}"
        raise
    finally:
        (out/"ads_execution.json").write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")
        de.close_workspace()

if __name__=="__main__":main()

