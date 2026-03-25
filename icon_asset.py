# -*- coding: utf-8 -*-
"""EXE(app.ico)·Tk 작업 표시줄/작업 관리자(iconbitmap)·트레이용 동일 래스터 생성."""
from PIL import Image, ImageDraw


def make_frame(size: int):
    """원문양 + 청록 테두리 + 재생 삼각형 (구버전 make_icon.py와 동일)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bw = max(2, size // 16)
    draw.ellipse(
        [bw, bw, size - bw - 1, size - bw - 1],
        fill="#1e1e1e",
        outline="#00c8e0",
        width=bw,
    )
    t = int(size * 0.22)
    b = int(size * 0.78)
    l = int(size * 0.30)
    r = int(size * 0.78)
    draw.polygon([(l, t), (l, b), (r, (t + b) // 2)], fill="#00c8e0")
    return img
