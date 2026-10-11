@echo off
rem OneCargo bot launcher for Windows. Double-click to start; close the window to stop.
cd /d "%~dp0"

if not exist .env (
  copy .env.example .env >nul
  echo Fill in .env (keys and tokens^), save it, then run start.bat again.
  notepad .env
  exit /b
)
if not exist service_account.json (
  echo service_account.json is missing. Put the Google key file next to start.bat.
  pause
  exit /b
)

if not exist .venv (
  echo First run: setting up Python environment...
  py -3 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -q --disable-pip-version-check -r requirements.txt

:loop
python -m bot.main
echo.
echo Bot stopped. Restarting in 10 seconds... (close this window to quit)
timeout /t 10 /nobreak >nul
goto loop
