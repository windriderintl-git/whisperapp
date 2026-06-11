@echo off
echo Starting Whisper 2.0 in terminal mode...
echo.

python --version >nul 2>&1
if errorlevel 1 goto :no_python

python -c "import keyboard, yaml, pystray" >nul 2>&1
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
goto :run_llm

:pull_model
echo Pulling qwen2.5:3b model, first run only, about 2GB...
ollama pull qwen2.5:3b
goto :run_llm

:no_ollama
echo.
echo WARNING: Ollama not found. Running without LLM polish.
echo.
python main.py --mode terminal --no-llm
pause
exit /b 0

:run_llm
echo.
python main.py --mode terminal
pause
exit /b 0

:no_python
echo ERROR: Python is not installed or not in PATH.
pause
exit /b 1

:install_failed
echo ERROR: Failed to install Python dependencies.
pause
exit /b 1
