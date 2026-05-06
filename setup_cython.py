"""
Cython 컴파일 설정 파일
Pure Python 코드를 Cython으로 컴파일하여 난독화 및 성능 향상
(main.py는 PyInstaller 진입점이므로 제외)
"""

from Cython.Build import cythonize
from setuptools import setup, Extension
import os
import glob

# 빌드 디렉토리
BUILD_DIR = "build_cython"

# 컴파일할 Python 모듈 목록 (main.py, app_icon.py 제외)
# app_icon.py는 Cython 제외: __file__ 경로 문제로 아이콘 로드 실패
modules = [
    "audio_com.pyx",
    "audio_capture.pyx",
    "log_utils.pyx",
    "mem_utils.pyx",
    "processes.pyx",
    "win32_utils.pyx",
    "updater.pyx",
    "auth.pyx",
    "db_manager.pyx",
    "video_hash.pyx",
    "similarity.pyx",
    "series_key.pyx",
]

# Extension 목록 생성
extensions = []

# 루트 모듈 (main.pyx 제외됨)
for mod in modules:
    mod_path = os.path.join(BUILD_DIR, mod)
    mod_name = mod.replace(".pyx", "")
    extensions.append(Extension(mod_name, [mod_path]))

# GUI 패키지 모듈들
gui_pyx_files = glob.glob(os.path.join(BUILD_DIR, "gui", "*.pyx"))
for pyx_file in gui_pyx_files:
    # gui/base.pyx → gui.base
    rel_path = os.path.relpath(pyx_file, BUILD_DIR)
    mod_name = rel_path.replace(os.sep, ".").replace(".pyx", "")
    # __init__.pyx는 gui 패키지로 (gui.pyx가 아니라)
    if os.path.basename(pyx_file) == "__init__.pyx":
        mod_name = "gui"
    extensions.append(Extension(mod_name, [pyx_file]))

print(f"[Cython] 컴파일할 모듈 수: {len(extensions)}")
for ext in extensions:
    print(f"  - {ext.name}")

# setup 실행
setup(
    name="AutoSync",
    version="1.0.0",
    ext_modules=cythonize(
        extensions,
        language_level="3",
        compiler_directives={
            "always_allow_keywords": True,
        },
        build_dir=os.path.join(BUILD_DIR, "build"),
    ),
    zip_safe=False,
)
