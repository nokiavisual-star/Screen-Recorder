"""
Screen capture module using dxcam (DirectX-based, zero-copy).

dxcam uses the Desktop Duplication API (DirectX) for fast,
GPU-accelerated screen capture with minimal CPU overhead.

Target: 30fps @ 1080p on Intel HD Graphics 520.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

import numpy as np

from capture.config import Config
from capture.errors import CaptureError


class ScreenCapture:
    """Captures screen frames at a target FPS using dxcam.

    Runs in a background daemon thread. Frames are pushed to a
    registered callback as numpy arrays (BGR, as expected by OpenCV).
    Uses ``threading.Event`` for graceful stop signalling.

    Attributes:
        config: Application configuration.
        current_fps: Rolling-average effective FPS (updated in real time).
    """

    def __init__(self, config: Config) -> None:
        """Initialize the screen capturer.

        Args:
            config: CapTure Config instance with FPS, region, etc.

        Raises:
            CaptureError: If dxcam fails to initialize (no DXGI support).
        """
        self.config: Config = config
        self.current_fps: float = 0.0

        # Internal state
        self._running: bool = False
        self._stop_event: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None
        self._camera: object | None = None  # dxcam.DXCamera
        self._frame_callback: Callable[[np.ndarray], None] | None = None
        self._fps_window: deque[float] = deque(maxlen=30)
        self._lock: threading.Lock = threading.Lock()

        # Validate dxcam availability early (best-effort; will retry in start).
        try:
            import dxcam  # noqa: F401
        except ImportError as exc:
            raise CaptureError(
                "dxcam is not installed. Please run: pip install dxcam"
            ) from exc

    # ── Public API ──────────────────────────────────────────────────

    def on_frame(self, callback: Callable[[np.ndarray], None]) -> None:
        """Register a callback to receive captured frames.

        The callback is invoked from the capture thread with each new
        frame as a numpy BGR array. Only one callback is supported;
        calling this again replaces the previous one.

        Args:
            callback: Called with each captured frame (numpy BGR array).
        """
        self._frame_callback = callback

    def start(self) -> None:
        """Start capturing frames in a background daemon thread.

        Initializes the dxcam DXCamera, clears the stop event, and
        launches the capture loop.

        Raises:
            CaptureError: If capture is already running or dxcam fails
                          to initialise (no DXGI / display found).
        """
        if self._running:
            raise CaptureError("Screen capture is already running.")

        import dxcam

        try:
            # Create camera for the primary monitor (output_idx=0) with BGR
            # colour space so OpenCV can consume frames directly.
            self._camera = dxcam.create(output_color="BGR", output_idx=0)
        except Exception as exc:
            raise CaptureError(
                "Failed to initialize screen capture. "
                "Make sure your GPU supports DirectX 11 and "
                "Desktop Duplication API is available."
            ) from exc

        self._stop_event.clear()
        self._running = True
        self._fps_window.clear()
        self.current_fps = 0.0

        self._thread = threading.Thread(
            target=self._capture_loop,
            name="cap-screen",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the capture loop gracefully and release resources.

        Signals the capture thread to stop, joins it (with a 5-second
        timeout), and releases the dxcam camera.
        """
        self._running = False
        self._stop_event.set()

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            self._thread = None

        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception:
                pass
            self._camera = None

    def is_running(self) -> bool:
        """Check whether the capture loop is currently active.

        Returns:
            True if frames are being captured, False otherwise.
        """
        return self._running and self._thread is not None and self._thread.is_alive()

    def get_current_fps(self) -> float:
        """Return the effective FPS over the last second.

        Computed as a rolling average over the most recent frame
        timestamps (up to 30 samples).

        Returns:
            Rolling-average FPS. 0.0 if not enough samples yet.
        """
        return self.current_fps

    # ── Internal ────────────────────────────────────────────────────

    def _capture_loop(self) -> None:
        """Main capture loop running in a background daemon thread.

        Starts the dxcam camera, then loops reading frames via
        ``get_latest_frame()`` (non-blocking). Each valid frame is
        forwarded to the registered callback.  Effective FPS is tracked
        via a rolling window of timestamps.

        The loop exits cleanly when ``_stop_event`` is set or
        ``_running`` becomes False.
        """
        try:
            # Configure and start the dxcam camera.
            region: tuple[int, int, int, int] | None = self.config.screen_region
            if region is not None:
                self._camera.start(region=region, target_fps=self.config.fps)
            else:
                self._camera.start(target_fps=self.config.fps)
        except Exception as exc:
            # Wrap dxcam-internal errors so the caller sees CaptureError.
            raise CaptureError(
                f"Failed to start the screen capture stream: {exc}"
            ) from exc

        try:
            while not self._stop_event.is_set() and self._running:
                try:
                    frame = self._camera.get_latest_frame()
                except Exception:
                    # Transient dxcam error — skip this iteration.
                    time.sleep(0.001)
                    continue

                if frame is None:
                    # No new frame available yet; tiny sleep to avoid
                    # busy-waiting at 100 % CPU.
                    time.sleep(0.001)
                    continue

                # Track effective FPS.
                now = time.perf_counter()
                self._fps_window.append(now)
                if len(self._fps_window) >= 2:
                    elapsed = self._fps_window[-1] - self._fps_window[0]
                    if elapsed > 0.0:
                        self.current_fps = (len(self._fps_window) - 1) / elapsed

                # Dispatch frame to the registered callback.
                callback = self._frame_callback  # local copy for thread-safety
                if callback is not None:
                    try:
                        callback(frame)
                    except Exception:
                        # Silently swallow callback errors — they must
                        # not crash the capture loop.
                        pass
        finally:
            # Always release the camera, even on unexpected errors.
            try:
                if self._camera is not None:
                    self._camera.stop()
            except Exception:
                pass
