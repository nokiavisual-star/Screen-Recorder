"""
Minimal UI for CapTure — console interface and system tray icon.

Provides:
- ConsoleUI: interactive keyboard-driven console interface with
  live status updates during recording.
- TrayUI: system tray icon via pystray + Pillow (optional).
- launch_ui(): entry point that selects the best available UI.

The UI layer never handles tracebacks — all errors are caught
by the controller and surfaced as friendly messages.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from typing import Callable

from capture.config import Config
from capture.controller import RecordingController, RecordingState
from capture.errors import show_error
from capture.utils import format_duration, format_size_bytes
from capture import __version__


# ═══════════════════════════════════════════════════════════════════════════
# ConsoleUI
# ═══════════════════════════════════════════════════════════════════════════


class ConsoleUI:
    """Simple console-based UI with keyboard-driven menu.

    Supports:
    - ``[R]`` Start recording
    - ``[S]`` Stop recording
    - ``[Q]`` Quit
    - Live status updates (FPS, frames, elapsed) every 500 ms while recording.

    Usage:
        config = Config()
        controller = RecordingController(config)
        ui = ConsoleUI(controller)
        ui.run()
    """

    STATUS_INTERVAL: float = 0.5  # seconds between live status refreshes

    def __init__(self, controller: RecordingController) -> None:
        """Initialize console UI.

        Args:
            controller: The recording controller to drive.
        """
        self._controller: RecordingController = controller
        self._running: bool = True
        self._status_thread: threading.Thread | None = None
        self._input_lock: threading.Lock = threading.Lock()

        # Register controller callbacks.
        self._controller.set_status_callback(self._on_status_update)
        self._controller.set_error_callback(self._on_error)

    def run(self) -> None:
        """Run the console UI loop (blocking).

        Displays the banner, then enters a loop reading keyboard input
        from stdin.  A background thread prints live status while
        recording is active.
        """
        self._print_banner()
        self._print_menu()

        # Start the live status thread.
        self._status_thread = threading.Thread(
            target=self._status_loop,
            name="ui-status",
            daemon=True,
        )
        self._status_thread.start()

        # Main input loop.
        try:
            while self._running:
                self._print_prompt()
                try:
                    line = input()
                except EOFError:
                    # stdin closed — exit cleanly.
                    break
                except KeyboardInterrupt:
                    print("\n")
                    self._handle_quit()
                    break

                choice = line.strip().upper()
                if choice == "R":
                    self._handle_record()
                elif choice == "S":
                    self._handle_stop()
                elif choice == "Q":
                    self._handle_quit()
                    break
                else:
                    if choice:
                        print(f"  Unknown command: '{choice}'. Use R, S, or Q.")
                    self._print_menu()
        finally:
            self._running = False
            # If still recording when exiting, stop gracefully.
            if self._controller.is_recording:
                print("\n  Stopping recording before exit...")
                self._controller.stop()

        print("\n  Goodbye!")

    # ── Command handlers ────────────────────────────────────────────

    def _handle_record(self) -> None:
        """Start recording via the controller."""
        state = self._controller.state
        if state in (RecordingState.RECORDING, RecordingState.STARTING):
            print("  Already recording!")
            return
        if state in (RecordingState.STOPPING, RecordingState.MUXING):
            print("  Please wait — finishing previous session...")
            return

        print("\n  Starting recording...")
        ok = self._controller.start()
        if ok:
            print("  Recording started. Press [S] to stop.")
        else:
            print("  Failed to start recording. Check errors above.")

    def _handle_stop(self) -> None:
        """Stop recording and show the output path."""
        if not self._controller.is_recording:
            print("  No active recording to stop.")
            return

        print("\n  Stopping recording...")
        result = self._controller.stop()
        if result is not None:
            try:
                file_size = format_size_bytes(
                    __import__("os").path.getsize(result)
                )
                print(f"  Recording saved: {result} ({file_size})")
            except Exception:
                print(f"  Recording saved: {result}")
        else:
            print("  Failed to save recording. Check errors above.")
        self._print_menu()

    def _handle_quit(self) -> None:
        """Quit the UI, stopping any active recording first."""
        self._running = False
        if self._controller.is_recording:
            print("\n  Recording in progress. Stopping before exit...")
            self._controller.stop()

    # ── Status thread ───────────────────────────────────────────────

    def _status_loop(self) -> None:
        """Background thread: print live status every STATUS_INTERVAL.

        Only prints while the controller is in RECORDING state.
        """
        last_status_line = False
        while self._running:
            if self._controller.is_recording:
                status = self._controller.get_status()
                fps = status.get("fps", 0.0)
                frames = status.get("frame_count", 0)
                audio_samples = status.get("audio_samples", 0)
                elapsed = float(status.get("elapsed", 0.0))

                line = (
                    f"\r  ● RECORDING | FPS: {fps:5.1f} | "
                    f"Frames: {frames:6d} | "
                    f"Audio: {audio_samples:8d} samples | "
                    f"Time: {format_duration(elapsed)}   "
                )
                sys.stdout.write(line)
                sys.stdout.flush()
                last_status_line = True
            else:
                if last_status_line:
                    # Clear the status line and move to a new line.
                    sys.stdout.write("\r" + " " * 80 + "\r")
                    sys.stdout.flush()
                    last_status_line = False

            time.sleep(self.STATUS_INTERVAL)

        # Clean up on exit.
        if last_status_line:
            sys.stdout.write("\r" + " " * 80 + "\r")
            sys.stdout.flush()

    # ── Controller callbacks ────────────────────────────────────────

    def _on_status_update(self, message: str) -> None:
        """Receive status updates from the controller.

        Args:
            message: Human-readable status string.
        """
        print(f"\n  [Status] {message}")

    def _on_error(self, exc: Exception) -> None:
        """Receive error notifications from the controller.

        Args:
            exc: The exception (already displayed by show_error).
        """
        pass  # Controller already calls show_error() — nothing extra needed.

    # ── Display helpers ─────────────────────────────────────────────

    @staticmethod
    def _print_banner() -> None:
        """Print the CapTure banner and version."""
        banner = r"""
   ____              _____
  / ____|___   ___  |_   _|  _  _ _ __  ___
 | |    / _ \ / _ \   | || | | | '_ \/ __|
 | |___| (_) | (_) |  | || |_| | |_) \__ \
  \____\___/ \___/    |_| \__,_| .__/|___/
                               |_|
        Free, lightweight screen recorder for Windows.
                     No FFmpeg required.
"""
        print(banner)
        print(f"  Version: {__version__}")
        print(f"  Optimized for ThinkPad T460s (Intel HD Graphics 520)")
        print()

    @staticmethod
    def _print_menu() -> None:
        """Print the command menu."""
        print("  ──────────────────────────────────────")
        print("  [R] Record      Start screen recording")
        print("  [S] Stop        Stop and save recording")
        print("  [Q] Quit        Exit CapTure")
        print("  ──────────────────────────────────────")

    @staticmethod
    def _print_prompt() -> None:
        """Print the input prompt."""
        sys.stdout.write("\n  > ")
        sys.stdout.flush()


# ═══════════════════════════════════════════════════════════════════════════
# TrayUI (optional — requires pystray + Pillow)
# ═══════════════════════════════════════════════════════════════════════════


class TrayUI:
    """System tray icon UI using pystray + Pillow.

    Provides a minimal system tray icon with a right-click menu:
    - Start Recording / Stop Recording (context-sensitive)
    - About
    - Exit

    Note:
        Requires pystray and Pillow. On Linux, may need
        additional system packages (libappindicator, etc.).
    """

    APP_NAME: str = "CapTure"

    def __init__(self, controller: RecordingController) -> None:
        """Initialize tray UI.

        Args:
            controller: The recording controller to drive.
        """
        self._controller: RecordingController = controller
        self._icon: object | None = None  # pystray.Icon
        self._running: bool = False

        # Register controller callbacks.
        self._controller.set_status_callback(self._on_status)
        self._controller.set_error_callback(self._on_error)

    def run(self) -> None:
        """Create and run the system tray icon (blocking).

        Builds a simple 32×32 icon via Pillow, creates a pystray.Icon
        with a right-click menu, and calls ``icon.run()``.
        """
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError as exc:
            print(
                f"  [CapTure] Tray UI unavailable: {exc}",
                file=sys.stderr,
            )
            print(
                "  Install pystray and Pillow: pip install pystray Pillow",
                file=sys.stderr,
            )
            return

        # Generate a simple icon: red circle when recording, grey when idle.
        icon_image = self._make_icon(is_recording=False)

        # Build the menu.
        menu = self._build_menu()

        self._icon = pystray.Icon(
            self.APP_NAME,
            icon_image,
            self.APP_NAME,
            menu=menu,
        )
        self._running = True

        # Start a background thread to update the icon during recording.
        updater = threading.Thread(
            target=self._icon_update_loop,
            name="tray-update",
            daemon=True,
        )
        updater.start()

        # Block here until icon.stop() is called.
        self._icon.run()

    def stop(self) -> None:
        """Stop the tray icon (called from another thread or signal handler)."""
        self._running = False
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass

    # ── Menu builders ───────────────────────────────────────────────

    def _build_menu(self) -> object:
        """Build the pystray right-click menu.

        Returns:
            pystray.Menu instance.
        """
        import pystray

        return pystray.Menu(
            pystray.MenuItem(
                "Start Recording",
                self._on_start,
                enabled=lambda item: not self._controller.is_recording,
            ),
            pystray.MenuItem(
                "Stop Recording",
                self._on_stop,
                enabled=lambda item: self._controller.is_recording,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("About", self._on_about),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._on_exit),
        )

    # ── Icon generation ─────────────────────────────────────────────

    @staticmethod
    def _make_icon(is_recording: bool) -> object:
        """Create a simple 32×32 PNG icon via Pillow.

        Args:
            is_recording: True → red circle indicator, False → grey.

        Returns:
            PIL.Image.Image in RGBA mode.
        """
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        if is_recording:
            fill = (220, 40, 40, 255)  # red
        else:
            fill = (120, 120, 120, 255)  # grey

        # Draw a filled circle.
        draw.ellipse([4, 4, 28, 28], fill=fill, outline=(255, 255, 255, 200))
        return img

    # ── Menu handlers ───────────────────────────────────────────────

    def _on_start(self, icon: object, item: object) -> None:
        """Start recording handler.

        Args:
            icon: pystray.Icon instance.
            item: pystray.MenuItem that triggered this.
        """
        try:
            ok = self._controller.start()
            if not ok:
                # Error already displayed by controller.
                pass
        except Exception as exc:
            show_error(exc)

    def _on_stop(self, icon: object, item: object) -> None:
        """Stop recording handler.

        Args:
            icon: pystray.Icon instance.
            item: pystray.MenuItem that triggered this.
        """
        try:
            result = self._controller.stop()
            if result is not None and icon is not None:
                try:
                    icon.notify(
                        f"Recording saved to:\n{result}",
                        title="CapTure",
                    )
                except Exception:
                    pass
        except Exception as exc:
            show_error(exc)

    def _on_about(self, icon: object, item: object) -> None:
        """About dialog handler.

        Args:
            icon: pystray.Icon instance.
            item: pystray.MenuItem that triggered this.
        """
        about_msg = (
            f"CapTure v{__version__}\n\n"
            "Free, lightweight screen recorder for Windows.\n"
            "No FFmpeg required — uses Media Foundation.\n"
            "Optimized for ThinkPad T460s.\n\n"
            "https://github.com/capture-app/capture"
        )
        if icon is not None:
            try:
                icon.notify(about_msg, title="About CapTure")
            except Exception:
                print(f"\n{about_msg}")

    def _on_exit(self, icon: object, item: object) -> None:
        """Exit handler — stop recording if active, then quit.

        Args:
            icon: pystray.Icon instance.
            item: pystray.MenuItem that triggered this.
        """
        if self._controller.is_recording:
            try:
                self._controller.stop()
            except Exception as exc:
                show_error(exc)
        self.stop()

    # ── Callbacks from controller ───────────────────────────────────

    def _on_status(self, message: str) -> None:
        """Status update from controller (no-op in tray mode).

        Args:
            message: Status string from RecordingController.
        """
        # In tray mode we don't print status lines — the icon color
        # acts as the indicator. If pystray supports tooltips, we
        # could update the tooltip here, but pystray's tooltip is
        # fixed at creation time.
        pass

    def _on_error(self, exc: Exception) -> None:
        """Error notification from controller.

        Args:
            exc: The exception (already shown by show_error).
        """
        pass

    # ── Icon update loop ────────────────────────────────────────────

    def _icon_update_loop(self) -> None:
        """Background thread: update icon colour based on recording state."""
        last_was_recording = False
        while self._running:
            is_rec = self._controller.is_recording
            if is_rec != last_was_recording:
                try:
                    if self._icon is not None:
                        self._icon.icon = self._make_icon(is_recording=is_rec)
                except Exception:
                    pass
                last_was_recording = is_rec
            time.sleep(0.5)


# ═══════════════════════════════════════════════════════════════════════════
# UI launcher
# ═══════════════════════════════════════════════════════════════════════════


def launch_ui(config: Config, force_console: bool = False) -> None:
    """Entry point: detect environment and launch the best UI.

    Tries tray UI first on Windows. Falls back to console UI if:
    - ``force_console`` is True.
    - Platform is not Windows.
    - Tray dependencies (pystray, Pillow) are missing.

    Installs signal handlers for clean shutdown on SIGINT/SIGTERM.

    Args:
        config: CapTure Config instance.
        force_console: If True, skip tray UI and use console directly.
    """
    controller = RecordingController(config)

    # Install signal handlers for clean shutdown.
    def _signal_handler(signum: int, frame: object) -> None:
        print("\n  [CapTure] Shutting down...")
        if controller.is_recording:
            controller.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
    except Exception:
        # SIGTERM handler may not be installable on all platforms.
        pass

    # Decide which UI to launch.
    if force_console:
        ui: ConsoleUI | TrayUI = ConsoleUI(controller)
    else:
        # Try tray UI on Windows, fall back to console.
        if sys.platform == "win32":
            try:
                import pystray  # noqa: F401
                from PIL import Image  # noqa: F401
                ui = TrayUI(controller)
            except ImportError:
                print(
                    "  [CapTure] Tray UI unavailable (missing pystray/Pillow). "
                    "Using console UI.",
                    file=sys.stderr,
                )
                ui = ConsoleUI(controller)
        else:
            print(
                "  [CapTure] Tray UI only available on Windows. "
                "Using console UI.",
                file=sys.stderr,
            )
            ui = ConsoleUI(controller)

    ui.run()
