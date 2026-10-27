"""Child commands work both from sources and from the bundled desktop executable."""
import sys


def module_command(module: str, *arguments: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--publisher-module", module, *arguments]
    return [sys.executable, "-m", module, *arguments]
