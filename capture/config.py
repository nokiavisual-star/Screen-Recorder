"""
Configuration module for CapTure screen recorder.

Provides the Config class with sensible defaults for ThinkPad T460s
target hardware: Intel HD Graphics 520, i5-6300U, 8GB RAM.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """Application configuration with sensible defaults.

    All values can be overridden at construction time or modified
    after instantiation. The defaults target 30fps 1080p recording
    on integrated Intel HD Graphics 520.

    Attributes:
        fps: Target frames per second for video capture.
        video_codec: FourCC code for MSMF-backed VideoWriter.
                     'avc1' = H.264 via Media Foundation.
        video_bitrate: Target video bitrate in bits per second.
        screen_region: Screen capture region as (left, top, right, bottom)
                       or None for full primary monitor.
        audio_sample_rate: Audio sample rate in Hz (44100 = CD quality).
        audio_channels: Number of audio channels (1=mono, 2=stereo).
        audio_bit_depth: Audio sample bit depth.
        audio_device_name: System audio device name for loopback capture
                           or None for default.
        mic_device_name: Microphone device name or None for default.
        output_dir: Directory for recorded video files.
        filename_template: Template for output filenames. Supports
                           {date} and {time} placeholders.
        temp_dir: Directory for intermediate files during muxing.
    """

    # ---- Video settings ----
    fps: int = 30
    video_codec: str = "avc1"  # H.264 via MSMF
    video_bitrate: int = 5_000_000  # 5 Mbps
    screen_region: tuple[int, int, int, int] | None = None  # full screen

    # ---- Audio settings ----
    audio_sample_rate: int = 44100
    audio_channels: int = 2  # stereo
    audio_bit_depth: int = 16
    audio_device_name: str | None = None
    mic_device_name: str | None = None

    # ---- Output settings ----
    output_dir: str = field(default_factory=lambda: str(
        Path.home() / "Videos" / "CapTure"
    ))
    filename_template: str = "CapTure_{date}_{time}.mp4"
    temp_dir: str = field(default_factory=lambda: str(
        Path.home() / "AppData" / "Local" / "Temp" / "CapTure"
    ))

    # ---- Advanced ----
    enable_mic: bool = True
    enable_system_audio: bool = True
    show_overlay: bool = True
    minimize_to_tray: bool = True

    def ensure_dirs(self) -> None:
        """Create output and temp directories if they don't exist."""
        for d in (self.output_dir, self.temp_dir):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as exc:
                raise OSError(
                    f"Cannot create directory '{d}'. "
                    f"Check permissions: {exc}"
                )

    def get_output_path(self) -> str:
        """Generate the full output file path from the template.

        Uses current date/time for {date} and {time} placeholders.
        """
        from datetime import datetime

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H-%M-%S")

        filename = self.filename_template.format(
            date=date_str, time=time_str
        )
        return os.path.join(self.output_dir, filename)
