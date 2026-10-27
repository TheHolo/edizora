"""Small Windows status menu; actions always pass through the managed launcher."""
import os
import subprocess
import threading


def start_tray(root):
    if os.name != 'nt':
        return None
    try:
        import pystray
    except ImportError:
        return None  # Source-mode development does not require desktop extras.
    from PIL import Image, ImageDraw
    from app.process import module_command
    from app.branding import APP_VERSION
    image = Image.new('RGBA', (64, 64), '#315e46')
    draw = ImageDraw.Draw(image)
    for x, y in [(14, 14), (35, 14), (14, 35), (35, 35)]:
        draw.rounded_rectangle((x, y, x + 14, y + 14), radius=3, fill='#f3f5f1')

    def launch(*arguments):
        subprocess.Popen(module_command('app.desktop.launcher', *arguments),
            creationflags=subprocess.CREATE_NO_WINDOW)

    def open_logs():
        os.startfile(root / 'logs')

    icon = pystray.Icon('Edizora', image, f'Edizora {APP_VERSION} — работает на этом компьютере', pystray.Menu(
        pystray.MenuItem('Открыть Edizora', lambda: launch(), default=True),
        pystray.MenuItem('Открыть диагностику', open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Остановить Edizora', lambda: launch('--stop'))))
    threading.Thread(target=icon.run, name='edizora-tray', daemon=True).start()
    return icon
