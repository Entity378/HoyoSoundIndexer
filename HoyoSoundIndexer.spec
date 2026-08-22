# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['hoyo_sound_indexer.py'],
    pathex=[],
    binaries=[],
    datas=[('blkdec/AnimeStudio.Ooz.dll', 'blkdec')],
    hiddenimports=['blkdec', 'blkdec.mhy', 'blkdec.blb', 'blkdec.encr', 'blkdec.unityfs', 'blkdec._keys', 'certifi'],
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
    name='HoyoSoundIndexer',
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
