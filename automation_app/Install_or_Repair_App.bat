@echo off
rem Creates/repairs automation_app\.venv (needs a Python 3.12+ that includes Tk) and the Desktop shortcut.
rem Does NOT touch D:\whisper\venv.
setlocal
cd /d "%~dp0"
echo === Coach Vee Automator: install / repair ===
set "PY="
for %%V in (3.14 3.13 3.12) do (
  if not defined PY (
    py -%%V -c "import tkinter" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  echo Could not find Python 3.12 or newer with Tk ^(tkinter^).
  echo Install Python from https://www.python.org/downloads/ and keep "tcl/tk and IDLE" ticked.
  pause
  exit /b 1
)
echo Using %PY%
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
".venv\Scripts\python.exe" create_desktop_shortcut.py
echo.
echo Done. Use the "Coach Vee YouTube Automator" icon on your Desktop.
pause
exit /b 0
:fail
echo Install failed - see the messages above.
pause
exit /b 1
