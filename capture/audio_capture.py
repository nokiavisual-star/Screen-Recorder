"""
Audio capture module using PyAudio.

Supports two simultaneous streams:
- System audio (WASAPI loopback — captures what you hear)
- Microphone input

Each stream runs in its own daemon thread. Audio chunks are delivered
to a registered callback as numpy int16 arrays tagged with the source
label ("system" or "mic").
"""

from __future__ import annotations

import threading
from typing import Callable

import numpy as np

from capture.config import Config
from capture.errors import AudioError


class AudioCapture:
    """Captures system audio and/or microphone via PyAudio.

    Runs separate daemon threads for system audio (WASAPI loopback)
    and mic streams. Both produce 16-bit stereo PCM at the configured
    sample rate.  Stop signalling uses ``threading.Event`` for clean,
    deadlock-free teardown.

    Attributes:
        config: Application configuration.
    """

    # PyAudio format constant for 16-bit signed int.
    FORMAT: int = 8  # pyaudio.paInt16
    CHUNK_SIZE: int = 1024  # samples per buffer

    def __init__(self, config: Config) -> None:
        """Initialize audio capture.

        Args:
            config: CapTure Config instance with audio settings.

        Raises:
            AudioError: If PyAudio is not installed.
        """
        self.config: Config = config

        # Internal state
        self._running: bool = False
        self._stop_event: threading.Event = threading.Event()

        # PyAudio instance (lazily created in start())
        self._pyaudio: object | None = None  # pyaudio.PyAudio

        # Streams
        self._system_stream: object | None = None  # pyaudio.Stream
        self._mic_stream: object | None = None      # pyaudio.Stream

        # Threads
        self._thread_system: threading.Thread | None = None
        self._thread_mic: threading.Thread | None = None

        # Callback — called as callback(chunk: np.ndarray, source: str)
        self._audio_callback: Callable[[np.ndarray, str], None] | None = None

        # Validate PyAudio availability early.
        try:
            import pyaudio  # noqa: F401
        except ImportError as exc:
            raise AudioError(
                "PyAudio is not installed. Please run: pip install pyaudio"
            ) from exc

    # ── Public API ──────────────────────────────────────────────────

    def on_audio(
        self, callback: Callable[[np.ndarray, str], None]
    ) -> None:
        """Register a callback to receive audio chunks.

        The callback is invoked from the capture threads with every
        audio chunk. Only one callback is supported; calling this
        again replaces the previous one.

        Args:
            callback: Called as ``callback(chunk, source)`` where
                      *chunk* is a numpy int16 array with shape
                      ``(samples, channels)`` (stereo) and *source*
                      is ``"system"`` or ``"mic"``.
        """
        self._audio_callback = callback

    def start(self) -> None:
        """Start audio capture streams.

        Opens WASAPI loopback for system audio (if
        ``config.enable_system_audio`` is True) and the default
        microphone (if ``config.enable_mic`` is True). Each stream
        runs in its own daemon thread.

        Raises:
            AudioError: If capture is already running or PyAudio
                        cannot be initialised.
        """
        if self._running:
            raise AudioError("Audio capture is already running.")

        import pyaudio

        try:
            self._pyaudio = pyaudio.PyAudio()
        except Exception as exc:
            raise AudioError(
                "Failed to initialize PyAudio. "
                "Make sure your audio drivers are installed."
            ) from exc

        self._stop_event.clear()
        self._running = True

        sample_rate = self.config.audio_sample_rate
        channels = self.config.audio_channels
        fmt = self.FORMAT
        chunk = self.CHUNK_SIZE

        # ── System audio (WASAPI loopback) ──────────────────────
        if self.config.enable_system_audio:
            sys_dev_idx = self._find_system_audio_device()
            if sys_dev_idx is not None:
                try:
                    self._system_stream = self._pyaudio.open(
                        format=fmt,
                        channels=channels,
                        rate=sample_rate,
                        input=True,
                        input_device_index=sys_dev_idx,
                        frames_per_buffer=chunk,
                    )
                    self._thread_system = threading.Thread(
                        target=self._capture_system_audio,
                        name="cap-audio-system",
                        daemon=True,
                    )
                    self._thread_system.start()
                except Exception:
                    # System audio is non-critical; continue without it.
                    self._system_stream = None
            else:
                # No loopback device found — not an error on many systems.
                self._system_stream = None

        # ── Microphone ──────────────────────────────────────────
        if self.config.enable_mic:
            mic_dev_idx = self._find_mic_device()
            if mic_dev_idx is not None:
                try:
                    # Open mic in mono — we'll upmix to stereo in the
                    # capture thread so both streams deliver identical
                    # channel layouts to the encoder.
                    self._mic_stream = self._pyaudio.open(
                        format=fmt,
                        channels=1,          # capture mono
                        rate=sample_rate,
                        input=True,
                        input_device_index=mic_dev_idx,
                        frames_per_buffer=chunk,
                    )
                    self._thread_mic = threading.Thread(
                        target=self._capture_mic,
                        name="cap-audio-mic",
                        daemon=True,
                    )
                    self._thread_mic.start()
                except Exception:
                    # Mic is non-critical; continue without it.
                    self._mic_stream = None
            else:
                self._mic_stream = None

    def stop(self) -> None:
        """Stop all audio streams gracefully and release PyAudio.

        Signals all capture threads to exit, joins them (with a
        3-second timeout each), closes the streams, and terminates
        the PyAudio instance.
        """
        self._running = False
        self._stop_event.set()

        # Join system audio thread.
        if self._thread_system is not None and self._thread_system.is_alive():
            self._thread_system.join(timeout=3.0)
            self._thread_system = None

        # Join mic thread.
        if self._thread_mic is not None and self._thread_mic.is_alive():
            self._thread_mic.join(timeout=3.0)
            self._thread_mic = None

        # Close system stream.
        if self._system_stream is not None:
            try:
                if self._system_stream.is_active():
                    self._system_stream.stop_stream()
                self._system_stream.close()
            except Exception:
                pass
            self._system_stream = None

        # Close mic stream.
        if self._mic_stream is not None:
            try:
                if self._mic_stream.is_active():
                    self._mic_stream.stop_stream()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None

        # Terminate PyAudio.
        if self._pyaudio is not None:
            try:
                self._pyaudio.terminate()
            except Exception:
                pass
            self._pyaudio = None

    def is_running(self) -> bool:
        """Check whether any audio capture thread is still active.

        Returns:
            True if at least one stream is being captured, False
            otherwise.
        """
        if not self._running:
            return False
        sys_alive = (
            self._thread_system is not None
            and self._thread_system.is_alive()
        )
        mic_alive = (
            self._thread_mic is not None
            and self._thread_mic.is_alive()
        )
        return sys_alive or mic_alive

    def get_audio_devices(self) -> list[dict[str, object]]:
        """Enumerate all available audio input devices.

        Returns:
            List of dicts with keys: ``index``, ``name``,
            ``max_input_channels``, ``max_output_channels``,
            ``default_sample_rate``, ``host_api``, ``is_loopback``.
            Returns an empty list if PyAudio is not initialised.
        """
        if self._pyaudio is None:
            import pyaudio

            try:
                pa = pyaudio.PyAudio()
            except Exception:
                return []
        else:
            pa = self._pyaudio

        devices: list[dict[str, object]] = []
        try:
            num_devices = pa.get_device_count()
            for i in range(num_devices):
                try:
                    info = pa.get_device_info_by_index(i)
                except Exception:
                    continue

                name: str = info.get("name", "")
                devices.append({
                    "index": i,
                    "name": name,
                    "max_input_channels": info.get("maxInputChannels", 0),
                    "max_output_channels": info.get("maxOutputChannels", 0),
                    "default_sample_rate": info.get("defaultSampleRate", 44100),
                    "host_api": info.get("hostApi", 0),
                    "is_loopback": "loopback" in name.lower(),
                })
        finally:
            if self._pyaudio is None:
                try:
                    pa.terminate()
                except Exception:
                    pass

        return devices

    # ── Capture threads ────────────────────────────────────────────

    def _capture_system_audio(self) -> None:
        """Background daemon thread: read system audio from loopback stream.

        Reads chunks from ``self._system_stream``, converts raw bytes
        to a numpy int16 array of shape ``(CHUNK_SIZE, channels)``, and
        forwards it to the registered callback with source label
        ``"system"``.

        Exits when ``_stop_event`` is set or ``_running`` becomes False.
        """
        stream = self._system_stream
        if stream is None:
            return

        channels = self.config.audio_channels
        chunk_size = self.CHUNK_SIZE

        try:
            while not self._stop_event.is_set() and self._running:
                try:
                    raw_data: bytes = stream.read(
                        chunk_size, exception_on_overflow=False
                    )
                except Exception:
                    # Overflow or transient error — skip this chunk.
                    continue

                if not raw_data:
                    continue

                callback = self._audio_callback
                if callback is not None:
                    try:
                        arr = np.frombuffer(raw_data, dtype=np.int16)
                        arr = arr.reshape(-1, channels)
                        callback(arr, "system")
                    except Exception:
                        # Swallow callback errors — they must not
                        # crash the capture thread.
                        pass
        finally:
            pass  # stream is closed by stop()

    def _capture_mic(self) -> None:
        """Background daemon thread: read microphone audio.

        Reads chunks from ``self._mic_stream`` (mono), converts raw
        bytes to a numpy int16 array, upmixes mono → stereo by
        duplicating the channel, and forwards to the registered
        callback with source label ``"mic"``.

        Exits when ``_stop_event`` is set or ``_running`` becomes False.
        """
        stream = self._mic_stream
        if stream is None:
            return

        chunk_size = self.CHUNK_SIZE
        channels = self.config.audio_channels  # target channel count (2)

        try:
            while not self._stop_event.is_set() and self._running:
                try:
                    raw_data: bytes = stream.read(
                        chunk_size, exception_on_overflow=False
                    )
                except Exception:
                    continue

                if not raw_data:
                    continue

                callback = self._audio_callback
                if callback is not None:
                    try:
                        # Mic is opened in mono → shape (chunk_size, 1).
                        arr = np.frombuffer(raw_data, dtype=np.int16)
                        arr = arr.reshape(-1, 1)

                        # Upmix mono → stereo by duplicating the column.
                        if channels == 2:
                            arr = np.tile(arr, (1, 2))

                        callback(arr, "mic")
                    except Exception:
                        pass
        finally:
            pass  # stream is closed by stop()

    # ── Device detection helpers ───────────────────────────────────

    def _find_system_audio_device(self) -> int | None:
        """Locate the WASAPI loopback device for system audio capture.

        Searches for a device whose name contains "loopback"
        (case-insensitive) and that has input channels. Falls back to
        any WASAPI-hosted input device if no explicit loopback device
        is found.

        Returns:
            Device index or None if no suitable device was found.
        """
        if self._pyaudio is None:
            return None

        try:
            num_devices: int = self._pyaudio.get_device_count()
        except Exception:
            return None

        # First pass: look for an explicit loopback device.
        for i in range(num_devices):
            try:
                info = self._pyaudio.get_device_info_by_index(i)
            except Exception:
                continue
            name: str = info.get("name", "")
            max_in: int = info.get("maxInputChannels", 0)
            if max_in > 0 and "loopback" in name.lower():
                return i

        # Second pass: any WASAPI input device as a fallback.
        # WASAPI host API index is typically 2 or 3 on Windows,
        # but we search by name to be safe.
        for i in range(num_devices):
            try:
                info = self._pyaudio.get_device_info_by_index(i)
            except Exception:
                continue
            name = info.get("name", "")
            max_in = info.get("maxInputChannels", 0)
            host_api_idx = info.get("hostApi", -1)
            if max_in > 0 and host_api_idx >= 0:
                try:
                    host_info = self._pyaudio.get_host_api_info_by_index(
                        host_api_idx
                    )
                    host_name: str = host_info.get("name", "")
                    if "wasapi" in host_name.lower():
                        return i
                except Exception:
                    continue

        return None

    def _find_mic_device(self) -> int | None:
        """Locate a suitable microphone device.

        Prefers a device with "microphone" or "mic" in its name
        (case-insensitive) that has input channels. Falls back to the
        system default input device.

        Returns:
            Device index or None if no microphone was found.
        """
        if self._pyaudio is None:
            return None

        try:
            num_devices: int = self._pyaudio.get_device_count()
        except Exception:
            return None

        # First pass: explicit microphone device.
        for i in range(num_devices):
            try:
                info = self._pyaudio.get_device_info_by_index(i)
            except Exception:
                continue
            name: str = info.get("name", "")
            max_in: int = info.get("maxInputChannels", 0)
            if max_in > 0:
                lower = name.lower()
                if "microphone" in lower or "mic" in lower:
                    # Exclude loopback devices.
                    if "loopback" not in lower:
                        return i

        # Second pass: use the system default input device.
        try:
            default_idx: int = self._pyaudio.get_default_input_device_info().get(
                "index", -1
            )
            if default_idx >= 0:
                return default_idx
        except Exception:
            pass

        return None
