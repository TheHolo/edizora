"""Generate product metadata, an application icon and bundled dependency notices."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw
from app.deployment.release import build_release

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--version', required=True)
args = parser.parse_args()
work = root / 'work'
work.mkdir(exist_ok=True)
image = Image.new('RGBA', (256, 256), '#315e46')
draw = ImageDraw.Draw(image)
for x, y in [(56, 56), (140, 56), (56, 140), (140, 140)]:
    draw.rounded_rectangle((x, y, x + 56, y + 56), radius=12, fill='#f3f5f1')
image.save(work / 'desktop-icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
(work / 'desktop-version.json').write_text(json.dumps({'version': args.version, 'channel': 'preview'}), encoding='utf-8')
notices = []
for distribution in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
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
