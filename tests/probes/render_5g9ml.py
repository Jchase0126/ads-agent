
import ads_ops
d = ads_ops._open_design("AI_lib", "Wilkinson_5G9_ML", "schematic", write=False)
p = r"D:/Antenna/ADS Agent/logs/probe_5g9ml_render.pdf"
d.print_design(p)
print("ok", p)
