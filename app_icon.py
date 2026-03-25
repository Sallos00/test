# -*- coding: utf-8 -*-
"""Tk / 트레이 / Windows iconbitmap — 모두 icon_asset.make_frame 과 동일 그래픽."""
import io
import os
import sys
from typing import Optional

from icon_asset import make_frame

_cached_ico_path: Optional[str] = None


def resource_base_dir() -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def bundled_ico_path() -> Optional[str]:
    p = os.path.join(resource_base_dir(), "app.ico")
    return p if os.path.isfile(p) else None


def _write_temp_ico_from_makeframe() -> str:
    """번들 app.ico 없을 때 — EXE와 동일 벡터 스타일의 멀티 ICO."""
    global _cached_ico_path
    if _cached_ico_path and os.path.isfile(_cached_ico_path):
        return _cached_ico_path
    from PIL import Image

    sizes = (16, 24, 32, 48, 64)
    imgs = [make_frame(s) for s in sizes]
    import tempfile

    fd, path = tempfile.mkstemp(prefix="autosync_icon_", suffix=".ico")
    os.close(fd)
    imgs[0].save(
        path,
        format="ICO",
        sizes=[(i.width, i.height) for i in imgs],
        append_images=imgs[1:],
    )
    _cached_ico_path = path
    return path


def ico_path_for_windows() -> str:
    b = bundled_ico_path()
    return b if b else _write_temp_ico_from_makeframe()


def pil_image_for_tray(size: int = 64):
    """pystray — 투명 바깥은 알림 영역에서 잘 안 보일 수 있어 약한 배경 합성."""
    from PIL import Image

    src = make_frame(size)
    base = Image.new("RGBA", (size, size), (30, 30, 30, 255))
    base.paste(src, (0, 0), src)
    return base


def _png_photo(tk_master):
    import tkinter as tk

    buf = io.BytesIO()
    make_frame(32).save(buf, format="PNG")
    return tk.PhotoImage(master=tk_master, data=buf.getvalue())


def apply_iconphoto(tk_root):
    ref = _png_photo(tk_root)
    tk_root.iconphoto(True, ref)
    return ref


def apply_windows_ico_bitmap(tk_widget):
    if sys.platform != "win32":
        return
    path = ico_path_for_windows()
    try:
        tk_widget.iconbitmap(default=path)
    except Exception:
        try:
            tk_widget.wm_iconbitmap(path)
        except Exception:
            pass


def apply_to_root_window(root):
    ref = apply_iconphoto(root)
    apply_windows_ico_bitmap(root)
    root._app_icon_photo_ref = ref
    return ref


def apply_to_toplevel(popup, master):
    img = _png_photo(master)
    popup.iconphoto(True, img)
    popup._app_icon_photo_ref = img
    apply_windows_ico_bitmap(popup)
    return img
