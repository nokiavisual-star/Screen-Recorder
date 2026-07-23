"""
CapTure — Free, lightweight screen recorder for Windows.

Entry point. Parses CLI arguments, loads configuration, and
launches the UI.

Usage:
    python -m capture.main              # Launch with best available UI
    python -m capture.main --console    # Force console UI
    python -m capture.main --version    # Show version and exit
    python -m capture.main --check      # Check system readiness
    python -m capture.main --sysinfo    # Show detailed system info
"""

from __future__ import annotations

import argparse
import sys

from capture import __version__
from capture.config import Config
from capture.errors import friendly_error, DependencyError, ConfigError
from capture.utils import (
    check_all_dependencies,
    check_dependency,
    get_system_info,
    REQUIRED_MODULES,
)

# ═══════════════════════════════════════════════════════════════════════════
# Banner
# ═══════════════════════════════════════════════════════════════════════════

BANNER = r"""
   ____              _____
  / ____|___   ___  |_   _|  _  _ _ __  ___
 | |    / _ \ / _ \   | || | | | '_ \/ __|
 | |___| (_) | (_) |  | || |_| | |_) \__ \
  \____\___/ \___/    |_| \__,_| .__/|___/
                               |_|
        Free, lightweight screen recorder for Windows.
                     No FFmpeg required.
"""


# ═══════════════════════════════════════════════════════════════════════════
# Argument parser
# ═══════════════════════════════════════════════════════════════════════════


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="capture",
        description="CapTure — Free, lightweight screen recorder for Windows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  capture                  Launch with best available UI\n"
            "  capture --console        Force console UI\n"
            "  capture --check          Verify dependencies\n"
            "  capture --sysinfo        Show detailed system info\n"
            "  capture --output ./vids  Save recordings to ./vids\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show version and exit.",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="Use console (keyboard-driven) UI.",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Use system tray UI (requires pystray + Pillow).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check system dependencies and exit.",
    )
    parser.add_argument(
        "--sysinfo",
        action="store_true",
        help="Show detailed system information and exit.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        metavar="DIR",
        help="Override output directory (default: ~/Videos/CapTure).",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=30,
        metavar="N",
        help="Target frames per second (default: 30).",
    )
    parser.add_argument(
        "--no-mic",
        action="store_true",
        help="Disable microphone recording.",
    )
    parser.add_argument(
        "--no-system-audio",
        action="store_true",
        help="Disable system audio recording.",
    )
    parser.add_argument(
        "--temp-dir",
        type=str,
        default=None,
        metavar="DIR",
        help="Override temporary files directory.",
    )
    return parser


# ═══════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════


@friendly_error(
    fallback_message="Fatal error during startup. "
                     "Please check your configuration and try again.",
    exit_on_error=True,
)
def main(argv: list[str] | None = None) -> None:
    """Main entry point.

    Parses CLI arguments, builds a Config, checks system readiness,
    and launches the UI (tray or console).

    Args:
        argv: Command-line arguments (defaults to sys.argv).
    """
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    # ── --version ───────────────────────────────────────────────────
    if args.version:
        print(BANNER)
        print(f"  CapTure v{__version__}")
        print(f"  Optimized for ThinkPad T460s (Intel HD Graphics 520)")
        return

    # ── --check ─────────────────────────────────────────────────────
    if args.check:
        _run_dependency_check()
        return

    # ── --sysinfo ───────────────────────────────────────────────────
    if args.sysinfo:
        _run_system_info()
        return

    # ── Build configuration ─────────────────────────────────────────
    try:
        config = Config(fps=args.fps)
    except Exception as exc:
        raise ConfigError(
            f"Invalid configuration: {exc}"
        ) from exc

    # Apply CLI overrides.
    if args.output:
        config.output_dir = args.output
    if args.temp_dir:
        config.temp_dir = args.temp_dir
    if args.no_mic:
        config.enable_mic = False
    if args.no_system_audio:
        config.enable_system_audio = False

    # Ensure directories exist.
    config.ensure_dirs()

    # Quick dependency check (non-blocking — critical deps also checked
    # by the controller at record time, but we warn early here).
    deps = check_all_dependencies()
    critical_missing = [
        name
        for name in ("cv2", "dxcam", "numpy", "pyaudio", "comtypes")
        if not deps.get(name, False)
    ]
    if critical_missing:
        friendly = [REQUIRED_MODULES.get(m, m) for m in critical_missing]
        print(
            f"\n  [CapTure] WARNING: Missing dependencies: "
            f"{', '.join(friendly)}",
            file=sys.stderr,
        )
        print(
            "  Recording may not work. Run 'capture --check' for details.",
            file=sys.stderr,
        )
        print()

    # ── Launch UI ───────────────────────────────────────────────────
    # Determine which UI to launch.
    # Default (no flags): tkinter GUI.
    # --console: keyboard-driven console UI.
    # --tray: system tray icon UI.
    if args.tray:
        from capture.ui import launch_ui
        launch_ui(config, force_console=False, force_tray=True)
    elif args.console:
        from capture.ui import launch_ui
        launch_ui(config, force_console=True, force_tray=False)
    else:
        # Default: try tkinter GUI, fall back to console on error.
        try:
            _launch_gui_mode(config)
        except Exception:
            # GUI failed — fall back to console UI.
            from capture.ui import launch_ui
            launch_ui(config, force_console=True, force_tray=False)


# ═══════════════════════════════════════════════════════════════════════════
# UI launchers
# ═══════════════════════════════════════════════════════════════════════════


def _launch_gui_mode(config: Config) -> None:
    """Launch the tkinter GUI (default mode).

    Prints a compact banner, then starts the GuiApp main loop.
    Falls back to the original behaviour if tkinter is unavailable.

    Args:
        config: CapTure Config instance.
    """
    # Quick check for tkinter availability.
    try:
        import tkinter  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "Tkinter is not available in this Python installation. "
            "Use --console or --tray instead."
        )

    print(BANNER)
    print(f"  CapTure v{__version__}")
    print(f"  Output: {config.output_dir}")
    print()

    from capture.gui import launch_gui
    launch_gui(config)


# ═══════════════════════════════════════════════════════════════════════════
# Subcommands
# ═══════════════════════════════════════════════════════════════════════════


def _run_dependency_check() -> None:
    """Check all dependencies and print a formatted report."""
    print(BANNER)
    print(f"  CapTure v{__version__} — Dependency Check")
    print()

    results = check_all_dependencies()

    print("  ──────────────── Dependency Status ────────────────")
    all_ok = True
    for module_name, friendly in REQUIRED_MODULES.items():
        available = results.get(module_name, False)
        status = "✓ OK" if available else "✗ MISSING"
        if not available:
            all_ok = False
        print(f"  {status:>12}  {friendly} ({module_name})")
    print("  ─────────────────────────────────────────────────────")

    if not all_ok:
        print()
        print("  Some dependencies are missing. Install them with:")
        print()
        print("    pip install opencv-python-headless dxcam numpy \\")
        print("                pyaudio comtypes pystray Pillow")
        print()
        print("  For PyAudio on Windows, you may need to install")
        print("  the wheel from: https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio")
        print()
        print("  Note: pystray and Pillow are optional (for tray UI only).")
    else:
        print()
        print("  All dependencies are available! CapTure is ready.")

    # Also show system info summary.
    print()
    info = get_system_info()
    print("  ──────────────── System Summary ───────────────────")
    print(f"  Platform:     {info.get('platform', '?')} {info.get('platform_release', '')}")
    print(f"  Python:       {info.get('python_version', '').split()[0]}")
    print(f"  OpenCV:       {info.get('opencv_version') or 'N/A'}")
    print(f"  DXCam:        {info.get('dxcam_version') or 'N/A'}")
    print(f"  Monitors:     {info.get('monitor_count', '?')}")
    print("  ─────────────────────────────────────────────────────")


def _run_system_info() -> None:
    """Print detailed system information for diagnostics."""
    print(BANNER)
    print(f"  CapTure v{__version__} — System Information")
    print()

    info = get_system_info()

    sections = [
        ("Platform", [
            ("OS", info.get("platform", "")),
            ("Release", info.get("platform_release", "")),
            ("Version", info.get("platform_version", "")),
            ("Architecture", info.get("architecture", "")),
            ("Hostname", info.get("hostname", "")),
        ]),
        ("CPU", [
            ("Processor", info.get("cpu_info", "")),
            ("Logical cores", str(info.get("cpu_count", ""))),
        ]),
        ("Python", [
            ("Version", info.get("python_version", "").split("\n")[0]
                     if info.get("python_version") else ""),
        ]),
        ("OpenCV", [
            ("Version", str(info.get("opencv_version") or "Not installed")),
            ("MSMF backend", "Yes" if info.get("opencv_msmf") else "No"),
        ]),
        ("DXCam", [
            ("Version", str(info.get("dxcam_version") or "Not installed")),
            ("Monitors", str(info.get("monitor_count") or "?")),
        ]),
        ("NumPy", [
            ("Version", str(info.get("numpy_version") or "Not installed")),
        ]),
        ("PyAudio", [
            ("Version", str(info.get("pyaudio_version") or "Not installed")),
        ]),
        ("comtypes", [
            ("Version", str(info.get("comtypes_version") or "Not installed")),
        ]),
    ]

    for section_name, rows in sections:
        print(f"  ── {section_name} ──")
        for key, value in rows:
            print(f"    {key:<18} {value}")
        print()


# ═══════════════════════════════════════════════════════════════════════════
# Script entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    main()
