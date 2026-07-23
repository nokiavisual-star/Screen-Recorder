"""
Main controller — orchestrates screen capture, audio capture,
video encoding, audio encoding, and muxing into a cohesive
recording session.

This is the "brain" of CapTure. It starts/stops all subsystems
in the correct order and handles the recording lifecycle:
  1. Start audio capture + encoder
  2. Start video capture + encoder
  3. Route frames/audio chunks from capture to encoders
  4. Stop in reverse order
  5. Mux video + audio into final MP4

Thread safety: capture threads push data through thread-safe
queues/callbacks. The controller owns the lifecycle.
"""

from __future__ import annotations

import threading
import time
from enum import Enum, auto
from typing import Callable

import numpy as np

from capture.config import Config
from capture.errors import (
    CapTureError,
    friendly_error,
    show_error,
)
from capture.screen_capture import ScreenCapture
from capture.audio_capture import AudioCapture
from capture.video_encoder import VideoEncoder
from capture.audio_encoder import AudioEncoder
from capture.muxer import Muxer
from capture.utils import check_all_dependencies


class RecordingState(Enum):
    """Recording lifecycle states."""
    IDLE = auto()
    STARTING = auto()
    RECORDING = auto()
    STOPPING = auto()
    MUXING = auto()
    ERROR = auto()


class RecordingController:
    """Orchestrates the full recording pipeline.

    Manages the lifecycle: init -> start -> record -> stop -> mux.

    Usage:
        config = Config()
        controller = RecordingController(config)
        if controller.start():
            # ... recording ...
            output = controller.stop()
            # Final MP4 is at *output* or controller.output_path

    Attributes:
        config: Application configuration.
        state: Current RecordingState.
        output_path: Path to the final MP4 (set after muxing).
        duration: Total recording duration in seconds.
    """

    def __init__(self, config: Config) -> None:
        """Initialize all capture and encoding subsystems.

        Args:
            config: CapTure Config instance.

        Raises:
            CapTureError: If any subsystem fails to initialize.
        """
        self.config: Config = config
        self.state: RecordingState = RecordingState.IDLE
        self.output_path: str = ""
        self.duration: float = 0.0

        # Subsystems (created at init, started/stopped during record).
        self._screen: ScreenCapture = ScreenCapture(config)
        self._audio: AudioCapture = AudioCapture(config)
        self._video_enc: VideoEncoder = VideoEncoder(config)
        self._audio_enc: AudioEncoder = AudioEncoder(config)
        self._muxer: Muxer = Muxer(config)

        # State tracking.
        self._start_time: float = 0.0
        self._lock: threading.Lock = threading.Lock()

        # Callbacks for UI updates.
        self._status_callback: Callable[[str], None] | None = None
        self._error_callback: Callable[[Exception], None] | None = None

    # ── Public API ──────────────────────────────────────────────────

    def set_status_callback(self, callback: Callable[[str], None]) -> None:
        """Register a callback for status updates (for UI).

        Args:
            callback: Called with a status message string.
        """
        self._status_callback = callback

    def set_error_callback(
        self, callback: Callable[[Exception], None]
    ) -> None:
        """Register a callback for error notifications (for UI).

        Args:
            callback: Called with the exception object.
        """
        self._error_callback = callback

    def start(self) -> bool:
        """Start the recording session.

        Initializes temp directories, starts encoders, connects
        callbacks, then starts capture sources.

        Returns:
            True if recording started successfully, False otherwise.
        """
        with self._lock:
            if self.state != RecordingState.IDLE:
                print(
                    "[CapTure] Cannot start: already recording or "
                    "processing a previous session.",
                    file=__import__("sys").stderr,
                )
                return False

            self.state = RecordingState.STARTING

        try:
            # 1. Check dependencies.
            deps = check_all_dependencies()
            critical_missing = [
                name
                for name in ("cv2", "dxcam", "numpy", "pyaudio", "comtypes")
                if not deps.get(name, False)
            ]
            if critical_missing:
                self._set_error()
                self._notify_status(
                    "Missing critical dependencies: "
                    + ", ".join(critical_missing)
                    + ". Run 'capture --check' for details."
                )
                return False

            # 2. Ensure directories exist.
            self.config.ensure_dirs()

            # Use a dedicated subdirectory for this recording session
            # so muxer cleanup removes everything cleanly.
            import os
            session_temp = os.path.join(
                self.config.temp_dir,
                f"session_{int(time.time() * 1000)}",
            )

            # 3. Start video encoder.
            self._video_enc.start(session_temp)

            # 4. Start audio encoder.
            self._audio_enc.start(session_temp)

            # 5. Connect screen → video encoder.
            self._screen.on_frame(self._on_frame_received)

            # 6. Connect audio → audio encoder (ignoring source label).
            self._audio.on_audio(self._on_audio_received)

            # 7. Start screen capture.
            self._screen.start()

            # 8. Start audio capture.
            self._audio.start()

            # 9. Record start time.
            self._start_time = time.perf_counter()

            with self._lock:
                self.state = RecordingState.RECORDING

            self._notify_status("Recording started...")
            return True

        except CapTureError as exc:
            self._notify_error(exc)
            self._emergency_stop()
            return False
        except Exception as exc:
            wrapped = CapTureError(
                f"Unexpected error during recording start: {exc}"
            )
            self._notify_error(wrapped)
            self._emergency_stop()
            return False

    def stop(self) -> str | None:
        """Stop the recording and mux the final MP4.

        Stops capture sources first (screen, then audio), drains
        encoding queues, then invokes the muxer. Final output is
        at ``self.output_path``.

        Returns:
            Path to the final MP4 file, or None on failure.
        """
        with self._lock:
            if self.state not in (RecordingState.RECORDING,):
                print(
                    "[CapTure] No active recording to stop.",
                    file=__import__("sys").stderr,
                )
                return None
            self.state = RecordingState.STOPPING

        video_path = ""
        audio_path = ""

        try:
            # 1. Stop screen capture.
            self._notify_status("Stopping screen capture...")
            self._screen.stop()

            # 2. Stop audio capture.
            self._notify_status("Stopping audio capture...")
            self._audio.stop()

            # 3. Stop video encoder, get its output path.
            self._notify_status("Finalizing video encoder...")
            self._video_enc.stop()
            video_path = self._video_enc.output_path

            # 4. Stop audio encoder, get its output path.
            self._notify_status("Finalizing audio encoder...")
            self._audio_enc.stop()
            audio_path = self._audio_enc.output_path

            # 5. Mux.
            self._notify_status("Muxing video + audio...")
            with self._lock:
                self.state = RecordingState.MUXING

            output_path = self.config.get_output_path()
            self._muxer.mux(video_path, audio_path, output_path)
            self.output_path = output_path

            # 6. Calculate duration.
            self.duration = time.perf_counter() - self._start_time

            # 7. Cleanup temp files.
            self._muxer.cleanup()

            with self._lock:
                self.state = RecordingState.IDLE

            self._notify_status(
                f"Saved to: {self.output_path} "
                f"({self._format_duration(self.duration)})"
            )
            return self.output_path

        except CapTureError as exc:
            self._notify_error(exc)
            self._try_cleanup_on_failure(video_path, audio_path)
            return None
        except Exception as exc:
            wrapped = CapTureError(
                f"Unexpected error during stop: {exc}"
            )
            self._notify_error(wrapped)
            self._try_cleanup_on_failure(video_path, audio_path)
            return None

    @property
    def is_recording(self) -> bool:
        """True if currently recording (not muxing, not idle)."""
        return self.state == RecordingState.RECORDING

    def get_status(self) -> dict[str, object]:
        """Return live recording statistics for the UI.

        Returns:
            Dict with keys: ``fps``, ``frame_count``, ``audio_samples``,
            ``elapsed`` (seconds), ``state`` (RecordingState name).
        """
        with self._lock:
            current_state = self.state

        if current_state not in (RecordingState.RECORDING,):
            return {
                "fps": 0.0,
                "frame_count": 0,
                "audio_samples": 0,
                "elapsed": 0.0,
                "state": current_state.name,
            }

        elapsed = time.perf_counter() - self._start_time if self._start_time > 0 else 0.0

        return {
            "fps": self._video_enc.real_fps,
            "frame_count": self._video_enc.frame_count,
            "audio_samples": self._audio_enc.total_samples,
            "elapsed": elapsed,
            "state": current_state.name,
        }

    # ── Frame / Audio routing callbacks ─────────────────────────────

    def _on_frame_received(self, frame: np.ndarray) -> None:
        """Route captured frames to the video encoder.

        Args:
            frame: BGR numpy array from ScreenCapture.
        """
        try:
            self._video_enc.enqueue_frame(frame)
        except Exception:
            # Silently drop — the encoder queue will handle backpressure.
            pass

    def _on_audio_received(self, chunk: np.ndarray, source: str) -> None:
        """Route captured audio chunks to the audio encoder.

        Args:
            chunk: numpy int16 array from AudioCapture.
            source: "system" or "mic" (ignored — both go to the same encoder).
        """
        try:
            self._audio_enc.enqueue_audio(chunk)
        except Exception:
            pass

    # ── Internal helpers ────────────────────────────────────────────

    def _notify_status(self, message: str) -> None:
        """Send a status message to the UI callback (thread-safe)."""
        cb = self._status_callback
        if cb is not None:
            try:
                cb(message)
            except Exception:
                pass

    def _notify_error(self, exc: Exception) -> None:
        """Send an error to the UI callback, print it, and set ERROR state.

        Args:
            exc: The exception to surface.
        """
        with self._lock:
            self.state = RecordingState.ERROR
        show_error(exc)
        cb = self._error_callback
        if cb is not None:
            try:
                cb(exc)
            except Exception:
                pass

    def _set_error(self) -> None:
        """Set state to ERROR without notifying (used for pre-start failures)."""
        with self._lock:
            self.state = RecordingState.ERROR

    def _emergency_stop(self) -> None:
        """Stop all subsystems as gracefully as possible after a failure.

        Tries each component independently — a failure in one must
        not prevent stopping the others.
        """
        components: list[tuple[str, object]] = [
            ("screen capture", self._screen),
            ("audio capture", self._audio),
            ("video encoder", self._video_enc),
            ("audio encoder", self._audio_enc),
        ]
        for name, comp in components:
            try:
                if hasattr(comp, "stop") and callable(comp.stop):  # type: ignore[union-attr]
                    comp.stop()  # type: ignore[union-attr]
            except Exception:
                pass

        with self._lock:
            if self.state != RecordingState.ERROR:
                self.state = RecordingState.IDLE

    def _try_cleanup_on_failure(
        self, video_path: str, audio_path: str
    ) -> None:
        """Best-effort cleanup after a muxing failure.

        Args:
            video_path: Path to the video temp file.
            audio_path: Path to the audio temp file.
        """
        # Try to copy the video as a fallback output.
        import shutil
        import os as _os

        try:
            output_path = self.config.get_output_path()
            if video_path and _os.path.isfile(video_path):
                shutil.copy2(video_path, output_path)
                self.output_path = output_path
                self._notify_status(
                    f"Video saved without audio to: {output_path}"
                )
        except Exception:
            pass

        # Clean up temp files if possible.
        try:
            self._muxer.cleanup()
        except Exception:
            pass

        with self._lock:
            self.state = RecordingState.IDLE

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format a duration as HH:MM:SS."""
        total = int(seconds)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
