"""
Tkinter-based graphical user interface for CapTure.

Provides a Bandicam-style window with a big round record button,
live recording timer, FPS counter, file size display, and a status bar.

The GUI uses only Python stdlib (tkinter) — no new dependencies.
All errors are caught and displayed as friendly messages, never tracebacks.

Usage:
    from capture.controller import RecordingController
    from capture.config import Config
    from capture.gui import GuiApp

    config = Config()
    controller = RecordingController(config)
    app = GuiApp(controller)
    app.run()
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Any

from capture.config import Config
from capture.controller import RecordingController, RecordingState
from capture.errors import show_error
from capture.utils import format_duration, format_size_bytes


# ── Colour palette ─────────────────────────────────────────────────────

COLOR_BG = "#1e1e2e"           # dark background
COLOR_PANEL = "#2a2a3c"        # slightly lighter panel
COLOR_TEXT = "#cdd6f4"         # light text
COLOR_TEXT_MUTED = "#6c7086"   # muted / secondary text
COLOR_RED = "#e64553"          # record button red
COLOR_RED_DIM = "#6e1c24"      # dimmed red (blink off)
COLOR_STOP = "#585b70"         # stop button grey
COLOR_ACCENT = "#89b4fa"       # accent blue
COLOR_SUCCESS = "#a6e3a1"      # green for "Saved"
COLOR_BUTTON_HOVER = "#3a3a50" # button hover
COLOR_BORDER = "#45475a"       # subtle border


# ═══════════════════════════════════════════════════════════════════════════
# Settings dialog
# ═══════════════════════════════════════════════════════════════════════════


class _SettingsDialog(tk.Toplevel):
    """Simple settings popup window.

    Allows the user to adjust FPS, mic toggle, and system audio toggle.
    """

    def __init__(self, parent: tk.Tk, config: Config) -> None:
        """Create and show the settings dialog.

        Args:
            parent: Parent tkinter window.
            config: CapTure Config instance (modified in-place on save).
        """
        super().__init__(parent)
        self.title("CapTure — Settings")
        self.resizable(False, False)
        self.configure(bg=COLOR_BG)
        self.transient(parent)
        self.grab_set()

        self._config = config
        self._result: bool = False

        # ── FPS ────────────────────────────────────────────────────
        fps_frame = tk.Frame(self, bg=COLOR_BG)
        fps_frame.pack(fill=tk.X, padx=20, pady=(20, 5))
        tk.Label(
            fps_frame, text="FPS:",
            fg=COLOR_TEXT, bg=COLOR_BG, font=("Segoe UI", 10),
        ).pack(side=tk.LEFT)

        self._fps_var = tk.StringVar(value=str(config.fps))
        fps_entry = tk.Entry(
            fps_frame, textvariable=self._fps_var,
            width=6, bg=COLOR_PANEL, fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT, relief=tk.FLAT,
            font=("Segoe UI", 10),
        )
        fps_entry.pack(side=tk.LEFT, padx=(10, 0))

        # ── Mic ────────────────────────────────────────────────────
        self._mic_var = tk.BooleanVar(value=config.enable_mic)
        mic_cb = tk.Checkbutton(
            self, text="Record microphone",
            variable=self._mic_var,
            fg=COLOR_TEXT, bg=COLOR_BG,
            selectcolor=COLOR_PANEL,
            activebackground=COLOR_BG,
            activeforeground=COLOR_TEXT,
            font=("Segoe UI", 10),
        )
        mic_cb.pack(fill=tk.X, padx=20, pady=(15, 0))

        # ── System audio ───────────────────────────────────────────
        self._sys_audio_var = tk.BooleanVar(value=config.enable_system_audio)
        sys_cb = tk.Checkbutton(
            self, text="Record system audio",
            variable=self._sys_audio_var,
            fg=COLOR_TEXT, bg=COLOR_BG,
            selectcolor=COLOR_PANEL,
            activebackground=COLOR_BG,
            activeforeground=COLOR_TEXT,
            font=("Segoe UI", 10),
        )
        sys_cb.pack(fill=tk.X, padx=20, pady=(8, 0))

        # ── Buttons ────────────────────────────────────────────────
        btn_frame = tk.Frame(self, bg=COLOR_BG)
        btn_frame.pack(fill=tk.X, padx=20, pady=(20, 20))

        cancel_btn = tk.Button(
            btn_frame, text="Cancel",
            command=self.destroy,
            bg=COLOR_PANEL, fg=COLOR_TEXT,
            activebackground=COLOR_BUTTON_HOVER, activeforeground=COLOR_TEXT,
            relief=tk.FLAT, font=("Segoe UI", 10),
            padx=16, pady=4, cursor="hand2",
        )
        cancel_btn.pack(side=tk.RIGHT, padx=(8, 0))

        save_btn = tk.Button(
            btn_frame, text="Save",
            command=self._on_save,
            bg=COLOR_ACCENT, fg=COLOR_BG,
            activebackground=COLOR_ACCENT, activeforeground=COLOR_BG,
            relief=tk.FLAT, font=("Segoe UI", 10, "bold"),
            padx=16, pady=4, cursor="hand2",
        )
        save_btn.pack(side=tk.RIGHT)

    def _on_save(self) -> None:
        """Validate and apply settings, then close."""
        try:
            fps = int(self._fps_var.get())
            if fps < 1 or fps > 120:
                messagebox.showwarning(
                    "CapTure",
                    "FPS must be between 1 and 120.",
                    parent=self,
                )
                return
            self._config.fps = fps
        except ValueError:
            messagebox.showwarning(
                "CapTure",
                "Please enter a valid integer for FPS.",
                parent=self,
            )
            return

        self._config.enable_mic = self._mic_var.get()
        self._config.enable_system_audio = self._sys_audio_var.get()
        self._result = True
        self.destroy()


# ═══════════════════════════════════════════════════════════════════════════
# Main GUI Application
# ═══════════════════════════════════════════════════════════════════════════


class GuiApp:
    """Bandicam-style tkinter GUI for CapTure.

    Features:
    - Large round red REC / grey STOP button (Canvas-drawn).
    - Recording timer (HH:MM:SS), FPS counter, file size display.
    - Blinking animation on the record button during recording.
    - Status bar with friendly messages.
    - Folder selector and settings button.

    All UI updates are driven by ``root.after()`` — no background threads
    touch the GUI directly.

    Usage:
        controller = RecordingController(config)
        app = GuiApp(controller)
        app.run()  # blocking
    """

    STATUS_INTERVAL: int = 500       # ms between status refreshes
    BLINK_INTERVAL: int = 500        # ms between record-button blinks
    WINDOW_WIDTH: int = 400
    WINDOW_HEIGHT: int = 360
    BUTTON_DIAMETER: int = 80

    def __init__(self, controller: RecordingController) -> None:
        """Initialise the GUI application.

        Args:
            controller: The RecordingController to drive.
        """
        self._controller: RecordingController = controller
        self._root: tk.Tk | None = None
        self._canvas: tk.Canvas | None = None
        self._button_id: int | None = None    # canvas oval item ID
        self._button_text_id: int | None = None
        self._timer_label: tk.Label | None = None
        self._stats_label: tk.Label | None = None
        self._status_label: tk.Label | None = None
        self._blink_job: str | None = None     # after() job ID
        self._status_job: str | None = None     # after() job ID
        self._blink_on: bool = True
        self._last_file_size: int = 0

        # Register callbacks.
        self._controller.set_status_callback(self._on_status_update)
        self._controller.set_error_callback(self._on_error)

    # ── Public API ──────────────────────────────────────────────────

    def run(self) -> None:
        """Create the window and start the tkinter main loop (blocking)."""
        self._root = tk.Tk()
        self._root.title("CapTure — Screen Recorder")
        self._root.resizable(False, False)
        self._root.configure(bg=COLOR_BG)

        # Centre on screen.
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = (screen_w - self.WINDOW_WIDTH) // 2
        y = (screen_h - self.WINDOW_HEIGHT) // 2
        self._root.geometry(
            f"{self.WINDOW_WIDTH}x{self.WINDOW_HEIGHT}+{x}+{y}"
        )

        # Prevent resizing via window manager hints.
        self._root.minsize(self.WINDOW_WIDTH, self.WINDOW_HEIGHT)
        self._root.maxsize(self.WINDOW_WIDTH, self.WINDOW_HEIGHT)

        # Close handler.
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()

        # Start periodic status updates.
        self._schedule_status_update()

        try:
            self._root.mainloop()
        except Exception as exc:
            show_error(exc)

    # ── UI construction ─────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Build all UI widgets."""
        assert self._root is not None

        # ── Logo ───────────────────────────────────────────────────
        logo_frame = tk.Frame(self._root, bg=COLOR_BG)
        logo_frame.pack(fill=tk.X, padx=20, pady=(24, 0))

        tk.Label(
            logo_frame, text="CapTure",
            fg=COLOR_ACCENT, bg=COLOR_BG,
            font=("Segoe UI", 28, "bold"),
        ).pack(side=tk.LEFT)

        # Settings and folder buttons (right side of logo row).
        btn_row = tk.Frame(logo_frame, bg=COLOR_BG)
        btn_row.pack(side=tk.RIGHT)

        folder_btn = tk.Button(
            btn_row, text="\U0001F4C1",    # 📁
            command=self._choose_folder,
            bg=COLOR_PANEL, fg=COLOR_TEXT,
            activebackground=COLOR_BUTTON_HOVER, activeforeground=COLOR_TEXT,
            relief=tk.FLAT, font=("Segoe UI", 14),
            width=2, cursor="hand2",
        )
        folder_btn.pack(side=tk.RIGHT, padx=(4, 0))

        settings_btn = tk.Button(
            btn_row, text="\u2699",         # ⚙
            command=self._open_settings,
            bg=COLOR_PANEL, fg=COLOR_TEXT,
            activebackground=COLOR_BUTTON_HOVER, activeforeground=COLOR_TEXT,
            relief=tk.FLAT, font=("Segoe UI", 14),
            width=2, cursor="hand2",
        )
        settings_btn.pack(side=tk.RIGHT, padx=(0, 4))

        # ── Record button (Canvas) ─────────────────────────────────
        canvas_frame = tk.Frame(self._root, bg=COLOR_BG)
        canvas_frame.pack(pady=(16, 0))

        self._canvas = tk.Canvas(
            canvas_frame,
            width=self.BUTTON_DIAMETER + 4,
            height=self.BUTTON_DIAMETER + 4,
            bg=COLOR_BG,
            highlightthickness=0,
            cursor="hand2",
        )
        self._canvas.pack()

        # Draw the circle.
        cx = (self.BUTTON_DIAMETER + 4) / 2
        cy = (self.BUTTON_DIAMETER + 4) / 2
        r = self.BUTTON_DIAMETER / 2

        self._button_id = self._canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            fill=COLOR_RED, outline=COLOR_RED_DIM, width=3,
        )
        self._button_text_id = self._canvas.create_text(
            cx, cy,
            text="\u25CF REC",             # ● REC
            fill="#ffffff", font=("Segoe UI", 12, "bold"),
        )
        self._canvas.tag_bind(self._button_id, "<Button-1>", self._on_button_click)
        self._canvas.tag_bind(self._button_text_id, "<Button-1>", self._on_button_click)

        # ── Timer ──────────────────────────────────────────────────
        self._timer_label = tk.Label(
            self._root, text="00:00:00",
            fg=COLOR_TEXT, bg=COLOR_BG,
            font=("Consolas", 26, "bold"),
        )
        self._timer_label.pack(pady=(8, 0))

        # ── Stats row (FPS + file size) ────────────────────────────
        self._stats_label = tk.Label(
            self._root, text="FPS: —   |   Size: —",
            fg=COLOR_TEXT_MUTED, bg=COLOR_BG,
            font=("Segoe UI", 9),
        )
        self._stats_label.pack(pady=(0, 8))

        # ── Status bar (bottom) ────────────────────────────────────
        status_frame = tk.Frame(self._root, bg=COLOR_PANEL, height=32)
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        status_frame.pack_propagate(False)

        self._status_label = tk.Label(
            status_frame, text="Ready",
            fg=COLOR_TEXT_MUTED, bg=COLOR_PANEL,
            font=("Segoe UI", 9),
            anchor=tk.W,
        )
        self._status_label.pack(fill=tk.X, padx=12, pady=4)

    # ── Button click handler ────────────────────────────────────────

    def _on_button_click(self, event: tk.Event) -> None:
        """Handle clicks on the record/stop button."""
        self._toggle_recording()

    def _toggle_recording(self) -> None:
        """Start or stop recording based on current state."""
        if self._controller.is_recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        """Start a new recording session."""
        state = self._controller.state
        if state in (RecordingState.RECORDING, RecordingState.STARTING):
            return
        if state in (RecordingState.STOPPING, RecordingState.MUXING):
            self._update_status("Please wait — finishing previous session...")
            return

        self._update_status("Starting...")
        ok = False
        try:
            ok = self._controller.start()
        except Exception as exc:
            show_error(exc)
            self._update_status("Error starting recording.")

        if ok:
            self._set_button_stop()
            self._start_blink()
            self._update_status("Recording...")
        else:
            self._update_status("Failed to start recording.")

    def _stop_recording(self) -> None:
        """Stop the current recording session."""
        self._stop_blink()
        self._set_button_record()
        self._update_status("Saving...")

        result = None
        try:
            result = self._controller.stop()
        except Exception as exc:
            show_error(exc)
            self._update_status("Error while saving.")

        if result is not None:
            try:
                fs = format_size_bytes(os.path.getsize(result))
                self._update_status(f"Saved: {result}  ({fs})")
            except Exception:
                self._update_status(f"Saved: {result}")
        else:
            self._update_status("Save failed — check errors above.")

        # Reset display.
        assert self._timer_label is not None
        self._timer_label.config(text="00:00:00")
        if self._stats_label is not None:
            self._stats_label.config(text="FPS: —   |   Size: —")

    # ── Button appearance ───────────────────────────────────────────

    def _set_button_record(self) -> None:
        """Switch button to the red REC state."""
        if self._canvas is None or self._button_id is None:
            return
        self._canvas.itemconfig(self._button_id, fill=COLOR_RED,
                                outline=COLOR_RED_DIM)
        if self._button_text_id is not None:
            self._canvas.itemconfig(self._button_text_id, text="\u25CF REC",
                                    fill="#ffffff")

    def _set_button_stop(self) -> None:
        """Switch button to the grey STOP state."""
        if self._canvas is None or self._button_id is None:
            return
        self._canvas.itemconfig(self._button_id, fill=COLOR_STOP,
                                outline=COLOR_BORDER)
        if self._button_text_id is not None:
            self._canvas.itemconfig(self._button_text_id,
                                    text="\u25A0 STOP",
                                    fill=COLOR_TEXT)

    # ── Blink animation ─────────────────────────────────────────────

    def _start_blink(self) -> None:
        """Begin blinking the record button red/dim."""
        self._blink_on = True
        self._do_blink()

    def _stop_blink(self) -> None:
        """Cancel the blink animation."""
        if self._blink_job is not None and self._root is not None:
            try:
                self._root.after_cancel(self._blink_job)
            except Exception:
                pass
        self._blink_job = None
        self._set_button_stop()

    def _do_blink(self) -> None:
        """Perform one blink toggle and schedule the next."""
        if self._canvas is None or self._button_id is None:
            return

        if not self._controller.is_recording:
            # Stopped externally — clean up.
            self._blink_job = None
            self._set_button_record()
            return

        self._blink_on = not self._blink_on
        fill = COLOR_RED if self._blink_on else COLOR_RED_DIM
        self._canvas.itemconfig(self._button_id, fill=fill)

        if self._root is not None:
            self._blink_job = self._root.after(self.BLINK_INTERVAL, self._do_blink)

    # ── Status updates ──────────────────────────────────────────────

    def _schedule_status_update(self) -> None:
        """Kick off the periodic status refresh loop."""
        if self._root is not None:
            self._status_job = self._root.after(self.STATUS_INTERVAL,
                                                self._refresh_live_stats)

    def _refresh_live_stats(self) -> None:
        """Update the timer, FPS, and file size labels from the controller.

        Runs on the main tkinter thread via ``root.after()``.
        """
        if self._timer_label is None or self._stats_label is None:
            # Widgets not yet created — reschedule.
            self._schedule_status_update()
            return

        status = self._controller.get_status()
        elapsed = float(status.get("elapsed", 0.0))
        fps = float(status.get("fps", 0.0))
        state_str = str(status.get("state", "IDLE"))

        # Timer.
        if state_str == "RECORDING" or elapsed > 0:
            self._timer_label.config(text=format_duration(elapsed))
        else:
            self._timer_label.config(text="00:00:00")

        # FPS and file size.
        if state_str == "RECORDING":
            fps_text = f"FPS: {fps:4.1f}" if fps > 0 else "FPS:  —"
            # File size — check the temp video if available.
            size_text = self._get_recording_size()
            self._stats_label.config(text=f"{fps_text}  |  Size: {size_text}")
        elif state_str in ("STOPPING", "MUXING"):
            self._stats_label.config(text="Finalizing...")
        else:
            self._stats_label.config(text="FPS: —   |   Size: —")

        # Reschedule.
        if self._root is not None:
            self._status_job = self._root.after(
                self.STATUS_INTERVAL, self._refresh_live_stats
            )

    def _get_recording_size(self) -> str:
        """Estimate current recording file size from temp files.

        Returns:
            Human-readable size string, or "—".
        """
        try:
            # Check video encoder's temp output.
            vpath = getattr(self._controller._video_enc, "output_path", "")
            total = 0
            if vpath and os.path.isfile(vpath):
                total += os.path.getsize(vpath)
            apath = getattr(self._controller._audio_enc, "output_path", "")
            if apath and os.path.isfile(apath):
                total += os.path.getsize(apath)
            if total > 0:
                return format_size_bytes(total)
        except Exception:
            pass
        return "\u2014"  # em dash

    # ── Status bar helpers ──────────────────────────────────────────

    def _update_status(self, message: str) -> None:
        """Update the status bar label.

        Args:
            message: Human-readable status string.
        """
        if self._status_label is not None:
            self._status_label.config(text=message)

    # ── Button handlers ─────────────────────────────────────────────

    def _choose_folder(self) -> None:
        """Open a folder picker dialog for the output directory."""
        if self._root is None:
            return
        folder = filedialog.askdirectory(
            parent=self._root,
            title="Choose output folder",
            initialdir=self._controller.config.output_dir,
        )
        if folder:
            self._controller.config.output_dir = folder
            try:
                self._controller.config.ensure_dirs()
            except Exception as exc:
                show_error(exc)
            self._update_status(f"Output folder: {folder}")

    def _open_settings(self) -> None:
        """Open the settings dialog."""
        if self._root is None:
            return
        try:
            _SettingsDialog(self._root, self._controller.config)
        except Exception as exc:
            show_error(exc)

    # ── Controller callbacks ────────────────────────────────────────

    def _on_status_update(self, message: str) -> None:
        """Receive status updates from the controller.

        We schedule the update on the main thread via after() to stay
        thread-safe since the controller can call this from a worker.

        Args:
            message: Human-readable status string.
        """
        if self._root is not None:
            self._root.after(0, self._update_status, message)

    def _on_error(self, exc: Exception) -> None:
        """Receive error notifications from the controller.

        The controller already calls show_error(). We update the
        status bar on the main thread.

        Args:
            exc: The exception object.
        """
        msg = "Error: " + (
            exc.message if hasattr(exc, "message") else str(exc)
        )
        if self._root is not None:
            self._root.after(0, self._update_status, msg)

    # ── Window close ────────────────────────────────────────────────

    def _on_close(self) -> None:
        """Handle window close — stop recording if active, then exit.

        Always saves the recording before closing.  This is the
        graceful path; the user can also force-quit but we make the
        best effort.
        """
        if self._controller.is_recording:
            try:
                self._controller.stop()
            except Exception as exc:
                show_error(exc)

        self._stop_blink()
        if self._status_job is not None and self._root is not None:
            try:
                self._root.after_cancel(self._status_job)
            except Exception:
                pass

        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# Module-level launch helper
# ═══════════════════════════════════════════════════════════════════════════


def launch_gui(config: Config) -> None:
    """Create a controller and launch the tkinter GUI.

    Convenience wrapper for use from ``main.py`` / ``launch_ui``.

    Args:
        config: CapTure Config instance.
    """
    controller = RecordingController(config)

    # Register signal handlers so Ctrl+C from a terminal can still
    # cleanly stop a recording.
    import signal

    def _signal_handler(signum: int, frame: object) -> None:
        if controller.is_recording:
            try:
                controller.stop()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
    except Exception:
        pass

    app = GuiApp(controller)
    app.run()
