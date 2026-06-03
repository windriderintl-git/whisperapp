@echo off
echo Starting Whisper 2.0...
echo.

REM --- Python check ---
python --version >nul 2>&1
if errorlevel 1 goto :no_python

REM --- Python deps check ---
python -c "import keyboard, yaml, pygetwindow" >nul 2>&1
if errorlevel 1 goto :install_deps
goto :check_ollama

:install_deps
echo Installing Python dependencies (first run only)...
python -m pip install -r requirements.txt
if errorlevel 1 goto :install_failed

:check_ollama
where ollama >nul 2>&1
if errorlevel 1 goto :no_ollama
ollama list 2>nul | findstr /i "qwen2.5:3b" >nul
if errorlevel 1 goto :pull_model
goto :run

:pull_model
echo Pulling qwen2.5:3b model, first run only, about 2GB...
ollama pull qwen2.5:3b
goto :run

:no_ollama
echo.
echo WARNING: Ollama not found in PATH.
echo LLM polish will be skipped; you will get raw Whisper output.
echo Install from https://ollama.com/ then re-run.
echo.
goto :run

:run
echo.
python main.py
pause
exit /b 0

:no_python
echo ERROR: Python is not installed or not in PATH.
echo Install from https://www.python.org/downloads/ and re-run.
pause
exit /b 1

:install_failed
echo ERROR: Failed to install Python dependencies.
echo Check your internet connection and re-run.
pause
exit /b 1
