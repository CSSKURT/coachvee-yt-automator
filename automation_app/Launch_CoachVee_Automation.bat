@echo off
rem Coach Vee YouTube Shorts Automator - launches the GUI with no console window.
rem The GUI runs in the app's own venv (automation_app\.venv); Whisper still runs in D:\whisper\venv.
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo The app's Python environment is missing - running the installer first ...
  call "%~dp0Install_or_Repair_App.bat"
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app_gui.py"
