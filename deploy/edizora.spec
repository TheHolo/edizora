# Build from the project root: python -m PyInstaller deploy/edizora.spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata

root = Path(SPECPATH).parent
hidden = ['aiosqlite']
for package in ('app', 'uvicorn', 'sqlalchemy.dialects.sqlite', 'pystray'):
    hidden += collect_submodules(package)
datas = [(str(root / 'frontend/dist'), 'frontend/dist'),
         (str(root / 'app/desktop/assets'), 'app/desktop/assets'),
         (str(root / 'work/desktop-version.json'), '.'),
         (str(root / 'work/desktop-release.zip'), '.'),
         (str(root / 'work/release-manifest.json'), '.'),
         (str(root / 'work/THIRD-PARTY-NOTICES.txt'), '.')]
for package in ('trafilatura', 'courlan', 'extruct', 'certifi', 'tzdata', 'pystray'):
    datas += collect_data_files(package)
for package in ('trafilatura', 'courlan', 'extruct', 'yt-dlp', 'youtube-transcript-api'):
    datas += copy_metadata(package, recursive=True)
analysis = Analysis([str(root / 'deploy/desktop_entry.py')], pathex=[str(root)],
    binaries=[], datas=datas, hiddenimports=hidden, hookspath=[],
    excludes=['pytest', 'tkinter', 'pip', 'setuptools', 'PyInstaller'], noarchive=False)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name='edizora',
    debug=False, strip=False, upx=False, console=False,
    version=str(root / 'work/windows-version.txt'), icon=str(root / 'work/desktop-icon.ico'))
bundle = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name='edizora')
