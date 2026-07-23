"""
CapTure — Free, lightweight screen recorder for Windows.

No FFmpeg dependency. Uses Windows Media Foundation (MSMF/COM)
for video/audio encoding and MP4 muxing.

Optimized for ThinkPad T460s (Intel HD Graphics 520, i5-6300U).
Target: 30fps @ 1080p.
"""

__version__ = "0.1.0"
__author__ = "CapTure Team"

from capture.config import Config
from capture.errors import (
    CapTureError,
    CaptureError,
    EncodeError,
    MuxerError,
    AudioError,
    friendly_error,
)

__all__ = [
    "Config",
    "CapTureError",
    "CaptureError",
    "EncodeError",
    "MuxerError",
    "AudioError",
    "friendly_error",
]
