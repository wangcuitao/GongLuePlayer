@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  GongLuePlayer - one-click build script
echo ============================================================
echo.

if not exist "libmpv-2.dll" (
    echo [ERROR] libmpv-2.dll not found in this folder.
    echo         Download it from:
    echo         https://sourceforge.net/projects/mpv-player-windows/files/libmpv/
    pause
    exit /b 1
)

set "PY=py -3.11"
py -3.11 --version >nul 2>nul && goto :gotpy
set "PY=py -3.10"
py -3.10 --version >nul 2>nul && goto :gotpy
set "PY=python"
python --version >nul 2>nul && goto :gotpy

echo [ERROR] Python 3.10 or 3.11 not found. Install it first.
pause
exit /b 1

:gotpy
echo Using: %PY%
%PY% --version
echo.

echo [1/3] Installing dependencies + PyInstaller ...
%PY% -m pip install --upgrade pip
%PY% -m pip install yt-dlp "python-mpv>=1.0.5" "pywin32>=306" "keyboard>=0.13.5" pyinstaller
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

echo [2/3] Building ...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name GongLuePlayer --add-data "libmpv-2.dll;." --collect-submodules yt_dlp --exclude-module numpy --exclude-module pandas --exclude-module matplotlib --exclude-module scipy --exclude-module PIL --exclude-module cv2 --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide2 --exclude-module PySide6 --exclude-module tkinter.test main.py
if errorlevel 1 (
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo [3/3] Done.
echo       Output: dist\GongLuePlayer.exe
echo.
echo NOTE: run the exe as Administrator (global hotkeys need admin).
echo       cookies.txt / config.json are saved next to the exe.
pause
exit /b 0
