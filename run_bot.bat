@echo off
chcp 65001 >nul
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
:loop
echo [%date% %time%] === start === >> bot.log
python -u bot_main.py >> bot.log 2>&1
echo [%date% %time%] === exit code %errorlevel% === >> bot.log
timeout /t 5 /nobreak >nul
goto loop
