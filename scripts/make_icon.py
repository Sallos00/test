# -*- coding: utf-8 -*-
import struct, io
from PIL import Image, ImageDraw

def make_frame(size):
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 어두운 원 + 청록 테두리
    bw = max(2, size // 16)
    draw.ellipse([bw, bw, size - bw - 1, size - bw - 1],
                 fill="#1e1e1e", outline="#00c8e0", width=bw)
    # 청록 삼각형
    t = int(size * 0.22)
    b = int(size * 0.78)
    l = int(size * 0.30)
    r = int(size * 0.78)
    draw.polygon([(l, t), (l, b), (r, (t + b) // 2)], fill="#00c8e0")
    return img

sizes   = [16, 24, 32, 48, 64, 128, 256]
pngs    = []
for s in sizes:
    buf = io.BytesIO()
    make_frame(s).save(buf, format="PNG")
    pngs.append(buf.getvalue())

# ICO 파일 직접 작성
n       = len(sizes)
offset  = 6 + n * 16   # ICONDIR + n * ICONDIRENTRY
ico     = io.BytesIO()
# ICONDIR
ico.write(struct.pack("<HHH", 0, 1, n))
# ICONDIRENTRY
for i, (s, png) in enumerate(zip(sizes, pngs)):
    w = s if s < 256 else 0
    h = s if s < 256 else 0
    ico.write(struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(png), offset))
    offset += len(png)
# PNG データ
for png in pngs:
    ico.write(png)

with open("app.ico", "wb") as f:
    f.write(ico.getvalue())

print("app.ico created (%d bytes)" % len(ico.getvalue()))
