@echo off
setlocal enabledelayedexpansion
title CapTure Build Script

echo.
echo  ========================================================
echo    CapTure — Build Script for Windows
echo    Free, lightweight screen recorder. No FFmpeg required.
echo  ========================================================
echo.

:: ── 1. Check Python ────────────────────────────────────────
echo  [1/4] Checking Python installation...
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo    ERROR: Python not found in PATH.
    echo    Please install Python 3.10+ from https://python.org
    echo    Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo    Found Python %%v

:: ── 2. Install dependencies ────────────────────────────────
echo.
echo  [2/4] Installing Python dependencies...
echo    This may take a few minutes on first run.
echo.

pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo.
    echo    WARNING: Some dependencies failed to install.
    echo    On Windows, pyaudio may need a prebuilt wheel.
    echo    Download from: https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio
    echo    Or try: pip install pipwin ^&^& pipwin install pyaudio
    echo.
    choice /c YN /m "Continue with build anyway"
    if %ERRORLEVEL% equ 2 exit /b 1
)

:: ── 3. Create .ico from .png (if needed) ───────────────────
echo.
echo  [3/4] Preparing icon...
if exist "capture\icon.ico" (
    echo    Found icon.ico — using existing icon.
) else if exist "icon.png" (
    echo    Found icon.png. Converting to .ico...
    python -c "from PIL import Image; img=Image.open('icon.png'); img.save('capture/icon.ico', format='ICO', sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)])"
    if %ERRORLEVEL% equ 0 (
        echo    icon.ico created successfully.
    ) else (
        echo    WARNING: Could not convert icon.png to .ico. Build will use default icon.
    )
) else (
    echo    No icon file found. Build will use default PyInstaller icon.
    echo    Place an icon.ico in capture/ or icon.png in project root to set a custom icon.
)

:: ── 4. Build with PyInstaller ──────────────────────────────
echo.
echo  [4/4] Building CapTure.exe...
echo    This will take a while. The .exe will be in dist/ when done.
echo.

pyinstaller capture.spec --clean --noconfirm

if %ERRORLEVEL% neq 0 (
    echo.
    echo  ========================================================
    echo    BUILD FAILED!
    echo  ========================================================
    echo.
    echo    Troubleshooting:
    echo    1. Make sure you have Visual C++ Redistributable installed:
    echo       https://aka.ms/vs/17/release/vc_redist.x64.exe
    echo    2. On Windows N/KN editions, install the Media Feature Pack:
    echo       Settings ^> Apps ^> Optional Features ^> Add a feature
    echo       Search for "Media Feature Pack"
    echo    3. Try running with more verbose output:
    echo       pyinstaller capture.spec --clean --noconfirm --log-level=DEBUG
    echo    4. Check that all dependencies installed:
    echo       python -c "import cv2, dxcam, numpy, pyaudio, comtypes, pystray, PIL"
    echo.
) else (
    echo.
    echo  ========================================================
    echo    BUILD SUCCESSFUL!
    echo  ========================================================
    echo.
    echo    CapTure.exe is in: dist\CapTure.exe
    echo    Size: 
    dir dist\CapTure.exe 2>nul | find "CapTure.exe"
    echo.
    echo    To run: dist\CapTure.exe
    echo    Or without compiling: python -m capture.main
    echo.
)

pause
endlocal
