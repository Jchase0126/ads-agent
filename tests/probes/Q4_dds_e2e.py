"""Q4c：查 save 后文件真实落点 + get_dds_path 语义。"""
import os
import tempfile

import keysight.ads.dds as dds

print("get_dds_path():", end=" ")
try:
    print(dds.get_dds_path())
except Exception as e:
    print(type(e).__name__, e)
print("get_dds_files():", end=" ")
try:
    print(dds.get_dds_files())
except Exception as e:
    print(type(e).__name__, e)

DS = r"E:\ADS\Work_AI\AI_wrk\ads_agent_sim\Wilkinson_5G9_20260925_002328\Wilkinson_5G9.ds"
tmp = tempfile.mkdtemp(prefix="dds_probe_").replace("/", "\\")
path = os.path.join(tmp, "probe.dds")
f = dds.new_dds_file(path)
f.add_dataset_alias("wil9", DS)
page = f.new_page("R1")
page.add_plot(traces=["dB(S(2,1))"], title="probe")
print("process cwd:", os.getcwd())
try:
    f.save(name="probe", path=tmp)
    print("save(name,path) ok")
except Exception as e:
    print("save(name,path) fail:", type(e).__name__, str(e)[:100])
    f.save(name="probe")
print("exists target =", os.path.exists(path))
for root in (tmp, os.getcwd(), r"E:\ADS\Work_AI\AI_wrk"):
    for dirpath, dirs, files in os.walk(root if os.path.isdir(root) else "."):
        if "probe.dds" in files:
            hit = os.path.join(dirpath, "probe.dds")
            print("FOUND:", hit, os.path.getsize(hit))
            os.remove(hit)
            print("removed")
            break
try:
    f.close(force=True)
except Exception:
    pass
