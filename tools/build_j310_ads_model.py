"""Build reviewed J310 common-gate data fixtures with real ADS 2027 APIs.

Use ADS's Python interpreter. Creates only a dedicated workspace; preserves
existing cells and uses ADS Agent's save/connectivity/simulation checks.
"""
from __future__ import annotations
import argparse
import json
import shutil
import sys
import uuid
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"addon"/"ads_agent"))

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser()
    parser.add_argument("--input",type=Path,default=ROOT/"output/datasheet/SMMBFJ310L")
    parser.add_argument("--workspace",type=Path)
    args=parser.parse_args();source=args.input.resolve()
    workspace=(args.workspace or source/"ADS_model_wrk").resolve()
    from keysight.ads import de
    from keysight.ads.de import db_uu as db
    import ads_ops
    if de.workspace_is_open():raise RuntimeError("Use an isolated ADS automation process")
    libname="J310_datasheet_lib"
    if not workspace.exists():
        w=de.create_workspace(str(workspace));w.open()
        libpath=workspace/libname
        de.create_new_library(libname,str(libpath))
        w.add_library(libname,str(libpath),de.LibraryMode.NON_SHARED)
    else:
        if workspace!=source/"ADS_model_wrk" and not (workspace/"j310_manifest.json").exists():
            raise RuntimeError("Existing workspace has no workflow ownership marker")
        w=de.open_workspace(str(workspace))
        if libname not in [l.name for l in w.libraries]:raise RuntimeError("Workflow library missing")
    result={"workspace":str(workspace),"library":libname,"models":{},
            "scope":json.loads((source/"mapping_validation.json").read_text())["scope"]}
    # Write ownership/scope even if simulator later fails; never claim complete early.
    result["status"]="building"
    run_id=uuid.uuid4().hex[:8]
    result["run_id"]=run_id
    manifest=workspace/"j310_manifest.json"
    manifest.write_text(json.dumps(result,indent=2),encoding="utf-8")
    try:
        data=workspace/"data";data.mkdir(exist_ok=True)
        for suffix in ("S","Y"):
            filename=f"SMMBFJ310L_CG_from_{suffix}.s2p"
            model_file=data/filename
            if model_file.exists() and model_file.read_bytes()!=(source/filename).read_bytes():
                raise RuntimeError("Existing model data changed; create a new workspace")
            shutil.copy2(source/filename,model_file)
            cell=f"CG_from_{suffix}_{run_id}"
            library=next(l for l in w.libraries if l.name==libname)
            if cell in [c.name for c in library.cells]:
                raise RuntimeError(f"Cell {cell} already exists; use a new workspace for another run")
            spec={"library":libname,"cell":cell,"layout":"auto","recreate":False,
              "instances":[
                {"master":"ads_simulation:TermG","name":"P1_SOURCE","params":{"Num":"1","Z":"50 Ohm"}},
                {"master":"ads_datacmps:S2P","name":"J310_CG","params":{"File":f'"{model_file}"',
                    "InterpMode":"Mode0","InterpDom":"ID1","Temp":"25",
                    "CheckPassivity":"y_n0","EnforcePassivity":"no"}},
                {"master":"ads_simulation:TermG","name":"P2_DRAIN","params":{"Num":"2","Z":"50 Ohm"}},
                {"master":"ads_rflib:GROUND","name":"GATE_REF"},
                {"master":"ads_simulation:S_Param","name":"SP1","params":{"Start":"100 MHz",
                    "Stop":"1000 MHz","Step":"5 MHz","CalcY":"yes","CalcS":"yes","CalcNoise":"No"}},
              ],"connections":[{"a":["P1_SOURCE",1],"b":["J310_CG",1]},
                                {"a":["J310_CG",2],"b":["P2_DRAIN",1]},
                                {"a":["J310_CG",3],"b":["GATE_REF",1]}]}
            print("Building",cell,flush=True)
            built=ads_ops.build_schematic(spec)
            result["models"][suffix]={"cell":cell,"build":built,"status":"built"}
            manifest.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")
            print("Simulating",cell,flush=True)
            sim=ads_ops.run_simulation({"library":libname,"cell":cell})
            result["models"][suffix]["simulation"]=sim
            result["models"][suffix]["status"]="simulated"
            from keysight.ads import dataset
            ds=dataset.open(sim["dataset_path"])
            names=list(ds.keys());print("DATASET blocks",names,flush=True)
            block=next(k for k in names if "SP1.SP" in str(k))
            df=ds[block].to_dataframe().reset_index()
            print("DATASET columns",list(df.columns),"rows",len(df),flush=True)
            df.to_csv(source/f"ads_from_{suffix}_raw.csv",index=False)
            cols={}
            for c in df.columns:
                values=df[c].to_numpy()
                if np.iscomplexobj(values):cols[str(c)]={"real":values.real.tolist(),"imag":values.imag.tolist()}
                elif np.issubdtype(values.dtype,np.number):cols[str(c)]=values.tolist()
            (source/f"ads_from_{suffix}_dataset.json").write_text(json.dumps(cols,indent=2),encoding="utf-8")
            result["models"][suffix]["dataset_rows"]=len(df)
            manifest.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")
        result["status"]="simulated_both_models"
    except Exception as e:
        result["status"]="failed";result["error"]=f"{type(e).__name__}: {e}"
        raise
    finally:
        manifest.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")
        (source/"ads_execution.json").write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")
        de.close_workspace()
    print("Saved",manifest,flush=True)

if __name__=="__main__":main()
