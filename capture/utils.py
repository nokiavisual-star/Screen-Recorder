"""
Utility helpers for CapTure — formatting, validation, and system checks.

No heavy logic here — just small, reusable functions that don't
belong in any specific capture/encode/mux module.
"""

from __future__ import annotations

import importlib
import os
import platform
import sys
from typing import Any


# ═══════════════════════════════════════════════════════════════════════════
# Formatting helpers
# ═══════════════════════════════════════════════════════════════════════════


def format_duration(seconds: float) -> str:
    """Format a duration in seconds as HH:MM:SS.

    Args:
        seconds: Duration in seconds (float, can be fractional).

    Returns:
        Formatted string like "00:02:35".
    """
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_size_bytes(size: int) -> str:
    """Format a byte count into a human-readable string.

    Args:
        size: Size in bytes.

    Returns:
        Human-readable string like "142.3 MB".
    """
    if size < 0:
        size = 0

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


# Alias for the task spec naming convention.
format_size = format_size_bytes


# ═══════════════════════════════════════════════════════════════════════════
# Dependency checks
# ═══════════════════════════════════════════════════════════════════════════

# Modules required by CapTure at runtime (for dependency checks).
REQUIRED_MODULES: dict[str, str] = {
    "cv2": "OpenCV (headless)",
    "dxcam": "DXCam",
    "numpy": "NumPy",
    "pyaudio": "PyAudio",
    "comtypes": "comtypes",
    "pystray": "pystray",
    "PIL": "Pillow",
}


def check_dependency(
    module_name: str, friendly_name: str | None = None
) -> bool:
    """Check whether a Python module can be imported.

    Args:
        module_name: The import name (e.g., 'cv2', 'dxcam').
        friendly_name: Optional display name for error messages.

    Returns:
        True if importable, False otherwise.
    """
    try:
        importlib.import_module(module_name)
        return True
    except ImportError:
        display = friendly_name or module_name
        print(
            f"  ✗ {display} — NOT FOUND",
            file=sys.stderr,
        )
        return False


def check_all_dependencies() -> dict[str, bool]:
    """Check all required dependencies for CapTure.

    Returns:
        Dict mapping module name -> available (bool).
    """
    results: dict[str, bool] = {}
    for module_name, friendly in REQUIRED_MODULES.items():
        results[module_name] = check_dependency(module_name, friendly)
    return results


# ═══════════════════════════════════════════════════════════════════════════
# System information
# ═══════════════════════════════════════════════════════════════════════════


def get_system_info() -> dict[str, Any]:
    """Collect relevant system information for diagnostics.

    Returns:
        Dict with keys like 'python_version', 'platform', 'os_version',
        'opencv_build', 'dxcam_version', 'cpu_info'.
    """
    info: dict[str, Any] = {
        "python_version": sys.version,
        "platform": platform.system(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "architecture": platform.machine(),
        "hostname": platform.node(),
        "os_version": "",
    }

    # ── OS version ────────────────────────────────────────────────────
    if platform.system() == "Windows":
        try:
            info["os_version"] = f"Windows {platform.release()} ({platform.version()})"
        except Exception:
            pass

    # ── CPU info ──────────────────────────────────────────────────────
    try:
        info["cpu_count"] = os.cpu_count() or 0
    except Exception:
        info["cpu_count"] = 0

    try:
        info["cpu_info"] = platform.processor() or "Unknown"
    except Exception:
        info["cpu_info"] = "Unknown"

    # ── OpenCV version ────────────────────────────────────────────────
    try:
        import cv2
        info["opencv_version"] = cv2.__version__
        # Check if MSMF backend is available (Windows-only).
        try:
            has_msmf = hasattr(cv2, "CAP_MSMF")
            info["opencv_msmf"] = has_msmf
        except Exception:
            info["opencv_msmf"] = False
    except ImportError:
        info["opencv_version"] = None
        info["opencv_msmf"] = False

    # ── DXCam version ─────────────────────────────────────────────────
    try:
        import dxcam
        info["dxcam_version"] = getattr(dxcam, "__version__", "installed")
    except ImportError:
        info["dxcam_version"] = None

    # ── NumPy version ─────────────────────────────────────────────────
    try:
        import numpy
        info["numpy_version"] = numpy.__version__
    except ImportError:
        info["numpy_version"] = None

    # ── PyAudio version ───────────────────────────────────────────────
    try:
        import pyaudio
        info["pyaudio_version"] = getattr(
            pyaudio, "__version__", "installed"
        )
    except ImportError:
        info["pyaudio_version"] = None

    # ── comtypes version ──────────────────────────────────────────────
    try:
        import comtypes
        info["comtypes_version"] = getattr(
            comtypes, "__version__", "installed"
        )
    except ImportError:
        info["comtypes_version"] = None

    # ── Monitor info (via dxcam) ──────────────────────────────────────
    try:
        import dxcam
        info["monitor_count"] = (
            dxcam.output_count() if hasattr(dxcam, "output_count") else 1
        )
    except Exception:
        info["monitor_count"] = None

    return info
