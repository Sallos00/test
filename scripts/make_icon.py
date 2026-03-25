# -*- coding: utf-8 -*-
"""app.ico 생성 — app_icon의 PNG(팝업과 동일)로 멀티해상도 ICO 작성."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_icon import png_bytes  # noqa: E402


def main():
    from PIL import Image
    import io

    im = Image.open(io.BytesIO(png_bytes())).convert("RGBA")
    sizes = (16, 24, 32, 48, 64, 128, 256)
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
