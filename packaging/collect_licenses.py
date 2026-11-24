"""Include dependency licenses and notices with every distributable."""
from importlib.metadata import distribution
from pathlib import Path
import shutil
import sys

output = Path(__file__).with_name('licenses')
output.mkdir(exist_ok=True)
for name in ('pystray', 'Pillow', 'six', 'packaging', 'pyinstaller', 'pywebview',
             'pythonnet', 'clr_loader', 'cffi', 'pycparser', 'proxy_tools', 'bottle',
             'typing_extensions'):
    package = distribution(name)
    target = output / name
    target.mkdir(exist_ok=True)
    for item in package.files:
        if any(word in item.name.lower() for word in ('license', 'copying')) and package.locate_file(item).is_file():
            shutil.copyfile(package.locate_file(item), target / item.name)
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if not python_license.exists():
    raise SystemExit('Python license missing; cannot distribute without notices')
shutil.copyfile(python_license, output / 'Python-LICENSE.txt')
(output / 'NOTICE.txt').write_text(
    'Netword Monitor bundles Python, pystray, Pillow, six, packaging, pywebview,\n'
    'pythonnet, clr_loader, cffi, pycparser, proxy_tools, bottle and typing_extensions.\n'
    'WebView2 SDK DLL notices: WebView2-SDK/.\n'
    'WebView2 Runtime is a separate Microsoft prerequisite, not bundled.\n'
    'pystray 0.19.5 is LGPLv3; source: https://github.com/moses-palmer/pystray/tree/v0.19.5\n'
    'You may modify/rebuild/replace that library under its license. No signing restrictions are imposed.\n'
    'Application source and rebuild instructions: https://github.com/kuanbill/Network-Monitoring\n'
    'PyInstaller bootloader uses its GPL exception for distributing non-GPL applications.\n'
    'See accompanying license files, including Pillow third-party codec notices.\n', encoding='utf-8')
print('Collected dependency license notices')
