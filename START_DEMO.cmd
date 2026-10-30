@echo off
setlocal
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
where py >nul 2>nul
if errorlevel 1 (
  python "%~dp0run.py" --demo --console %*
) else (
  py -3 "%~dp0run.py" --demo --console %*
)
set result=%errorlevel%
if not "%result%"=="0" pause
exit /b %result%
