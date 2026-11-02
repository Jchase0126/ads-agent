"""Reproduce vector digitization of the supplied, SHA-locked ON J310 datasheet.

Run with the document runtime (PyMuPDF, NumPy, SciPy, Matplotlib).
This is an explicitly reviewed sample workflow, not a generic PDF font bypass.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
from pathlib import Path
import numpy as np
import pymupdf
from scipy.optimize import least_squares

SHA = "113d3cb19b47f74879741cf408983f03ab89b0123f1e1beecc3bd8a2442871b8"
# PDF points: x-left, y-top, x-right, y-bottom. Axes reviewed on page 4.
BOXES = {"Ymag": (100.460,69.165,241.285,213.619),
         "Smag": (361.757,68.824,502.469,213.278),
         "Yphase": (100.573,302.570,241.285,445.946),
         "Sphase": (361.701,302.570,502.696,446.853)}
# drawing index, axis group, value at bottom, value at top
CURVES = {
 "Y11mag": (72,"Ymag",0,30), "Y21mag": (74,"Ymag",0,30),
 "Y22mag": (76,"Ymag",0,30), "Y12mag": (78,"Ymag",0,3),
 "S11mag": (83,"Smag",.15,.45), "S12mag": (84,"Smag",0,.06),
 "S21mag": (85,"Smag",.55,.85), "S22mag": (86,"Smag",.90,1),
 "Y11phase": (115,"Yphase",0,50), "Y12phase": (116,"Yphase",-200,0),
 "Y22phase": (117,"Yphase",82,87), "Y21phase": (118,"Yphase",130,180),
 "S11phase": (126,"Sphase",-120,-20), "S21phase": (127,"Sphase",-100,0),
 "S22phase": (128,"Sphase",-100,0), "S12phase": (129,"Sphase",20,120),
}

def flatten(items):
    points=[]
    for item in items:
        if item[0]=="l":
            points.extend([tuple(item[1]),tuple(item[2])])
        elif item[0]=="c":
            p=np.array([tuple(p) for p in item[1:]])
            for t in np.linspace(0,1,51):
                points.append(((1-t)**3*p[0]+3*(1-t)**2*t*p[1]+3*(1-t)*t*t*p[2]+t**3*p[3]).tolist())
        else: raise ValueError(f"Unexpected path operator {item[0]}")
    a=np.array(points); a=a[np.argsort(a[:,0])]
    x,unique=np.unique(a[:,0],return_index=True)
    return np.column_stack((x,a[unique,1]))

def sample(path, spec, freq):
    _,box,low,high=spec; x0,y0,x1,y1=BOXES[box]
    x=x0+np.log10(freq/100)*(x1-x0)
    if np.min(x)<path[0,0]-1.5 or np.max(x)>path[-1,0]+1.5:
        raise ValueError("Curve does not cover the requested domain")
    y=np.interp(x,path[:,0],path[:,1])
    return low+(y1-y)/(y1-y0)*(high-low)

def matrix(values,letter):
    out=np.zeros((len(next(iter(values.values()))),2,2),complex)
    for i,j in ((1,1),(1,2),(2,1),(2,2)):
        key=f"{letter}{i}{j}"
        out[:,i-1,j-1]=values[key+"mag"]*np.exp(1j*np.deg2rad(values[key+"phase"]))
    return out*(.001 if letter=="Y" else 1)

def y_to_s(y,z0=50):
    eye=np.eye(2)
    return np.array([(eye-z0*m)@np.linalg.inv(eye+z0*m) for m in y])

def s_to_y(s,z0=50):
    eye=np.eye(2)
    return np.array([(eye-m)@np.linalg.inv(eye+m)/z0 for m in s])

def metrics(actual,target):
    result={}
    for i,j in ((1,1),(1,2),(2,1),(2,2)):
        a=actual[:,i-1,j-1]; b=target[:,i-1,j-1]
        phase=np.rad2deg(np.angle(a*np.conj(b)))
        result[f"{i}{j}"]={"magnitude_rmse":float(np.sqrt(np.mean((abs(a)-abs(b))**2))),
                          "magnitude_max_abs":float(np.max(abs(abs(a)-abs(b)))),
                          "phase_rmse_deg":float(np.sqrt(np.mean(phase**2))),
                          "phase_max_abs_deg":float(np.max(abs(phase))),
                          "complex_rmse":float(np.sqrt(np.mean(abs(a-b)**2)))}
    return result

def write_touchstone(path,freq,s,origin):
    with path.open("w",encoding="ascii") as stream:
        stream.write("! Approximate digitized common-gate J310 family curves, NOT manufacturer model\n")
        stream.write("! VDS=10V ID=10mA TA=25C; port1=source port2=drain common=gate\n")
        stream.write("! 100-1000 MHz only; endpoint path clipping <=1.5 PDF points; no DC/noise/nonlinear claims\n")
        stream.write(f"! Origin: {origin}; source SHA256 {SHA}\n# MHz S RI R 50\n")
        for f,m in zip(freq,s):
            v=[m[0,0],m[1,0],m[0,1],m[1,1]]
            stream.write(f"{f:.9g} "+" ".join(f"{z.real:.12g} {z.imag:.12g}" for z in v)+"\n")

def main():
    parser=argparse.ArgumentParser();parser.add_argument("pdf",type=Path)
    parser.add_argument("--output",type=Path,default=Path("output/datasheet/SMMBFJ310L"))
    args=parser.parse_args();raw=args.pdf.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=SHA: raise ValueError("Reviewed sample SHA mismatch")
    out=args.output;out.mkdir(parents=True,exist_ok=True)
    pdf=pymupdf.open(args.pdf);draw=pdf[3].get_drawings()
    paths={name:flatten(draw[spec[0]]["items"]) for name,spec in CURVES.items()}
    for name,p in paths.items():
        if p[-1,0]-p[0,0]<135: raise ValueError(f"Incomplete vector curve {name}")
    (out/"curve_calibration.json").write_text(json.dumps({"source_sha256":SHA,"page":4,
        "axes_pdf_points":BOXES,"curve_axes":CURVES,"frequency_axis":"log10",
        "endpoint_note":"Clamped to path endpoint within 1.5 PDF points; endpoints are approximate",
        "raw_paths":{k:v.tolist() for k,v in paths.items()}},indent=2),encoding="utf-8")
    freq=np.arange(100,1001,10,dtype=float)
    values={k:sample(paths[k],CURVES[k],freq) for k in CURVES}
    s=matrix(values,"S");y=matrix(values,"Y");from_y=y_to_s(y)
    with (out/"digitized_curves.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.writer(f);w.writerow(["frequency_MHz",*CURVES])
        w.writerows([[float(freq[i]),*[float(values[k][i]) for k in CURVES]] for i in range(len(freq))])
    write_touchstone(out/"SMMBFJ310L_CG_from_S.s2p",freq,s,"Figures 5 and 7")
    write_touchstone(out/"SMMBFJ310L_CG_from_Y.s2p",freq,from_y,"Figures 4 and 6, converted with 50 ohm reference")
    fit=least_squares(lambda v:np.concatenate(((y_to_s(y,np.exp(v[0]))-s).real.ravel(),
                                              (y_to_s(y,np.exp(v[0]))-s).imag.ravel())),[np.log(50)],
                      bounds=([np.log(1)],[np.log(500)]))
    held=np.arange(105,1000,10,dtype=float)
    held_s=matrix({k:sample(paths[k],CURVES[k],held) for k in CURVES},"S")
    interpolated=np.empty_like(held_s)
    for i in range(2):
        for j in range(2): interpolated[:,i,j]=np.interp(held,freq,s[:,i,j].real)+1j*np.interp(held,freq,s[:,i,j].imag)
    report={"source_sha256":SHA,"scope":{"device":"J310 family typical curves; not unit characterization",
       "configuration":"common_gate","port1":"source","port2":"drain","reference":"gate",
       "vds_V":10,"id_mA":10,"ta_C":25,"frequency_MHz":[100,1000],"z0_ohm":50,
       "z0_status":"assumed; tested by independent Y/S cross-chart conversion; not explicitly labelled in PDF"},
       "Y_to_S_cross_chart":metrics(from_y,s),"S_to_Y_cross_chart_SI":metrics(s_to_y(s),y),
       "best_fit_equal_real_z0_ohm":float(np.exp(fit.x[0])),
       "linear_complex_interpolation_held_out":metrics(interpolated,held_s),
       "roundtrip_y_s_y_max_abs_S":float(np.max(abs(s_to_y(from_y)-y))),
       "validation_note":"Cross-chart agreement is evidence of digitization/mapping, not measured device accuracy."}
    (out/"mapping_validation.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    import matplotlib;matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(4,2,figsize=(11,12),sharex=True)
    for row,(i,j) in enumerate(((1,1),(2,1),(1,2),(2,2))):
        key=f"S{i}{j}";a=s[:,i-1,j-1];b=from_y[:,i-1,j-1]
        axes[row,0].semilogx(freq,abs(a),label="Figures 5/7")
        axes[row,0].semilogx(freq,abs(b),"--",label="Figures 4/6 -> S (50 ohm)")
        axes[row,1].semilogx(freq,np.rad2deg(np.angle(a)),label="Figures 5/7")
        axes[row,1].semilogx(freq,np.rad2deg(np.angle(b)),"--",label="Figures 4/6 -> S (50 ohm)")
        axes[row,0].set_ylabel(f"|{key}|");axes[row,1].set_ylabel(f"angle {key} (deg)")
        for ax in axes[row]:ax.grid(True,which="both",alpha=.3);ax.legend(fontsize=8)
    axes[-1,0].set_xlabel("Frequency (MHz)");axes[-1,1].set_xlabel("Frequency (MHz)")
    fig.suptitle("J310 datasheet vector mapping: independent Y/S cross-chart check\nVDS=10 V, ID=10 mA, TA=25 C, common gate; approximate digitization")
    fig.tight_layout();fig.savefig(out/"mapping_comparison.png",dpi=150);plt.close(fig)
    print(json.dumps(report,indent=2))

if __name__=="__main__":main()
