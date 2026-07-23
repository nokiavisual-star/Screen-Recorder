# CapTure

**Free, lightweight screen recorder for Windows — no FFmpeg required.**

CapTure captures your screen and audio using Windows Media Foundation, producing H.264 + AAC MP4 files. Optimized for modest hardware like the ThinkPad T460s (Intel HD Graphics 520, i5-6300U).

```
   ____              _____
  / ____|___   ___  |_   _|  _  _ _ __  ___
 | |    / _ \ / _ \   | || | | | '_ \/ __|
 | |___| (_) | (_) |  | || |_| | |_) \__ \
  \____\___/ \___/    |_| \__,_| .__/|___/
                               |_|
```

---

## System Requirements

| Component | Minimum |
|-----------|---------|
| **OS** | Windows 10 21H2+ or Windows 11 |
| **Python** | 3.10 or newer (3.11 recommended) |
| **RAM** | 4 GB (8 GB recommended for 1080p) |
| **GPU** | DirectX 11 capable (Intel HD 520+, NVIDIA, AMD) |
| **Storage** | ~500 MB free for the .exe + temp files during recording |
| **Extras** | [Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe) |
| **N/KN Editions** | [Windows Media Feature Pack](https://support.microsoft.com/en-us/topic/media-feature-pack-for-windows-n-8622b390-4ce6-43c9-9b42-549e5328e407) |

---

## Quick Start (without compiling)

```bash
# 1. Clone or download this repository
cd CapTure

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
python -m capture.main
```

This launches the tray icon on Windows (or the console UI on other platforms / with `--console`).

---

## Building a Standalone .exe

CapTure ships with a PyInstaller spec file that produces a single `.exe` — no Python installation needed on the target machine.

### One-command build

```bash
# On Windows:
build_windows.bat
```

Or manually:

```bash
# 1. Install build dependencies
pip install -r requirements.txt

# 2. Build
pyinstaller capture.spec --clean --noconfirm
```

The output is `dist/CapTure.exe`. Distribute this single file — it contains everything.

### Build options

```bash
# Build with debug output (if something goes wrong)
pyinstaller capture.spec --clean --noconfirm --log-level=DEBUG

# Build without UPX compression (larger .exe, fewer issues)
# Edit capture.spec: set upx=False
```

---

## Usage

### Command-line flags

```
python -m capture.main [OPTIONS]

Options:
  --version          Show version and exit
  --console          Force console UI (skip tray icon)
  --check            Verify all dependencies and exit
  --sysinfo          Show detailed system information and exit
  --output DIR       Override output directory (default: ~/Videos/CapTure)
  --fps N            Target frames per second (default: 30)
  --no-mic           Disable microphone recording
  --no-system-audio  Disable system audio recording
  --temp-dir DIR     Override temporary files directory
```

### Hotkeys (Console UI)

| Key | Action |
|-----|--------|
| **R** | Start recording |
| **S** | Stop and save recording |
| **Q** | Quit |

### Tray UI (Windows default)

Right-click the tray icon:
- **Start Recording** — begin capture
- **Stop Recording** — finish and save
- **About** — version info
- **Exit** — quit CapTure

The tray icon turns **red** while recording, **grey** when idle.

### Output

Recordings are saved to `%USERPROFILE%\Videos\CapTure\` by default, named like `CapTure_2026-07-22_15-30-00.mp4`.

---

## How It Works

CapTure uses **zero FFmpeg**. The entire pipeline runs on Windows-native APIs:

```
Screen  ──[dxcam (DirectX)]──▶  BGR frames  ──▶  cv2.VideoWriter (MSMF H.264)
                                                          │
Audio   ──[PyAudio (WASAPI)]──▶  PCM chunks  ──▶  WAV  ──▶  MF AAC encoder
                                                          │
                                     MP4  ◀──  MF Sink Writer (mux)
```

- **Screen capture**: `dxcam` (Desktop Duplication API — GPU-accelerated, zero-copy)
- **Video encode**: OpenCV `cv2.VideoWriter` with MSMF backend (`avc1` → H.264)
- **Audio capture**: `pyaudio` (WASAPI loopback + microphone)
- **Audio encode**: Windows Media Foundation AAC encoder via `ctypes` COM interop
- **Muxing**: MF Source Reader + Sink Writer (H.264 + AAC → MP4)

---

## Project Structure

```
capture/
├── capture/                  # Python package
│   ├── __init__.py           # Version, public API
│   ├── main.py               # Entry point, CLI parsing
│   ├── config.py             # Configuration dataclass
│   ├── controller.py         # Recording lifecycle orchestrator
│   ├── screen_capture.py     # dxcam wrapper
│   ├── audio_capture.py      # PyAudio (WASAPI) wrapper
│   ├── video_encoder.py      # cv2.VideoWriter (MSMF H.264)
│   ├── audio_encoder.py      # WAV → AAC via MF COM
│   ├── muxer.py              # MP4 muxing via MF COM
│   ├── ui.py                 # Console + tray UIs
│   ├── utils.py              # Formatting, dependency checks
│   ├── errors.py             # Exception hierarchy
│   └── icon.ico              # Application icon (optional)
├── capture.spec              # PyInstaller build spec
├── build_windows.bat         # One-click Windows build script
├── setup.iss                 # Inno Setup installer script (optional)
├── icon.png                  # Source icon (256×256 PNG)
├── requirements.txt          # Python dependencies
└── README.md                 # This file
```

---

## Troubleshooting

### "Media Foundation startup failed" or "mfplat.dll not found"

You are likely on **Windows N or KN edition** (sold without media features in some regions).

**Fix:** Install the Media Feature Pack:
1. Open **Settings → Apps → Optional Features → Add a feature**
2. Search for "Media Feature Pack" and install it
3. Restart your computer

Or download directly from [Microsoft](https://support.microsoft.com/en-us/topic/media-feature-pack-for-windows-n-8622b390-4ce6-43c9-9b42-549e5328e407).

### "The program can't start because VCRUNTIME140.dll is missing"

Install the [Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe) (both x64 and x86).

### "dxcam is not installed" / "Failed to initialize screen capture"

- Make sure your GPU supports DirectX 11 (Intel HD Graphics 4xxx and newer do)
- Update your GPU drivers
- On laptops with hybrid graphics, make sure the application runs on the dedicated GPU if the integrated one doesn't support DXGI Desktop Duplication

### "PyAudio installation failed"

PyAudio has no official wheel for newer Python versions. Options:
1. Install via `pipwin`: `pip install pipwin && pipwin install pyaudio`
2. Download a prebuilt wheel from [Christoph Gohlke's site](https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio)
3. Install Visual C++ Build Tools and compile from source

### Recording has no audio

- Make sure your output device is playing audio (system audio capture uses WASAPI loopback — it captures what you *hear*)
- Check that the microphone is not muted in Windows sound settings
- Try running `python -m capture.main --sysinfo` to verify audio devices are detected

### .exe is very large (200+ MB)

This is expected — PyInstaller bundles Python, OpenCV, NumPy, and all dependencies. Tips to reduce size:
- Use UPX compression (enabled by default in `capture.spec`)
- Create a virtual environment with only the needed packages before building
- Consider using Nuitka instead of PyInstaller for smaller output

---

## Custom Icon

To replace the default icon:

1. Create or obtain a **256×256 PNG** image
2. Convert it to `.ico` format:
   ```python
   from PIL import Image
   img = Image.open("your_icon.png")
   img.save("capture/icon.ico", format="ICO", sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)])
   ```
3. Place `icon.ico` in `capture/icon.ico`
4. Rebuild: `pyinstaller capture.spec --clean --noconfirm`

The current `icon.png` is a simple placeholder — a dark rounded square with a red recording dot and a "C" letter.

---

## Building an Installer (Optional)

If you want to distribute CapTure as a Windows installer:

1. Install [Inno Setup](https://jrsoftware.org/isinfo.php) (free)
2. Open `setup.iss` in Inno Setup Compiler
3. Adjust paths if needed
4. Compile → produces `CapTure_Setup.exe`

The installer places CapTure in `%ProgramFiles%\CapTure`, creates Start Menu shortcuts, and optionally adds a desktop icon.

---

## Contributing

CapTure is in active development. See the codebase for details. PRs welcome!

---

## License

CapTure is donation-supported freeware. All rights reserved.
