"""
Video encoder using OpenCV's cv2.VideoWriter with MSMF backend.

On Windows, specifying the 'avc1' FourCC code triggers the Media
Foundation (MSMF) backend in OpenCV, producing H.264-encoded video.
No FFmpeg required — this is pure Windows Media Foundation via OpenCV.

Frames are received from ScreenCapture as numpy BGR arrays and
written directly to the VideoWriter in a background daemon thread.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Tuple

import numpy as np

from capture.config import Config
from capture.errors import EncodeError


class VideoEncoder:
    """Encodes video frames to H.264 using cv2.VideoWriter (MSMF backend).

    Runs in its own daemon thread to decouple capture from encoding.
    Frames are read from a thread-safe queue and written to the
    intermediate video file.  The VideoWriter is opened lazily when the
    first frame arrives so the encoder can determine the true resolution.

    Attributes:
        config: Application configuration.
        output_path: Path to the intermediate .mp4 file (no audio).
        frame_size: (width, height) of encoded frames, set on first frame.
        is_encoding: True while the encoder thread is accepting frames.
        frame_count: Total frames written to disk.
        real_fps: Rolling-average effective encoding FPS.
    """

    # Sentinel value pushed to the queue to signal the encode loop to exit.
    _SENTINEL: object = object()

    # Maximum queue depth — frames beyond this are dropped (backpressure).
    _MAX_QUEUE_SIZE: int = 300

    def __init__(self, config: Config) -> None:
        """Initialize the video encoder.

        Args:
            config: CapTure Config instance.

        Raises:
            EncodeError: If OpenCV (cv2) cannot be imported.
        """
        self.config: Config = config
        self._output_path: str = ""
        self.frame_size: Tuple[int, int] = (0, 0)
        self.is_encoding: bool = False

        # cv2 module reference (imported here for clarity).
        try:
            import cv2

            self._cv2 = cv2
        except ImportError as exc:
            raise EncodeError(
                "OpenCV (cv2) is not installed. "
                "Please run: pip install opencv-python-headless"
            ) from exc

        self._writer: object | None = None          # cv2.VideoWriter
        self._thread: threading.Thread | None = None
        self._frame_queue: queue.Queue = queue.Queue(
            maxsize=self._MAX_QUEUE_SIZE,
        )
        self._lock: threading.Lock = threading.Lock()
        self._frame_count: int = 0
        self._real_fps: float = 0.0
        self._fps_times: list[float] = []

    # ── Public API ──────────────────────────────────────────────────

    def start(self, temp_dir: str) -> str:
        """Start the encode thread and prepare the intermediate video file.

        The actual ``cv2.VideoWriter`` is opened lazily when the first
        frame is enqueued so the resolution can be determined from the
        frame data rather than guessed.

        Args:
            temp_dir: Directory for temporary files.

        Returns:
            Path to the intermediate video file being written.

        Raises:
            EncodeError: If the temp directory cannot be created.
        """
        try:
            os.makedirs(temp_dir, exist_ok=True)
        except OSError as exc:
            raise EncodeError(
                f"Cannot create temp directory '{temp_dir}': {exc}"
            ) from exc

        self._output_path = os.path.join(temp_dir, "video_temp.mp4")

        # Reset state for a new recording session.
        self._frame_queue = queue.Queue(maxsize=self._MAX_QUEUE_SIZE)
        self._frame_count = 0
        self._real_fps = 0.0
        self._fps_times.clear()
        self.is_encoding = True

        self._thread = threading.Thread(
            target=self._encode_loop,
            name="enc-video",
            daemon=True,
        )
        self._thread.start()

        return self._output_path

    def stop(self) -> None:
        """Stop the encode thread and release the VideoWriter.

        Signals the encode loop to exit, waits for the queue to drain
        (with a 10-second timeout), releases the ``cv2.VideoWriter``,
        and finalizes the output file.
        """
        if not self.is_encoding:
            return

        self.is_encoding = False

        # Push sentinel to wake the thread if it is blocked on get().
        try:
            self._frame_queue.put(self._SENTINEL, timeout=1.0)
        except queue.Full:
            pass

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10.0)
            self._thread = None

        self._release_writer()

    def enqueue_frame(self, frame: np.ndarray) -> None:
        """Add a frame to the encoding queue (non-blocking).

        Called by the ``ScreenCapture`` callback from the capture
        thread.  If the queue is full the frame is silently dropped
        (backpressure to avoid unbounded memory growth).

        Args:
            frame: BGR numpy array from dxcam via ScreenCapture.
        """
        if not self.is_encoding:
            return

        try:
            self._frame_queue.put(frame, timeout=0.02)
        except queue.Full:
            # Drop frame — queue is saturated, encoder can't keep up.
            pass

    # ── Properties ──────────────────────────────────────────────────

    @property
    def output_path(self) -> str:
        """Path to the intermediate video file (no audio)."""
        return self._output_path

    @property
    def frame_count(self) -> int:
        """Total number of frames encoded so far."""
        return self._frame_count

    @property
    def real_fps(self) -> float:
        """Actual encoding FPS (rolling average over last ~30 frames)."""
        return self._real_fps

    # ── Internals ───────────────────────────────────────────────────

    def _encode_loop(self) -> None:
        """Background daemon thread: dequeue frames and write to VideoWriter.

        On the first frame the ``cv2.VideoWriter`` is created with the
        frame's native resolution.  The loop drains the queue, writing
        each frame to disk.  It tracks both frame count and a rolling
        FPS estimate.

        The loop exits when ``is_encoding`` is False **and** the queue
        is empty, or when the sentinel value (``None``) is received.
        """
        while self.is_encoding or not self._frame_queue.empty():
            try:
                frame = self._frame_queue.get(timeout=0.5)
            except queue.Empty:
                # Timeout — re-check the loop condition.
                continue

            # Sentinel received → exit cleanly.
            if frame is self._SENTINEL or frame is None:
                break

            # First frame: determine resolution and open VideoWriter.
            if self._writer is None:
                try:
                    self._open_writer(frame)
                except EncodeError:
                    # Already wrapped; just re-raise so the thread dies
                    # and the controller sees the error via stop().
                    raise

            # Write the frame.
            with self._lock:
                if self._writer is not None:
                    try:
                        self._writer.write(frame)
                    except Exception as exc:
                        raise EncodeError(
                            f"cv2.VideoWriter.write() failed: {exc}"
                        ) from exc

            self._frame_count += 1
            self._update_fps()

        # Final cleanup.
        self._release_writer()

    def _open_writer(self, frame: np.ndarray) -> None:
        """Create the ``cv2.VideoWriter`` using the frame's dimensions.

        Tries MSMF backend first (``cv2.CAP_MSMF``), then falls back to
        OpenCV's default backend selection.

        Args:
            frame: First BGR numpy array — used to read (h, w).

        Raises:
            EncodeError: If the VideoWriter cannot be opened with any
                         backend / codec combination.
        """
        h, w = frame.shape[:2]
        self.frame_size = (w, h)
        fourcc_str = self.config.video_codec  # e.g. 'avc1'

        fourcc = self._cv2.VideoWriter_fourcc(*fourcc_str)

        # --- Attempt 1: explicit MSMF backend ---
        try:
            self._writer = self._cv2.VideoWriter(
                self._output_path,
                self._cv2.CAP_MSMF,
                fourcc,
                self.config.fps,
                (w, h),
            )
            if self._writer.isOpened():
                return
        except Exception:
            self._writer = None

        # --- Attempt 2: default backend (OpenCV picks best available) ---
        try:
            self._writer = self._cv2.VideoWriter(
                self._output_path,
                fourcc,
                self.config.fps,
                (w, h),
            )
            if self._writer.isOpened():
                return
        except Exception as exc:
            raise EncodeError(
                f"Failed to open VideoWriter with codec '{fourcc_str}' "
                f"at {w}x{h} {self.config.fps}fps. "
                "Make sure OpenCV was built with MSMF support and the "
                "H.264 encoder is available on this system."
            ) from exc

        raise EncodeError(
            f"Cannot open VideoWriter with codec '{fourcc_str}'. "
            "The MSMF H.264 encoder may not be available. "
            "Try installing Windows Media Feature Pack or "
            "a newer OpenCV build."
        )

    def _release_writer(self) -> None:
        """Release the ``cv2.VideoWriter``, swallowing any errors."""
        with self._lock:
            if self._writer is not None:
                try:
                    self._writer.release()
                except Exception:
                    pass
                self._writer = None

    def _update_fps(self) -> None:
        """Update the rolling-average FPS counter."""
        now = time.perf_counter()
        self._fps_times.append(now)
        # Keep a sliding window of ~30 timestamps.
        if len(self._fps_times) > 30:
            self._fps_times.pop(0)
        if len(self._fps_times) >= 2:
            elapsed = self._fps_times[-1] - self._fps_times[0]
            if elapsed > 0.0:
                self._real_fps = (len(self._fps_times) - 1) / elapsed
