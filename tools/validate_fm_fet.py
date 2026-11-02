"""Read the actual ADS amplifier dataset, compare implementation, report FM performance."""
import json
from pathlib import Path
import numpy as np
from design_fm_fet import network,measures
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'output/FET_FM'
def main():
    d=json.loads((OUT/'design.json').read_text());e=json.loads((OUT/'ads_execution.json').read_text())
    if e['status']!='simulated':raise RuntimeError('Simulation incomplete')
    data=json.loads((OUT/'ads_dataset.json').read_text());f=np.array(data['freq'])/1e6
    s=np.empty((len(f),2,2),complex)
    for i in range(2):
        for j in range(2):
            v=data[f'S[{i+1},{j+1}]'];s[:,i,j]=np.array(v['real'])+1j*np.array(v['imag'])
    x=np.array(list(d['values'].values()));expected=network(f,x);err=float(abs(expected-s).max())
    if err>1e-4:raise AssertionError(f'ADS implementation mismatch {err}')
    m=measures(s);fm=(f>=88)&(f<=108);validated=f>=100
    r={'implementation_max_complex_S_difference':err,'points':len(f),'FM':{},'sweep_stability':{}}
    for k,v in m.items():r['FM'][k]={'min':float(v[fm].min()),'max':float(v[fm].max()),'at_98MHz':float(v[np.where(f==98)[0][0]])}
    for k in ['K','mu','mu2','delta_abs']:
        v=m[k];r['sweep_stability'][k]={'min':float(v[validated].min()),'max':float(v[validated].max())}
    r['FM_unconditionally_stable_on_sampled_points']=bool(np.all(m['K'][fm]>1)&np.all(m['delta_abs'][fm]<1))
    r['validated_model_band_unconditionally_stable_on_sampled_points']=bool(np.all(m['K'][validated]>1)&np.all(m['delta_abs'][validated]<1))
    (OUT/'validation.json').write_text(json.dumps(r,indent=2),encoding='utf-8')
    import matplotlib;matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axs=plt.subplots(3,1,figsize=(10,10),sharex=True)
    axs[0].plot(f[fm],m['gain_dB'][fm],label='ADS S21 gain');axs[0].set_ylabel('Gain (dB)')
    for k in ['s11_dB','s22_dB']:axs[1].plot(f[fm],m[k][fm],label=k)
    axs[1].set_ylabel('Reflection (dB)')
    for k in ['K','mu','mu2']:axs[2].plot(f[fm],m[k][fm],label=k)
    axs[2].axhline(1,color='k',linestyle=':');axs[2].set_ylabel('Stability factors');axs[2].set_xlabel('Frequency (MHz)')
    for ax in axs:ax.axvspan(88,100,color='orange',alpha=.13,label='Below source data: extrapolation');ax.grid(alpha=.3);ax.legend(fontsize=8)
    fig.suptitle('J310 FM common-source amplifier: actual ADS simulation');fig.tight_layout();fig.savefig(OUT/'FM_performance.png',dpi=150)
    lines=['# FET 工程：J310 FM 共源放大器','',
      '已在独立 ADS 工程中创建 88–108 MHz、50 Ω 输入/输出的单级共源放大器并完成实际 S 参数仿真。采用前次反推的 J310 有源小信号模型，直流工作点适用条件为 VDS=10 V、ID=10 mA。','',
      f"工程：`{e['workspace']}`；库：`{e['library']}`；原理图：`{e['cell']}:schematic`。",'',
      '## 电路与元件','',
      '输入采用 1 nF 隔直电容和串联匹配电感，输入端并联匹配电容。栅极串联阻尼电阻；源极增加小电阻。漏端电感兼作供电射频隔离及匹配，输出串联电容隔直。漏栅反馈电阻通过 1 nF 电容隔直；漏端另有 1 nF 隔直的阻尼负载。栅极通过 1 MΩ 接偏置参考。内部三端 R/L 和 FET 参数保持前次反推值。','',
      '| 元件参数 | 值 |','|---|---:|']
    for k,v in d['values'].items():lines.append(f'| {k} | {v:g} |')
    lines+=['','这些匹配值是数值设计值；实物选型应采用接近值/可调元件并重新仿真。匹配 L/C 尚未加入厂家 Q、SRF、PCB 或焊盘模型，内部有源 FET 已包含损耗及延迟。','',
      '## 实际 ADS 仿真结果','', '| 指标 | FM 全带范围 | 98 MHz |','|---|---:|---:|']
    for k,v in r['FM'].items():lines.append(f"| {k} | {v['min']:.4g} ～ {v['max']:.4g} | {v['at_98MHz']:.4g} |")
    lines+=['',f"实际仿真 {len(f)} 点，88–1000 MHz，步进 1 MHz；ADS 与独立网络计算最大复数 S 差 {err:.3g}。FM 扫描点满足 K>1 且 |Δ|<1：{r['FM_unconditionally_stable_on_sampled_points']}；100–1000 MHz 扫描点满足该条件：{r['validated_model_band_unconditionally_stable_on_sampled_points']}。这些是线性二端口、指定频率扫描点上的稳定性判据，不是全频、启动瞬态或实物稳定性证明。",'',
      '## 偏置实施与有效范围','',
      'ADS 原理图是工作点附近的射频等效电路。G_VDD_AC 和 G_VBIAS_AC 表示已去耦电源及偏置的交流参考地，不表示实物将漏极供电或栅极偏置直接接地。',
      '实物中 L_FEED_MATCH 的参考端接充分去耦的 VDD；R_BIAS 的参考端接可调负栅压。源极电阻另一端接地。先调栅极偏置使漏极电流为 10 mA，再调供电使器件外部 VDS 为 10 V；VDD 约为 10 V + 10 mA × Rsource，另加电感与线路直流压降。精确栅压需要实测或原厂 DC 模型，不能由本次小信号模型确定。',
      'J310 是耗尽型 N 沟道 JFET，零栅压电流范围较宽，不能以某个未经验证的固定栅压保证 10 mA。该工程没有用线性 FET 的 DC 求解冒充真实偏置验证。',
      '原始 RF 图为公共栅、100–1000 MHz。改成公共源以及 88–100 MHz 属于等效模型推算，尚无独立测量验证；100–108 MHz 也仅在模型已有频段内，公共源接法仍需测量校验。噪声、压缩、互调与谐波未验证。','',
      '结构参考：[TU Delft 共源级教材](https://analog-electronics.tudelft.nl/webbook/SED/_build/html/CSstage/CS-stage_Introduction.html)。具体元件值来自本工程数值综合，不是教材或厂家推荐电路。','',
      '## 文件','', '- design.json：元件值和优化记录。','- build_request.json：完整原理图实例与接线清单。','- ads_execution.json：真实网表与数据集路径。','- ads_raw.csv / ads_dataset.json：仿真原始数据。','- validation.json：验证指标。','- FM_performance.png：实际 ADS 性能曲线。','',
      '复现：tools/design_fm_fet.py → tools/build_fm_fet_ads.py → tools/validate_fm_fet.py；中间一步使用 ADS 自带 Python。']
    (OUT/'FM放大器设计报告.md').write_text('\n'.join(lines),encoding='utf-8')
    (Path(e['workspace'])/'FM设计说明.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps(r,indent=2))
if __name__=='__main__':main()
