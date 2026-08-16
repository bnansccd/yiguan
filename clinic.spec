# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller打包配置 - 昭德堂健康管理中心业务系统"""

import os
import sys

block_cipher = None

# 应用目录（SPECPATH 由 PyInstaller 注入，兼容不同版本/调用方式的回退写法）
try:
    app_dir = os.path.dirname(os.path.abspath(SPECPATH))
except NameError:
    app_dir = os.path.dirname(os.path.abspath(__file__))

a = Analysis(
    ['app.py'],
    pathex=[app_dir],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
    ],
    hiddenimports=[
        'flask',
        'flask_sqlalchemy',
        'flask_login',
        'sqlalchemy',
        'sqlalchemy.dialects.sqlite',
        'werkzeug',
        'werkzeug.security',
        'jinja2',
        'openpyxl',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='昭德堂健康管理中心业务系统',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
