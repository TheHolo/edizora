"""Generate product metadata, an application icon and bundled dependency notices."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw
from app.deployment.release import build_release
from app.branding import APP_NAME, APP_VERSION

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--version', required=True)
args = parser.parse_args()
frontend_version = json.loads((root / 'frontend/package.json').read_text(encoding='utf-8'))['version']
if args.version != APP_VERSION or frontend_version != APP_VERSION:
    parser.error('Backend, frontend and requested build versions must match')
work = root / 'work'
work.mkdir(exist_ok=True)
image = Image.new('RGBA', (256, 256), '#315e46')
draw = ImageDraw.Draw(image)
for x, y in [(56, 56), (140, 56), (56, 140), (140, 140)]:
    draw.rounded_rectangle((x, y, x + 56, y + 56), radius=12, fill='#f3f5f1')
image.save(work / 'desktop-icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
(work / 'desktop-version.json').write_text(json.dumps({'version': args.version, 'channel': 'preview'}), encoding='utf-8')
numeric_version = tuple(int(part) for part in args.version.split('.'))
numeric_version += (0,) * (4 - len(numeric_version))
from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct,
    VarFileInfo, VarStruct,
)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=numeric_version, prodvers=numeric_version,
                     mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
    kids=[StringFileInfo([StringTable('040904B0', [
        StringStruct('CompanyName', 'TheHolo'),
        StringStruct('FileDescription', f'{APP_NAME} — Telegram publishing'),
        StringStruct('FileVersion', args.version),
        StringStruct('InternalName', 'edizora'),
        StringStruct('OriginalFilename', 'edizora.exe'),
        StringStruct('ProductName', APP_NAME),
        StringStruct('ProductVersion', args.version),
    ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])],
)
(work / 'windows-version.txt').write_text(str(version_info), encoding='utf-8')
notices = [f'{APP_NAME} {APP_VERSION}\n']
for distribution in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
    if distribution.metadata['Name'].lower() in {'telegram-image-publisher', 'edizora'}:
        continue
    notices.append(f"{distribution.metadata['Name']} {distribution.version}\nLicense: {distribution.metadata.get('License-Expression') or distribution.metadata.get('License') or 'See package license'}\n")
    for file in distribution.files or []:
        if file.name.lower() in {'license', 'license.txt', 'license.md', 'copying', 'notice'}:
            path = distribution.locate_file(file)
            if path.is_file():
                notices.append(path.read_text(encoding='utf-8', errors='replace') + '\n')
(work / 'THIRD-PARTY-NOTICES.txt').write_text('\n'.join(notices), encoding='utf-8')
import shutil
import zipfile
release = build_release(root, work / 'desktop-releases')
shutil.copy2(release['path'], work / 'desktop-release.zip')
with zipfile.ZipFile(release['path']) as archive:
    (work / 'release-manifest.json').write_bytes(archive.read('release-manifest.json'))
