# -*- coding: utf-8 -*-
"""Measured AppCAD page chrome (title / menu button / right column / web bar).

All numbers are pixels measured on the real AppCAD 4.00 windows captured in
``tmp/appcad/pages`` (966x669 client area).  Layout managers are deliberately
not used for these; see ``appcad_spec.SpecPage``.
"""

# Reference client size of an AppCAD calculator window.
PAGE_W, PAGE_H = 966, 669

# Menu strip (File Calculate ... Help) drawn by Windows on the real form.
MENU_Y, MENU_H = 9, 19
# "Main Menu [F8]" push button, identical on every page.
MAINMENU = (882, 33, 76, 17)
# Title baseline: glyph ink starts at y=50 and cap height is 25 px for the
# 18 pt bold Times titles, i.e. baseline = 72.
TITLE_Y = 72
TITLE_FONT = ("Times New Roman", 18, True)

# Bottom web link bar.
WEBBAR = (8, 641, 950, 20)
WEBLINK = (246, 643, 499, 16)
WEB_TEXT = ("Click for Web: APPLICATION NOTES - MODELS - DESIGN TIPS - "
            "DATA SHEETS - S-PARAMETERS")
# Banner colour per page family (measured).
WEB_COLORS = {
    "microstrip": "#FFFF00", "cpw": "#FFFF00", "stripline": "#FFFF00",
    "parallel": "#FFFF00", "wire_gnd": "#FFFF00", "coax_round": "#FFFF00",
    "coax_square": "#FFFF00", "trough": "#FFFF00",
    "fet_self": "#408080", "bjt_base": "#FFFF00", "bjt_coll": "#FFFF00",
    "rfic_v": "#408080", "rfic_i": "#408080",
    "det_large": "#0000A0", "det_small": "#0000A0",
    "reflection": "#0000A0", "thermal": "#008080",
}

# Page background colours, measured as the dominant colour of each capture.
PAGE_BG = {
    "microstrip": "#FFFFC0", "cpw": "#FFFFC0", "stripline": "#FFFFC0",
    "parallel": "#FFFFC0", "wire_gnd": "#FFFFC0", "coax_round": "#FFFFC0",
    "coax_square": "#FFFFC0", "trough": "#FFFFC0",
    "fet_self": "#F0F5FA", "bjt_base": "#F0F5FA", "bjt_coll": "#F0F5FA",
    "rfic_v": "#E5ECEF", "rfic_i": "#E5ECEF",
    "det_large": "#FFF0F0", "det_small": "#FFF0F0",
    "reflection": "#F4F4E5", "thermal": "#F4F4E5",
}

# Title text x origin and the AppCAD English title of each page.
TITLES = {
    "microstrip": (30, "Microstrip"),
    "cpw": (31, "Coplanar Waveguide"),
    "stripline": (28, "Stripline"),
    "parallel": (28, "Parallel Wires"),
    "wire_gnd": (27, "Wire Over Groundplane"),
    "coax_round": (29, "Coax (Round)"),
    "coax_square": (27, "Coax (Square)"),
    "trough": (28, "Trough Line"),
    "fet_self": (20, "FET Bias - Source Resistor (self bias)"),
    "bjt_base": (24, "Base Bias Network (non-stabilized)"),
    "bjt_coll": (23, "Collector Feedback Bias"),
    "rfic_v": (29, "Voltage-Biased RFICs"),
    "rfic_i": (28, "Current-Biased RFICs"),
    "det_large": (28, "Large Signal Detector"),
    "det_small": (26, "Small Signal Detector"),
    "reflection": (32, "Reflection Calculator"),
    "thermal": (38, "Device Thermal Calculator"),
}

# Measured absolute placement for the spec-driven transmission-line pages:
# buttons (raised grey), dielectric combo, first result box y, mid column.
# All values are pixels measured on the reference captures.
TL_LAYOUT = {
    "microstrip": dict(buttons=[(503, 104, 90, 24)], combo=(78, 323, 231, 18),
                       res_x=518, res_y=252, res_dy=27, res_w=91),
    "cpw":        dict(buttons=[(500, 103, 90, 24)], combo=(76, 347, 231, 18),
                       res_x=515, res_y=251, res_dy=27, res_w=91),
    "stripline":  dict(buttons=[(504, 103, 90, 24)], combo=(77, 324, 231, 18),
                       res_x=519, res_y=251, res_dy=27, res_w=71),
    "parallel":   dict(buttons=[(505, 103, 94, 22), (505, 138, 94, 22)], combo=(77, 324, 231, 18),
                       res_x=518, res_y=252, res_dy=27, res_w=71),
    "wire_gnd":   dict(buttons=[(508, 102, 94, 22), (508, 137, 94, 22)], combo=(77, 324, 231, 18),
                       res_x=521, res_y=251, res_dy=27, res_w=71),
    "coax_round": dict(buttons=[(511, 102, 94, 22), (511, 137, 94, 22)], combo=(77, 324, 231, 18),
                       res_x=523, res_y=251, res_dy=27, res_w=71),
    "coax_square": dict(buttons=[(505, 102, 94, 22), (505, 137, 94, 22)], combo=(77, 324, 231, 18),
                        res_x=518, res_y=251, res_dy=27, res_w=71),
    "trough":     dict(buttons=[(509, 102, 94, 22), (509, 137, 94, 22)], combo=(77, 324, 231, 18),
                       res_x=522, res_y=251, res_dy=27, res_w=71),
}

# Bias-circuit pages: measured placement of the Calculate button.
CIRC_LAYOUT = {
    "fet_self": dict(buttons=[(413, 330, 81, 22)]),
    "bjt_base": dict(buttons=[(260, 333, 81, 23), (346, 333, 90, 23)]),
}
