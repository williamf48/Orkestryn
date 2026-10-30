@echo off
setlocal
cd /d "%~dp0"
echo ORKESTRYN - OPTIONAL DOWNLOAD CHECK
echo Free scripted rehearsal. No provider SDK or API key is needed.
echo Python 3.10 or newer is required. Extract the complete ZIP before starting.
if not exist "downloadable_demo.py" goto incomplete
if exist ".venv\Scripts\python.exe" goto use_venv
where py >nul 2>&1
if not errorlevel 1 goto use_launcher
where python >nul 2>&1
if not errorlevel 1 goto use_python
echo Python was not found. Install Python 3.10 or newer from python.org, then try again.
pause
exit /b 1
:incomplete
echo Extract All files from the complete ZIP into one folder, then run this launcher.
pause
exit /b 1
:use_venv
".venv\Scripts\python.exe" downloadable_demo.py check
goto finished
:use_launcher
py -3 downloadable_demo.py check
goto finished
:use_python
python downloadable_demo.py check
:finished
if errorlevel 1 pause
endlocal
