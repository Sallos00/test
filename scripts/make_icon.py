# -*- coding: utf-8 -*-
"""저장소 루트에 app.ico 생성 — 내용은 app_icon.py 한곳에서만 정의."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app_icon import write_app_ico_file  # noqa: E402

if __name__ == "__main__":
    out = os.path.join(_ROOT, "app.ico")
    write_app_ico_file(out)
    print("app.ico -> %s (%d bytes)" % (out, os.path.getsize(out)))
