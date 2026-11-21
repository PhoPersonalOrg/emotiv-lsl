"""Emotiv LSL package."""

from __future__ import annotations

import os
import platform
import sys


def _configure_macos_native_libs() -> None:
    """Prefer Homebrew arm64 liblsl over an Intel copy in /usr/local/lib."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return
    if os.environ.get("PYLSL_LIB"):
        return
    for path in (
        "/opt/homebrew/opt/lsl/lib/liblsl.dylib",
        "/opt/homebrew/lib/liblsl.dylib",
    ):
        if os.path.isfile(path):
            os.environ["PYLSL_LIB"] = path
            break


_configure_macos_native_libs()
