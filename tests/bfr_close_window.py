"""外部进程:关闭 ADS 里打开的 FM_SC 原理图窗口(WM_CLOSE,无脏提示)。"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

user32 = ctypes.windll.user32
WM_CLOSE = 0x0010

targets = []
main_hwnds = []


@ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
def top_cb(hwnd, _):
    length = user32.GetWindowTextLengthW(hwnd)
    if length:
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if user32.IsWindowVisible(hwnd) and (
                "advanced design system" in buf.value.lower()
                or "ads" in buf.value.lower()):
            main_hwnds.append((hwnd, buf.value))
    return True


user32.EnumWindows(top_cb, 0)
print("顶层候选:", [(hex(h), t[:40]) for h, t in main_hwnds])

for hwnd, title in main_hwnds:
    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def child_cb(chwnd, _):
        length = user32.GetWindowTextLengthW(chwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(chwnd, buf, length + 1)
            if "FM_SC" in buf.value:
                targets.append((chwnd, buf.value))
        return True
    user32.EnumChildWindows(hwnd, child_cb, 0)

print("FM_SC 子窗口:", [(hex(h), t[:60]) for h, t in targets])
for h, t in targets:
    user32.PostMessageW(h, WM_CLOSE, 0, 0)
    print("已发送 WM_CLOSE ->", t[:60])
time.sleep(1.5)
