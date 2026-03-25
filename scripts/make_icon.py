# -*- coding: utf-8 -*-
"""app.ico 생성 — PyInstaller/탐색기용. 128·256 프레임은 일부 환경에서 깨져 보일 수 있어
작은 해상도만 사용 (기존 app_icon._write_temp_ico_from_png와 동일 구성)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_icon import png_bytes  # noqa: E402


def main():
    from PIL import Image
    import io

    im = Image.open(io.BytesIO(png_bytes())).convert("RGBA")
    sizes = (16, 24, 32, 48, 64)
    imgs = [im.resize((s, s), Image.Resampling.LANCZOS) for s in sizes]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "app.ico")
    imgs[0].save(
        out,
        format="ICO",
        sizes=[(i.width, i.height) for i in imgs],
        append_images=imgs[1:],
    )
    print("app.ico created (%s)" % out)


if __name__ == "__main__":
    main()
