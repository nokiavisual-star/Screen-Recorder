"""
MP4 muxer using Windows Media Foundation COM APIs.

Combines an H.264 video stream (from VideoEncoder) and an AAC audio
stream (from AudioEncoder) into a single MP4 container.

Uses Media Foundation's Source Reader + Sink Writer to multiplex
both streams without re-encoding and without FFmpeg.

Fallback: if Media Foundation is unavailable, the video file is copied
as-is and the audio remains as a separate AAC file with a warning.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from ctypes import (
    POINTER,
    byref,
    c_uint8,
    c_uint32,
    c_void_p,
)
from ctypes.wintypes import BOOL, DWORD, HRESULT, LARGE_INTEGER, UINT32

from capture.config import Config
from capture.errors import MuxerError


# ═══════════════════════════════════════════════════════════════════════
# Media Foundation GUIDs and constants (shared subset with audio_encoder)
# ═══════════════════════════════════════════════════════════════════════

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", c_uint8 * 8),
    ]


def _guid(data1: int, data2: int, data3: int, d4: str) -> _GUID:
    parts = [int(x, 16) for x in d4.replace("-", "").replace(" ", "")]
    return _GUID(data1, data2, data3, (c_uint8 * 8)(*parts))


# Major types
MFMediaType_Video = _guid(0x73646976, 0x0000, 0x0010,
                          "80 00 00 AA 00 38 9B 71")
MFMediaType_Audio = _guid(0x73646961, 0x0000, 0x0010,
                          "80 00 00 AA 00 38 9B 71")

# Subtypes
MFVideoFormat_H264 = _guid(0x34363248, 0x0000, 0x0010,
                           "80 00 00 AA 00 38 9B 71")
MFAudioFormat_AAC = _guid(0x00001610, 0x0000, 0x0010,
                          "80 00 00 AA 00 38 9B 71")

# Attribute keys
MF_MT_MAJOR_TYPE = _guid(0x48EBA18E, 0xF8C9, 0x4687,
                         "BF 11 0A 74 C9 F9 6A 8F")
MF_MT_SUBTYPE = _guid(0xF7E34C9A, 0x42E8, 0x4714,
                      "B7 4B CB 29 D7 2C 35 E5")
MF_MT_FRAME_SIZE = _guid(0x1652C33D, 0xD6B2, 0x4012,
                         "B8 34 72 03 08 49 A3 7D")
MF_MT_FRAME_RATE = _guid(0xC459A2E8, 0x3D2C, 0x4E44,
                         "B1 32 FE E5 15 6C 7B B0")
MF_MT_AUDIO_NUM_CHANNELS = _guid(0x37E48BF5, 0x645E, 0x4C5B,
                                 "89 DE AD A9 E2 9B 69 6A")
MF_MT_AUDIO_SAMPLES_PER_SECOND = _guid(0x5FAEEAE7, 0x0290, 0x4C31,
                                       "9E 8A C5 34 F6 8D 9D BA")
MF_MT_AUDIO_BITS_PER_SAMPLE = _guid(0xF2DEB57F, 0x40FA, 0x4764,
                                    "AA 33 ED 4F 2D 1F F6 69")
MF_MT_AUDIO_BLOCK_ALIGNMENT = _guid(0x322DE230, 0x9EEB, 0x43BD,
                                    "AB 7A FF 41 22 51 54 1D")
MF_MT_AUDIO_AVG_BYTES_PER_SECOND = _guid(0x1AAB75C8, 0xCFEF, 0x451C,
                                         "AB 95 AC 03 4B 8E 17 31")

MF_SOURCE_READER_FIRST_VIDEO_STREAM = 0xFFFFFFFC
MF_SOURCE_READER_FIRST_AUDIO_STREAM = 0xFFFFFFFD
MF_SOURCE_READER_CURRENT_TYPE_INDEX = 0xFFFFFFFF

MF_SINK_WRITER_DISABLE_THROTTLING = 1

MF_VERSION = 0x00020070


# ═══════════════════════════════════════════════════════════════════════
# Lightweight COM helpers
# ═══════════════════════════════════════════════════════════════════════

def _com_call(this: ctypes.c_void_p, vtbl_idx: int, restype,
              *args: object) -> object:
    """Call a COM method at *vtbl_idx* in *this* object's vtable."""
    vtable = ctypes.cast(this, POINTER(POINTER(c_void_p)))
    func = vtable[0][vtbl_idx]
    arg_types = []
    for a in args:
        if isinstance(a, ctypes._Pointer):
            arg_types.append(type(a))
        elif isinstance(a, ctypes._SimpleCData):
            arg_types.append(type(a))
        elif isinstance(a, int):
            arg_types.append(ctypes.c_uint32)
        else:
            arg_types.append(ctypes.c_void_p)
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *arg_types)
    return proto(ctypes.cast(func, ctypes.c_void_p))(this, *args)


def _com_release(obj: ctypes.c_void_p) -> int:
    """IUnknown::Release (vtable idx 2)."""
    if obj:
        return _com_call(obj, 2, ctypes.c_ulong)
    return 0


# ── IMFSourceReader ───────────────────────────────────────────────────

def _source_reader_get_media_type(reader: ctypes.c_void_p, stream_idx: int,
                                  ) -> tuple[int, ctypes.c_void_p]:
    """Get the native media type for a stream (vtable idx 18)."""
    mt = c_void_p(0)
    hr = _com_call(reader, 18, ctypes.c_long,
                   DWORD(stream_idx), DWORD(MF_SOURCE_READER_CURRENT_TYPE_INDEX),
                   byref(mt))
    return (hr, mt)


def _source_reader_set_stream_selection(reader: ctypes.c_void_p,
                                        stream_idx: int,
                                        select: bool) -> int:
    """Enable/disable a stream (vtable idx 4)."""
    return _com_call(reader, 4, ctypes.c_long,
                     DWORD(stream_idx), BOOL(select))


def _source_reader_read_sample(reader: ctypes.c_void_p,
                               stream_idx: int,
                               ) -> tuple[int, int, int, ctypes.c_void_p]:
    """Read the next sample (vtable idx 6).

    Returns (hr, stream_flags, timestamp, sample).
    """
    flags = DWORD(0)
    ts = LARGE_INTEGER(0)
    sample = c_void_p(0)
    hr = _com_call(reader, 6, ctypes.c_long,
                   DWORD(stream_idx), DWORD(0),
                   byref(flags), byref(ts), byref(sample))
    return (hr, flags.value, ts.value, sample)


# ── IMFSinkWriter ─────────────────────────────────────────────────────

def _sink_writer_add_stream(sw: ctypes.c_void_p,
                            output_type: ctypes.c_void_p,
                            ) -> tuple[int, int]:
    """Add a stream; returns (hr, stream_index). (vtable idx 4)."""
    idx = DWORD(0)
    hr = _com_call(sw, 4, ctypes.c_long, output_type, byref(idx))
    return (hr, idx.value)


def _sink_writer_set_input_media_type(sw: ctypes.c_void_p,
                                      stream_idx: int,
                                      input_type: ctypes.c_void_p,
                                      ) -> int:
    """Set input media type (vtable idx 6)."""
    return _com_call(sw, 6, ctypes.c_long,
                     DWORD(stream_idx), input_type, c_void_p(0))


def _sink_writer_begin_write(sw: ctypes.c_void_p) -> int:
    """Begin writing (vtable idx 7)."""
    return _com_call(sw, 7, ctypes.c_long)


def _sink_writer_write_sample(sw: ctypes.c_void_p,
                              stream_idx: int,
                              sample: ctypes.c_void_p) -> int:
    """Write a sample (vtable idx 8)."""
    return _com_call(sw, 8, ctypes.c_long,
                     DWORD(stream_idx), sample)


def _sink_writer_finalize(sw: ctypes.c_void_p) -> int:
    """Finalize the output file (vtable idx 22)."""
    return _com_call(sw, 22, ctypes.c_long)


# ── MF API prototypes via ctypes ──────────────────────────────────────

def _load_mf_dlls():
    """Load MF DLLs and set up function prototypes.

    Returns (mfplat, mfreadwrite) or raises MuxerError.
    """
    try:
        mfplat = ctypes.WinDLL("mfplat.dll")
        mfreadwrite = ctypes.WinDLL("mfreadwrite.dll")
    except OSError as exc:
        raise MuxerError(
            "Media Foundation runtime (mfplat.dll / mfreadwrite.dll) "
            "is not available. This may happen on Windows N/KN editions "
            "without the Media Feature Pack. Install it from Microsoft "
            "and try again."
        ) from exc

    mfplat.MFStartup.restype = HRESULT
    mfplat.MFStartup.argtypes = [UINT32, DWORD]
    mfplat.MFShutdown.restype = HRESULT
    mfplat.MFCreateMediaType.restype = HRESULT
    mfplat.MFCreateMediaType.argtypes = [POINTER(c_void_p)]

    mfreadwrite.MFCreateSourceReaderFromURL.restype = HRESULT
    mfreadwrite.MFCreateSourceReaderFromURL.argtypes = [
        ctypes.c_wchar_p, c_void_p, POINTER(c_void_p),
    ]
    mfreadwrite.MFCreateSinkWriterFromURL.restype = HRESULT
    mfreadwrite.MFCreateSinkWriterFromURL.argtypes = [
        ctypes.c_wchar_p, c_void_p, c_void_p, POINTER(c_void_p),
    ]

    return mfplat, mfreadwrite


# ═══════════════════════════════════════════════════════════════════════
# Muxer
# ═══════════════════════════════════════════════════════════════════════

# MF_SAMPLE_FLAG_ENDOFSTREAM = 1


class Muxer:
    """Muxes H.264 video and AAC audio into an MP4 file.

    Uses Media Foundation Source Readers to read the individual
    streams and a Sink Writer to multiplex them into a single MP4
    container — without re-encoding either stream.

    If Media Foundation is unavailable (e.g. Windows N/KN without the
    Media Feature Pack), the muxer falls back to copying the video file
    as the final output and warns the user that audio is separate.

    Attributes:
        config: Application configuration.
        output_path: Final MP4 output path (set by ``mux()``).
        _temp_files: List of temporary file paths to clean up.
    """

    def __init__(self, config: Config) -> None:
        """Initialize the muxer.

        Args:
            config: CapTure Config instance.
        """
        self.config: Config = config
        self.output_path: str = ""
        self._temp_files: list[str] = []

    # ── Public API ──────────────────────────────────────────────────

    def mux(
        self,
        video_path: str,
        audio_path: str,
        output_path: str,
    ) -> str:
        """Mux video and audio files into a single MP4.

        Args:
            video_path: Path to H.264 video-only file (from VideoEncoder).
            audio_path: Path to AAC audio-only file (from AudioEncoder).
            output_path: Desired output MP4 path.

        Returns:
            Path to the final MP4 file.

        Raises:
            MuxerError: If muxing fails and no fallback is possible.
        """
        self.output_path = output_path
        self._temp_files = [video_path, audio_path]

        # Ensure output directory exists.
        out_dir = os.path.dirname(output_path)
        if out_dir:
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as exc:
                raise MuxerError(
                    f"Cannot create output directory '{out_dir}': {exc}"
                ) from exc

        # ── Video-only fallback if no audio ─────────────────────────
        if not os.path.isfile(audio_path) or os.path.getsize(audio_path) == 0:
            return self._fallback_copy_video(video_path, output_path,
                                             "No audio data to mux.")

        if not os.path.isfile(video_path) or os.path.getsize(video_path) == 0:
            raise MuxerError(
                f"Video file '{video_path}' is missing or empty. "
                "Cannot create output."
            )

        # ── Attempt MF-based muxing ─────────────────────────────────
        try:
            return self._mux_via_mf(video_path, audio_path, output_path)
        except MuxerError:
            # MF muxing failed — fall back gracefully.
            return self._fallback_copy_video(
                video_path, output_path,
                "Media Foundation muxing failed. "
                "Video saved without audio. "
                f"Audio preserved at: {audio_path}",
            )

    def cleanup(self) -> None:
        """Remove temporary video and audio files.

        Call this after ``mux()`` succeeds (or after handling errors)
        to avoid leaving intermediate files on disk.
        """
        for path in self._temp_files:
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._temp_files.clear()

    # ── MF-based muxing ─────────────────────────────────────────────

    def _mux_via_mf(
        self, video_path: str, audio_path: str, output_path: str,
    ) -> str:
        """Perform Media Foundation-based muxing.

        Workflow:
        1. Initialise MF.
        2. Open source readers for video and audio.
        3. Create sink writer for the output MP4.
        4. Configure video output stream (matching source type).
        5. Configure audio output stream (matching source type).
        6. Copy all video samples → sink.
        7. Copy all audio samples → sink.
        8. Finalize and shut down.

        Returns output_path on success.

        Raises:
            MuxerError: On any MF failure.
        """
        mfplat, mfreadwrite = _load_mf_dlls()

        hr = mfplat.MFStartup(MF_VERSION, 0)
        if hr < 0:
            raise MuxerError(
                f"Media Foundation startup failed (HRESULT: 0x{hr:08X})."
            )

        vid_reader = c_void_p(0)
        aud_reader = c_void_p(0)
        sink_writer = c_void_p(0)
        vid_out_type = c_void_p(0)
        aud_out_type = c_void_p(0)
        vid_in_type = c_void_p(0)
        aud_in_type = c_void_p(0)

        try:
            # ── Open source readers ─────────────────────────────────
            hr = mfreadwrite.MFCreateSourceReaderFromURL(
                video_path, c_void_p(0), byref(vid_reader),
            )
            if hr < 0 or not vid_reader:
                raise MuxerError("Cannot open video source reader.")

            hr = mfreadwrite.MFCreateSourceReaderFromURL(
                audio_path, c_void_p(0), byref(aud_reader),
            )
            if hr < 0 or not aud_reader:
                raise MuxerError("Cannot open audio source reader.")

            # ── Get native media types from sources ─────────────────
            _hr_v, vid_in_type = _source_reader_get_media_type(
                vid_reader, MF_SOURCE_READER_FIRST_VIDEO_STREAM,
            )
            if not vid_in_type:
                raise MuxerError("Video source has no media type.")

            _hr_a, aud_in_type = _source_reader_get_media_type(
                aud_reader, MF_SOURCE_READER_FIRST_AUDIO_STREAM,
            )
            if not aud_in_type:
                raise MuxerError("Audio source has no media type.")

            # ── Create sink writer ──────────────────────────────────
            hr = mfreadwrite.MFCreateSinkWriterFromURL(
                output_path, c_void_p(0), c_void_p(0), byref(sink_writer),
            )
            if hr < 0 or not sink_writer:
                raise MuxerError("Cannot create output sink writer.")

            # ── Add video stream (passthrough) ──────────────────────
            hr_mt, vid_out_type = self._duplicate_media_type(
                mfplat, vid_in_type,
            )
            hr_add, vid_stream_idx = _sink_writer_add_stream(
                sink_writer, vid_out_type,
            )
            if hr_add < 0:
                raise MuxerError("Cannot add video stream to sink writer.")

            hr = _sink_writer_set_input_media_type(
                sink_writer, vid_stream_idx, vid_in_type,
            )
            if hr < 0:
                raise MuxerError("Cannot set video input media type.")

            # ── Add audio stream (passthrough) ──────────────────────
            hr_mt, aud_out_type = self._duplicate_media_type(
                mfplat, aud_in_type,
            )
            hr_add, aud_stream_idx = _sink_writer_add_stream(
                sink_writer, aud_out_type,
            )
            if hr_add < 0:
                raise MuxerError("Cannot add audio stream to sink writer.")

            hr = _sink_writer_set_input_media_type(
                sink_writer, aud_stream_idx, aud_in_type,
            )
            if hr < 0:
                raise MuxerError("Cannot set audio input media type.")

            # ── Begin writing ───────────────────────────────────────
            hr = _sink_writer_begin_write(sink_writer)
            if hr < 0:
                raise MuxerError("Sink writer BeginWrite failed.")

            # ── Copy all video samples ──────────────────────────────
            self._copy_samples(vid_reader,
                               MF_SOURCE_READER_FIRST_VIDEO_STREAM,
                               sink_writer, vid_stream_idx)

            # ── Copy all audio samples ──────────────────────────────
            self._copy_samples(aud_reader,
                               MF_SOURCE_READER_FIRST_AUDIO_STREAM,
                               sink_writer, aud_stream_idx)

            # ── Finalize ────────────────────────────────────────────
            hr = _sink_writer_finalize(sink_writer)
            if hr < 0:
                raise MuxerError(
                    f"Sink writer Finalize failed (HRESULT: 0x{hr:08X})."
                )

            return output_path

        finally:
            _com_release(vid_in_type)
            _com_release(aud_in_type)
            _com_release(vid_out_type)
            _com_release(aud_out_type)
            _com_release(vid_reader)
            _com_release(aud_reader)
            _com_release(sink_writer)
            mfplat.MFShutdown()

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _duplicate_media_type(
        mfplat: ctypes.CDLL, source_type: ctypes.c_void_p,
    ) -> tuple[int, ctypes.c_void_p]:
        """Create a new IMFMediaType by copying *source_type*.

        Returns (hr, new_type).
        """
        new_type = c_void_p(0)
        hr = mfplat.MFCreateMediaType(byref(new_type))
        if hr < 0:
            return (hr, c_void_p(0))
        # IMFMediaType::CopyAllItems (vtable idx 19)
        hr = _com_call(new_type, 19, ctypes.c_long, source_type)
        return (hr, new_type)

    @staticmethod
    def _copy_samples(
        reader: ctypes.c_void_p,
        stream_idx: int,
        sink_writer: ctypes.c_void_p,
        out_stream_idx: int,
    ) -> None:
        """Read all samples from *reader* and write to *sink_writer*.

        Stops when the source reader returns the end-of-stream flag.

        Raises:
            MuxerError: If a read or write fails.
        """
        while True:
            hr, flags, timestamp, sample = _source_reader_read_sample(
                reader, stream_idx,
            )
            if hr < 0:
                raise MuxerError(
                    f"Source reader ReadSample failed "
                    f"(stream {stream_idx}, HRESULT: 0x{hr:08X})."
                )

            # MF_SOURCE_READERF_ENDOFSTREAM = 0x1
            if flags & 0x1:
                # Tell sink writer this stream is done.
                _sink_writer_write_sample(sink_writer, out_stream_idx,
                                          c_void_p(0))
                break

            if sample:
                hr = _sink_writer_write_sample(sink_writer,
                                               out_stream_idx, sample)
                _com_release(sample)
                if hr < 0:
                    raise MuxerError(
                        f"Sink writer WriteSample failed "
                        f"(stream {out_stream_idx}, "
                        f"HRESULT: 0x{hr:08X})."
                    )

    # ── Fallback ────────────────────────────────────────────────────

    def _fallback_copy_video(
        self, video_path: str, output_path: str, warning: str,
    ) -> str:
        """Fallback: copy the video file as-is, print a warning.

        Used when audio is missing or Media Foundation is unavailable.
        """
        try:
            shutil.copy2(video_path, output_path)
        except OSError as exc:
            raise MuxerError(
                f"Failed to copy video file to '{output_path}': {exc}"
            ) from exc

        print(
            f"\n  [CapTure] WARNING: {warning}",
            file=sys.stderr,
        )
        return output_path
