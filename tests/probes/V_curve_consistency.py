"""V：闭环3 曲线一致性 —— read_traces 与 dataset 直读逐点对比。"""
import sys

ROOT = r"D:\Antenna\ADS Agent"
sys.path.insert(0, ROOT + r"\backend")
import config  # noqa: E402
import tools  # noqa: E402

cfg = config.load()
DS = r"E:\ADS\Work_AI\AI_wrk\ads_agent_sim\Wilkinson_2G4_v2_20260925_022750\Wilkinson_2G4_v2.ds"

r = tools.call(cfg, "read_traces", {"path": DS, "expressions": ["dB(S(2,1))", "S(2,1)"],
                                    "max_points": 100000})
tr = r.get("traces") or {}
x1 = tr["dB(S(2,1))"]["x"]
y1 = tr["dB(S(2,1))"]["y"]
c1 = tr["S(2,1)"]["y"]

CODE = f'''
import json
import math
import keysight.ads.dataset as dataset
data = dataset.open(r"{DS}")
vb = data["SP1.SP"]
df = vb.to_dataframe()
freq = list(df.index)
s21 = [complex(v) for v in df["S[2,1]"]]
out = {{"freq": freq, "s21": [[v.real, v.imag] for v in s21]}}
print(json.dumps(out))
'''
rp = tools.call(cfg, "run_python", {"code": CODE})
import json
d2 = json.loads(rp["stdout"])
freq2, s21_2 = d2["freq"], [complex(a, b) for a, b in d2["s21"]]

print(f"read_traces 点数 {len(x1)}, dataset 直读点数 {len(freq2)}")
assert len(x1) == len(freq2), "点数不一致"
df_err = max(abs(a - b) for a, b in zip(x1, freq2))
mag_err = max(abs(abs(complex(c)) - abs(s)) for c, s in zip(c1, s21_2))
db_err = max(abs(a - (20 * __import__("math").log10(abs(s)))) for a, s in zip(y1, s21_2))
print(f"横轴最大差 {df_err:.3g} Hz；幅度最大差 {mag_err:.3g}；dB 值最大差 {db_err:.3g} dB")
print("一致" if df_err < 1e-3 and mag_err < 1e-12 and db_err < 1e-9 else "不一致！")
