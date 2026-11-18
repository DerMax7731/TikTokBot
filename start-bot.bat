@echo off
setlocal
cd /d "%~dp0"

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg is missing. Install it with:  winget install Gyan.FFmpeg
  echo Then close this window and start the bot again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\tiktokbot.exe" (
  echo First start: installing the bot. This takes a few minutes.
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if not exist ".venv\Scripts\python.exe" (
    echo Python 3.10 or newer is missing. Install it with:  winget install Python.Python.3.12
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -e ".[subtitles,vision]"
  if errorlevel 1 (
    echo Installation failed, see the messages above.
    pause
    exit /b 1
  )
)

start "" http://localhost:8080
".venv\Scripts\tiktokbot.exe" run --web
pause
