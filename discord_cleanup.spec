# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['src/discord_cleanup/ui/app.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        ('src/discord_cleanup/ui/theme.qss', 'discord_cleanup/ui'),
        ('src/discord_cleanup/ui/assets', 'discord_cleanup/ui/assets'),
        ('src/discord_cleanup/ui/theme.qss', 'ui'),
    ],
    hiddenimports=[
        'keyring.backends.Windows',
        'keyring.backends.SecretService',
        'keyring.backends.chpass',
        'keyring.backends.macOS',
        'qtawesome',
        'websocket',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Discord-Mass-Cleanup-Tool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
