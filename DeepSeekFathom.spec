# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['clr', 'proxy_tools', 'bottle', 'webview.platforms.edgechromium', 'webview.platforms.winforms', 'webview.platforms.mshtml']
hiddenimports += collect_submodules('webview')
hiddenimports += collect_submodules('deepseekfathom')
# Package UI assets from this checkout. collect_all('deepseekfathom') can resolve
# an older site-packages copy and produce a new EXE with an old frontend.
datas += [('build\\desktop-release\\assets', 'deepseekfathom\\_core\\desktop\\assets')]
datas += [('build\\third-party-licenses', 'licenses')]
datas += [('LICENSE', '.')]
datas += [('NOTICE', '.')]
datas += [('scripts\\Languages\\LICENSE', 'licenses\\inno-setup-chinese-translation')]
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['scripts\\desktop_launcher.py'],
    pathex=['src'],
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
    name='DeepSeekFathom',
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
    version='build\\desktop-release\\windows-version-info.txt',
    icon=['assets\\app-icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DeepSeekFathom',
)
