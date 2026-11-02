"""Validate native ADS circuits and explore sensitivity to digitization errors."""
import json,hashlib
from pathlib import Path
import numpy as np
from scipy.optimize import least_squares
from fit_j310_physical import PARAMS,MODELS,circuit_y,y_to_s,score
from extract_j310_curves import CURVES,sample,matrix,metrics

def main():
    root=Path(__file__).resolve().parents[1]/'output/datasheet/SMMBFJ310L'
    out=root/'inverse'
    fit=json.loads((out/'fit_results.json').read_text())
    exe=json.loads((out/'ads_execution.json').read_text())
    if exe['status']!='simulated':raise RuntimeError('ADS execution incomplete')
    cal=json.loads((root/'curve_calibration.json').read_text())
    paths={k:np.array(v) for k,v in cal['raw_paths'].items()}
    def target(f,l):return matrix({k:sample(paths[k],CURVES[k],f) for k in CURVES},l)
    report={'implementation_tolerance_max_complex_S':1e-5,
        'tolerance_note':'Engineering agreement check, not machine precision; far below digitized chart discrepancies.',
        'models':{}}
    import matplotlib;matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(4,2,figsize=(11,12),sharex=True)
    for kind in ['intrinsic','package_delay']:
        d=json.loads((out/(kind+'_ads_dataset.json')).read_text())
        f=np.array(d['freq'])/1e6
        actual={}
        for l in ['S','Y']:
            a=np.empty((len(f),2,2),complex)
            for i in range(2):
                for j in range(2):
                    v=d[f'{l}[{i+1},{j+1}]'];a[:,i,j]=np.array(v['real'])+1j*np.array(v['imag'])
            actual[l]=a
        expected=y_to_s(circuit_y(f,fit['candidates'][kind]['parameters'],kind))
        err=float(abs(actual['S']-expected).max())
        if len(f)!=181 or err>1e-5:raise AssertionError(f'{kind}: implementation mismatch {err}')
        ds=Path(exe['models'][kind]['simulation']['dataset_path'])
        train=np.array(fit['training']['frequency_MHz'])
        held=np.array(fit['validation']['held_out_midband_MHz'])
        scale=np.maximum(np.max(abs(target(train,'Y')),axis=0),1e-5)
        idx=np.searchsorted(f,held)
        report['models'][kind]={'cell':exe['models'][kind]['cell'],'dataset':str(ds),
            'dataset_sha256':hashlib.sha256(ds.read_bytes()).hexdigest(),'points':len(f),
            'uses_s2p':False,'implementation_max_complex_S_error':err,'implementation_verified':True,
            'held_out_Y_weighted_complex_nrmse':score(actual['Y'][idx],target(held,'Y'),scale),
            'vs_independent_S_figures':metrics(actual['S'],target(f,'S')),
            'vs_Y_figures_SI':metrics(actual['Y'],target(f,'Y'))}
        for r,(i,j) in enumerate([(0,0),(1,0),(0,1),(1,1)]):
            z=actual['S'][:,i,j]
            axes[r,0].semilogx(f,abs(z),label='ADS '+kind)
            axes[r,1].semilogx(f,np.angle(z,deg=True),label='ADS '+kind)
            if kind=='package_delay':
                z=target(f,'S')[:,i,j]
                axes[r,0].semilogx(f,abs(z),'k:',label='Independent datasheet S')
                axes[r,1].semilogx(f,np.angle(z,deg=True),'k:',label='Independent datasheet S')
                for ax in axes[r]:ax.axvspan(350,600,color='gray',alpha=.12);ax.grid(True,alpha=.25);ax.legend(fontsize=8)
                axes[r,0].set_ylabel(f'|S{i+1}{j+1}|');axes[r,1].set_ylabel(f'S{i+1}{j+1} phase (deg)')
    for ax in axes[-1]:ax.set_xlabel('Frequency (MHz)')
    fig.suptitle('Actual ADS active circuits vs independent datasheet S curves');fig.tight_layout()
    fig.savefig(out/'ads_inverse_validation.png',dpi=160);plt.close(fig)
    (out/'ads_validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    # Explicit hypothetical perturbations, not statistical confidence intervals.
    names=MODELS['package_delay'];p=fit['candidates']['package_delay']['parameters']
    seed=np.array([p[n] for n in names]);lo=np.array([PARAMS[n][0] for n in names]);hi=np.array([PARAMS[n][1] for n in names])
    yt=target(train,'Y');yh=target(held,'Y');rng=np.random.default_rng(310);rows=[]
    for n in range(24):
        perturbed=yt*(1+rng.normal(0,.01,yt.shape))*np.exp(1j*np.deg2rad(rng.normal(0,.5,yt.shape)))
        def residual(x):
            v=(circuit_y(train,dict(zip(names,x)))-perturbed)/scale
            return np.r_[v.real.ravel(),v.imag.ravel()]
        sol=least_squares(residual,seed,bounds=(lo,hi),x_scale='jac',max_nfev=800)
        q=dict(zip(names,map(float,sol.x)))
        rows.append({'parameters':q,'fit_success':bool(sol.success),
            'unperturbed_held_out_nrmse':score(circuit_y(held,q),yh,scale)})
    sensitivity={'seed':310,'scenario_count':24,'magnitude_fraction_std':.01,'phase_deg_std':.5,
        'meaning':'Hypothetical independent curve-reading perturbations only. Not measured uncertainty, not confidence intervals.',
        'parameter_ranges':{n:[min(r['parameters'][n] for r in rows),max(r['parameters'][n] for r in rows)] for n in names},
        'unperturbed_held_out_nrmse_range':[min(r['unperturbed_held_out_nrmse'] for r in rows),max(r['unperturbed_held_out_nrmse'] for r in rows)],
        'scenarios':rows}
    (out/'sensitivity.json').write_text(json.dumps(sensitivity,indent=2),encoding='utf-8')
    print(json.dumps({'ADS':report['models'],'sensitivity_ranges':sensitivity['parameter_ranges']},indent=2))

if __name__=='__main__':main()
