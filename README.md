# Syncra OCR

Real-time screen capture, OCR and translation tool optimized for game dialogs and menus.

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python)
![PyQt6](https://img.shields.io/badge/PyQt6-6.11+-green)
![OpenCV](https://img.shields.io/badge/OpenCV-4.13+-orange)
![License](https://img.shields.io/badge/License-MIT-yellow)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D4)

## Features

- **Dual OCR Backend** - Windows OCR (WinRT) + Tesseract fallback
- **Real-time Capture** - Continuous screen capture with configurable interval
- **Image Processing** - OpenCV-powered filtering pipeline (blur, threshold, dilate, HSV color modes)
- **Auto Translation** - Google Translate integration with 8+ target languages
- **Quick Capture** - Global hotkey for instant region OCR
- **Text Stabilization** - Smart text gate with duplicate filtering
- **Auto-Adjust** - One-click optimal filter settings finder
- **Multi-Region** - Separate dialogue and menu region support
- **Filter Profiles** - Save and load custom filter configurations
- **Output Window** - Separate draggable output window with Ctrl+scroll font zoom
- **Live Preview** - Real-time filter preview panels
- **Dark Theme** - Modern minimal dark UI

## Screenshots

| Main Tab | Filter Tab | Settings | About |
|----------|-----------|----------|-------|
| Capture controls, region selection, stabilization | Filter preview, presets, advanced HSV tuning | OCR engine, preprocessing, output window | System info, usage, hotkeys |

## Requirements

- **OS**: Windows 10/11 (1920x1080 or higher recommended)
- **Python**: 3.10+
- **Tesseract OCR** (optional): Install and add to PATH if using Tesseract backend

## Installation

### From Source

```powershell
git clone https://github.com/yourusername/syncra-ocr.git
cd syncra-ocr
python -m pip install -r requirements.txt
```

### Quick Start

```powershell
python main.py
```

## Dependencies

| Package | Purpose |
|---------|---------|
| `PyQt6` | Desktop UI framework |
| `opencv-contrib-python` | Image processing and filtering |
| `numpy` | Array operations for OpenCV |
| `Pillow` | Image format conversion |
| `mss` | Fast screen capture |
| `pytesseract` | Tesseract OCR backend |
| `deep-translator` | Google Translate integration |
| `argostranslate` | Offline translation (optional) |
| `winsdk` | Windows OCR (WinRT) backend |

## Usage

1. **Select Monitors** - Choose capture screen (game) and output screen
2. **Select Regions** - Click "Select Dialogue Region" / "Select Menu Region" and draw
3. **Configure Filter** - Choose a preset or fine-tune blur/threshold/dilate settings
4. **Test OCR** - Click "Test OCR Once" to verify text extraction
5. **Start Capture** - Click "Start" for continuous OCR loop
6. **Auto Translate** - Enable auto-translation and select target language

### Hotkeys

| Hotkey | Action |
|--------|--------|
| `Ctrl+Shift+Q` | Quick Capture |
| `Ctrl+Shift+R` | Toggle Region Select |
| `Ctrl+Shift+S` | Start / Stop OCR |

### Filter Presets

| Preset | Description |
|--------|-------------|
| `Subtitle Auto` | General subtitle extraction |
| `Wuwa Dialogue` | Outlined white/yellow game subtitles |
| `Anime RPG` | Anime-style game text |
| `White Text` | White text on dark backgrounds |
| `White + Yellow` | White and yellow text combined |
| `High Contrast` | High contrast black/white |

### Color Modes

| Mode | Description |
|------|-------------|
| `Grayscale` | Standard grayscale threshold |
| `White Only` | Isolate white text only |
| `White + Yellow` | Isolate white and yellow text |
| `Custom HSV` | Full HSV range control |

## Project Structure

```
syncra-ocr/
  main.py                 # Entry point
  requirements.txt        # Python dependencies
  build_requirements.txt  # Build dependencies (PyInstaller)
  syncra.spec             # PyInstaller build spec
  build_exe.ps1           # Windows build script
  LICENSE                 # MIT License
  README.md               # This file
  assets/                 # Application icons
    syncra-app.ico
    syncra-24.ico
    syncra-32.ico
    syncra-48.ico
    syncra-96.ico
  syncra/                 # Application package
    __init__.py
    app.py                # App initialization
    main_window.py        # Main UI and logic
    filters.py            # Image processing pipeline
    ocr.py                # OCR backends (WinRT, Tesseract)
    translation.py        # Translation backends (Google, Argos)
```

## Building Executable

### Prerequisites

```powershell
python -m pip install -r requirements.txt
python -m pip install -r build_requirements.txt
```

### Build

```powershell
.\build_exe.ps1
```

Output: `dist\SyncraOCR\SyncraOCR.exe`

### Notes

- Run only `dist\SyncraOCR\SyncraOCR.exe`. Do not run from `build\` folder.
- `config.json` and `syncra_error.log` are created next to the executable.
- Tesseract backend requires `tesseract.exe` in PATH (ship separately if needed).

## Configuration

On first run, `config.json` is auto-created. Key settings:

```json
{
  "ocr_engine": "winrt",
  "translation_enabled": true,
  "translation_target": "tr",
  "ocr_interval_ms": 150,
  "opencv": {
    "ocr_mode": "filtered",
    "preset": "custom",
    "color_mode": "gray",
    "threshold": 170,
    "blur": 3,
    "dilate_iter": 1
  }
}
```

## Troubleshooting

### NumPy version conflict

```powershell
python -m pip uninstall -y numpy opencv-python opencv-python-headless opencv-contrib-python
python -m pip install --no-cache-dir numpy==1.26.4 opencv-contrib-python==4.6.0.66
```

### WinRT not available

Ensure `winsdk` is installed:
```powershell
pip install winsdk>=1.0.0b10
```

### Tesseract not found

Install Tesseract OCR and add to PATH, or download from [UB-Mannheim](https://github.com/UB-Mannheim/tesseract/wiki).

## License

MIT License - see [LICENSE](LICENSE) for details.
