"""
Audio encoder: WAV intermediate + AAC encoding via Media Foundation.

Strategy (two-phase):
1. During recording: write raw PCM audio to a temporary WAV file
   using Python's ``wave`` module. Fast, lossless, near-zero CPU.
2. After recording stops: transcode WAV to AAC using Windows Media
   Foundation COM APIs through ``ctypes``.  The AAC stream is then
   muxed with video by the Muxer module.

Why WAV first: PyAudio gives us raw PCM — dumping to WAV costs
almost nothing while recording.  Transcoding to AAC happens after
recording stops so it doesn't compete with capture resources.
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
import wave
from ctypes import (
    POINTER,
    byref,
    c_uint8,
    c_uint32,
    c_void_p,
)
from ctypes.wintypes import BOOL, DWORD, LARGE_INTEGER, UINT32

# HRESULT is typedef LONG (32-bit signed); used only as restype for COM/
# Media Foundation functions; PyInstaller's bundled ctypes.wintypes omits it
# in frozen builds.
HRESULT = ctypes.c_long

import numpy as np

from capture.config import Config
from capture.errors import AudioError


# ═══════════════════════════════════════════════════════════════════════
# Media Foundation GUIDs and constants
# ═══════════════════════════════════════════════════════════════════════

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", c_uint8 * 8),
    ]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _GUID):
            return NotImplemented
        return bool(
            self.Data1 == other.Data1
            and self.Data2 == other.Data2
            and self.Data3 == other.Data3
            and bytes(self.Data4) == bytes(other.Data4)
        )


def _guid(data1: int, data2: int, data3: int, d4: str) -> _GUID:
    """Build a _GUID from human-readable components."""
    parts = [int(x, 16) for x in d4.replace("-", "").replace(" ", "")]
    return _GUID(data1, data2, data3, (c_uint8 * 8)(*parts))


# Media type major types
MFMediaType_Audio = _guid(0x73646961, 0x0000, 0x0010,
                          "80 00 00 AA 00 38 9B 71")
MFMediaType_Video = _guid(0x73646976, 0x0000, 0x0010,
                          "80 00 00 AA 00 38 9B 71")

# Audio subtypes
MFAudioFormat_PCM = _guid(0x00000001, 0x0000, 0x0010,
                          "80 00 00 AA 00 38 9B 71")
MFAudioFormat_AAC = _guid(0x00001610, 0x0000, 0x0010,
                          "80 00 00 AA 00 38 9B 71")

# Attribute keys (MF_MT_*)
MF_MT_MAJOR_TYPE = _guid(0x48EBA18E, 0xF8C9, 0x4687,
                         "BF 11 0A 74 C9 F9 6A 8F")
MF_MT_SUBTYPE = _guid(0xF7E34C9A, 0x42E8, 0x4714,
                      "B7 4B CB 29 D7 2C 35 E5")
MF_MT_AUDIO_NUM_CHANNELS = _guid(0x37E48BF5, 0x645E, 0x4C5B,
                                 "89 DE AD A9 E2 9B 69 6A")
MF_MT_AUDIO_SAMPLES_PER_SECOND = _guid(0x5FAEEAE7, 0x0290, 0x4C31,
                                       "9E 8A C5 34 F6 8D 9D BA")
MF_MT_AUDIO_BLOCK_ALIGNMENT = _guid(0x322DE230, 0x9EEB, 0x43BD,
                                    "AB 7A FF 41 22 51 54 1D")
MF_MT_AUDIO_AVG_BYTES_PER_SECOND = _guid(0x1AAB75C8, 0xCFEF, 0x451C,
                                         "AB 95 AC 03 4B 8E 17 31")
MF_MT_AUDIO_BITS_PER_SAMPLE = _guid(0xF2DEB57F, 0x40FA, 0x4764,
                                    "AA 33 ED 4F 2D 1F F6 69")
MF_MT_AAC_AUDIO_PROFILE_LEVEL_INDICATION = _guid(
    0x7632F0E6, 0x9536, 0x4DE9,
    "AC B0 73 7E 6C BA 55 96",
)

# Sink Writer flags
MF_SINK_WRITER_DISABLE_THROTTLING = 0x00000001

# MF Startup
MF_VERSION = 0x00020070  # 2.7


# ═══════════════════════════════════════════════════════════════════════
# COM interface helpers (lightweight — only the methods we call)
# ═══════════════════════════════════════════════════════════════════════

IUnknown_vtable = 3  # QueryInterface, AddRef, Release come first


def _com_call(this: ctypes.c_void_p, vtbl_idx: int, restype,
              *args: object) -> object:
    """Call a COM method at *vtbl_idx* in *this* object's vtable."""
    vtable = ctypes.cast(this, POINTER(POINTER(c_void_p)))
    func = vtable[0][vtbl_idx]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *[type(a)
                               if not isinstance(a, ctypes._SimpleCData)
                               else type(a) for a in args])
    return proto(ctypes.cast(func, ctypes.c_void_p))(this, *args)


# ── IMFMediaType helpers ──────────────────────────────────────────────

def _mf_media_type_set_guid(mt: ctypes.c_void_p, key: _GUID,
                            value: _GUID) -> int:
    """Set a GUID attribute on an IMFMediaType (vtable idx 34)."""
    return _com_call(mt, 34, ctypes.c_long, byref(key), byref(value))


def _mf_media_type_set_uint32(mt: ctypes.c_void_p, key: _GUID,
                              value: int) -> int:
    """Set a UINT32 attribute on an IMFMediaType (vtable idx 40)."""
    return _com_call(mt, 40, ctypes.c_long, byref(key),
                     c_uint32(value))


# ── IMFSinkWriter helpers ─────────────────────────────────────────────

def _mf_sink_writer_add_stream(sw: ctypes.c_void_p,
                               output_type: ctypes.c_void_p,
                               ) -> tuple[int, int]:
    """Add a stream to the Sink Writer (vtable idx 4). Returns (hr, idx)."""
    idx = DWORD(0)
    hr = _com_call(sw, 4, ctypes.c_long, output_type, byref(idx))
    return (hr, idx.value)


def _mf_sink_writer_set_input_media_type(
    sw: ctypes.c_void_p, stream_idx: int, input_type: ctypes.c_void_p,
    encoding_params: ctypes.c_void_p | None = None,
) -> int:
    """Set input media type (vtable idx 6)."""
    return _com_call(sw, 6, ctypes.c_long, DWORD(stream_idx),
                     input_type, encoding_params or c_void_p(0))


def _mf_sink_writer_begin_write(sw: ctypes.c_void_p) -> int:
    """Signal the start of writing (vtable idx 7)."""
    return _com_call(sw, 7, ctypes.c_long)


def _mf_sink_writer_write_sample(
    sw: ctypes.c_void_p, stream_idx: int, sample: ctypes.c_void_p,
) -> int:
    """Write a sample (vtable idx 8)."""
    return _com_call(sw, 8, ctypes.c_long, DWORD(stream_idx), sample)


def _mf_sink_writer_finalize(sw: ctypes.c_void_p) -> int:
    """Finalize the sink writer (vtable idx 22)."""
    return _com_call(sw, 22, ctypes.c_long)


# ── IMFAttributes helpers (vtable shared by IMFMediaType) ─────────────

def _mf_attributes_release(obj: ctypes.c_void_p) -> int:
    """Release a COM object (IUnknown::Release, vtable idx 2)."""
    return _com_call(obj, 2, ctypes.c_ulong)


# ═══════════════════════════════════════════════════════════════════════
# AudioEncoder
# ═══════════════════════════════════════════════════════════════════════


class AudioEncoder:
    """Records PCM audio to WAV during capture, then encodes to AAC.

    Incoming audio chunks (from ``AudioCapture``) are written to a WAV
    file in real-time by a background daemon thread.  After recording
    stops, the WAV is transcoded to AAC via Windows Media Foundation
    COM interfaces.

    Attributes:
        config: Application configuration.
        wav_path: Path to the intermediate WAV file.
        aac_path: Path to the encoded AAC file (set after transcoding).
        is_recording: True while writing audio chunks to WAV.
        total_samples: Cumulative number of audio samples written.
    """

    def __init__(self, config: Config) -> None:
        """Initialize the audio encoder.

        Args:
            config: CapTure Config instance.

        Raises:
            AudioError: If the audio configuration is invalid.
        """
        self.config: Config = config
        self.wav_path: str = ""
        self.aac_path: str = ""
        self.is_recording: bool = False

        self._wave_writer: wave.Wave_write | None = None
        self._chunk_queue: queue.Queue[np.ndarray | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._total_samples: int = 0
        self._lock: threading.Lock = threading.Lock()

    # ── Public API ──────────────────────────────────────────────────

    def start(self, temp_dir: str) -> str:
        """Open WAV file and start the recording thread.

        Args:
            temp_dir: Directory for temporary files.

        Returns:
            Path to the intermediate WAV file being written.

        Raises:
            AudioError: If the WAV file cannot be opened for writing.
        """
        try:
            os.makedirs(temp_dir, exist_ok=True)
        except OSError as exc:
            raise AudioError(
                f"Cannot create temp directory '{temp_dir}': {exc}"
            ) from exc

        self.wav_path = os.path.join(temp_dir, "audio_temp.wav")
        self.aac_path = os.path.join(temp_dir, "audio_temp.aac")
        self._total_samples = 0
        self._chunk_queue = queue.Queue()

        try:
            self._wave_writer = wave.open(self.wav_path, "wb")
            self._wave_writer.setnchannels(self.config.audio_channels)
            self._wave_writer.setsampwidth(
                self.config.audio_bit_depth // 8
            )
            self._wave_writer.setframerate(self.config.audio_sample_rate)
        except (OSError, wave.Error) as exc:
            raise AudioError(
                f"Cannot open WAV file for writing: {self.wav_path}. "
                f"Check disk space and permissions: {exc}"
            ) from exc

        self.is_recording = True

        self._thread = threading.Thread(
            target=self._record_loop,
            name="enc-audio",
            daemon=True,
        )
        self._thread.start()

        return self.wav_path

    def stop(self) -> None:
        """Stop recording, close the WAV file, and transcode to AAC.

        Sends a sentinel to the recording queue, joins the write
        thread, closes the WAV file, and then launches the WAV→AAC
        transcoding via Media Foundation.
        """
        if not self.is_recording:
            return

        self.is_recording = False

        # Wake the record thread.
        try:
            self._chunk_queue.put(None, timeout=1.0)
        except queue.Full:
            pass

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10.0)
            self._thread = None

        # Close WAV.
        with self._lock:
            if self._wave_writer is not None:
                try:
                    self._wave_writer.close()
                except Exception:
                    pass
                self._wave_writer = None

        # Transcode WAV → AAC (if there was any audio).
        if self._total_samples > 0 and os.path.isfile(self.wav_path):
            try:
                self._transcode_wav_to_aac()
            except Exception as exc:
                if isinstance(exc, AudioError):
                    raise
                raise AudioError(
                    f"Failed to encode audio to AAC: {exc}"
                ) from exc

    def enqueue_audio(self, chunk: np.ndarray) -> None:
        """Add an audio chunk to the recording queue.

        Called by ``AudioCapture`` callback from the capture thread.

        Args:
            chunk: numpy int16 array, shape ``(samples, channels)``.
        """
        if not self.is_recording:
            return

        try:
            self._chunk_queue.put(chunk, timeout=0.02)
        except queue.Full:
            # Drop chunk if queue is backed up.
            pass

    # ── Properties ──────────────────────────────────────────────────

    @property
    def output_path(self) -> str:
        """Path to the encoded AAC file (set after ``stop()`` completes)."""
        return self.aac_path

    @property
    def total_samples(self) -> int:
        """Total PCM samples written to WAV."""
        return self._total_samples

    @property
    def duration_seconds(self) -> float:
        """Duration of recorded audio in seconds."""
        if self.config.audio_sample_rate <= 0 or self.config.audio_channels <= 0:
            return 0.0
        return self._total_samples / (
            self.config.audio_sample_rate * self.config.audio_channels
        )

    # ── Recording thread ────────────────────────────────────────────

    def _record_loop(self) -> None:
        """Background daemon thread: dequeue audio chunks and write to WAV.

        Reads numpy arrays from ``_chunk_queue``, converts to raw bytes
        via ``.tobytes()``, and writes to the ``wave`` file.  Exits when
        it receives ``None`` as a sentinel.
        """
        while True:
            try:
                chunk = self._chunk_queue.get(timeout=0.5)
            except queue.Empty:
                if not self.is_recording:
                    break
                continue

            if chunk is None:
                break

            with self._lock:
                if self._wave_writer is None:
                    continue
                try:
                    raw = chunk.tobytes()
                    self._wave_writer.writeframes(raw)
                except Exception:
                    # Swallow transient write errors.
                    pass

            self._total_samples += int(chunk.size)

    # ── AAC transcoding via Media Foundation ────────────────────────

    def _transcode_wav_to_aac(self) -> None:
        """Transcode the recorded WAV file to AAC via Media Foundation.

        Workflow:
        1. Initialise Media Foundation (``MFStartup``).
        2. Create an AAC Sink Writer.
        3. Configure input (PCM) and output (AAC) media types.
        4. Read PCM samples from the WAV and write them to the Sink
           Writer.  Media Foundation encodes to AAC transparently.
        5. Finalize and shut down.

        Raises:
            AudioError: If Media Foundation is unavailable or
                        the AAC encoder is not present.
        """
        # Read PCM data from the WAV.
        pcm_data, sample_rate, num_channels, bits_per_sample = (
            self._read_wav_pcm(self.wav_path)
        )

        if len(pcm_data) == 0:
            raise AudioError("WAV file contains no audio data to encode.")

        # Load MF DLLs.
        try:
            mfplat = ctypes.WinDLL("mfplat.dll")
            mfreadwrite = ctypes.WinDLL("mfreadwrite.dll")
        except OSError as exc:
            raise AudioError(
                "Media Foundation runtime (mfplat.dll / mfreadwrite.dll) "
                "is not available. This may happen on Windows N/KN editions "
                "without the Media Feature Pack. Please install the Media "
                "Feature Pack from Microsoft."
            ) from exc

        # Prototype the MF functions we need.
        mfplat.MFStartup.restype = HRESULT
        mfplat.MFStartup.argtypes = [UINT32, DWORD]
        mfplat.MFShutdown.restype = HRESULT
        mfplat.MFCreateMediaType.restype = HRESULT
        mfplat.MFCreateMediaType.argtypes = [POINTER(c_void_p)]
        mfplat.MFCreateMemoryBuffer.restype = HRESULT
        mfplat.MFCreateMemoryBuffer.argtypes = [DWORD, POINTER(c_void_p)]
        mfplat.MFCreateSample.restype = HRESULT
        mfplat.MFCreateSample.argtypes = [POINTER(c_void_p)]

        mfreadwrite.MFCreateSinkWriterFromURL.restype = HRESULT
        mfreadwrite.MFCreateSinkWriterFromURL.argtypes = [
            ctypes.c_wchar_p, c_void_p, c_void_p, POINTER(c_void_p),
        ]

        # ── Initialise MF ───────────────────────────────────────────
        hr = mfplat.MFStartup(MF_VERSION, 0)
        if hr < 0:
            raise AudioError(
                f"Media Foundation failed to initialize (HRESULT: 0x{hr:08X}). "
                "The AAC encoder may not be available on this system."
            )

        sink_writer = c_void_p(0)
        try:
            # ── Create Sink Writer for AAC output ───────────────────
            hr = mfreadwrite.MFCreateSinkWriterFromURL(
                self.aac_path, c_void_p(0), c_void_p(0), byref(sink_writer),
            )
            if hr < 0 or not sink_writer:
                raise AudioError(
                    f"Cannot create AAC sink writer (HRESULT: 0x{hr:08X})."
                )

            # ── Build output (AAC) media type ───────────────────────
            out_type = c_void_p(0)
            hr = mfplat.MFCreateMediaType(byref(out_type))
            if hr < 0:
                raise AudioError("MFCreateMediaType failed for AAC output.")

            _mf_media_type_set_guid(out_type, MF_MT_MAJOR_TYPE,
                                    MFMediaType_Audio)
            _mf_media_type_set_guid(out_type, MF_MT_SUBTYPE,
                                    MFAudioFormat_AAC)
            _mf_media_type_set_uint32(out_type, MF_MT_AUDIO_SAMPLES_PER_SECOND,
                                      sample_rate)
            _mf_media_type_set_uint32(out_type, MF_MT_AUDIO_NUM_CHANNELS,
                                      num_channels)
            # AAC-LC profile level 2
            _mf_media_type_set_uint32(
                out_type, MF_MT_AAC_AUDIO_PROFILE_LEVEL_INDICATION, 0x29,
            )
            avg_bytes = sample_rate * num_channels * (bits_per_sample // 8)
            _mf_media_type_set_uint32(out_type, MF_MT_AUDIO_AVG_BYTES_PER_SECOND,
                                      avg_bytes)

            # ── Add stream to sink writer ───────────────────────────
            hr, stream_idx = _mf_sink_writer_add_stream(sink_writer,
                                                        out_type)
            if hr < 0:
                raise AudioError(
                    f"Cannot add AAC stream to sink writer "
                    f"(HRESULT: 0x{hr:08X})."
                )

            # ── Build input (PCM) media type ────────────────────────
            in_type = c_void_p(0)
            hr = mfplat.MFCreateMediaType(byref(in_type))
            if hr < 0:
                raise AudioError("MFCreateMediaType failed for PCM input.")

            _mf_media_type_set_guid(in_type, MF_MT_MAJOR_TYPE,
                                    MFMediaType_Audio)
            _mf_media_type_set_guid(in_type, MF_MT_SUBTYPE,
                                    MFAudioFormat_PCM)
            _mf_media_type_set_uint32(in_type, MF_MT_AUDIO_SAMPLES_PER_SECOND,
                                      sample_rate)
            _mf_media_type_set_uint32(in_type, MF_MT_AUDIO_NUM_CHANNELS,
                                      num_channels)
            _mf_media_type_set_uint32(in_type, MF_MT_AUDIO_BITS_PER_SAMPLE,
                                      bits_per_sample)
            block_align = num_channels * (bits_per_sample // 8)
            _mf_media_type_set_uint32(in_type, MF_MT_AUDIO_BLOCK_ALIGNMENT,
                                      block_align)
            _mf_media_type_set_uint32(in_type, MF_MT_AUDIO_AVG_BYTES_PER_SECOND,
                                      avg_bytes)

            hr = _mf_sink_writer_set_input_media_type(
                sink_writer, stream_idx, in_type, None,
            )
            if hr < 0:
                raise AudioError(
                    f"Cannot set PCM input type on sink writer "
                    f"(HRESULT: 0x{hr:08X})."
                )

            # ── Begin writing ───────────────────────────────────────
            hr = _mf_sink_writer_begin_write(sink_writer)
            if hr < 0:
                raise AudioError("Sink writer BeginWrite failed.")

            # ── Write PCM samples in chunks ─────────────────────────
            self._write_pcm_to_sink(
                mfplat, sink_writer, stream_idx,
                pcm_data, sample_rate, num_channels, bits_per_sample,
            )

            # ── Finalize ────────────────────────────────────────────
            hr = _mf_sink_writer_finalize(sink_writer)
            if hr < 0:
                raise AudioError(
                    f"Sink writer Finalize failed (HRESULT: 0x{hr:08X})."
                )

        finally:
            # Release COM objects.
            if sink_writer:
                _mf_attributes_release(sink_writer)
            if 'out_type' in dir() and out_type:
                _mf_attributes_release(out_type)
            if 'in_type' in dir() and in_type:
                _mf_attributes_release(in_type)
            mfplat.MFShutdown()

    # ── PCM → Sink Writer helper ────────────────────────────────────

    def _write_pcm_to_sink(
        self,
        mfplat: ctypes.CDLL,
        sink_writer: ctypes.c_void_p,
        stream_idx: int,
        pcm_data: bytes,
        sample_rate: int,
        num_channels: int,
        bits_per_sample: int,
    ) -> None:
        """Write raw PCM bytes to the MF Sink Writer in ~100 ms chunks.

        Media Foundation encodes to AAC transparently.
        """
        bytes_per_sample = bits_per_sample // 8
        bytes_per_frame = num_channels * bytes_per_sample
        frames_per_chunk = sample_rate // 10  # 100 ms per chunk
        chunk_bytes = frames_per_chunk * bytes_per_frame

        total_bytes = len(pcm_data)
        offset = 0
        timestamp_ns = 0
        ticks_per_sec = 10_000_000  # MF uses 100 ns ticks
        ticks_per_frame = ticks_per_sec // sample_rate

        while offset < total_bytes:
            remaining = total_bytes - offset
            size = min(chunk_bytes, remaining)
            # Align to frame boundary.
            size = (size // bytes_per_frame) * bytes_per_frame
            if size == 0:
                break

            chunk = pcm_data[offset:offset + size]
            num_frames = size // bytes_per_frame

            # Create MF buffer.
            buf = c_void_p(0)
            hr = mfplat.MFCreateMemoryBuffer(size, byref(buf))
            if hr < 0:
                raise AudioError("MFCreateMemoryBuffer failed.")

            # Lock buffer and copy PCM data.
            buf_ptr = ctypes.c_void_p(0)
            max_len = DWORD(0)
            cur_len = DWORD(0)

            # IMFMediaBuffer::Lock  (vtable idx 3)
            lock_hr = _com_call(buf, 3, ctypes.c_long,
                                byref(buf_ptr), byref(max_len),
                                byref(cur_len))
            if lock_hr < 0:
                _mf_attributes_release(buf)
                raise AudioError("Failed to lock MF buffer.")

            ctypes.memmove(buf_ptr, chunk, size)

            # IMFMediaBuffer::SetCurrentLength (vtable idx 5)
            _com_call(buf, 5, ctypes.c_long, DWORD(size))
            # IMFMediaBuffer::Unlock (vtable idx 4)
            _com_call(buf, 4, ctypes.c_long)

            # Create sample and add buffer.
            sample = c_void_p(0)
            hr = mfplat.MFCreateSample(byref(sample))
            if hr < 0:
                _mf_attributes_release(buf)
                raise AudioError("MFCreateSample failed.")

            # IMFSample::AddBuffer (vtable idx 5)
            _com_call(sample, 5, ctypes.c_long, buf)

            # Set sample timestamp.
            # IMFSample::SetSampleTime (vtable idx 3)
            _com_call(sample, 3, ctypes.c_long,
                      LARGE_INTEGER(timestamp_ns))
            # IMFSample::SetSampleDuration (vtable idx 4)
            duration = num_frames * ticks_per_frame
            _com_call(sample, 4, ctypes.c_long,
                      LARGE_INTEGER(duration))

            # Write to sink.
            hr = _mf_sink_writer_write_sample(sink_writer, stream_idx,
                                              sample)
            _mf_attributes_release(buf)
            _mf_attributes_release(sample)

            if hr < 0:
                raise AudioError(
                    f"Sink writer WriteSample failed (HRESULT: 0x{hr:08X})."
                )

            offset += size
            timestamp_ns += duration

    # ── WAV reader ──────────────────────────────────────────────────

    @staticmethod
    def _read_wav_pcm(wav_path: str) -> tuple[bytes, int, int, int]:
        """Read raw PCM data and header info from a WAV file.

        Args:
            wav_path: Path to the WAV file.

        Returns:
            Tuple of ``(pcm_bytes, sample_rate, num_channels, bits_per_sample)``.

        Raises:
            AudioError: If the WAV cannot be read or has an unsupported format.
        """
        try:
            with wave.open(wav_path, "rb") as wf:
                num_channels = wf.getnchannels()
                sample_rate = wf.getframerate()
                bits_per_sample = wf.getsampwidth() * 8
                num_frames = wf.getnframes()
                pcm_data = wf.readframes(num_frames)
        except (OSError, wave.Error) as exc:
            raise AudioError(
                f"Cannot read WAV file '{wav_path}': {exc}"
            ) from exc

        if num_channels not in (1, 2):
            raise AudioError(
                f"Unsupported channel count in WAV: {num_channels}. "
                "Only mono (1) and stereo (2) are supported."
            )
        if bits_per_sample != 16:
            raise AudioError(
                f"Unsupported bit depth in WAV: {bits_per_sample}. "
                "Only 16-bit PCM is supported."
            )

        return pcm_data, sample_rate, num_channels, bits_per_sample
