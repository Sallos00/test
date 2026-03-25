# -*- coding: utf-8 -*-
"""팝업과 동일한 PNG로 Tk / 트레이 / Windows 작업 표시줄·작업 관리자용 아이콘 통일."""
import base64
import io
import os
import sys
from typing import Optional

# gui_ui / 기존 팝업과 동일한 32×32 PNG 데이터
APP_ICON_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAABF0lEQVR4nOWXwQ2DMAxFQ9U5EGNwYgyGZIycOkbFIuVCICS24x8TqRL/SmI/vp1gnHu6OnjH5/sTn48DFFO/uJS4EqS8iEjczxO5dF08DCIDRMm5pJwuMALEq0XybI9QPpps3xCCrIuvggg63CCcyB1gaNfF0zVGRMRmSyA1GgoiuXcFSKyXhIIcMRMX+CZUylqWEwC9aBIICCTKlTlg7XYJhIptLkENSHMABOTdEkBTzmYOaHvpdgfQJs4cqD3X/TwVk1OxTwBwkkmTQ4pymUpguTOCriXYyUpl0Nidivsks6eAg6h5a+mF8hKMQ4fMgZCIPuMbzziSOaebC/mLKNpQczS1Q+mfj+UFEFG3/ZigIIYL7ZnaADPxiheQWUzuAAAAAElFTkSuQmCC"
)

_cached_ico_path: Optional[str] = None


def png_bytes() -> bytes:
    return base64.b64decode(APP_ICON_PNG_B64)


def resource_base_dir() -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def bundled_ico_path() -> Optional[str]:
    p = os.path.join(resource_base_dir(), "app.ico")
    return p if os.path.isfile(p) else None


def _write_temp_ico_from_png() -> str:
    global _cached_ico_path
    if _cached_ico_path and os.path.isfile(_cached_ico_path):
        return _cached_ico_path
    from PIL import Image

    im = Image.open(io.BytesIO(png_bytes())).convert("RGBA")
    sizes = (16, 24, 32, 48, 64)
    imgs = [im.resize((s, s), Image.Resampling.LANCZOS) for s in sizes]
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
    return b if b else _write_temp_ico_from_png()


def pil_image_for_tray(size: int = 64):
    """pystray.Icon(..., image=...) 용 — 팝업 PNG와 동일 원본."""
    from PIL import Image

    im = Image.open(io.BytesIO(png_bytes())).convert("RGBA")
    if im.size != (size, size):
        im = im.resize((size, size), Image.Resampling.LANCZOS)
    return im


def apply_iconphoto(tk_root):
    """title bar 등 — PhotoImage는 참조 유지 필요."""
    import tkinter as tk

    img = tk.PhotoImage(master=tk_root, data=png_bytes())
    tk_root.iconphoto(True, img)
    return img


def apply_windows_ico_bitmap(tk_widget):
    """Windows: 작업 표시줄·작업 관리자에서 깨짐 완화 (멀티해상도 .ico)."""
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
    """메인 창 — 팝업과 동일 PNG + Windows .ico."""
    ref = apply_iconphoto(root)
    apply_windows_ico_bitmap(root)
    root._app_icon_photo_ref = ref
    return ref


def apply_to_toplevel(popup, master):
    """Toplevel 팝업 — master 기준 PhotoImage + Windows .ico."""
    import tkinter as tk

    img = tk.PhotoImage(master=master, data=png_bytes())
    popup.iconphoto(True, img)
    popup._app_icon_photo_ref = img
    apply_windows_ico_bitmap(popup)
    return img
