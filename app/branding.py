"""Product identity and data-directory compatibility for Edizora."""
import os
from pathlib import Path

APP_NAME = "Edizora"
APP_VERSION = "0.1"
RELEASES_URL = "https://github.com/TheHolo/edizora/releases"


def data_roots() -> tuple[Path, Path]:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / APP_NAME, base / "Publisher"


def default_data_root() -> Path:
    """Reuse an existing pre-rename installation without moving live databases."""
    current, legacy = data_roots()
    if not (current / "control/node.json").exists() and (legacy / "control/node.json").exists():
        return legacy
    if not current.exists() and legacy.exists():
        return legacy
    return current
