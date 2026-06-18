"""Souffleur — local, real-time Microsoft Teams live-caption capture for Windows
that pushes the live transcript to Microsoft Scout (Clawpilot) on a hotkey.

Run it with ``python -m souffleur`` or, once installed, the ``souffleur`` command.
"""
from __future__ import annotations

__version__ = "0.1.0"

from .cli import main

__all__ = ["main", "__version__"]
