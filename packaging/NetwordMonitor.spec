# Only source resources are allowed: never collect data/, env files or backups.
from pathlib import Path
root = Path(SPECPATH).parent
analysis = Analysis(
    [str(root / 'windows_app.py')], pathex=[str(root)],
    binaries=[], datas=[(str(root / 'static'), 'static'),
                        (str(root / 'packaging' / 'netword.ico'), 'packaging'),
                        (str(root / 'packaging' / 'licenses'), 'licenses')],
    hiddenimports=['pystray._win32'], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['tkinter', 'pytest', 'psutil'], noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name='NetwordMonitor',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, icon=str(root / 'packaging' / 'netword.ico'))
collection = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name='NetwordMonitor')
