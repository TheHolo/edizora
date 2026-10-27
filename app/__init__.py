"""Edizora — artwork and news publishing for Telegram."""
import os


def apply_environment_aliases() -> None:
    """Accept branded settings while preserving existing host/worker contracts.

    New EDIZORA_* settings take precedence over their PUBLISHER_* aliases.
    Normalize once at process startup, so internal launcher overrides propagate
    correctly to child processes, including on older managed nodes.
    """
    for name, value in tuple(os.environ.items()):
        if name.startswith("EDIZORA_"):
            os.environ["PUBLISHER_" + name[len("EDIZORA_"):]] = value
            del os.environ[name]


apply_environment_aliases()
