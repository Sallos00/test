# -*- coding: utf-8 -*-
"""
앱 아이콘 단일 정의.

- 빌드: write_app_ico_file() → app.ico → PyInstaller --icon
- 실행 중: PyInstaller가 넣은 app.ico(번들) → Tk iconbitmap·작업 표시줄·작업 관리자
- 트레이: pil_image_for_tray()
"""
import io
import os
import struct
import sys
import tempfile
from typing import Optional

from PIL import Image, ImageDraw


ICO_SIZES_FULL = (16, 24, 32, 48, 64, 128, 256)
ICO_SIZES_TEMP = (16, 24, 32, 48, 64)

_cached_ico_path: Optional[str] = None


def make_frame(size: int) -> Image.Image:
    """재생 버튼 스타일 — 원 + 청록 링 + 삼각형. 배경은 투명(RGBA 0,0,0,0).

    일부 탐색기 보기에서는 투명을 흰색처럼 그릴 수 있음(Windows 셸 동작).
    """
    s = int(size)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bw = max(2, s // 16)
    draw.ellipse(
        [bw, bw, s - bw - 1, s - bw - 1],
        fill="#1e1e1e",
        outline="#00c8e0",
        width=bw,
    )
    t, b = int(s * 0.22), int(s * 0.78)
    l, r = int(s * 0.30), int(s * 0.78)
    draw.polygon([(l, t), (l, b), (r, (t + b) // 2)], fill="#00c8e0")
    return img


def build_ico_bytes(sizes):
    """멀티 PNG 임베드 ICO 바이너리."""
    pngs = []
    for sz in sizes:
        buf = io.BytesIO()
        make_frame(int(sz)).save(buf, format="PNG")
        pngs.append(buf.getvalue())
    n = len(sizes)
    off = 6 + n * 16
    ico = io.BytesIO()
    ico.write(struct.pack("<HHH", 0, 1, n))
    for sz, png in zip(sizes, pngs):
        w = int(sz) if int(sz) < 256 else 0
        h = int(sz) if int(sz) < 256 else 0
        ico.write(struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(png), off))
        off += len(png)
    for png in pngs:
        ico.write(png)
    return ico.getvalue()


def write_app_ico_file(path: str) -> None:
    """CI/로컬에서 app.ico 생성."""
    data = build_ico_bytes(ICO_SIZES_FULL)
    with open(path, "wb") as f:
        f.write(data)


def resource_base_dir() -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def bundled_ico_path() -> Optional[str]:
    p = os.path.join(resource_base_dir(), "app.ico")
    return p if os.path.isfile(p) else None


def ico_path_for_windows() -> str:
    """Tk wm iconbitmap — 번들 app.ico 우선, 없으면 임시 파일(동일 그래픽)."""
    global _cached_ico_path
    b = bundled_ico_path()
    if b:
        return b
    if _cached_ico_path and os.path.isfile(_cached_ico_path):
        return _cached_ico_path
    fd, p = tempfile.mkstemp(prefix="autosync_", suffix=".ico")
    os.close(fd)
    with open(p, "wb") as f:
        f.write(build_ico_bytes(ICO_SIZES_TEMP))
    _cached_ico_path = p
    return p


def pil_image_for_tray(size: int = 64) -> Image.Image:
    """pystray — 알림 영역 대비 연한 배경 합성."""
    s = int(size)
    src = make_frame(s)
    base = Image.new("RGBA", (s, s), (30, 30, 30, 255))
    base.paste(src, (0, 0), src)
    return base


def _photo32(master):
    import tkinter as tk

    buf = io.BytesIO()
    make_frame(32).save(buf, format="PNG")
    return tk.PhotoImage(master=master, data=buf.getvalue())


def apply_iconphoto(tk_root):
    ref = _photo32(tk_root)
    tk_root.iconphoto(True, ref)
    return ref


def apply_windows_ico_bitmap(widget):
    if sys.platform != "win32":
        return
    path = ico_path_for_windows()
    try:
        widget.iconbitmap(default=path)
    except Exception:
        try:
            widget.wm_iconbitmap(path)
        except Exception:
            pass


def apply_to_root_window(root):
    ref = apply_iconphoto(root)
    apply_windows_ico_bitmap(root)
    root._app_icon_photo_ref = ref
    return ref


def apply_to_toplevel(popup, master):
    img = _photo32(master)
    popup.iconphoto(True, img)
    popup._app_icon_photo_ref = img
    apply_windows_ico_bitmap(popup)
    return img
