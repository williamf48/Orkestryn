@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto use_venv
where py >nul 2>&1
if not errorlevel 1 goto use_launcher
where python >nul 2>&1
if not errorlevel 1 goto use_python
echo Python was not found. Install Python 3.10 or newer, then try again.
pause
exit /b 1
:use_venv
".venv\Scripts\python.exe" project.py dashboard --open
goto finished
:use_launcher
py -3 project.py dashboard --open
goto finished
:use_python
python project.py dashboard --open
:finished
if errorlevel 1 pause
endlocal
