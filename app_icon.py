# -*- coding: utf-8 -*-
"""팝업과 동일한 PNG로 Tk / 트레이 / Windows 작업 표시줄·작업 관리자용 아이콘 통일."""
import base64
import io
import os
import sys
from typing import List, Optional, Sequence

# gui_ui / 기존 팝업과 동일한 32×32 PNG 데이터
APP_ICON_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAABF0lEQVR4nOWXwQ2DMAxFQ9U5EGNwYgyGZIycOkbFIuVCICS24x8TqRL/SmI/vp1gnHu6OnjH5/sTn48DFFO/uJS4EqS8iEjczxO5dF08DCIDRMm5pJwuMALEq0XybI9QPpps3xCCrIuvggg63CCcyB1gaNfF0zVGRMRmSyA1GgoiuXcFSKyXhIIcMRMX+CZUylqWEwC9aBIICCTKlTlg7XYJhIptLkENSHMABOTdEkBTzmYOaHvpdgfQJs4cqD3X/TwVk1OxTwBwkkmTQ4pymUpguTOCriXYyUpl0Nidivsks6eAg6h5a+mF8hKMQ4fMgZCIPuMbzziSOaebC/mLKNpQczS1Q+mfj+UFEFG3/ZigIIYL7ZnaADPxiheQWUzuAAAAAElFTkSuQmCC"
)

_cached_ico_path: Optional[str] = None


def png_bytes() -> bytes:
    return base64.b64decode(APP_ICON_PNG_B64)


def _load_master_rgba_cropped():
    """원본 32×32 PNG는 실제 문양 주변에 투명 여백이 커서, 아이콘·EXE에서 작게 보인다. 알파 기준 타이트 크롭."""
    from PIL import Image

    im = Image.open(io.BytesIO(png_bytes())).convert("RGBA")
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    return im


def _square_cover_rgba(im, size: int):
    """문양을 size×size에 맞게 확대한 뒤 중앙 크롭(여백 없이 타일에 꽉 차게)."""
    from PIL import Image

    w, h = im.size
    if w < 1 or h < 1 or size < 1:
        return Image.new("RGBA", (max(1, size), max(1, size)), (0, 0, 0, 0))
    scale = max(size / w, size / h)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    resized = im.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - size) // 2
    top = (nh - size) // 2
    return resized.crop((left, top, left + size, top + size))


def ico_frame_images(sizes: Sequence[int]) -> List:
    """EXE/Tk용 멀티 ICO — 각 해상도별 RGBA 정사각형 프레임."""
    master = _load_master_rgba_cropped()
    return [_square_cover_rgba(master, int(s)) for s in sizes]


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

    sizes = (16, 24, 32, 48, 64)
    imgs = ico_frame_images(sizes)
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
    """pystray.Icon(..., image=...) 용 — 팝업 PNG와 동일 모양, 트레이에서 투명만 보이지 않게 배경 합성."""
    from PIL import Image

    src = _square_cover_rgba(_load_master_rgba_cropped(), size)
    # Windows 알림 영역은 투명 픽셀만 있으면 '안 보임'처럼 느껴질 수 있음
    base = Image.new("RGBA", (size, size), (30, 30, 30, 255))
    base.paste(src, (0, 0), src)
    return base


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
