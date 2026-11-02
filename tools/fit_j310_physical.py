"""Constrained inverse extraction of a fixed-bias active JFET RF circuit.

Fits ONLY Y curves outside a withheld mid-band. S figures are independent
cross-chart validation. Positive engineering bounds are not measured values.
"""
from __future__ import annotations
import argparse,json
from pathlib import Path
import numpy as np
from scipy.optimize import least_squares
from extract_j310_curves import CURVES,sample,matrix,y_to_s,metrics

PARAMS={
 "gm_mS":(8,25,14),"gds_uS":(.1,250,70),
 "Cgs_pF":(.1,15,5),"Cgd_pF":(.1,10,1.8),"Cds_pF":(0,3,.05),
 "Rg_Ohm":(.001,20,1),"Rs_Ohm":(.001,20,1),"Rd_Ohm":(.001,20,1),
 "Lg_nH":(.001,4,.5),"Ls_nH":(.001,4,.5),"Ld_nH":(.001,4,.5),
 "tau_ps":(0,150,30),
}
MODELS={"intrinsic":list(PARAMS)[:5],"intrinsic_delay":list(PARAMS)[:5]+["tau_ps"],
        "package_delay":list(PARAMS)}

def intrinsic_y3(freq_mhz,p):
    jw=2j*np.pi*np.asarray(freq_mhz)*1e6
    a=np.zeros((len(jw),3,3),complex) # g, s, d
    for i,j,name in ((0,1,"Cgs_pF"),(0,2,"Cgd_pF"),(1,2,"Cds_pF")):
        v=jw*p[name]*1e-12
        a[:,i,i]+=v;a[:,j,j]+=v;a[:,i,j]-=v;a[:,j,i]-=v
    v=p["gds_uS"]*1e-6
    a[:,1,1]+=v;a[:,2,2]+=v;a[:,1,2]-=v;a[:,2,1]-=v
    gm=p["gm_mS"]*.001*np.exp(-jw*p.get("tau_ps",0)*1e-12)
    a[:,2,0]+=gm;a[:,2,1]-=gm;a[:,1,0]-=gm;a[:,1,1]+=gm
    return a

def circuit_y(freq_mhz,p,kind="package_delay"):
    a=intrinsic_y3(freq_mhz,p)
    if kind!="package_delay":return a[:,[1,2],:][:,:,[1,2]]
    jw=2j*np.pi*np.asarray(freq_mhz)*1e6
    branches=np.stack([1/(p[f"R{n}_Ohm"]+jw*p[f"L{n}_nH"]*1e-9) for n in ("g","s","d")],axis=1)
    m=a.copy()
    for i in range(3):m[:,i,i]+=branches[:,i]
    b=np.zeros((len(jw),3,2),complex);b[:,1,0]=branches[:,1];b[:,2,1]=branches[:,2]
    ext=np.zeros((len(jw),2,2),complex);ext[:,0,0]=branches[:,1];ext[:,1,1]=branches[:,2]
    return ext-np.swapaxes(b,1,2)@np.linalg.solve(m,b)

def cg_to_cs(y):
    # [Vs-Vg,Vd-Vg] = B [Vg-Vs,Vd-Vs], [Ig,Id] = B.T [Is,Id]
    b=np.array([[-1,0],[-1,1]])
    return b.T@y@b

def score(y,target,scale):return float(np.sqrt(np.mean(abs((y-target)/scale)**2)))

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--input",type=Path,default=Path("output/datasheet/SMMBFJ310L"))
    args=parser.parse_args();source=args.input.resolve();out=source/"inverse";out.mkdir(exist_ok=True)
    cal=json.loads((source/"curve_calibration.json").read_text(encoding="utf-8"))
    paths={k:np.array(v) for k,v in cal["raw_paths"].items()}
    def target(f,letter):return matrix({k:sample(paths[k],CURVES[k],f) for k in CURVES},letter)
    freq=np.arange(100,1001,10,dtype=float)
    train=np.array([100,150,200,250,300,650,700,750,800,850,900,950,1000],float)
    held=np.array([350,400,450,500,550,600],float)
    yt=target(train,"Y");yh=target(held,"Y");ys=target(freq,"Y");ss=target(freq,"S")
    scale=np.maximum(np.max(abs(yt),axis=0),1e-5)
    report={"source_sha256":cal["source_sha256"],"scope":json.loads((source/"mapping_validation.json").read_text())["scope"],
      "training":{"source":"Y Figures 4/6 only","frequency_MHz":train.tolist(),"z0_ohm":50},
      "validation":{"held_out_midband_MHz":held.tolist(),"independent_source":"S Figures 5/7 never fitted",
         "exploratory_rule":"held-out Y weighted complex NRMSE <= 0.10, effective gm 8..18 mS and gds <=250 uS",
         "note":"Exploratory engineering rule, not manufacturer accuracy or user acceptance specification"},
      "engineering_bounds":{k:{"min":v[0],"max":v[1]} for k,v in PARAMS.items()},"candidates":{}}
    for kind,names in MODELS.items():
        lo=np.array([PARAMS[n][0] for n in names]);hi=np.array([PARAMS[n][1] for n in names])
        seed=np.array([PARAMS[n][2] for n in names]);rng=np.random.default_rng(310)
        def unpack(x):return dict(zip(names,map(float,x)))
        def residual(x):
            r=(circuit_y(train,unpack(x),kind)-yt)/scale
            return np.concatenate((r.real.ravel(),r.imag.ravel()))
        solutions=[]
        for n in range(8 if kind=="package_delay" else 3):
            x=seed if n==0 else np.clip(seed*np.exp(rng.normal(0,.55,len(seed))),lo+1e-7,hi-1e-7)
            fit=least_squares(residual,x,bounds=(lo,hi),x_scale="jac",max_nfev=1500,
                ftol=1e-10,xtol=1e-10,gtol=1e-10)
            solutions.append(fit)
        best=min(solutions,key=lambda a:a.cost);p=unpack(best.x)
        y=circuit_y(freq,p,kind);s=y_to_s(y)
        dc=cg_to_cs(circuit_y(np.array([.001]),p,kind))[0]
        near=[v for v in solutions if v.cost<=best.cost*1.05+1e-12]
        scaled_jac=best.jac*(hi-lo)[None,:]
        sv=np.linalg.svd(scaled_jac,compute_uv=False)
        gm=float(dc[1,0].real*1000);gds=float(dc[1,1].real*1e6)
        held_score=score(circuit_y(held,p,kind),yh,scale)
        candidate={"parameters":p,"parameter_count":len(names),"fit_success":bool(best.success),
           "train_weighted_complex_nrmse":score(circuit_y(train,p,kind),yt,scale),
           "held_out_weighted_complex_nrmse":held_score,
           "effective_CS_gm_mS_at_1kHz_extrapolated":gm,"effective_CS_gds_uS_at_1kHz_extrapolated":gds,
           "vs_Y_full_band_SI":metrics(y,ys),"vs_S_independent_figures":metrics(s,ss),
           "parameter_bound_hits":[names[i] for i in range(len(names)) if min((best.x[i]-lo[i]),(hi[i]-best.x[i]))/(hi[i]-lo[i])<.001],
           "scaled_jacobian_condition":float(sv[0]/max(sv[-1],1e-30)),
           "near_best_multistart_parameter_ranges":{k:[min(v.x[i] for v in near),max(v.x[i] for v in near)] for i,k in enumerate(names)},
           "multistart_costs":[float(v.cost) for v in solutions],
           "exploratory_rule_pass":bool(held_score<=.1 and 8<=gm<=18 and 0<=gds<=250),
           "not_validated":"1 kHz extrapolation is a consistency check only; VGS/VDS/temperature/noise/nonlinear behavior not fitted"}
        report["candidates"][kind]=candidate
        rows=np.column_stack([freq,*[v for i,j in ((0,0),(1,0),(0,1),(1,1)) for v in (s[:,i,j].real,s[:,i,j].imag)]])
        np.savetxt(out/f"{kind}_S.csv",rows,delimiter=",",header="frequency_MHz,S11_real,S11_imag,S21_real,S21_imag,S12_real,S12_imag,S22_real,S22_imag",comments="")
        print(kind,"train",candidate["train_weighted_complex_nrmse"],"held",held_score,"params",p,flush=True)
    eligible=[k for k,c in report["candidates"].items() if c["exploratory_rule_pass"]]
    selected=min(eligible,key=lambda k:report["candidates"][k]["held_out_weighted_complex_nrmse"]) if eligible else None
    report["selected"]=selected;report["status"]="candidate_selected" if selected else "no_model_meets_exploratory_rule"
    (out/"fit_results.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    if selected:
        (out/"selected_parameters.json").write_text(json.dumps({"kind":selected,**report["candidates"][selected]},indent=2),encoding="utf-8")
    import matplotlib;matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(4,2,figsize=(11,12),sharex=True)
    for row,(i,j) in enumerate(((0,0),(1,0),(0,1),(1,1))):
        for kind,c in report["candidates"].items():
            s=y_to_s(circuit_y(freq,c["parameters"],kind));z=s[:,i,j]
            axes[row,0].semilogx(freq,abs(z),label=kind);axes[row,1].semilogx(freq,np.angle(z,deg=True),label=kind)
        for col in (0,1):
            ax=axes[row,col];z=ss[:,i,j];ax.semilogx(freq[::5],abs(z[::5]) if col==0 else np.angle(z[::5],deg=True),"k.",label="Independent PDF S")
            ax.axvspan(350,600,color="gray",alpha=.15,label="Y training withheld band");ax.grid(True,which="both",alpha=.25);ax.legend(fontsize=7)
        axes[row,0].set_ylabel(f"|S{i+1}{j+1}|");axes[row,1].set_ylabel(f"phase S{i+1}{j+1} (deg)")
    axes[-1,0].set_xlabel("Frequency (MHz)");axes[-1,1].set_xlabel("Frequency (MHz)")
    fig.suptitle("Inverse active JFET circuits vs independent S figures\nFit only Y curves outside 350-600 MHz; fixed bias; no S2P used in circuit")
    fig.tight_layout();fig.savefig(out/"inverse_fit_comparison.png",dpi=140);plt.close(fig)
    print("Selected",selected)

if __name__=="__main__":main()
