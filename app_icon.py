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


def _draw_icon(draw: "ImageDraw.ImageDraw", s: int) -> None:
    """실제 아이콘 도형을 draw 객체에 그린다 (크기 s 기준)."""
    bw = max(3, s // 10)
    # 청록 외곽 원
    draw.ellipse([0, 0, s - 1, s - 1], fill="#00c8e0")
    # 검은 내부 원
    draw.ellipse([bw, bw, s - bw - 1, s - bw - 1], fill="#1e1e1e")
    # 청록 삼각형
    t = int(s * 0.22)
    b = int(s * 0.78)
    l = int(s * 0.32)
    r = int(s * 0.75)
    draw.polygon([(l, t), (l, b), (r, (t + b) // 2)], fill="#00c8e0")


def make_frame(size: int, for_ico: bool = False) -> Image.Image:
    """재생 버튼 스타일 — 청록 링 + 검은 원 + 청록 삼각형.

    작은 크기(32px 이하)는 슈퍼샘플링(고해상도로 그린 뒤 축소)을 적용해
    작업 관리자·트레이 등 16px 표시에서도 선명하게 보이도록 한다.

    for_ico=True : 어두운 배경(투명 없음) — ICO/탐색기용  (현재 미사용, 하위 호환)
    for_ico=False: 투명 배경 — iconphoto/트레이용
    """
    s = int(size)

    # 슈퍼샘플링 배율: 작을수록 크게 그린 뒤 LANCZOS 축소
    if s <= 16:
        scale = 8
    elif s <= 32:
        scale = 4
    else:
        scale = 1  # 48px 이상은 직접 그리기로 충분

    big = s * scale
    tmp = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    _draw_icon(ImageDraw.Draw(tmp), big)

    if scale > 1:
        img = tmp.resize((s, s), Image.LANCZOS)
    else:
        img = tmp

    return img


def build_ico_bytes(sizes):
    """멀티 PNG 임베드 ICO 바이너리 (어두운 배경, 탐색기 깨짐 방지)."""
    pngs = []
    for sz in sizes:
        buf = io.BytesIO()
        make_frame(int(sz), for_ico=True).save(buf, format="PNG")
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
    # True → 모든 Toplevel에 적용되어 팝업 뜰 때 아이콘 바뀌는 문제 발생
    # False → 해당 창에만 적용
    tk_root.iconphoto(False, ref)
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
    # 팝업에는 iconphoto만 적용 (iconbitmap 생략 → 메인창 아이콘 변경 방지)
    img = _photo32(master)
    popup.iconphoto(False, img)
    popup._app_icon_photo_ref = img
    return img
