# -*- coding: utf-8 -*-
import io
import os
import struct
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from icon_asset import make_frame  # noqa: E402

sizes = [16, 24, 32, 48, 64, 128, 256]
pngs = []
for s in sizes:
    buf = io.BytesIO()
    make_frame(s).save(buf, format="PNG")
    pngs.append(buf.getvalue())

n = len(sizes)
offset = 6 + n * 16
ico = io.BytesIO()
ico.write(struct.pack("<HHH", 0, 1, n))
for s, png in zip(sizes, pngs):
    w = s if s < 256 else 0
    h = s if s < 256 else 0
    ico.write(struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(png), offset))
    offset += len(png)
for png in pngs:
    ico.write(png)

out = os.path.join(_ROOT, "app.ico")
with open(out, "wb") as f:
    f.write(ico.getvalue())

print("app.ico created (%d bytes) -> %s" % (len(ico.getvalue()), out))
