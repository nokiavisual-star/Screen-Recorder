# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for building CapTure into a standalone .exe.

Usage:
    pyinstaller capture.spec

Produces a single-file .exe in dist/ with no console window
(--noconsole) for the tray-icon build. The .exe is named 'CapTure.exe'.

Requirements:
    pip install pyinstaller
    pip install -r requirements.txt

Notes:
    - UPX is disabled for opencv binaries (they can break under UPX).
    - dxcam is explicitly listed in hiddenimports (lazy-imported at runtime).
    - The tray UI (pystray + Pillow) is included; console fallback is always
      available via --console flag.
"""

from pathlib import Path

# ---- Project paths ----
PROJECT_ROOT = Path(__file__).parent
CAPTURE_PKG = PROJECT_ROOT / "capture"

# ---- Icon path (optional) ----
# If you have an .ico file, place it in the capture/ package directory
# and name it 'icon.ico'. Otherwise the .exe gets the default PyInstaller icon.
ICON_PATH = CAPTURE_PKG / "icon.ico"
_icon_arg = str(ICON_PATH) if ICON_PATH.exists() else None

# ---- Analysis ----
a = Analysis(
    [str(CAPTURE_PKG / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        # Bundle icon.ico if present (included in the capture package so
        # TrayUI can reference it at runtime).
        (str(CAPTURE_PKG / "icon.ico"), "capture")
        if ICON_PATH.exists()
        else None,
    ],
    hiddenimports=[
        # ── Critical runtime dependencies ────────────────────────
        "cv2",
        "cv2.videoio_registry",     # MSMF backend registration
        "numpy",
        "numpy.core._methods",
        "numpy.lib.format",
        "numpy._core",              # NumPy 2.x internals
        "numpy._globals",
        # ── Screen capture ───────────────────────────────────────
        "dxcam",
        "dxcam.dxcam",
        # ── Audio capture ────────────────────────────────────────
        "pyaudio",
        "pyaudio._portaudio",
        # ── Windows Media Foundation (COM) ───────────────────────
        "comtypes",
        "comtypes.client",
        "comtypes.gen",
        "comtypes.automation",
        "comtypes.typeinfo",
        "comtypes.persist",
        # ── Tray UI ───────────────────────────────────────────────
        "pystray",
        "pystray._win32",
        "pystray._base",
        # ── Pillow (icon generation in tray UI) ──────────────────
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL._imagingtk",
        "PIL._tkinter_finder",
        "PIL._imaging",
        # ── Standard-library modules PyInstaller may miss ────────
        "queue",
        "wave",
        "struct",
        "threading",
        "json",
        "ctypes",
        "ctypes.wintypes",
        "shutil",
        "tempfile",
        "platform",
        "signal",
        "argparse",
        "importlib",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavyweight/irrelevant modules to reduce .exe size.
        "tkinter",
        "tkinter.ttk",
        "unittest",
        "test",
        "pydoc",
        "distutils",
        "setuptools",
        "pip",
        "email",
        "html",
        "http",
        "xmlrpc",
        "pdb",
        "doctest",
        # Matplotlib / SciPy (not used).
        "matplotlib",
        "scipy",
        "pandas",
        # Jupyter / IPython.
        "IPython",
        "ipykernel",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

# Remove None entries from datas (icon not found case).
a.datas = [d for d in a.datas if d is not None]

# ---- Filter zip cruft ----
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# ---- EXE ----
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="CapTure",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                          # Use UPX compression for smaller .exe
    upx_exclude=[
        # OpenCV DLLs break under UPX compression.
        "opencv_videoio",
        "opencv_core",
        "opencv_imgproc",
        "opencv_video",
        "opencv_imgcodecs",
        "opencv_ffmpeg",
        # NumPy compiled extensions can also be problematic.
        "_multiarray_umath",
        # pyaudio native extension.
        "_portaudio",
        # Pillow C extensions.
        "_imaging",
    ],
    runtime_tmpdir=None,
    console=False,                    # No console window for tray UI
    disable_windowed_traceback=True,  # Never show raw tracebacks to users
    argv_emulation=False,
    target_arch=None,                 # "x86_64" or None for auto-detect
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon_arg,
)
