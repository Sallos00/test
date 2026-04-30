"""
Cython 컴파일 설정 파일
Pure Python 코드를 Cython으로 컴파일하여 난독화 및 성능 향상
"""

from Cython.Build import cythonize
from setuptools import setup, Extension
import os
import glob

# 컴파일할 Python 모듈 목록
modules = [
    "app_icon.pyx",
    "audio_com.pyx",
    "audio_capture.pyx",
    "log_utils.pyx",
    "mem_utils.pyx",
    "processes.pyx",
    "win32_utils.pyx",
    "auth.pyx",
    "db_manager.pyx",
    "video_hash.pyx",
    "similarity.pyx",
    "series_key.pyx",
]

# GUI 패키지 모듈
gui_modules = glob.glob("gui/*.pyx", recursive=False)

# Extension 목록 생성
extensions = [
    Extension(mod.replace(".pyx", ""), [mod])
    for mod in modules
]

# GUI 모듈 추가
for mod in gui_modules:
    mod_name = f"gui.{os.path.basename(mod).replace('.pyx', '')}"
    extensions.append(Extension(mod_name, [mod]))

# setup 실행
setup(
    name="AutoSync",
    version="1.0.0",
    ext_modules=cythonize(
        extensions,
        language_level="3",
        compiler_directives={
            "always_allow_keywords": True,
            "boundscheck": False,
            "wraparound": False,
        },
    ),
    zip_safe=False,
)
