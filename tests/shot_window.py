"""外部进程 Win32 PrintWindow 截图（不向 ADS 注入代码，避免主线程死锁）。

用法: python tests/shot_window.py <title_substr> <out_png> [hwnd_hex]
"""
import ctypes
import ctypes.wintypes as wt
import sys

import PIL.Image as Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 0x00000002


def find_hwnd(substr):
    res = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if substr.lower() in buf.value.lower() and user32.IsWindowVisible(hwnd):
                res.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return res


def shoot(hwnd, out):
    # 恢复并前置（PrintWindow 对后台窗口通常也有效，但先确保非最小化）
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
        import time
        time.sleep(0.6)
    rc = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    ok = user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT)
    # 拷贝位图数据
    class BMIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                    ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    class BMI(ctypes.Structure):
        _fields_ = [("bmiHeader", BMIH), ("biColors", wt.DWORD * 3)]

    bmi = BMI()
    bmi.bmiHeader.biSize = ctypes.sizeof(BMIH)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    img = Image.frombytes("RGBA", (w, h), buf.raw, "raw", "BGRA")
    img.convert("RGB").save(out)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    return ok, (w, h)


if __name__ == "__main__":
    title, out = sys.argv[1], sys.argv[2]
    hwnds = find_hwnd(title)
    if not hwnds:
        raise SystemExit(f"no window with {title!r}")
    hwnd, text = hwnds[0]
    print("target:", hex(hwnd), text)
    ok, size = shoot(hwnd, out)
    print("PrintWindow ok:", ok, "size:", size, "->", out)
