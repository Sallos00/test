# -*- coding: utf-8 -*-
import struct, io
from PIL import Image, ImageDraw

def make_frame(size):
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bw = max(2, size // 16)
    draw.ellipse([bw, bw, size - bw - 1, size - bw - 1],
                 fill="#1e1e1e", outline="#00c8e0", width=bw)
    t = int(size * 0.22)
    b = int(size * 0.78)
    l = int(size * 0.30)
    r = int(size * 0.78)
    draw.polygon([(l, t), (l, b), (r, (t + b) // 2)], fill="#00c8e0")
    # BMP 기반으로 변환 (tkinter 호환)
    bmp_img = img.convert("RGBA")
    return bmp_img

sizes = [16, 24, 32, 48, 64, 128, 256]
frames = []
for s in sizes:
    frames.append(make_frame(s))

# PIL의 save로 ICO 생성 (BMP 기반, tkinter 호환)
frames[0].save(
    "app.ico",
    format="ICO",
    sizes=[(s, s) for s in sizes],
    append_images=frames[1:]
)

import os
print("app.ico created (%d bytes)" % os.path.getsize("app.ico"))
