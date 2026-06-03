@echo off
setlocal
set ROOT=%~dp0..
pushd "%ROOT%"

echo === Cleaning prior build output ===
if exist build\pyinstaller rmdir /s /q build\pyinstaller
if exist dist rmdir /s /q dist

echo === Rendering icons ===
if exist tools\render_icons.py (
    python tools\render_icons.py || goto :err
) else (
    echo SKIP: tools\render_icons.py not found
)

echo === Running PyInstaller ===
python -m PyInstaller --clean --noconfirm ^
    --workpath build\pyinstaller ^
    --distpath dist ^
    build\Whisper2.spec
if errorlevel 1 goto :err

echo === Frozen app at dist\Whisper2\Whisper2.exe ===

REM Locate Inno Setup compiler
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo.
    echo NOTE: Inno Setup 6 not found. Skipping installer step.
    echo Download from https://jrsoftware.org/isdl.php and re-run build.bat
    goto :ok
)

if not exist installer\Whisper2.iss (
    echo NOTE: installer\Whisper2.iss not found. Skipping installer step.
    goto :ok
)

echo === Compiling installer with Inno Setup ===
"%ISCC%" installer\Whisper2.iss
if errorlevel 1 goto :err

echo === Installer at dist\installer\Whisper2-Setup.exe ===

:ok
popd
endlocal
exit /b 0

:err
echo.
echo BUILD FAILED.
popd
endlocal
exit /b 1
