"""FM common-source amplifier synthesis using the reviewed active J310 core."""
import json
from pathlib import Path
import numpy as np
from scipy.optimize import differential_evolution
from fit_j310_physical import circuit_y,cg_to_cs,y_to_s
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/FET_FM';OUT.mkdir(exist_ok=True)
P=json.loads((ROOT/'output/datasheet/SMMBFJ310L/inverse/selected_parameters.json').read_text())['parameters']

def network(f,x):
    # Input: series L to gate, shunt C at input; output: drain shunt L and series C.
    li,ci,lo,co,rg,rs,rd,rf=x
    p=P.copy();p['Rg_Ohm']+=rg;p['Rs_Ohm']+=rs
    core=cg_to_cs(circuit_y(f,p))
    w=2j*np.pi*np.array(f)*1e6
    a=np.zeros((len(f),4,4),complex);a[:,2:,2:]=core
    def branch(i,j,v):
        a[:,i,i]+=v
        if j is not None:a[:,j,j]+=v;a[:,i,j]-=v;a[:,j,i]-=v
    # Fixed coupling caps and gate bias return retained in optimization.
    branch(0,2,1/(w*li*1e-9+1/(w*1000e-12)))
    branch(0,None,w*ci*1e-12)
    branch(3,1,w*co*1e-12)
    branch(3,None,1/(w*lo*1e-9)+1/(rd+1/(w*1000e-12)))
    branch(2,None,1/1e6)
    branch(2,3,1/(rf+1/(w*1000e-12)))
    y=a[:,:2,:2]-a[:,:2,2:]@np.linalg.solve(a[:,2:,2:],a[:,2:,:2])
    return y_to_s(y)

def measures(s):
    s11,s12,s21,s22=s[:,0,0],s[:,0,1],s[:,1,0],s[:,1,1]
    delta=s11*s22-s12*s21
    k=(1-abs(s11)**2-abs(s22)**2+abs(delta)**2)/(2*abs(s12*s21))
    mu=(1-abs(s11)**2)/(abs(s22-delta*np.conj(s11))+abs(s12*s21))
    mu2=(1-abs(s22)**2)/(abs(s11-delta*np.conj(s22))+abs(s12*s21))
    return {'gain_dB':20*np.log10(abs(s21)),'s11_dB':20*np.log10(abs(s11)),
        's22_dB':20*np.log10(abs(s22)),'K':k,'mu':mu,'mu2':mu2,'delta_abs':abs(delta)}

def main():
    f=np.linspace(88,108,11);wide=np.linspace(100,1000,46)
    def cost(x):
        m=measures(network(f,x));w=measures(network(wide,x))
        return float(np.mean(np.maximum(8-m['gain_dB'],0)**2)+
          2*np.mean(np.maximum(m['s11_dB']+10,0)**2)+2*np.mean(np.maximum(m['s22_dB']+10,0)**2)+
          1000*np.mean(np.maximum(1.01-w['mu'],0)**2)+1000*np.mean(np.maximum(1.01-w['mu2'],0)**2)+
          10000*np.mean(np.maximum(1.15-m['mu'],0)**2)+10000*np.mean(np.maximum(1.15-m['mu2'],0)**2))
    bounds=[(10,1500),(1,150),(50,3000),(1,300),(1,150),(.1,60),(100,3000),(200,20000)]
    r=differential_evolution(cost,bounds,seed=310,popsize=12,maxiter=320,tol=.001,polish=True)
    x=r.x
    # Preferred values keep this a concrete buildable proposal.
    vals=np.array([round(x[0]/10)*10,round(x[1],1),round(x[2]/10)*10,round(x[3],1),round(x[4],1),round(x[5],1),round(x[6]/10)*10,round(x[7]/10)*10])
    m=measures(network(np.arange(88,109),vals))
    report={'frequency_MHz':[88,108],'center_MHz':98,'z0_ohm':50,'model_validated_frequency_MHz':[100,1000],
       'below_100MHz':'extrapolation, no independent datasheet validation',
       'values':dict(zip(['Lin_nH','Cin_pF','Lout_nH','Cout_pF','Rgate_Ohm','Rsource_Ohm','Rdrain_Ohm','Rfeedback_Ohm'],vals.tolist())),
       'optimizer_cost':float(r.fun),'parameters':P,'FM_metrics':{k:{'min':float(v.min()),'max':float(v.max()),'center':float(v[10])} for k,v in m.items()}}
    (OUT/'design.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
if __name__=='__main__':main()
