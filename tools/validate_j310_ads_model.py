"""Validate actual ADS datasets against digitized input and independent curves."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from extract_j310_curves import CURVES,sample,matrix,metrics

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--input",type=Path,default=Path("output/datasheet/SMMBFJ310L"))
    out=parser.parse_args().input.resolve()
    execution=json.loads((out/"ads_execution.json").read_text(encoding="utf-8"))
    if execution["status"]!="simulated_both_models":raise ValueError("ADS run incomplete")
    calibration=json.loads((out/"curve_calibration.json").read_text(encoding="utf-8"))
    paths={k:np.array(v) for k,v in calibration["raw_paths"].items()}
    results={"scope":execution["scope"],"models":{}}
    import matplotlib;matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(4,2,figsize=(11,12),sharex=True)
    for suffix in ("S","Y"):
        data=json.loads((out/f"ads_from_{suffix}_dataset.json").read_text(encoding="utf-8"))
        freq=np.array(data["freq"])/1e6
        actual={}
        for letter in ("S","Y"):
            a=np.zeros((len(freq),2,2),complex)
            for i in (1,2):
                for j in (1,2):
                    entry=data[f"{letter}[{i},{j}]"]
                    a[:,i-1,j-1]=np.array(entry["real"])+1j*np.array(entry["imag"])
            actual[letter]=a
        # Read the exact file ADS consumed, not a separately reconstructed copy.
        file=Path(execution["workspace"])/"data"/f"SMMBFJ310L_CG_from_{suffix}.s2p"
        rows=np.loadtxt(file,comments=["!","#"])
        input_matrix=np.zeros((len(rows),2,2),complex)
        for n,(i,j) in enumerate(((0,0),(1,0),(0,1),(1,1))):
            input_matrix[:,i,j]=rows[:,1+2*n]+1j*rows[:,2+2*n]
        expected=np.empty_like(actual["S"])
        for i in range(2):
            for j in range(2):
                z=input_matrix[:,i,j]
                expected[:,i,j]=np.interp(freq,rows[:,0],z.real)+1j*np.interp(freq,rows[:,0],z.imag)
        digitized={k:sample(paths[k],CURVES[k],freq) for k in CURVES}
        target_s=matrix(digitized,"S");target_y=matrix(digitized,"Y")
        max_error=float(np.max(abs(actual["S"]-expected)))
        if len(freq)!=181 or max_error>1e-8:raise AssertionError(f"ADS data ingestion error {max_error}")
        ds=Path(execution["models"][suffix]["simulation"]["dataset_path"])
        results["models"][suffix]={"cell":execution["models"][suffix]["cell"],
            "dataset":str(ds),"dataset_sha256":hashlib.sha256(ds.read_bytes()).hexdigest(),
            "model_sha256":hashlib.sha256(file.read_bytes()).hexdigest(),"frequency_points":len(freq),
            "input_playback_max_complex_abs_error":max_error,"input_playback_verified":True,
            "vs_figures_5_7":metrics(actual["S"],target_s),
            "vs_figures_4_6_SI":metrics(actual["Y"],target_y)}
        for row,(i,j) in enumerate(((1,1),(2,1),(1,2),(2,2))):
            z=actual["S"][:,i-1,j-1]
            axes[row,0].semilogx(freq,abs(z),"-" if suffix=="S" else "--",label=f"ADS: from {suffix} curves")
            axes[row,1].semilogx(freq,np.rad2deg(np.angle(z)),"-" if suffix=="S" else "--",label=f"ADS: from {suffix} curves")
            if suffix=="S":
                source_s=target_s[:,i-1,j-1]
                axes[row,0].semilogx(freq[::8],abs(source_s[::8]),"k.",label="PDF S curves (held-out incl.)")
                axes[row,1].semilogx(freq[::8],np.rad2deg(np.angle(source_s[::8])),"k.",label="PDF S curves (held-out incl.)")
                axes[row,0].set_ylabel(f"|S{i}{j}|");axes[row,1].set_ylabel(f"angle S{i}{j} (deg)")
    for ax in axes.ravel():ax.grid(True,which="both",alpha=.3);ax.legend(fontsize=8)
    axes[-1,0].set_xlabel("Frequency (MHz)");axes[-1,1].set_xlabel("Frequency (MHz)")
    fig.suptitle("Actual ADS 2027 datasets vs datasheet digitization\n181 points, 100-1000 MHz; common gate, VDS=10 V, ID=10 mA, TA=25 C")
    fig.tight_layout();fig.savefig(out/"ads_validation.png",dpi=150);plt.close(fig)
    results["meaning"]="Input playback verifies ADS loading/port mapping. Cross-chart errors limit fidelity; neither proves measured device performance."
    (out/"ads_validation.json").write_text(json.dumps(results,indent=2),encoding="utf-8")
    print(json.dumps({k:{"cell":v["cell"],"points":v["frequency_points"],"input_error":v["input_playback_max_complex_abs_error"]}
                      for k,v in results["models"].items()},indent=2))

if __name__=="__main__":main()
