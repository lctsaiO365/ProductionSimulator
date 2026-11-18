@echo off
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 (echo Python not found. Install Python 3.10+ from python.org & pause & exit /b 1)
call .venv\Scripts\python.exe -m pip install --upgrade pip
call .venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (echo Installation failed & pause & exit /b 1)
echo Installation completed.
pause
