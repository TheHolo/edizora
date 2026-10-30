# Build from the project root: python -m PyInstaller deploy/edizora.spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata
from imageio_ffmpeg import get_ffmpeg_exe

root = Path(SPECPATH).parent
hidden = ['aiosqlite', 'imageio_ffmpeg.binaries']
for package in ('app', 'uvicorn', 'sqlalchemy.dialects.sqlite', 'pystray', 'rapidocr', 'yt_dlp_ejs'):
    hidden += collect_submodules(package)
datas = [(str(root / 'frontend/dist'), 'frontend/dist'),
         (str(root / 'app/desktop/assets'), 'app/desktop/assets'),
         (str(root / 'work/desktop-version.json'), '.'),
         (str(root / 'work/desktop-release.zip'), '.'),
         (str(root / 'work/release-manifest.json'), '.'),
         (str(root / 'work/THIRD-PARTY-NOTICES.txt'), '.')]
for package in ('trafilatura', 'courlan', 'extruct', 'certifi', 'tzdata', 'pystray',
                'rapidocr', 'faster_whisper', 'yt_dlp_ejs'):
    datas += collect_data_files(package)
for package in ('trafilatura', 'courlan', 'extruct', 'yt-dlp', 'youtube-transcript-api',
                'rapidocr', 'onnxruntime', 'faster-whisper', 'imageio-ffmpeg', 'yt-dlp-ejs'):
    datas += copy_metadata(package, recursive=True)
analysis = Analysis([str(root / 'deploy/desktop_entry.py')], pathex=[str(root)],
    binaries=[(get_ffmpeg_exe(), 'imageio_ffmpeg/binaries')], datas=datas, hiddenimports=hidden, hookspath=[],
    excludes=['pytest', 'tkinter', 'pip', 'setuptools', 'PyInstaller'], noarchive=False)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name='edizora',
    debug=False, strip=False, upx=False, console=False,
    version=str(root / 'work/windows-version.txt'), icon=str(root / 'work/desktop-icon.ico'))
bundle = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name='edizora')
