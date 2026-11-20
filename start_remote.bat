@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" /D "%~dp0" pythonw app.py %*
) else (
  start "" /D "%~dp0" pyw -3 app.py %*
)
