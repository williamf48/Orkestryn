@echo off
setlocal
cd /d "%~dp0"
echo ORKESTRYN - LIVE JUDGE EVALUATION
echo Uses the supplied hosted evaluation login. No Nebius key or provider SDK is needed.
echo Python 3.10 or newer is required. Extract the complete ZIP before starting.
if not exist "relay_demo.py" goto incomplete
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
".venv\Scripts\python.exe" relay_demo.py start
goto finished
:use_launcher
py -3 relay_demo.py start
goto finished
:use_python
python relay_demo.py start
:finished
if errorlevel 1 pause
endlocal
