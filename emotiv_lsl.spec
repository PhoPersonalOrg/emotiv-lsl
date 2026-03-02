# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = [('/usr/local/opt/hidapi/lib/libhidapi.dylib', 'Contents/Frameworks'), ('/usr/local/opt/hidapi/lib/libhidapi.0.dylib', 'Contents/Frameworks'), ('/usr/local/lib/libhidapi.dylib', 'Contents/Frameworks'), ('/usr/local/lib/libhidapi.0.dylib', 'Contents/Frameworks')]
hiddenimports = ['pylsl', 'mne']
binaries += collect_dynamic_libs('pylsl')
tmp_ret = collect_all('mne')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='emotiv_lsl',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['images/icons/emotiv_lsl_icon_design.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='emotiv_lsl',
)
app = BUNDLE(
    coll,
    name='emotiv_lsl.app',
    icon='images/icons/emotiv_lsl_icon_design.ico',
    bundle_identifier=None,
)
