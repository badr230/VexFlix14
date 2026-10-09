@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Install it from python.org and tick Add Python to PATH, then run this file again.
  pause
  exit /b
)

echo Installing requirements...
python -m pip install -q -r requirements.txt

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg is missing and it is needed for Reels. Installing it now...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  echo Close this window and open start.bat again so ffmpeg is detected.
  pause
  exit /b
)

findstr /b /c:"FB_PAGE_TOKEN=" .env >nul 2>nul
if errorlevel 1 python main.py --setup

:menu
echo.
echo ================= MENU =================
echo 1 - Test run, nothing is posted
echo 2 - Generate and schedule posts
echo 3 - Monitor posts, hide normal and show viral, keep this window open
echo 4 - Run setup again
echo 5 - Exit
set /p c=Choose a number:
if "%c%"=="1" python main.py --dry-run
if "%c%"=="2" python main.py
if "%c%"=="3" python main.py --monitor --loop
if "%c%"=="4" python main.py --setup
if "%c%"=="5" exit /b
goto menu
