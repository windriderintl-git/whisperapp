@echo off
echo ============================================================
echo  Whisper 2.0 GPU setup (NVIDIA only)
echo ============================================================
echo.

REM --- Check NVIDIA driver ---
nvidia-smi >nul 2>&1
if errorlevel 1 goto :no_driver

echo Detected GPU:
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
echo.

REM --- Install full CUDA runtime stack that ctranslate2 + faster-whisper need ---
echo Installing CUDA runtime libs as Python wheels...
echo (cuBLAS alone is not enough -- it depends on the CUDA runtime + nvJitLink.)
echo.
python -m pip install --upgrade ^
    nvidia-cuda-runtime-cu12 ^
    nvidia-cuda-nvrtc-cu12 ^
    nvidia-nvjitlink-cu12 ^
    nvidia-cublas-cu12 ^
    "nvidia-cudnn-cu12==9.*"
if errorlevel 1 goto :pip_failed

echo.
echo ============================================================
echo  GPU setup complete.
echo ============================================================
echo.
echo What this changed:
echo   - Installed CUDA runtime, nvrtc, nvJitLink, cuBLAS, cuDNN wheels.
echo   - Your config.yaml has whisper.device: auto, which will pick
echo     CUDA automatically on the next run.
echo   - Ollama detects the GPU on its own once NVIDIA drivers are
echo     present, so qwen2.5:3b polish also gets GPU acceleration.
echo.
echo Expected speedup: 5-10x on Whisper, 3-5x on Ollama.
echo Total dictation latency should drop from ~2-3s to under 1s.
echo.
echo Run run.bat as usual. Look for "[whisper] loaded on GPU." at startup.
echo.
pause
exit /b 0

:no_driver
echo ERROR: nvidia-smi not found. NVIDIA driver is not installed or not in PATH.
echo Download from https://www.nvidia.com/Download/index.aspx and re-run.
pause
exit /b 1

:pip_failed
echo ERROR: pip install failed. Check your internet connection.
echo If the error mentions Python version, faster-whisper GPU support
echo requires Python 3.9-3.12. You appear to be on Python 3.13 -- if so,
echo install a Python 3.12 alongside and use it for this app.
pause
exit /b 1
