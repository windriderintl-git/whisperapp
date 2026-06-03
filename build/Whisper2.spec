# -*- mode: python ; coding: utf-8 -*-
import os
# PyInstaller injects SPECPATH as the directory containing this .spec file
# (i.e. .../Whisper2.0/build), NOT the file path itself.
HERE = SPECPATH
PROJECT = os.path.normpath(os.path.join(HERE, ".."))

block_cipher = None

a = Analysis(
    [os.path.join(PROJECT, "tray_app.py")],
    pathex=[PROJECT],
    binaries=[],
    datas=[
        (os.path.join(PROJECT, "prompts"), "prompts"),
        (os.path.join(PROJECT, "config.yaml"), "."),
        (os.path.join(PROJECT, "assets", "icons"), "assets/icons"),
    ],
    hiddenimports=[
        "pystray._win32",
        "PIL.Image",
        "PIL._tkinter_finder",
        "tkinter",
        "tkinter.ttk",
        "win32com.client",
        "win32com.gen_py",
        "pythoncom",
        "pywintypes",
        "encodings.idna",
        "encodings.utf_8",
    ],
    hookspath=[os.path.join(HERE, "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # We do NOT bundle nvidia-* wheels - the first-run wizard fetches them
        # into {install_dir}/cuda/bin/ on demand.
        "nvidia",
        "nvidia.cublas",
        "nvidia.cudnn",
        "nvidia.cuda_runtime",
        "nvidia.cuda_nvrtc",
        "nvidia.nvjitlink",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Whisper2",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                    # UPX often flags AV; skip for v1
    console=False,                # windowed app, no console
    disable_windowed_traceback=False,
    icon=os.path.join(PROJECT, "assets", "icons", "app.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Whisper2",
)
