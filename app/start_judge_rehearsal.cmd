@echo off
setlocal
echo ORKESTRYN - JUDGE ACCOUNT REHEARSAL
echo Uses separate manager and customer-owner accounts in a private evaluation workspace.
echo Free scripted mode. No provider SDK or key is needed.
echo Account details are saved locally in judge_access_private. Keep that folder private.
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
".venv\Scripts\python.exe" judge_setup.py rehearse --open
goto finished
:use_launcher
py -3 judge_setup.py rehearse --open
goto finished
:use_python
python judge_setup.py rehearse --open
:finished
if errorlevel 1 pause
endlocal
