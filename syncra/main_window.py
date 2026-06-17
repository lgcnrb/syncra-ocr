from __future__ import annotations

import asyncio
import colorsys
import ctypes
import io
import json
import re
import sys
import time
import threading
import traceback
from concurrent.futures import TimeoutError as FutureTimeoutError
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QDateTime, QLockFile, QStandardPaths, QThreadPool, QTimer, Qt, QRect, QObject, QRunnable, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

if sys.platform == "win32":
    from ctypes import wintypes
else:  # pragma: no cover - non-Windows fallback
    wintypes = None

try:
    import mss
except Exception:  # pragma: no cover - optional import
    mss = None

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - optional import
    Image = None
    ImageOps = None

try:
    import numpy as np
except Exception:  # pragma: no cover - optional import
    np = None

try:
    import cv2
except Exception:  # pragma: no cover - optional import
    cv2 = None

from .filters import (
    smart_ocr_filter,
    upscale_for_ocr,
    fast_enhance_for_ocr,
    enhance_image,
    score_ocr_result,
    pil_from_np,
    FILTER_PRESETS,
    COLOR_MODES,
)
from .ocr import build_backend, OCRBackendBase
from .translation import build_translation_backend, TranslationBackendBase


APP_NAME = "Syncra OCR"
APP_USER_MODEL_ID = "Syncra.OCR.App"


_prev_cpu_ticks = 0
_prev_cpu_time = 0.0

def _get_process_usage() -> dict:
    """Get current process CPU %, memory MB, and system total RAM / CPU cores."""
    global _prev_cpu_ticks, _prev_cpu_time
    import os as _os
    import time as _time
    result = {"cpu_pct": 0.0, "mem_mb": 0.0, "sys_ram_gb": 0.0, "sys_ram_used_gb": 0.0, "sys_cores": _os.cpu_count() or 0}
    if sys.platform != "win32":
        return result
    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        handle = kernel32.GetCurrentProcess()

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_bool

        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            result["mem_mb"] = round(pmc.WorkingSetSize / (1024 * 1024), 1)

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        mem_status = MEMORYSTATUSEX()
        mem_status.dwLength = ctypes.sizeof(mem_status)
        kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_status))
        result["sys_ram_gb"] = round(mem_status.ullTotalPhys / (1024 ** 3), 1)
        result["sys_ram_used_gb"] = round((mem_status.ullTotalPhys - mem_status.ullAvailPhys) / (1024 ** 3), 1)

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

        creation, exit_t, kernel_t, user_t = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t), ctypes.byref(kernel_t), ctypes.byref(user_t))
        total_ticks = (kernel_t.dwHighDateTime << 32 | kernel_t.dwLowDateTime) + (user_t.dwHighDateTime << 32 | user_t.dwLowDateTime)
        now = _time.monotonic()
        dt = now - _prev_cpu_time if _prev_cpu_time > 0 else 0
        if dt > 0.5 and _prev_cpu_ticks > 0:
            delta_ticks = total_ticks - _prev_cpu_ticks
            cores = _os.cpu_count() or 1
            result["cpu_pct"] = round(min(100.0, (delta_ticks / 10_000_000) / dt / cores * 100), 1)
        _prev_cpu_ticks = total_ticks
        _prev_cpu_time = now
    except Exception as e:
        try:
            with open(Path(__file__).resolve().parent.parent / "syncra_error.log", "a", encoding="utf-8") as _f:
                _f.write(f"[usage] {e}\n")
        except Exception:
            pass
    return result
TARGET_LANGUAGES = [
    ("Turkish", "tr"),
    ("English", "en"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("German", "de"),
    ("French", "fr"),
    ("Spanish", "es"),
    ("Russian", "ru"),
]
HOTKEY_MODIFIERS = [
    ("Ctrl+Shift", "ctrl+shift"),
    ("Ctrl+Alt", "ctrl+alt"),
    ("Alt+Shift", "alt+shift"),
    ("Ctrl+Shift+Alt", "ctrl+shift+alt"),
]
HOTKEY_KEYS = [chr(code) for code in range(ord("A"), ord("Z") + 1)] + [f"F{i}" for i in range(1, 13)]

if sys.platform == "win32":
    USER32 = ctypes.windll.user32
    WM_HOTKEY = 0x0312
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000
else:  # pragma: no cover - non-Windows fallback
    USER32 = None
    WM_HOTKEY = 0
    MOD_ALT = 0
    MOD_CONTROL = 0
    MOD_SHIFT = 0
    MOD_NOREPEAT = 0


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = app_base_dir() / "config.json"
ERROR_LOG_PATH = app_base_dir() / "syncra_error.log"
ASSETS_DIR = app_base_dir() / "assets"
APP_ICON_PATH = ASSETS_DIR / "syncra-app.ico"
ICON_CANDIDATE_PATHS = [
    APP_ICON_PATH,
    ASSETS_DIR / "syncra-24.ico",
    ASSETS_DIR / "syncra-32.ico",
    ASSETS_DIR / "syncra-48.ico",
    ASSETS_DIR / "syncra-96.ico",
]
_APP_ICON: QIcon | None = None
_APP_LOCK: QLockFile | None = None


def build_app_icon() -> QIcon:
    global _APP_ICON
    if _APP_ICON is not None:
        return _APP_ICON

    icon = QIcon()
    for path in ICON_CANDIDATE_PATHS:
        if path.exists():
            icon.addFile(str(path))

    _APP_ICON = icon
    return _APP_ICON


def apply_window_icon(widget: QWidget) -> None:
    icon = build_app_icon()
    if not icon.isNull():
        widget.setWindowIcon(icon)


def set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def single_instance_lock_path() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    if not location:
        location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)
    base = Path(location) if location else (app_base_dir() / ".runtime")
    base.mkdir(parents=True, exist_ok=True)
    return base / "syncra.lock"


def acquire_single_instance_lock() -> bool:
    global _APP_LOCK
    lock = QLockFile(str(single_instance_lock_path()))
    lock.setStaleLockTime(30000)
    if not lock.tryLock(100):
        return False
    _APP_LOCK = lock
    return True


def release_single_instance_lock() -> None:
    global _APP_LOCK
    if _APP_LOCK is not None:
        try:
            _APP_LOCK.unlock()
        except Exception:
            pass
    _APP_LOCK = None


TRANSLATIONS = {
    "en": {
        "app_title": "Syncra OCR",
        "monitor_settings": "Monitor and OCR Settings",
        "capture_screen": "Capture screen",
        "output_screen": "Output screen",
        "ocr_interval": "OCR interval (ms)",
        "auto_translate": "Auto translate",
        "target_language": "Target language",
        "translate_engine": "Translate engine",
        "source_language": "Source language",
        "manage_packs": "Manage Language Packs",
        "ocr_backend": "OCR backend",
        "status": "Status",
        "filter_settings": "Filter Settings",
        "ocr_mode": "OCR Mode",
        "direct": "Direct",
        "filtered": "Filtered",
        "opencv_ready": "OpenCV ready",
        "opencv_not_available": "OpenCV not available",
        "preset": "Preset",
        "apply": "Apply",
        "enable_filter": "Enable filter",
        "live_preview": "Live preview",
        "profile": "Profile",
        "save": "Save",
        "load": "Load",
        "delete": "Delete",
        "show_advanced": "Show advanced settings",
        "invert": "Invert output",
        "blur": "Blur",
        "threshold": "Threshold",
        "dilate": "Dilate",
        "auto_adjust": "AUTO-ADJUST",
        "auto_adjust_testing": "Testing...",
        "auto_adjust_no_frame": "No frame - start capture first",
        "auto_adjust_no_backend": "No OCR backend available",
        "auto_adjust_desc": "Auto-find best filter settings for the image",
        "filter_preview": "Filter Preview",
        "refresh_preview": "Refresh Preview",
        "dialogue": "Dialogue",
        "menu": "Menu",
        "manual_regions": "Manual Regions",
        "select_dialogue": "Select Dialogue Region",
        "select_menu": "Select Menu Region",
        "test_ocr": "Test OCR Once",
        "start": "Start",
        "stop": "Stop",
        "stabilization": "Stabilization and Quick Capture",
        "confirm_repeated": "Confirm repeated OCR",
        "stable_frames": "Stable frames",
        "similarity": "Similarity",
        "quick_capture": "Quick capture",
        "enable_hotkey": "Enable global hotkey",
        "hotkey": "Hotkey",
        "hotkey_status": "Hotkey status",
        "quick_select_now": "Quick Select Now",
        "original": "Original",
        "translation": "Translation",
        "main_tab": "Main",
        "filter_tab": "Filter",
        "settings": "Settings",
        "theme": "Theme",
        "dark": "Dark",
        "light": "Light",
        "language": "Language",
        "english": "English",
        "preprocessing": "Preprocessing",
        "contrast": "Contrast",
        "sharpness": "Sharpness",
        "denoise": "Denoise",
        "auto_enhance": "Auto enhance",
        "idle": "Idle",
        "running": "Running",
        "stopped": "Stopped",
        "select_region": "Select Region",
        "ocr_engine": "OCR Engine",
        "preprocess": "Preprocess",
        "hotkey_idle": "Hotkey idle",
        "window_shortcut_on": "Window shortcut on",
        "global_off": "Global off",
        "settings_tab": "Settings",
        "about_tab": "About",
        "general": "General",
        "translation_settings": "Translation Settings",
        "performance": "Performance",
        "fps": "FPS",
        "avg_fps": "Avg FPS",
        "capture_time": "Capture (ms)",
        "ocr_time": "OCR (ms)",
        "total_time": "Total (ms)",
        "frame_count": "Frame Count",
        "system_info": "System Info",
        "python": "Python",
        "os": "OS",
        "opencv_ver": "OpenCV",
        "numpy_ver": "NumPy",
        "pil_ver": "Pillow",
        "mss_ver": "mss",
        "tesseract_ver": "Tesseract",
        "winrt_ver": "WinRT",
        "active_backend": "Active Backend",
        "output_settings": "Output Window Settings",
        "output_size": "Window Size",
        "output_opacity": "Opacity",
        "output_auto_show": "Auto Show",
        "output_save_pos": "Save Position",
        "color_mode": "Color Mode",
        "color_gray": "Grayscale Threshold",
        "color_white": "White Text Only",
        "color_white_yellow": "White + Yellow Text",
        "color_custom_hsv": "Custom HSV Range",
    }
}

THEME_COLORS = {
    "dark": {
        "bg_primary": "#0b0d11",
        "bg_secondary": "#10121a",
        "bg_tertiary": "#0e1017",
        "bg_elevated": "#161a24",
        "border": "#1e2330",
        "border_active": "#3b82f6",
        "text_primary": "#e2e8f0",
        "text_secondary": "#94a3b8",
        "text_muted": "#475569",
        "accent": "#3b82f6",
        "accent_hover": "#60a5fa",
        "accent_bg": "#0f1a2e",
        "success": "#22c55e",
        "warning": "#eab308",
        "error": "#ef4444",
        "group_title": "#64748b",
    },
    "light": {
        "bg_primary": "#f8fafc",
        "bg_secondary": "#ffffff",
        "bg_tertiary": "#f1f5f9",
        "bg_elevated": "#ffffff",
        "border": "#e2e8f0",
        "border_active": "#3b82f6",
        "text_primary": "#0f172a",
        "text_secondary": "#475569",
        "text_muted": "#94a3b8",
        "accent": "#3b82f6",
        "accent_hover": "#2563eb",
        "accent_bg": "#eff6ff",
        "success": "#16a34a",
        "warning": "#ca8a04",
        "error": "#dc2626",
        "group_title": "#475569",
    },
}


def get_translation(key: str, lang: str = "en") -> str:
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, key)


def get_theme_colors(theme: str = "dark") -> dict:
    return THEME_COLORS.get(theme, THEME_COLORS["dark"])


def build_theme_stylesheet(theme: str = "dark") -> str:
    colors = get_theme_colors(theme)
    return f"""
        QMainWindow, QWidget {{
            background: {colors['bg_primary']};
            color: {colors['text_primary']};
            font-size: 12px;
        }}
        QGroupBox {{
            border: 1px solid {colors['border']};
            border-radius: 10px;
            margin-top: 14px;
            padding: 6px 8px 8px 8px;
            font-weight: 600;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 6px;
            color: {colors['group_title']};
            font-size: 11px;
            letter-spacing: 0.4px;
        }}
        QLabel#FieldLabel {{
            color: {colors['text_muted']};
            font-size: 11px;
        }}
        QLabel#SectionHeader {{
            color: {colors['text_muted']};
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 1.2px;
            margin-top: 4px;
        }}
        QLabel#StatusLabel {{
            color: {colors['text_secondary']};
            font-size: 11px;
        }}
        QTabWidget::pane {{
            border: 1px solid {colors['border']};
            border-radius: 8px;
            top: -1px;
        }}
        QTabBar::tab {{
            padding: 7px 18px;
            color: {colors['text_muted']};
            font-weight: 600;
            border: none;
            border-bottom: 2px solid transparent;
        }}
        QTabBar::tab:selected {{
            color: {colors['accent']};
            border-bottom: 2px solid {colors['accent']};
            background: transparent;
        }}
        QTabBar::tab:hover:!selected {{
            color: {colors['text_secondary']};
        }}
        QPushButton {{
            padding: 5px 12px;
            background: {colors['bg_secondary']};
            color: {colors['text_secondary']};
            border: 1px solid {colors['border']};
            border-radius: 7px;
            font-weight: 600;
        }}
        QPushButton:hover {{
            background: {colors['bg_elevated']};
            border-color: {colors['border_active']};
            color: {colors['text_primary']};
        }}
        QPushButton:pressed {{
            background: {colors['bg_tertiary']};
        }}
        QPushButton:disabled {{
            color: {colors['text_muted']};
            border-color: {colors['border']};
            background: {colors['bg_primary']};
        }}
        QPushButton#BtnAccent {{
            background: {colors['accent_bg']};
            color: {colors['accent']};
            border-color: {colors['accent_bg']};
            font-weight: 700;
        }}
        QPushButton#BtnAccent:hover {{
            background: {colors['accent_hover']};
            color: {colors['bg_primary']};
        }}
        QPushButton#OcrModeBtn {{
            padding: 4px 16px;
            background: {colors['bg_tertiary']};
            color: {colors['text_muted']};
            border: 1px solid {colors['border']};
            border-radius: 0;
            font-weight: 700;
            font-size: 11px;
        }}
        QPushButton#OcrModeBtn:first-of-type {{
            border-radius: 6px 0 0 6px;
        }}
        QPushButton#OcrModeBtn:last-of-type {{
            border-radius: 0 6px 6px 0;
        }}
        QPushButton#OcrModeBtn:checked {{
            background: {colors['accent_bg']};
            color: {colors['accent']};
            border-color: {colors['accent_bg']};
        }}
        QPushButton#OcrModeBtn:hover:!checked {{
            background: {colors['bg_secondary']};
            color: {colors['text_secondary']};
        }}
        QComboBox {{
            padding: 4px 10px;
            background: {colors['bg_tertiary']};
            color: {colors['text_secondary']};
            border: 1px solid {colors['border']};
            border-radius: 6px;
            selection-background-color: {colors['accent_bg']};
        }}
        QComboBox:hover {{
            border-color: {colors['border_active']};
        }}
        QComboBox::drop-down {{
            border: none;
            width: 22px;
        }}
        QComboBox QAbstractItemView {{
            background: {colors['bg_tertiary']};
            color: {colors['text_secondary']};
            border: 1px solid {colors['border']};
            selection-background-color: {colors['accent_bg']};
            outline: none;
        }}
        QCheckBox {{
            color: {colors['text_secondary']};
            spacing: 7px;
        }}
        QCheckBox::indicator {{
            width: 15px;
            height: 15px;
            border: 1px solid {colors['border_active']};
            border-radius: 4px;
            background: {colors['bg_tertiary']};
        }}
        QCheckBox::indicator:checked {{
            background: {colors['accent_bg']};
            border-color: {colors['accent']};
        }}
        QCheckBox::indicator:hover {{
            border-color: {colors['accent']};
        }}
        QCheckBox#SecondaryCheck {{
            color: {colors['text_muted']};
        }}
        QCheckBox#AdvancedToggle {{
            color: {colors['text_muted']};
            font-size: 11px;
        }}
        QSpinBox {{
            padding: 4px 8px;
            background: {colors['bg_tertiary']};
            color: {colors['text_secondary']};
            border: 1px solid {colors['border']};
            border-radius: 6px;
        }}
        QSpinBox:hover {{
            border-color: {colors['border_active']};
        }}
        QSlider::groove:horizontal {{
            border: none;
            height: 4px;
            background: {colors['bg_secondary']};
            border-radius: 2px;
        }}
        QSlider::sub-page:horizontal {{
            background: {colors['accent']};
            border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            background: {colors['text_secondary']};
            border: none;
            width: 13px;
            height: 13px;
            margin: -5px 0;
            border-radius: 7px;
        }}
        QSlider::handle:horizontal:hover {{
            background: {colors['accent_hover']};
        }}
        QPlainTextEdit {{
            background: {colors['bg_primary']};
            color: {colors['text_secondary']};
            border: 1px solid {colors['border']};
            border-radius: 7px;
            padding: 4px 6px;
            selection-background-color: {colors['accent_bg']};
        }}
        QScrollBar:vertical {{
            background: {colors['bg_primary']};
            width: 8px;
            margin: 0;
        }}
        QScrollBar::handle:vertical {{
            background: {colors['border_active']};
            min-height: 24px;
            border-radius: 4px;
        }}
        QScrollBar:horizontal {{
            background: {colors['bg_primary']};
            height: 8px;
        }}
        QScrollBar::handle:horizontal {{
            background: {colors['border_active']};
            min-width: 24px;
            border-radius: 4px;
        }}
        QSplitter::handle {{
            background: {colors['bg_tertiary']};
        }}
        QFrame#Separator {{
            border: none;
            border-top: 1px solid {colors['border']};
            margin: 2px 0;
        }}
        QFrame#AboutHero {{
            background: {colors['bg_secondary']};
            border: 1px solid {colors['border']};
            border-radius: 12px;
        }}
        QLabel#AboutTitle {{
            color: {colors['accent']};
            font-size: 24px;
            font-weight: 800;
            letter-spacing: 6px;
            background: transparent;
        }}
        QLabel#AboutVersionBadge {{
            background: {colors['accent_bg']};
            color: {colors['accent']};
            border: 1px solid {colors['accent']};
            border-radius: 10px;
            padding: 2px 12px;
            font-size: 10px;
            font-weight: 700;
        }}
        QLabel#AboutDesc {{
            color: {colors['text_secondary']};
            font-size: 11px;
            background: transparent;
        }}
        QLabel#AboutSectionHeader {{
            color: {colors['text_muted']};
            font-size: 9px;
            font-weight: 700;
            letter-spacing: 2px;
            margin-top: 2px;
            margin-bottom: 0px;
        }}
        QFrame#AboutFeatureCard {{
            background: {colors['bg_secondary']};
            border: 1px solid {colors['border']};
            border-radius: 8px;
        }}
        QFrame#AboutFeatureCard:hover {{
            border-color: {colors['border_active']};
            background: {colors['bg_elevated']};
        }}
        QLabel#AboutCardTitle {{
            color: {colors['text_primary']};
            font-size: 11px;
            font-weight: 700;
        }}
        QLabel#AboutCardDesc {{
            color: {colors['text_muted']};
            font-size: 10px;
        }}
        QFrame#AboutHotkeyCard {{
            background: {colors['bg_secondary']};
            border: 1px solid {colors['border']};
            border-radius: 8px;
        }}
        QFrame#AboutHotkeyCard:hover {{
            border-color: {colors['border_active']};
        }}
        QLabel#AboutHotkeyKey {{
            font-size: 11px;
            font-weight: 700;
            font-family: Consolas, monospace;
        }}
        QLabel#AboutHotkeyAction {{
            color: {colors['text_secondary']};
            font-size: 11px;
        }}
        QLabel#AboutFooter {{
            color: {colors['text_muted']};
            font-size: 10px;
            border-top: 1px solid {colors['border']};
            padding-top: 10px;
        }}
        QLabel#SettingsSectionHeader {{
            color: {colors['text_muted']};
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 2px;
            padding-left: 2px;
            margin-top: 4px;
        }}
        QLabel#SysInfoValue {{
            color: {colors['text_secondary']};
            font-size: 11px;
        }}
        QLabel#PerfValue {{
            color: {colors['accent']};
            font-size: 11px;
            font-weight: 700;
            font-family: Consolas, monospace;
        }}
    """


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "theme": "dark",
        "capture_screen_index": 0,
        "output_screen_index": 0,
        "ocr_interval_ms": 1200,
        "translation_enabled": True,
        "translation_target": "en",
        "ocr_engine": "auto",
        "ocr_preprocessing": {
            "contrast": 1.2,
            "sharpness": 1.0,
            "denoise": 0,
            "auto_enhance": True,
        },
        "stabilizer": {
            "enabled": True,
            "frames": 2,
            "similarity_percent": 88,
        },
        "quick_capture": {
            "enabled": False,
            "modifier": "ctrl+shift",
            "key": "Q",
        },
        "opencv": {
            "enabled": True,
            "preset": "subtitle_auto",
            "color_mode": "gray",
            "show_advanced": False,
            "live_preview": True,
            "invert": False,
            "blur": 3,
            "threshold": 180,
            "dilate_iter": 1,
        },
        "filter_profiles": {},
        "active_filter_profile": "",
        "regions": {
            "dialogue": [0, 0, 0, 0],
            "menu": [0, 0, 0, 0],
        },
        "output_window": {
            "width": 1100,
            "height": 750,
            "opacity": 0.95,
            "always_on_top": True,
            "font_size_dialogue": 17,
            "font_size_menu": 13,
            "font_size_source_dialogue": 11,
            "font_size_source_menu": 10,
            "show_timestamp": True,
            "auto_show": True,
            "save_position": True,
            "x": -1,
            "y": -1,
        },
    }


def load_config() -> dict[str, Any]:
    cfg = default_config()
    if not CONFIG_PATH.exists():
        return cfg
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            cfg.update(loaded)
            regions = loaded.get("regions", {})
            if isinstance(regions, dict):
                cfg["regions"].update(regions)
            opencv_cfg = loaded.get("opencv", {})
            if isinstance(opencv_cfg, dict):
                cfg["opencv"].update(opencv_cfg)
            filter_profiles = loaded.get("filter_profiles", {})
            if isinstance(filter_profiles, dict):
                cfg["filter_profiles"] = filter_profiles
            active_filter_profile = loaded.get("active_filter_profile", "")
            if isinstance(active_filter_profile, str):
                cfg["active_filter_profile"] = active_filter_profile
            stabilizer_cfg = loaded.get("stabilizer", {})
            if isinstance(stabilizer_cfg, dict):
                cfg["stabilizer"].update(stabilizer_cfg)
            quick_capture_cfg = loaded.get("quick_capture", {})
            if isinstance(quick_capture_cfg, dict):
                cfg["quick_capture"].update(quick_capture_cfg)
    except Exception:
        pass
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def log_exception_text(text: str) -> None:
    try:
        ERROR_LOG_PATH.write_text(text, encoding="utf-8")
    except Exception:
        pass


def show_fatal_error(text: str) -> None:
    log_exception_text(text)
    try:
        QMessageBox.critical(
            None,
            APP_NAME,
            "Syncra crashed during startup or runtime.\n\n"
            f"Details were written to:\n{ERROR_LOG_PATH}\n\n"
            "Short error:\n"
            + text.splitlines()[-1],
        )
    except Exception:
        pass


def list_to_qrect(values: list[int] | tuple[int, int, int, int]) -> QRect:
    if len(values) != 4:
        return QRect()
    x, y, w, h = (int(v) for v in values)
    return QRect(x, y, max(0, w), max(0, h))


def qrect_to_list(rect: QRect) -> list[int]:
    return [int(rect.x()), int(rect.y()), int(rect.width()), int(rect.height())]


def is_valid_region(rect: QRect) -> bool:
    return rect.width() > 10 and rect.height() > 10


def set_plain_text_if_changed(editor: QPlainTextEdit, text: str) -> None:
    normalized = text or ""
    if editor.toPlainText() != normalized:
        editor.setPlainText(normalized)


def normalize_for_compare(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def similarity_ratio(left: str, right: str) -> float:
    left_norm = normalize_for_compare(left)
    right_norm = normalize_for_compare(right)
    if not left_norm and not right_norm:
        return 1.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def ocr_text_score(text: str) -> int:
    normalized = normalize_for_compare(text)
    if not normalized:
        return 0
    letters = sum(1 for char in normalized if char.isalpha())
    digits = sum(1 for char in normalized if char.isdigit())
    spaces = normalized.count(" ")
    unique_chars = len(set(normalized))
    return len(normalized) + (letters * 2) + digits + spaces + unique_chars


def hotkey_modifier_flags(value: str) -> int:
    mapping = {
        "ctrl+shift": MOD_CONTROL | MOD_SHIFT,
        "ctrl+alt": MOD_CONTROL | MOD_ALT,
        "alt+shift": MOD_ALT | MOD_SHIFT,
        "ctrl+shift+alt": MOD_CONTROL | MOD_SHIFT | MOD_ALT,
    }
    return mapping.get((value or "").lower(), MOD_CONTROL | MOD_SHIFT)


def hotkey_virtual_key(value: str) -> int:
    token = (value or "").upper()
    if len(token) == 1 and "A" <= token <= "Z":
        return ord(token)
    if token.startswith("F"):
        try:
            index = int(token[1:])
        except ValueError:
            return ord("Q")
        if 1 <= index <= 24:
            return 0x6F + index
    return ord("Q")


def hotkey_display_text(modifier_value: str, key_value: str) -> str:
    label_map = {value: label for label, value in HOTKEY_MODIFIERS}
    modifier_label = label_map.get((modifier_value or "").lower(), "Ctrl+Shift")
    return f"{modifier_label} + {(key_value or 'Q').upper()}"


def hotkey_sequence_text(modifier_value: str, key_value: str) -> str:
    label_map = {value: label for label, value in HOTKEY_MODIFIERS}
    modifier_label = label_map.get((modifier_value or "").lower(), "Ctrl+Shift")
    return f"{modifier_label}+{(key_value or 'Q').upper()}"


class StableTextGate:
    def __init__(self) -> None:
        self._committed_source = ""
        self._committed_translated = ""
        self._pending_source = ""
        self._pending_translated = ""
        self._pending_hits = 0

    def reset(self) -> None:
        self._committed_source = ""
        self._committed_translated = ""
        self._pending_source = ""
        self._pending_translated = ""
        self._pending_hits = 0

    def committed(self) -> tuple[str, str]:
        return self._committed_source, self._committed_translated

    def _same_payload(self, source_a: str, translated_a: str, source_b: str, translated_b: str, threshold: float) -> bool:
        source_score = similarity_ratio(source_a, source_b)
        translated_a_norm = normalize_for_compare(translated_a)
        translated_b_norm = normalize_for_compare(translated_b)
        translated_score = 0.0
        if translated_a_norm and translated_b_norm:
            translated_score = similarity_ratio(translated_a, translated_b)
        return max(source_score, translated_score) >= threshold

    def push(
        self,
        source_text: str,
        translated_text: str,
        enabled: bool,
        required_frames: int,
        similarity_threshold: float,
    ) -> dict[str, Any]:
        source_clean = (source_text or "").strip()
        translated_clean = (translated_text or "").strip()

        if not enabled or required_frames <= 1:
            self._committed_source = source_clean
            self._committed_translated = translated_clean
            self._pending_source = ""
            self._pending_translated = ""
            self._pending_hits = 0
            return {
                "updated": True,
                "source": self._committed_source,
                "translated": self._committed_translated,
                "stable": True,
                "pending_hits": required_frames,
                "required_frames": required_frames,
            }

        if not self._committed_source and not self._committed_translated and (source_clean or translated_clean):
            self._committed_source = source_clean
            self._committed_translated = translated_clean
            self._pending_source = ""
            self._pending_translated = ""
            self._pending_hits = 0
            return {
                "updated": True,
                "source": self._committed_source,
                "translated": self._committed_translated,
                "stable": True,
                "pending_hits": required_frames,
                "required_frames": required_frames,
            }

        if self._same_payload(
            source_clean,
            translated_clean,
            self._committed_source,
            self._committed_translated,
            similarity_threshold,
        ):
            self._pending_source = ""
            self._pending_translated = ""
            self._pending_hits = 0
            return {
                "updated": False,
                "source": self._committed_source,
                "translated": self._committed_translated,
                "stable": True,
                "pending_hits": required_frames,
                "required_frames": required_frames,
            }

        if self._same_payload(
            source_clean,
            translated_clean,
            self._pending_source,
            self._pending_translated,
            similarity_threshold,
        ):
            self._pending_hits += 1
        else:
            self._pending_source = source_clean
            self._pending_translated = translated_clean
            self._pending_hits = 1

        if self._pending_hits >= required_frames:
            self._committed_source = self._pending_source
            self._committed_translated = self._pending_translated
            self._pending_source = ""
            self._pending_translated = ""
            self._pending_hits = 0
            return {
                "updated": True,
                "source": self._committed_source,
                "translated": self._committed_translated,
                "stable": True,
                "pending_hits": required_frames,
                "required_frames": required_frames,
            }

        return {
            "updated": False,
            "source": self._committed_source,
            "translated": self._committed_translated,
            "stable": False,
            "pending_hits": self._pending_hits,
            "required_frames": required_frames,
            "candidate_source": self._pending_source,
        }


class AsyncLoopRunner:
    """Runs async coroutines in a dedicated event loop thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro, timeout: float = 6.0):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=1.0)


class OCRBackendBase:
    name = "none"

    def is_ready(self) -> bool:
        return False

    def recognize(self, image) -> str:
        return ""

    def recognize_with_confidence(self, image) -> tuple[str, float]:
        return "", 0.0

    def close(self) -> None:
        return


class WinRtOCRBackend(OCRBackendBase):
    name = "winrt"

    def __init__(self) -> None:
        self._runner: AsyncLoopRunner | None = None
        self._engine = None
        self._ready = False
        try:
            from winsdk.windows.media.ocr import OcrEngine

            self._engine = OcrEngine.try_create_from_user_profile_languages()
            self._runner = AsyncLoopRunner()
            self._ready = self._engine is not None
        except Exception:
            self._ready = False

    def is_ready(self) -> bool:
        return bool(self._ready and self._engine is not None and self._runner is not None)

    async def _software_bitmap_from_pil(self, image):
        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        png_bytes = io.BytesIO()
        image.save(png_bytes, format="PNG")
        data = png_bytes.getvalue()

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream.get_output_stream_at(0))
        writer.write_bytes(data)
        await writer.store_async()
        await writer.flush_async()
        writer.detach_stream()
        writer.close()
        stream.seek(0)

        decoder = await BitmapDecoder.create_async(stream)
        software_bitmap = await decoder.get_software_bitmap_async()
        return software_bitmap

    async def _recognize_async(self, image) -> str:
        if not self._engine:
            return ""
        software_bitmap = await self._software_bitmap_from_pil(image)
        result = await self._engine.recognize_async(software_bitmap)
        lines = [line.text for line in result.lines]
        return "\n".join(line for line in lines if line).strip()

    def recognize(self, image) -> str:
        if not self.is_ready() or not self._runner:
            return ""
        try:
            return self._runner.run(self._recognize_async(image), timeout=6.0)
        except FutureTimeoutError:
            return ""

    def close(self) -> None:
        if self._runner:
            self._runner.close()
            self._runner = None


class TesseractBackend(OCRBackendBase):
    name = "tesseract"

    def __init__(self) -> None:
        self._ready = False
        self._pytesseract = None
        try:
            import pytesseract

            self._pytesseract = pytesseract
            self._ready = True
        except Exception:
            self._ready = False

    def is_ready(self) -> bool:
        return bool(self._ready and self._pytesseract is not None)

    def recognize(self, image) -> str:
        if not self.is_ready() or not self._pytesseract:
            return ""
        try:
            return self._pytesseract.image_to_string(image).strip()
        except Exception:
            return ""

    def recognize_with_confidence(self, image) -> tuple[str, float]:
        if not self.is_ready() or not self._pytesseract:
            return "", 0.0
        try:
            data = self._pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
            texts = []
            confs = []
            for i, conf in enumerate(data["conf"]):
                if conf > 0:
                    text = data["text"][i].strip()
                    if text:
                        texts.append(text)
                        confs.append(conf / 100.0)
            result = " ".join(texts)
            avg_conf = sum(confs) / len(confs) if confs else 0.0
            return result, avg_conf
        except Exception:
            return "", 0.0


OCR_ENGINE_PRIORITY = ["winrt", "tesseract"]


def build_backend(preference: str = "auto") -> OCRBackendBase:
    engines = {
        "winrt": WinRtOCRBackend(),
        "tesseract": TesseractBackend(),
    }

    if preference == "auto":
        for name in OCR_ENGINE_PRIORITY:
            backend = engines.get(name)
            if backend and backend.is_ready():
                for other_name, other_backend in engines.items():
                    if other_name != name:
                        other_backend.close()
                return backend
        return OCRBackendBase()
    else:
        selected = engines.get(preference, engines["winrt"])
        if selected.is_ready():
            for name, backend in engines.items():
                if name != preference:
                    backend.close()
            return selected
        for name, backend in engines.items():
            backend.close()
        return OCRBackendBase()


class TranslationBackendBase:
    name = "none"

    def is_ready(self) -> bool:
        return False

    @staticmethod
    def normalize_for_translation(text: str) -> str:
        if not text:
            return ""
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        merged = " ".join(line for line in lines if line)
        merged = re.sub(r"\s+([,.!?;:])", r"\1", merged)
        return merged.strip()

    def translate(self, text: str, source_lang: str = "auto", target_lang: str = "tr") -> str:
        return ""


class GoogleTranslateBackend(TranslationBackendBase):
    name = "google"

    def __init__(self) -> None:
        self._ready = False
        self._translator_cls = None
        self._cache: dict[tuple[str, str, str], str] = {}
        try:
            from deep_translator import GoogleTranslator

            self._translator_cls = GoogleTranslator
            self._ready = True
        except Exception:
            self._ready = False

    def is_ready(self) -> bool:
        return bool(self._ready and self._translator_cls is not None)

    def translate(self, text: str, source_lang: str = "auto", target_lang: str = "tr") -> str:
        if not self.is_ready():
            return ""
        normalized = self.normalize_for_translation(text)
        if not normalized:
            return ""

        key = (source_lang, target_lang, normalized)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        try:
            translator = self._translator_cls(source=source_lang, target=target_lang)
            translated = translator.translate(normalized) or ""
            self._cache[key] = translated
            return translated
        except Exception:
            return ""


class ArgosTranslateBackend(TranslationBackendBase):
    """Offline translation via Argos Translate.

    Language packages must be installed before use.  Call
    ``install_package(from_code, to_code)`` once per language pair or use
    the in-app package manager dialog.

    If a direct model for the requested pair is unavailable, Argos attempts
    an automatic two-step pivot through English (e.g. ja→en→tr).
    """

    name = "argos"

    def __init__(self) -> None:
        self._ready = False
        self._cache: dict[tuple[str, str, str], str] = {}
        self._argos_translate = None
        self._argos_package = None
        try:
            import argostranslate.translate as _at
            import argostranslate.package as _ap

            self._argos_translate = _at
            self._argos_package = _ap
            self._ready = True
        except Exception:
            self._ready = False

    def is_ready(self) -> bool:
        return bool(self._ready)

    # ── Package helpers ────────────────────────────────────────────────────

    def available_packages(self) -> list:
        """Return list of all downloadable package objects (may be empty if
        index has never been fetched)."""
        if not self._argos_package:
            return []
        try:
            return list(self._argos_package.get_available_packages())
        except Exception:
            return []

    def installed_packages(self) -> list:
        if not self._argos_package:
            return []
        try:
            return list(self._argos_package.get_installed_packages())
        except Exception:
            return []

    def update_package_index(self) -> None:
        if self._argos_package:
            try:
                self._argos_package.update_package_index()
            except Exception:
                pass

    def install_package(self, from_code: str, to_code: str) -> bool:
        """Download and install the language package for from_code→to_code.
        Returns True on success."""
        if not self._argos_package:
            return False
        try:
            available = self.available_packages()
            pkg = next(
                (p for p in available if p.from_code == from_code and p.to_code == to_code),
                None,
            )
            if pkg is None:
                return False
            path = pkg.download()
            self._argos_package.install_from_path(path)
            return True
        except Exception:
            return False

    def is_pair_installed(self, from_code: str, to_code: str) -> bool:
        installed = self.installed_packages()
        return any(
            p.from_code == from_code and p.to_code == to_code for p in installed
        )

    def get_installed_codes(self) -> list[tuple[str, str]]:
        return [(p.from_code, p.to_code) for p in self.installed_packages()]

    # ── Translation ────────────────────────────────────────────────────────

    def translate(self, text: str, source_lang: str = "auto", target_lang: str = "tr") -> str:
        if not self.is_ready() or not self._argos_translate:
            return ""
        normalized = self.normalize_for_translation(text)
        if not normalized:
            return ""

        src = source_lang if source_lang != "auto" else "ja"  # best-effort default
        key = (src, target_lang, normalized)
        if key in self._cache:
            return self._cache[key]

        try:
            installed = self._argos_translate.get_installed_languages()
            from_lang_obj = next((l for l in installed if l.code == src), None)
            to_lang_obj = next((l for l in installed if l.code == target_lang), None)

            if from_lang_obj and to_lang_obj:
                trans = from_lang_obj.get_translation(to_lang_obj)
                if trans:
                    result = trans.translate(normalized) or ""
                    self._cache[key] = result
                    return result

            # Pivot via English if direct path is unavailable
            en_obj = next((l for l in installed if l.code == "en"), None)
            if en_obj and from_lang_obj:
                t1 = from_lang_obj.get_translation(en_obj)
                if to_lang_obj and en_obj:
                    t2 = en_obj.get_translation(to_lang_obj)
                    if t1 and t2:
                        mid = t1.translate(normalized) or ""
                        result = t2.translate(mid) or "" if mid else ""
                        self._cache[key] = result
                        return result
        except Exception:
            pass
        return ""


def build_translation_backend(preference: str = "google") -> TranslationBackendBase:
    if preference == "argos":
        argos = ArgosTranslateBackend()
        if argos.is_ready():
            return argos
    google = GoogleTranslateBackend()
    if google.is_ready():
        return google
    return TranslationBackendBase()


# Source-language choices shown in the UI
TRANSLATION_SOURCE_LANGUAGES = [
    ("Auto-detect", "auto"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Chinese (Simplified)", "zh"),
    ("Chinese (Traditional)", "zh-tw"),
    ("English", "en"),
    ("Russian", "ru"),
    ("German", "de"),
    ("French", "fr"),
    ("Spanish", "es"),
]

TRANSLATION_BACKENDS = [
    ("Google Translate (Online)", "google"),
    ("Argos Translate (Offline)", "argos"),
]


class ArgosPackageManagerDialog(QWidget):
    """Stand-alone window for downloading and managing Argos language packs.

    Argos Translate does not have direct models for every language pair.
    For example, Japanese→Turkish does not exist, so the app uses a two-step
    pivot: Japanese→English then English→Turkish.  This dialog shows clearly
    which packs are needed for the current language pair and lets the user
    install them in one click.
    """

    _STYLE = """
        QWidget { background: #0d1117; color: #c8d4de; font-size: 12px; }
        QGroupBox {
            border: 1px solid #1e2833; border-radius: 10px;
            margin-top: 14px; padding: 8px 10px 10px 10px; font-weight: 600;
        }
        QGroupBox::title {
            subcontrol-origin: margin; left: 10px; padding: 0 6px;
            color: #5a8fae; font-size: 11px;
        }
        QPushButton {
            padding: 5px 14px; background: #141c28; color: #a8b8c8;
            border: 1px solid #1e2c3c; border-radius: 7px; font-weight: 600;
        }
        QPushButton:hover { background: #1a2840; border-color: #2e4a6a; color: #c8dce8; }
        QPushButton:disabled { color: #364050; border-color: #141c24; background: #0d1117; }
        QPushButton#BtnGo {
            background: #14304a; color: #7ac4f0;
            border-color: #1e4a6c; font-weight: 700; padding: 6px 18px;
        }
        QPushButton#BtnGo:hover { background: #1a3e60; }
        QComboBox {
            padding: 4px 10px; background: #0f1720; color: #a8b8c8;
            border: 1px solid #1e2c3a; border-radius: 6px;
        }
        QLabel#Hint { color: #4a6a7a; font-size: 11px; }
        QLabel#Done { color: #4caf50; font-size: 11px; font-weight: 700; }
        QLabel#Warn { color: #e7b85c; font-size: 11px; }
        QLabel#Err  { color: #ef5350; font-size: 11px; }
    """

    def __init__(
        self,
        backend: ArgosTranslateBackend,
        source_lang: str = "auto",
        target_lang: str = "tr",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        self._source = source_lang if source_lang != "auto" else "ja"
        self._target = target_lang
        self._available: list = []

        self.setWindowTitle("Offline Language Packs — Argos Translate")
        apply_window_icon(self)
        self.setWindowFlags(Qt.WindowType.Window)
        self.setMinimumSize(640, 520)
        self.setStyleSheet(self._STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # ── Explanation banner ─────────────────────────────────────────────
        note = QLabel(
            "Argos Translate works entirely offline using downloaded model files.  "
            "Most language pairs route through English as an intermediate step, so "
            "you typically need to install <b>two packs</b>."
        )
        note.setObjectName("Hint")
        note.setWordWrap(True)
        root.addWidget(note)

        # ── Quick Setup ────────────────────────────────────────────────────
        qs_group = QGroupBox("Quick Setup")
        qs_layout = QVBoxLayout(qs_group)
        qs_layout.setSpacing(8)

        self._qs_rows: list[dict] = []  # [{from, to, status_lbl, install_btn}]
        self._qs_grid = QGridLayout()
        self._qs_grid.setColumnStretch(0, 0)
        self._qs_grid.setColumnStretch(1, 1)
        self._qs_grid.setColumnStretch(2, 0)
        qs_layout.addLayout(self._qs_grid)

        qs_btn_row = QHBoxLayout()
        self.btn_install_all = QPushButton("Download && Install All Required Packs")
        self.btn_install_all.setObjectName("BtnGo")
        self.btn_install_all.clicked.connect(self._install_all_required)
        qs_btn_row.addWidget(self.btn_install_all)
        qs_btn_row.addStretch()
        qs_layout.addLayout(qs_btn_row)

        self.qs_status = QLabel("")
        self.qs_status.setObjectName("Hint")
        self.qs_status.setWordWrap(True)
        qs_layout.addWidget(self.qs_status)
        root.addWidget(qs_group)

        # ── Installed list ─────────────────────────────────────────────────
        inst_group = QGroupBox("All Installed Packs")
        inst_layout = QVBoxLayout(inst_group)
        self.installed_label = QLabel("None installed yet.")
        self.installed_label.setObjectName("Hint")
        self.installed_label.setWordWrap(True)
        inst_layout.addWidget(self.installed_label)
        root.addWidget(inst_group)

        # ── Browse / manual install ────────────────────────────────────────
        browse_group = QGroupBox("Browse All Packs (Manual)")
        browse_layout = QVBoxLayout(browse_group)
        browse_layout.setSpacing(8)

        fetch_row = QHBoxLayout()
        fetch_row.setSpacing(8)
        self.btn_fetch = QPushButton("Fetch Package Index")
        self.btn_fetch.clicked.connect(self._fetch_index)
        fetch_row.addWidget(self.btn_fetch)
        fetch_row.addStretch()
        browse_layout.addLayout(fetch_row)

        sel_row = QHBoxLayout()
        sel_row.setSpacing(8)
        _fl = QLabel("From")
        _fl.setObjectName("Hint")
        _fl.setFixedWidth(36)
        self.from_combo = QComboBox()
        _tl = QLabel("To")
        _tl.setObjectName("Hint")
        _tl.setFixedWidth(24)
        self.to_combo = QComboBox()
        self.btn_dl_one = QPushButton("Download && Install")
        self.btn_dl_one.setEnabled(False)
        self.btn_dl_one.clicked.connect(self._install_one)
        sel_row.addWidget(_fl)
        sel_row.addWidget(self.from_combo, stretch=1)
        sel_row.addWidget(_tl)
        sel_row.addWidget(self.to_combo, stretch=1)
        sel_row.addWidget(self.btn_dl_one)
        browse_layout.addLayout(sel_row)

        self.browse_status = QLabel("")
        self.browse_status.setObjectName("Hint")
        self.browse_status.setWordWrap(True)
        browse_layout.addWidget(self.browse_status)
        root.addWidget(browse_group)

        root.addStretch()

        # Build quick-setup rows and refresh installed list
        self._build_qs_rows()
        self._refresh_installed()

    # ── Required pairs for the current source→target setting ──────────────

    def _required_pairs(self) -> list[tuple[str, str]]:
        """Return the ordered list of (from, to) packs needed.
        If a direct model exists in installed packs, use that; otherwise pivot
        through English."""
        direct = (self._source, self._target)
        installed_set = set(self._backend.get_installed_codes())
        # Check available index for direct path (only if index was fetched)
        avail_set = {(p.from_code, p.to_code) for p in self._available}
        if avail_set and direct in avail_set:
            return [direct]
        # Use pivot via English
        pairs: list[tuple[str, str]] = []
        if self._source != "en":
            pairs.append((self._source, "en"))
        if self._target != "en":
            pairs.append(("en", self._target))
        return pairs

    def _build_qs_rows(self) -> None:
        """Populate the quick-setup grid with one row per required pack."""
        # Clear old rows
        while self._qs_grid.count():
            item = self._qs_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._qs_rows.clear()

        pairs = self._required_pairs()
        installed_set = set(self._backend.get_installed_codes())

        for row_idx, (fc, tc) in enumerate(pairs):
            pack_lbl = QLabel(f"{fc}  →  {tc}")
            pack_lbl.setStyleSheet("font-weight: 600; color: #a8b8c8;")
            is_ok = (fc, tc) in installed_set
            status_lbl = QLabel("Installed" if is_ok else "Not installed")
            status_lbl.setObjectName("Done" if is_ok else "Warn")
            install_btn = QPushButton("Install" if not is_ok else "Re-install")
            install_btn.setEnabled(True)
            install_btn.setFixedWidth(88)
            _fc, _tc = fc, tc  # capture for lambda
            install_btn.clicked.connect(lambda _, f=_fc, t=_tc: self._install_single(f, t))
            self._qs_grid.addWidget(pack_lbl, row_idx, 0)
            self._qs_grid.addWidget(status_lbl, row_idx, 1)
            self._qs_grid.addWidget(install_btn, row_idx, 2)
            self._qs_rows.append({"from": fc, "to": tc, "status": status_lbl, "btn": install_btn})

        all_ok = all((r["from"], r["to"]) in installed_set for r in self._qs_rows)
        if all_ok and self._qs_rows:
            self.btn_install_all.setText("All Required Packs Installed")
            self.btn_install_all.setEnabled(False)
        else:
            self.btn_install_all.setText("Download && Install All Required Packs")
            self.btn_install_all.setEnabled(True)

    def _refresh_installed(self) -> None:
        pairs = self._backend.get_installed_codes()
        if pairs:
            self.installed_label.setText(
                "    ".join(f"{a} → {b}" for a, b in sorted(pairs))
            )
            self.installed_label.setObjectName("Done")
        else:
            self.installed_label.setText("None installed yet.")
            self.installed_label.setObjectName("Hint")

    # ── Install helpers ────────────────────────────────────────────────────

    def _install_single(self, fc: str, tc: str) -> None:
        self.qs_status.setText(f"Downloading {fc} → {tc}… (may take a minute)")
        QApplication.processEvents()
        ok = self._backend.install_package(fc, tc)
        if ok:
            self.qs_status.setText(f"Installed {fc} → {tc}.")
        else:
            self.qs_status.setText(f"Failed: {fc} → {tc}. Check internet or try fetching index first.")
        self._build_qs_rows()
        self._refresh_installed()

    def _install_all_required(self) -> None:
        pairs = self._required_pairs()
        installed_set = set(self._backend.get_installed_codes())
        needed = [(f, t) for f, t in pairs if (f, t) not in installed_set]
        if not needed:
            self.qs_status.setText("All required packs are already installed.")
            return
        for fc, tc in needed:
            self.qs_status.setText(f"Downloading {fc} → {tc}… ({needed.index((fc,tc))+1}/{len(needed)})")
            QApplication.processEvents()
            ok = self._backend.install_package(fc, tc)
            if not ok:
                self.qs_status.setText(
                    f"Failed to install {fc} → {tc}. Try fetching the index below first."
                )
                self._build_qs_rows()
                self._refresh_installed()
                return
        self.qs_status.setText("All required packs installed. Ready to translate offline!")
        self._build_qs_rows()
        self._refresh_installed()

    # ── Browse / manual ────────────────────────────────────────────────────

    def _fetch_index(self) -> None:
        self.browse_status.setText("Fetching package index from Argos servers…")
        QApplication.processEvents()
        self._backend.update_package_index()
        self._available = self._backend.available_packages()
        if not self._available:
            self.browse_status.setText("Could not fetch index. Check your internet connection.")
            return
        from_codes = sorted({p.from_code for p in self._available})
        to_codes = sorted({p.to_code for p in self._available})
        self.from_combo.clear()
        self.to_combo.clear()
        for c in from_codes:
            self.from_combo.addItem(c, c)
        for c in to_codes:
            self.to_combo.addItem(c, c)
        self.btn_dl_one.setEnabled(True)
        self.browse_status.setText(f"Found {len(self._available)} packs. Select a pair and click Download.")
        self._build_qs_rows()  # Re-check now we know what's available

    def _install_one(self) -> None:
        fc = self.from_combo.currentData() or ""
        tc = self.to_combo.currentData() or ""
        if not fc or not tc:
            return
        self.browse_status.setText(f"Downloading {fc} → {tc}…")
        QApplication.processEvents()
        ok = self._backend.install_package(fc, tc)
        if ok:
            self.browse_status.setText(f"Installed: {fc} → {tc}")
            self._build_qs_rows()
            self._refresh_installed()
        else:
            self.browse_status.setText(f"Failed: {fc} → {tc}. Try fetching the index again.")


class SliderField(QWidget):
    valueChanged = pyqtSignal(int)

    def __init__(self, minimum: int, maximum: int, value: int, parent=None) -> None:
        super().__init__(parent)
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(int(minimum), int(maximum))
        self._value_label = QLabel()
        self._value_label.setFixedWidth(36)
        self._value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._slider, stretch=1)
        layout.addWidget(self._value_label)

        self._slider.valueChanged.connect(self._on_slider_changed)
        self.setValue(int(value))

    def _on_slider_changed(self, value: int) -> None:
        self._value_label.setText(str(int(value)))
        self.valueChanged.emit(int(value))

    def setRange(self, minimum: int, maximum: int) -> None:
        self._slider.setRange(int(minimum), int(maximum))

    def setValue(self, value: int) -> None:
        self._slider.setValue(int(value))
        self._value_label.setText(str(int(self._slider.value())))

    def value(self) -> int:
        return int(self._slider.value())

    def blockSignals(self, block: bool) -> bool:  # noqa: N802
        self._slider.blockSignals(block)
        return super().blockSignals(block)


class HSVRangeEditor(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(6)
        layout.setVerticalSpacing(4)

        self.h_min = SliderField(0, 179, 0)
        self.h_max = SliderField(0, 179, 179)
        self.s_min = SliderField(0, 255, 0)
        self.s_max = SliderField(0, 255, 255)
        self.v_min = SliderField(0, 255, 0)
        self.v_max = SliderField(0, 255, 255)

        layout.addWidget(QLabel("H min"), 0, 0)
        layout.addWidget(self.h_min, 0, 1)
        layout.addWidget(QLabel("H max"), 0, 2)
        layout.addWidget(self.h_max, 0, 3)
        layout.addWidget(QLabel("S min"), 1, 0)
        layout.addWidget(self.s_min, 1, 1)
        layout.addWidget(QLabel("S max"), 1, 2)
        layout.addWidget(self.s_max, 1, 3)
        layout.addWidget(QLabel("V min"), 2, 0)
        layout.addWidget(self.v_min, 2, 1)
        layout.addWidget(QLabel("V max"), 2, 2)
        layout.addWidget(self.v_max, 2, 3)

    def widgets(self) -> list[SliderField]:
        return [self.h_min, self.h_max, self.s_min, self.s_max, self.v_min, self.v_max]

    def set_values(
        self,
        h_min: int,
        h_max: int,
        s_min: int,
        s_max: int,
        v_min: int,
        v_max: int,
    ) -> None:
        self.h_min.setValue(int(h_min))
        self.h_max.setValue(int(h_max))
        self.s_min.setValue(int(s_min))
        self.s_max.setValue(int(s_max))
        self.v_min.setValue(int(v_min))
        self.v_max.setValue(int(v_max))

    def get_values(self) -> dict[str, int]:
        return {
            "h_min": int(self.h_min.value()),
            "h_max": int(self.h_max.value()),
            "s_min": int(self.s_min.value()),
            "s_max": int(self.s_max.value()),
            "v_min": int(self.v_min.value()),
            "v_max": int(self.v_max.value()),
        }


class ColorPickerWidget(QWidget):
    """User-friendly color selector that translates a picked RGB color into
    OpenCV HSV range parameters automatically.  The internal HSVRangeEditor
    remains the authoritative data-source; this widget is a convenience INPUT
    layer on top of it."""

    colorChanged = pyqtSignal()

    # (label, hue_radius, saturation_lower_bound_offset, value_lower_bound_offset)
    _TOLERANCES: list[tuple[str, int, int, int]] = [
        ("Narrow",  6,  30,  40),
        ("Normal",  14, 100, 100),
        ("Wide",    25, 160, 140),
        ("Maximum", 40, 220, 180),
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._color = QColor(220, 220, 220)
        self._updating = False

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.swatch_btn = QPushButton()
        self.swatch_btn.setFixedSize(44, 28)
        self.swatch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.swatch_btn.setToolTip("Click to pick a color")
        self.swatch_btn.clicked.connect(self._open_picker)

        self.hex_label = QLabel("#DCDCDC")
        self.hex_label.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 11px; color: #8a96a4;"
        )

        range_label = QLabel("Range:")
        range_label.setStyleSheet("color: #6a7a8a; font-size: 11px;")

        self.tolerance_combo = QComboBox()
        self.tolerance_combo.setFixedWidth(82)
        for name, *_ in self._TOLERANCES:
            self.tolerance_combo.addItem(name, name)
        self.tolerance_combo.setCurrentIndex(1)  # Normal default
        self.tolerance_combo.currentIndexChanged.connect(self._on_changed)

        row.addWidget(self.swatch_btn)
        row.addWidget(self.hex_label, stretch=1)
        row.addWidget(range_label)
        row.addWidget(self.tolerance_combo)

        self._update_swatch()

    # ── Private helpers ────────────────────────────────────────────────────

    def _open_picker(self) -> None:
        color = QColorDialog.getColor(self._color, self, "Pick a color for text detection")
        if color.isValid():
            self._color = color
            self._update_swatch()
            self._on_changed()

    def _update_swatch(self) -> None:
        r, g, b = self._color.red(), self._color.green(), self._color.blue()
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        text_col = "#111" if lum > 140 else "#eee"
        self.swatch_btn.setStyleSheet(
            f"background: rgb({r},{g},{b}); border: 2px solid #3a4a5c; "
            f"border-radius: 8px; color: {text_col}; font-size: 10px;"
        )
        self.hex_label.setText(f"#{r:02X}{g:02X}{b:02X}")

    def _on_changed(self) -> None:
        if not self._updating:
            self.colorChanged.emit()

    def _tolerance_params(self) -> tuple[int, int, int]:
        key = self.tolerance_combo.currentData() or "Normal"
        for name, hue_r, sat_off, val_off in self._TOLERANCES:
            if name == key:
                return hue_r, sat_off, val_off
        return 14, 100, 100

    # ── Public API ─────────────────────────────────────────────────────────

    def get_hsv_range(self) -> dict[str, int]:
        """Return a dict with keys h_min/max, s_min/max, v_min/max in OpenCV
        scale (H 0-179, S 0-255, V 0-255) derived from the currently selected
        color and tolerance setting."""
        r = self._color.red() / 255.0
        g = self._color.green() / 255.0
        b = self._color.blue() / 255.0
        h, s, v = colorsys.rgb_to_hsv(r, g, b)

        h_ocv = int(round(h * 179))
        s_ocv = int(round(s * 255))
        v_ocv = int(round(v * 255))

        hue_r, sat_off, val_off = self._tolerance_params()

        # Achromatic detection: if saturation is very low, the hue is meaningless
        is_achromatic = s_ocv < 25

        if is_achromatic:
            h_min, h_max = 0, 179
            s_min = 0
            s_max = min(255, s_ocv + sat_off)
        else:
            h_min = max(0, h_ocv - hue_r)
            h_max = min(179, h_ocv + hue_r)
            s_min = max(0, s_ocv - sat_off)
            s_max = 255

        v_min = max(0, v_ocv - val_off)
        v_max = 255

        return {
            "h_min": h_min, "h_max": h_max,
            "s_min": s_min, "s_max": s_max,
            "v_min": v_min, "v_max": v_max,
        }

    def set_from_hsv_range(
        self,
        h_min: int, h_max: int,
        s_min: int, s_max: int,
        v_min: int, v_max: int,
    ) -> None:
        """Reconstruct an approximate representative display color from an HSV
        range.  Does NOT emit colorChanged (used for display sync only)."""
        self._updating = True
        try:
            full_hue_range = (h_max - h_min) >= 170
            if full_hue_range:
                # Achromatic range — display as a neutral gray based on V
                v_c = (v_min + v_max) / 2.0 / 255.0
                rf, gf, bf = colorsys.hsv_to_rgb(0.0, 0.0, v_c)
            else:
                h_c = (h_min + h_max) / 2.0 / 179.0
                s_c = (s_min + s_max) / 2.0 / 255.0
                v_c = (v_min + v_max) / 2.0 / 255.0
                rf, gf, bf = colorsys.hsv_to_rgb(h_c, s_c, v_c)
            self._color = QColor(int(rf * 255), int(gf * 255), int(bf * 255))
            self._update_swatch()
        finally:
            self._updating = False

    def get_color(self) -> QColor:
        return QColor(self._color)


class RegionSelector(QWidget):
    selected = pyqtSignal(QRect)
    canceled = pyqtSignal()

    def __init__(self, screen, title: str, background: QPixmap | None = None) -> None:
        super().__init__(None)
        self.screen = screen
        self._background = background
        self._start = None
        self._end = None
        self._rect = QRect()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)

        geo = screen.geometry()
        self.setGeometry(geo)

        self.help = QLabel(title, self)
        self.help.setStyleSheet(
            """
            QLabel {
                color: white;
                background: rgba(0, 0, 0, 190);
                padding: 8px 10px;
                border-radius: 8px;
                font-size: 12px;
            }
            """
        )
        self.help.move(20, 20)
        self.help.adjustSize()

        self.info = QLabel("", self)
        self.info.setStyleSheet(
            """
            QLabel {
                color: white;
                background: rgba(0, 0, 0, 180);
                padding: 6px 8px;
                border-radius: 6px;
                font-size: 11px;
            }
            """
        )
        self.info.move(20, 58)
        self.info.hide()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.activateWindow()
        self.raise_()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.canceled.emit()
            self.close()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if is_valid_region(self._rect):
                self.selected.emit(self._rect)
            else:
                self.canceled.emit()
            self.close()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = event.position().toPoint()
            self._end = self._start
            self._update_rect()
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._start is not None:
            self._end = event.position().toPoint()
            self._update_rect()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._start is not None:
            self._end = event.position().toPoint()
            self._update_rect()
            self.update()

    def _update_rect(self) -> None:
        if self._start is None or self._end is None:
            self._rect = QRect()
            self.info.hide()
            return
        x1, y1 = self._start.x(), self._start.y()
        x2, y2 = self._end.x(), self._end.y()
        left, right = sorted([x1, x2])
        top, bottom = sorted([y1, y2])
        self._rect = QRect(left, top, right - left, bottom - top)
        self.info.setText(
            f"x={self._rect.x()} y={self._rect.y()}  w={self._rect.width()} h={self._rect.height()}"
        )
        self.info.adjustSize()
        self.info.show()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)

        if self._background:
            painter.drawPixmap(self.rect(), self._background)

        painter.fillRect(self.rect(), QColor(0, 0, 0, 120))

        if not self._rect.isNull():
            painter.fillRect(self._rect, QColor(0, 0, 0, 20))
            pen = QPen(QColor(0, 200, 255, 255))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(self._rect)


class OutputWindow(QWidget):
    quickCaptureRequested = pyqtSignal()
    settingsChanged = pyqtSignal()

    def __init__(self, cfg: dict | None = None) -> None:
        super().__init__()
        self._cfg = cfg or {}
        self._out_cfg = self._cfg.get("output_window", {})
        self.setWindowTitle("Syncra Output")
        apply_window_icon(self)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumSize(500, 350)
        self._last_payload: tuple[str, str, str, str, str, str] | None = None
        self._zoom_level = self._out_cfg.get("font_size_dialogue", 17)

        _w = self._out_cfg.get("width", 1100)
        _h = self._out_cfg.get("height", 750)
        self.resize(_w, _h)

        self.setMouseTracking(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────────
        header = QFrame()
        header.setObjectName("OutputHeader")
        header.setFixedHeight(56)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 0, 16, 0)
        header_layout.setSpacing(12)

        self.output_lang_badge = QLabel("TR")
        self.output_lang_badge.setObjectName("OutputLangBadge")
        header_layout.addWidget(self.output_lang_badge)

        title = QLabel("SYNCRA")
        title.setObjectName("OutputBrand")
        header_layout.addWidget(title)

        header_layout.addStretch()

        self.output_status_badge = QLabel("Idle")
        self.output_status_badge.setObjectName("OutputStatusBadge")
        header_layout.addWidget(self.output_status_badge)

        self.output_hotkey_badge = QLabel("Ctrl+Shift+Q")
        self.output_hotkey_badge.setObjectName("OutputHotkeyBadge")
        header_layout.addWidget(self.output_hotkey_badge)

        self.quick_capture_button = QPushButton("Capture")
        self.quick_capture_button.setObjectName("OutputCaptureBtn")
        self.quick_capture_button.clicked.connect(self.quickCaptureRequested.emit)
        header_layout.addWidget(self.quick_capture_button)

        root.addWidget(header)

        # ── Control Bar ─────────────────────────────────────────────────
        ctrl_bar = QFrame()
        ctrl_bar.setObjectName("OutputCtrlBar")
        ctrl_bar.setFixedHeight(32)
        ctrl_layout = QHBoxLayout(ctrl_bar)
        ctrl_layout.setContentsMargins(16, 0, 16, 0)
        ctrl_layout.setSpacing(8)

        self.btn_on_top = QPushButton("On Top")
        self.btn_on_top.setObjectName("OutputCtrlBtn")
        self.btn_on_top.setCheckable(True)
        self.btn_on_top.setChecked(self._out_cfg.get("always_on_top", True))
        self.btn_on_top.setFixedHeight(22)
        self.btn_on_top.clicked.connect(self._toggle_on_top)
        ctrl_layout.addWidget(self.btn_on_top)

        _font_lbl = QLabel("Font:")
        _font_lbl.setObjectName("OutputCtrlLabel")
        ctrl_layout.addWidget(_font_lbl)
        self.font_spin = QSpinBox()
        self.font_spin.setObjectName("OutputFontSpin")
        self.font_spin.setRange(8, 36)
        self.font_spin.setValue(self._zoom_level)
        self.font_spin.setFixedWidth(40)
        self.font_spin.valueChanged.connect(self._on_font_changed)
        ctrl_layout.addWidget(self.font_spin)

        ctrl_layout.addStretch()

        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setObjectName("OutputCtrlBtn")
        self.btn_clear.setFixedHeight(22)
        self.btn_clear.clicked.connect(self._clear_all)
        ctrl_layout.addWidget(self.btn_clear)

        root.addWidget(ctrl_bar)

        # ── Content Cards ───────────────────────────────────────────────
        content_widget = QWidget()
        content_widget.setObjectName("OutputContent")
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(12, 8, 12, 8)
        content_layout.setSpacing(8)

        _dfs = max(8, int(self._zoom_level) if self._zoom_level else 17)
        _dss = max(8, _dfs - 6)
        _mfs = max(8, _dfs - 4)
        _mss = max(8, _dfs - 7)

        (
            _dialogue_translation_card,
            self.dialogue_translated_label,
            self.dialogue_translated_text,
        ) = self._build_output_card(
            "Dialogue Translation",
            accent="#e7b85c",
            body_font=QFont("Segoe UI", _dfs),
            min_height=160,
            editor_object="OutputEditorTranslation",
        )
        (
            _dialogue_source_card,
            self.dialogue_source_label,
            self.dialogue_text,
        ) = self._build_output_card(
            "Dialogue OCR",
            accent="#4fc3f7",
            body_font=QFont("Consolas", _dss),
            min_height=160,
            editor_object="OutputEditorSource",
        )
        (
            _menu_translation_card,
            self.menu_translated_label,
            self.menu_translated_text,
        ) = self._build_output_card(
            "Menu Translation",
            accent="#9fd46d",
            body_font=QFont("Segoe UI", _mfs),
            min_height=120,
            editor_object="OutputEditorTranslation",
        )
        (
            _menu_source_card,
            self.menu_source_label,
            self.menu_text,
        ) = self._build_output_card(
            "Menu OCR",
            accent="#74d1c6",
            body_font=QFont("Consolas", _mss),
            min_height=120,
            editor_object="OutputEditorSource",
        )

        top_split = QSplitter(Qt.Orientation.Horizontal)
        top_split.addWidget(_dialogue_translation_card)
        top_split.addWidget(_dialogue_source_card)
        top_split.setStretchFactor(0, 5)
        top_split.setStretchFactor(1, 3)
        top_split.setChildrenCollapsible(False)

        bottom_split = QSplitter(Qt.Orientation.Horizontal)
        bottom_split.addWidget(_menu_translation_card)
        bottom_split.addWidget(_menu_source_card)
        bottom_split.setStretchFactor(0, 4)
        bottom_split.setStretchFactor(1, 3)
        bottom_split.setChildrenCollapsible(False)

        content_split = QSplitter(Qt.Orientation.Vertical)
        content_split.addWidget(top_split)
        content_split.addWidget(bottom_split)
        content_split.setStretchFactor(0, 3)
        content_split.setStretchFactor(1, 2)
        content_split.setChildrenCollapsible(False)

        content_layout.addWidget(content_split)
        root.addWidget(content_widget, stretch=1)

        # Apply initial opacity
        self.setWindowOpacity(self._out_cfg.get("opacity", 0.95))

        self.setObjectName("OutputRoot")
        self.setStyleSheet("""
            QWidget#OutputRoot { background: #0c0e14; color: #d8dce6; }
            QFrame#OutputHeader { background: #111318; border-bottom: 1px solid #1c2028; }
            QLabel#OutputLangBadge { background: #1a3a5c; color: #7ec8f0; border: 1px solid #2a5080; border-radius: 4px; padding: 3px 8px; font-size: 12px; font-weight: 700; }
            QLabel#OutputBrand { color: #e8ecf2; font-size: 16px; font-weight: 800; letter-spacing: 2px; }
            QLabel#OutputStatusBadge { background: #15181e; color: #8891a0; border: 1px solid #222830; border-radius: 4px; padding: 3px 10px; font-size: 11px; }
            QLabel#OutputHotkeyBadge { background: #15181e; color: #6b7580; border: 1px solid #1e242c; border-radius: 4px; padding: 3px 10px; font-size: 11px; }
            QPushButton#OutputCaptureBtn { background: #1a5276; color: #d0e8ff; border: 1px solid #2a7aaa; border-radius: 4px; padding: 4px 14px; font-weight: 700; }
            QPushButton#OutputCaptureBtn:hover { background: #21709a; }
            QFrame#OutputCtrlBar { background: #0e1016; border-bottom: 1px solid #1a1e26; }
            QPushButton#OutputCtrlBtn { background: #181c24; color: #a0a8b4; border: 1px solid #252a34; border-radius: 3px; padding: 2px 10px; font-size: 10px; }
            QPushButton#OutputCtrlBtn:hover { background: #222830; color: #c8d0dc; }
            QPushButton#OutputCtrlBtn:checked { background: #1a3a5c; color: #7ec8f0; border-color: #2a5080; }
            QLabel#OutputCtrlLabel { color: #606878; font-size: 10px; }
            QLabel#OutputCtrlHint { color: #404858; font-size: 9px; font-style: italic; }
            QFrame#OutputCard { background: #111318; border: 1px solid #1c2028; border-radius: 8px; }
            QFrame#OutputCard:hover { border-color: #283040; }
            QLabel#CardTitle { color: #c0c8d4; font-size: 11px; font-weight: 700; }
            QPlainTextEdit { background: transparent; border: none; selection-background-color: #2a4060; }
            QPlainTextEdit#OutputEditorTranslation { color: #f0ebe0; }
            QPlainTextEdit#OutputEditorSource { color: #8898a8; }
            QSplitter::handle { background: #181c24; }
            QScrollBar:vertical { background: transparent; width: 8px; }
            QScrollBar::handle:vertical { background: #282e38; min-height: 24px; border-radius: 4px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

    def move_to_screen(self, screen) -> None:
        geo = screen.geometry()
        self.setGeometry(geo)

    def _toggle_on_top(self) -> None:
        on_top = self.btn_on_top.isChecked()
        flags = self.windowFlags()
        if on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()
        self._out_cfg["always_on_top"] = on_top
        self._save_output_cfg()

    def _on_opacity_changed(self, value: int) -> None:
        self.opacity_label.setText(f"{value}%")
        self.setWindowOpacity(value / 100.0)
        self._out_cfg["opacity"] = value / 100.0
        self._save_output_cfg()

    def _on_font_changed(self, value: int) -> None:
        self._zoom_level = value
        self._apply_zoom()
        self.settingsChanged.emit()

    def _clear_all(self) -> None:
        self.dialogue_text.clear()
        self.dialogue_translated_text.clear()
        self.menu_text.clear()
        self.menu_translated_text.clear()
        self.output_status_badge.setText("Cleared")
        self._last_payload = None

    def _save_output_cfg(self) -> None:
        self._cfg["output_window"] = self._out_cfg

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta == 0:
                return super().wheelEvent(event)
            step = 1 if delta > 0 else -1
            new_size = max(8, min(36, self._zoom_level + step))
            if new_size != self._zoom_level:
                self._zoom_level = new_size
                self.font_spin.blockSignals(True)
                self.font_spin.setValue(new_size)
                self.font_spin.blockSignals(False)
                self._apply_zoom()
            event.accept()
        else:
            super().wheelEvent(event)

    def _apply_zoom(self) -> None:
        fs = max(8, int(self._zoom_level) if self._zoom_level else 17)
        for editor in (self.dialogue_translated_text, self.menu_translated_text):
            editor.setFont(QFont("Segoe UI", fs))
        for editor in (self.dialogue_text, self.menu_text):
            editor.setFont(QFont("Consolas", max(8, fs - 6)))
        self._out_cfg["font_size_dialogue"] = fs
        self._out_cfg["font_size_menu"] = max(8, fs - 4)
        self._save_output_cfg()

    def set_hotkey_text(self, value: str) -> None:
        self.output_hotkey_badge.setText(value)

    def closeEvent(self, event) -> None:
        if self._out_cfg.get("save_position", True):
            geo = self.geometry()
            self._out_cfg["x"] = geo.x()
            self._out_cfg["y"] = geo.y()
            self._out_cfg["width"] = geo.width()
            self._out_cfg["height"] = geo.height()
            self._save_output_cfg()
        super().closeEvent(event)

    def _build_output_card(
        self,
        title_text: str,
        accent: str,
        body_font: QFont,
        min_height: int,
        editor_object: str,
    ) -> tuple[QFrame, QLabel, QPlainTextEdit]:
        card = QFrame()
        card.setObjectName("OutputCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(4)

        card_header = QHBoxLayout()
        card_header.setSpacing(6)
        accent_bar = QFrame()
        accent_bar.setFixedWidth(3)
        accent_bar.setStyleSheet(f"background: {accent}; border-radius: 1px;")
        card_header.addWidget(accent_bar)
        title = QLabel(title_text)
        title.setObjectName("CardTitle")
        card_header.addWidget(title)
        card_header.addStretch()

        editor = QPlainTextEdit()
        editor.setObjectName(editor_object)
        editor.setReadOnly(True)
        editor.setPlaceholderText("Waiting for content...")
        editor.setFont(body_font)
        editor.setMinimumHeight(min_height)
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        card_layout.addLayout(card_header)
        card_layout.addWidget(editor, stretch=1)
        return card, title, editor

    def update_text(
        self,
        dialogue: str,
        menu: str,
        dialogue_translated: str,
        menu_translated: str,
        target_lang: str,
        status_override: str | None = None,
    ) -> None:
        lang_label = (target_lang or "tr").upper()
        payload = (
            dialogue or "",
            menu or "",
            dialogue_translated or "",
            menu_translated or "",
            lang_label,
            status_override or "",
        )
        if self._last_payload == payload:
            return

        self._last_payload = payload
        timestamp = QDateTime.currentDateTime().toString("HH:mm:ss")
        if status_override:
            status_text = status_override
        elif dialogue_translated or menu_translated:
            status_text = "Translated"
        elif dialogue or menu:
            status_text = "OCR Only"
        else:
            status_text = "Waiting"

        self.output_lang_badge.setText(lang_label)
        self.output_status_badge.setText(f"{status_text}  {timestamp}")
        set_plain_text_if_changed(self.dialogue_text, dialogue)
        set_plain_text_if_changed(self.menu_text, menu)
        set_plain_text_if_changed(self.dialogue_translated_text, dialogue_translated)
        set_plain_text_if_changed(self.menu_translated_text, menu_translated)


class QuickTranslateWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Syncra Quick Translate")
        apply_window_icon(self)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumSize(760, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        header = QHBoxLayout()
        header_text = QVBoxLayout()
        header_text.setSpacing(2)
        title = QLabel("Quick Capture Translate")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #fff3df;")
        subtitle = QLabel("Press the hotkey, draw a region, get one-shot OCR and translation.")
        subtitle.setStyleSheet("font-size: 11px; color: #9eabb7;")
        header_text.addWidget(title)
        header_text.addWidget(subtitle)

        self.hotkey_badge = QLabel("Ctrl+Shift + Q")
        self.hotkey_badge.setStyleSheet(
            "QLabel { background: #203447; color: #dceefe; border: 1px solid #35516b; "
            "border-radius: 12px; padding: 6px 12px; font-weight: 700; }"
        )
        self.status_badge = QLabel("Idle")
        self.status_badge.setStyleSheet(
            "QLabel { background: #1d242d; color: #d5dde5; border: 1px solid #303944; "
            "border-radius: 12px; padding: 6px 12px; font-weight: 600; }"
        )

        header.addLayout(header_text, stretch=1)
        header.addWidget(self.hotkey_badge)
        header.addWidget(self.status_badge)

        self.translated_text = QPlainTextEdit()
        self.translated_text.setReadOnly(True)
        self.translated_text.setPlaceholderText("Translated text will appear here")
        self.translated_text.setFont(QFont("Segoe UI", 16))

        self.source_text = QPlainTextEdit()
        self.source_text.setReadOnly(True)
        self.source_text.setPlaceholderText("Source OCR will appear here")
        self.source_text.setFont(QFont("Consolas", 10))

        root.addLayout(header)
        root.addWidget(QLabel("Translation"), stretch=0)
        root.addWidget(self.translated_text, stretch=3)
        root.addWidget(QLabel("Source OCR"), stretch=0)
        root.addWidget(self.source_text, stretch=2)

        self.setStyleSheet(
            """
            QWidget {
                background: #10141a;
                color: #eef2f7;
            }
            QPlainTextEdit {
                background: #151b23;
                border: 1px solid #2b3541;
                border-radius: 14px;
                padding: 12px;
            }
            """
        )

    def move_to_screen(self, screen) -> None:
        geo = screen.geometry()
        width = max(520, min(920, geo.width() - 80))
        height = max(320, min(620, geo.height() - 80))
        x = geo.x() + max(20, (geo.width() - width) // 2)
        y = geo.y() + max(20, (geo.height() - height) // 2)
        self.setGeometry(x, y, width, height)

    def set_hotkey_text(self, value: str) -> None:
        self.hotkey_badge.setText(value)

    def set_status(self, value: str) -> None:
        self.status_badge.setText(value)

    def update_text(self, source_text: str, translated_text: str, target_lang: str) -> None:
        label = (target_lang or "tr").upper()
        self.set_status(f"Ready {QDateTime.currentDateTime().toString('HH:mm:ss')}")
        set_plain_text_if_changed(self.source_text, source_text)
        set_plain_text_if_changed(self.translated_text, translated_text or source_text)
        self.translated_text.setPlaceholderText(f"{label} translation will appear here")


class WorkerSignals(QObject):
    done = pyqtSignal(str, str, str, str, str)


class OCRWorker(QRunnable):
    def __init__(
        self,
        backend: OCRBackendBase,
        translator_backend: TranslationBackendBase,
        dialogue_image,
        menu_image,
        translate_enabled: bool,
        target_lang: str,
        source_lang: str = "auto",
    ):
        super().__init__()
        self.setAutoDelete(False)
        self.backend = backend
        self.translator_backend = translator_backend
        self.dialogue_image = dialogue_image
        self.menu_image = menu_image
        self.translate_enabled = translate_enabled
        self.target_lang = target_lang
        self.source_lang = source_lang
        self.signals = WorkerSignals()

    def run(self) -> None:
        dialogue_text = ""
        menu_text = ""
        dialogue_translated = ""
        menu_translated = ""
        error_text = ""
        try:
            if self.dialogue_image is not None:
                dialogue_text = self.backend.recognize(self.dialogue_image)
            if self.menu_image is not None:
                menu_text = self.backend.recognize(self.menu_image)

            if self.translate_enabled and self.translator_backend.is_ready():
                if dialogue_text:
                    dialogue_translated = self.translator_backend.translate(
                        dialogue_text, source_lang=self.source_lang, target_lang=self.target_lang
                    )
                if menu_text:
                    menu_translated = self.translator_backend.translate(
                        menu_text, source_lang=self.source_lang, target_lang=self.target_lang
                    )
        except Exception:
            error_text = traceback.format_exc(limit=1)
        self.signals.done.emit(
            dialogue_text,
            menu_text,
            dialogue_translated,
            menu_translated,
            error_text,
        )


class AutoAdjustSignals(QObject):
    done = pyqtSignal(bool, str)


class AutoAdjustWorker(QRunnable):
    def __init__(self, main_window, rgb_frame):
        super().__init__()
        self.setAutoDelete(False)
        self._mw = main_window
        self._frame = rgb_frame
        self.signals = AutoAdjustSignals()

    def run(self) -> None:
        try:
            success, message = self._mw._auto_adjust_filter(self._frame)
            self.signals.done.emit(success, message)
        except Exception as e:
            self.signals.done.emit(False, f"Error: {e}")


class QuickCaptureSignals(QObject):
    done = pyqtSignal(str, str, str)


class QuickCaptureWorker(QRunnable):
    def __init__(
        self,
        backend: OCRBackendBase,
        translator_backend: TranslationBackendBase,
        images: list[Any],
        translate_enabled: bool,
        target_lang: str,
        source_lang: str = "auto",
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.backend = backend
        self.translator_backend = translator_backend
        self.images = images
        self.translate_enabled = translate_enabled
        self.target_lang = target_lang
        self.source_lang = source_lang
        self.signals = QuickCaptureSignals()

    def run(self) -> None:
        source_text = ""
        translated_text = ""
        error_text = ""
        try:
            best_score = -1
            for image in self.images:
                if image is None:
                    continue
                candidate = self.backend.recognize(image).strip()
                candidate_score = ocr_text_score(candidate)
                if candidate_score > best_score:
                    source_text = candidate
                    best_score = candidate_score
            if self.translate_enabled and source_text and self.translator_backend.is_ready():
                translated_text = self.translator_backend.translate(
                    source_text,
                    source_lang=self.source_lang,
                    target_lang=self.target_lang,
                )
        except Exception:
            error_text = traceback.format_exc(limit=1)
        self.signals.done.emit(source_text, translated_text, error_text)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        apply_window_icon(self)
        self.resize(1100, 720)
        self.setMinimumSize(900, 600)

        self.cfg = load_config()

        current_theme = self.cfg.get("theme", "dark")
        self.setStyleSheet(build_theme_stylesheet(current_theme))

        self.screens = []
        self.backend = build_backend()
        _trans_pref = str(self.cfg.get("translation_backend", "google"))
        self.translation_backend = build_translation_backend(preference=_trans_pref)
        self._argos_backend: ArgosTranslateBackend | None = None
        self._argos_pack_manager: ArgosPackageManagerDialog | None = None
        self.output_window = OutputWindow(self.cfg)
        self.quick_translate_window = QuickTranslateWindow()
        self.output_window.quickCaptureRequested.connect(self.start_quick_capture_selection)
        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(2)
        self._ocr_running = False
        self._test_mode = False
        self._quick_capture_running = False
        self._quick_capture_resume_live = False
        self._capture = mss.mss() if mss else None
        self._selector = None
        self._quick_selector = None
        self._quick_hidden_windows: list[QWidget] = []
        self._active_workers = []
        self._current_ocr_worker: OCRWorker | None = None
        self._current_quick_capture_worker: QuickCaptureWorker | None = None
        self._is_closing = False
        self._last_dialogue_raw = None
        self._last_menu_raw = None
        self._profile_apply_in_progress = False
        self._global_hotkey_id = 0xA150
        self._global_hotkey_registered = False
        self._global_hotkey_hwnd = 0
        self._dialogue_gate = StableTextGate()
        self._menu_gate = StableTextGate()
        self._window_shortcuts: list[QShortcut] = []
        self._frame_count = 0
        self._frame_times: list[float] = []
        self._last_tick_time = 0.0
        self._capture_ms = 0.0
        self._ocr_ms = 0.0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        monitor_group = QGroupBox("Monitor and OCR Settings")
        self._grp_monitor = monitor_group
        monitor_form = QFormLayout(monitor_group)
        monitor_form.setHorizontalSpacing(10)
        monitor_form.setVerticalSpacing(4)

        self.capture_screen_combo = QComboBox()
        self.capture_screen_combo.currentIndexChanged.connect(self._on_capture_screen_changed)

        self.output_screen_combo = QComboBox()
        self.output_screen_combo.currentIndexChanged.connect(self._on_output_screen_changed)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(150, 10000)
        self.interval_spin.setSingleStep(50)
        self.interval_spin.setValue(int(self.cfg.get("ocr_interval_ms", 1200)))
        self.interval_spin.valueChanged.connect(self._on_interval_changed)

        self.translation_enabled_check = QCheckBox("Auto translate OCR text")
        self.translation_enabled_check.setChecked(bool(self.cfg.get("translation_enabled", True)))
        self.translation_enabled_check.toggled.connect(self._on_translation_enabled_changed)

        self.translation_target_combo = QComboBox()
        for label, code in TARGET_LANGUAGES:
            self.translation_target_combo.addItem(f"{label} ({code})", code)
        target_code = str(self.cfg.get("translation_target", "tr")).lower()
        self.translation_target_combo.blockSignals(True)
        self._set_translation_target(target_code)
        self.translation_target_combo.blockSignals(False)
        self.translation_target_combo.currentIndexChanged.connect(self._on_translation_target_changed)

        # Translation backend selector
        self.translation_backend_combo = QComboBox()
        for label, code in TRANSLATION_BACKENDS:
            self.translation_backend_combo.addItem(label, code)
        _cur_trans = str(self.cfg.get("translation_backend", "google"))
        _tb_idx = max(0, self.translation_backend_combo.findData(_cur_trans))
        self.translation_backend_combo.setCurrentIndex(_tb_idx)
        self.translation_backend_combo.currentIndexChanged.connect(self._on_translation_backend_changed)

        # Source language (used by Argos; Google always uses "auto")
        self.translation_source_combo = QComboBox()
        for label, code in TRANSLATION_SOURCE_LANGUAGES:
            self.translation_source_combo.addItem(label, code)
        _cur_src = str(self.cfg.get("translation_source", "auto"))
        _src_idx = max(0, self.translation_source_combo.findData(_cur_src))
        self.translation_source_combo.setCurrentIndex(_src_idx)
        self.translation_source_combo.currentIndexChanged.connect(self._on_translation_source_changed)

        # Argos pack manager button (only relevant when Argos is selected)
        self.btn_argos_packs = QPushButton("Manage Language Packs")
        self.btn_argos_packs.clicked.connect(self._open_argos_pack_manager)
        self.btn_argos_packs.setVisible(_cur_trans == "argos")

        # Theme settings
        theme_row = QHBoxLayout()
        theme_row.setSpacing(6)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Dark", "dark")
        self.theme_combo.addItem("Light", "light")
        current_theme = str(self.cfg.get("theme", "dark"))
        theme_idx = max(0, self.theme_combo.findData(current_theme))
        self.theme_combo.setCurrentIndex(theme_idx)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)

        self._lbl_theme = QLabel("Theme:")
        theme_row.addWidget(self._lbl_theme)
        theme_row.addWidget(self.theme_combo, stretch=1)
        theme_row.addStretch()

        # OCR Engine selection
        self.ocr_engine_combo = QComboBox()
        self.ocr_engine_combo.addItem("Auto", "auto")
        self.ocr_engine_combo.addItem("Windows OCR (WinRT)", "winrt")
        self.ocr_engine_combo.addItem("Tesseract", "tesseract")
        current_engine = str(self.cfg.get("ocr_engine", "winrt"))
        engine_idx = max(0, self.ocr_engine_combo.findData(current_engine))
        self.ocr_engine_combo.setCurrentIndex(engine_idx)
        self.ocr_engine_combo.currentIndexChanged.connect(self._on_ocr_engine_changed)

        # Preprocessing settings
        self.preproc_contrast_slider = SliderField(0.5, 2.0, float(self.cfg.get("ocr_preprocessing", {}).get("contrast", 1.2)))
        self.preproc_contrast_slider.setRange(50, 200)
        self.preproc_contrast_slider.setValue(int(float(self.cfg.get("ocr_preprocessing", {}).get("contrast", 1.2)) * 100))
        self.preproc_contrast_slider.valueChanged.connect(self._on_preprocessing_changed)

        self.preproc_sharpness_slider = SliderField(0.5, 2.0, float(self.cfg.get("ocr_preprocessing", {}).get("sharpness", 1.0)))
        self.preproc_sharpness_slider.setRange(50, 200)
        self.preproc_sharpness_slider.setValue(int(float(self.cfg.get("ocr_preprocessing", {}).get("sharpness", 1.0)) * 100))
        self.preproc_sharpness_slider.valueChanged.connect(self._on_preprocessing_changed)

        self.preproc_denoise_slider = SliderField(0, 10, int(self.cfg.get("ocr_preprocessing", {}).get("denoise", 0)))
        self.preproc_denoise_slider.valueChanged.connect(self._on_preprocessing_changed)

        self.preproc_auto_enhance_check = QCheckBox("Auto enhance")
        self.preproc_auto_enhance_check.setChecked(bool(self.cfg.get("ocr_preprocessing", {}).get("auto_enhance", True)))
        self.preproc_auto_enhance_check.toggled.connect(self._on_preprocessing_changed)

        self.backend_label = QLabel()
        self.translation_backend_label = QLabel()
        self.status_label = QLabel("Idle")

        self._form_labels = {}
        for key, text in [
            ("capture_screen", "Capture screen"),
            ("output_screen", "Output screen"),
            ("ocr_interval", "OCR interval (ms)"),
            ("theme_lang", "Theme"),
            ("ocr_engine", "OCR Engine"),
            ("preprocess_contrast", "Preprocess: Contrast"),
            ("preprocess_sharpness", "Preprocess: Sharpness"),
            ("preprocess_denoise", "Preprocess: Denoise"),
            ("auto_translate", "Auto translate"),
            ("target_language", "Target language"),
            ("translate_engine", "Translate engine"),
            ("source_language", "Source language"),
            ("ocr_backend", "OCR backend"),
            ("status", "Status"),
            ("invert", "Invert"),
            ("blur", "Blur"),
            ("threshold", "Threshold"),
            ("dilate", "Dilate"),
            ("stabilization", "Stabilization"),
            ("stable_frames", "Stable frames"),
            ("similarity", "Similarity"),
            ("quick_capture", "Quick capture"),
            ("hotkey", "Hotkey"),
            ("hotkey_status", "Hotkey status"),
            ("fps", "FPS"),
            ("avg_fps", "Avg FPS"),
            ("capture_time", "Capture (ms)"),
            ("ocr_time", "OCR (ms)"),
            ("total_time", "Total (ms)"),
            ("frame_count", "Frames"),
            ("performance", "Performance Analysis"),
            ("system_info", "System Info"),
            ("python", "Python"),
            ("os", "OS"),
            ("opencv_ver", "OpenCV"),
            ("numpy_ver", "NumPy"),
            ("pil_ver", "Pillow"),
            ("mss_ver", "mss"),
            ("tesseract_ver", "Tesseract"),
            ("winrt_ver", "WinRT"),
            ("active_backend", "Active Backend"),
            ("output_size", "Window Size"),
            ("output_opacity", "Opacity"),
            ("output_auto_show", "Auto Show"),
            ("output_save_pos", "Save Position"),
        ]:
            self._form_labels[key] = QLabel(text)

        monitor_form.addRow(self._form_labels["capture_screen"], self.capture_screen_combo)
        monitor_form.addRow(self._form_labels["output_screen"], self.output_screen_combo)
        monitor_form.addRow(self._form_labels["ocr_interval"], self.interval_spin)
        monitor_form.addRow(self._form_labels["status"], self.status_label)

        opencv_cfg = self.cfg.get("opencv", {})

        # ── Filter Settings Group ──────────────────────────────────────────
        filter_group = QWidget()
        filter_group.setObjectName("FilterPanel")
        filter_group_layout = QVBoxLayout(filter_group)
        filter_group_layout.setContentsMargins(14, 12, 14, 12)
        filter_group_layout.setSpacing(8)

        # OCR Mode toggle — Direct vs Filtered
        _ocr_mode_cfg = str(opencv_cfg.get("ocr_mode", "filtered"))
        _mode_row = QHBoxLayout()
        _mode_row.setSpacing(0)
        _mode_lbl = QLabel("OCR Mode")
        _mode_lbl.setObjectName("FieldLabel")
        _mode_lbl.setFixedWidth(72)
        self.btn_ocr_mode_direct = QPushButton("Direct")
        self.btn_ocr_mode_direct.setObjectName("OcrModeBtn")
        self.btn_ocr_mode_direct.setCheckable(True)
        self.btn_ocr_mode_direct.setFixedHeight(28)
        self.btn_ocr_mode_filtered = QPushButton("Filtered")
        self.btn_ocr_mode_filtered.setObjectName("OcrModeBtn")
        self.btn_ocr_mode_filtered.setCheckable(True)
        self.btn_ocr_mode_filtered.setFixedHeight(28)
        self.btn_ocr_mode_direct.setChecked(_ocr_mode_cfg == "direct")
        self.btn_ocr_mode_filtered.setChecked(_ocr_mode_cfg != "direct")
        self.btn_ocr_mode_direct.clicked.connect(lambda: self._set_ocr_mode("direct"))
        self.btn_ocr_mode_filtered.clicked.connect(lambda: self._set_ocr_mode("filtered"))
        _mode_row.addWidget(_mode_lbl)
        _mode_row.addWidget(self.btn_ocr_mode_direct)
        _mode_row.addWidget(self.btn_ocr_mode_filtered)
        _mode_row.addStretch()
        filter_group_layout.addLayout(_mode_row)

        # Status row
        cv_ok = cv2 is not None and np is not None
        cv_status_row = QHBoxLayout()
        cv_status_row.setSpacing(6)
        cv_dot = QLabel("●")
        cv_dot.setStyleSheet(
            "color: #4caf50; font-size: 12px;" if cv_ok else "color: #ef5350; font-size: 12px;"
        )
        self.cv_status_label = QLabel("OpenCV ready" if cv_ok else "OpenCV not available")
        self.cv_status_label.setObjectName("StatusLabel")
        cv_status_row.addWidget(cv_dot)
        cv_status_row.addWidget(self.cv_status_label, stretch=1)
        filter_group_layout.addLayout(cv_status_row)

        # ── Preset + Color Mode ────────────────────────────────────────────
        _sep0 = QFrame()
        _sep0.setFrameShape(QFrame.Shape.HLine)
        _sep0.setObjectName("Separator")
        filter_group_layout.addWidget(_sep0)

        # Preset row
        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        _preset_lbl = QLabel("Preset")
        _preset_lbl.setObjectName("FieldLabel")
        _preset_lbl.setFixedWidth(56)
        self.filter_preset_combo = QComboBox()
        self.filter_preset_combo.addItem("Wuwa Dialogue (Recommended)", "wuwa_dialogue")
        self.filter_preset_combo.addItem("Subtitle Auto (Recommended)", "subtitle_auto")
        self.filter_preset_combo.addItem("Anime RPG Dialogue", "anime_rpg")
        self.filter_preset_combo.addItem("White Text", "white_text")
        self.filter_preset_combo.addItem("White + Yellow Text", "white_yellow")
        self.filter_preset_combo.addItem("High Contrast", "high_contrast")
        self.filter_preset_combo.addItem("Custom / Saved", "custom")
        preset_from_cfg = str(opencv_cfg.get("preset", "subtitle_auto"))
        preset_idx = max(0, self.filter_preset_combo.findData(preset_from_cfg))
        self.filter_preset_combo.setCurrentIndex(preset_idx)
        self.btn_apply_preset = QPushButton("Apply")
        self.btn_apply_preset.setObjectName("BtnAccent")
        self.btn_apply_preset.setFixedWidth(64)
        self.btn_apply_preset.clicked.connect(self.apply_filter_preset)
        preset_row.addWidget(_preset_lbl)
        preset_row.addWidget(self.filter_preset_combo, stretch=1)
        preset_row.addWidget(self.btn_apply_preset)
        filter_group_layout.addLayout(preset_row)

        # Color mode row
        _color_row = QHBoxLayout()
        _color_row.setSpacing(6)
        _color_lbl = QLabel("Color")
        _color_lbl.setObjectName("FieldLabel")
        _color_lbl.setFixedWidth(56)
        self.filter_color_combo = QComboBox()
        for key, label in COLOR_MODES.items():
            self.filter_color_combo.addItem(label, key)
        color_from_cfg = str(opencv_cfg.get("color_mode", "gray"))
        color_idx = max(0, self.filter_color_combo.findData(color_from_cfg))
        self.filter_color_combo.setCurrentIndex(color_idx)
        self.filter_color_combo.currentIndexChanged.connect(self._on_color_mode_changed)
        _color_row.addWidget(_color_lbl)
        _color_row.addWidget(self.filter_color_combo, stretch=1)
        filter_group_layout.addLayout(_color_row)

        # ── Toggles ────────────────────────────────────────────────────────
        self.filter_enabled_check = QCheckBox("Enable filter")
        self.filter_enabled_check.setChecked(bool(opencv_cfg.get("enabled", True)))
        self.live_preview_check = QCheckBox("Live preview")
        self.live_preview_check.setChecked(bool(opencv_cfg.get("live_preview", True)))
        _toggles_row = QHBoxLayout()
        _toggles_row.setSpacing(16)
        _toggles_row.addWidget(self.filter_enabled_check)
        _toggles_row.addWidget(self.live_preview_check)
        _toggles_row.addStretch()
        filter_group_layout.addLayout(_toggles_row)

        # ── Advanced toggle ────────────────────────────────────────────────
        self.show_advanced_check = QCheckBox("Show advanced settings")
        self.show_advanced_check.setChecked(bool(opencv_cfg.get("show_advanced", False)))
        self.show_advanced_check.setObjectName("AdvancedToggle")
        filter_group_layout.addWidget(self.show_advanced_check)

        # ── Advanced Panel ────────────────────────────────────────────────
        self.filter_invert_check = QCheckBox("Invert output")
        self.filter_invert_check.setChecked(bool(opencv_cfg.get("invert", False)))

        self.filter_blur_spin = SliderField(0, 15, int(opencv_cfg.get("blur", 3)))
        self.filter_threshold_spin = SliderField(0, 255, int(opencv_cfg.get("threshold", 170)))
        self.filter_dilate_spin = SliderField(0, 6, int(opencv_cfg.get("dilate_iter", 1)))

        self.btn_refresh_filter_preview = QPushButton("Refresh Preview")
        self.btn_refresh_filter_preview.clicked.connect(self.refresh_filter_preview)

        self.advanced_filter_panel = QWidget()
        _adv_form = QFormLayout(self.advanced_filter_panel)
        _adv_form.setContentsMargins(0, 4, 0, 0)
        _adv_form.setHorizontalSpacing(10)
        _adv_form.setVerticalSpacing(4)
        _adv_form.addRow(self._form_labels["invert"], self.filter_invert_check)
        _adv_form.addRow(self._form_labels["blur"], self.filter_blur_spin)
        _adv_form.addRow(self._form_labels["threshold"], self.filter_threshold_spin)
        _adv_form.addRow(self._form_labels["dilate"], self.filter_dilate_spin)

        self.advanced_filter_panel.setVisible(self.show_advanced_check.isChecked())
        filter_group_layout.addWidget(self.advanced_filter_panel)

        # ── Profile + Auto-Adjust ──────────────────────────────────────────
        _sep2 = QFrame()
        _sep2.setFrameShape(QFrame.Shape.HLine)
        _sep2.setObjectName("Separator")
        filter_group_layout.addWidget(_sep2)

        # Profile row
        _profile_row = QHBoxLayout()
        _profile_row.setSpacing(6)
        _profile_lbl = QLabel("Profile")
        _profile_lbl.setObjectName("FieldLabel")
        _profile_lbl.setFixedWidth(56)
        self.filter_profile_combo = QComboBox()
        self.btn_save_filter_profile = QPushButton("Save")
        self.btn_load_filter_profile = QPushButton("Load")
        self.btn_delete_filter_profile = QPushButton("Delete")
        self.btn_save_filter_profile.clicked.connect(self.save_filter_profile)
        self.btn_load_filter_profile.clicked.connect(self.load_filter_profile)
        self.btn_delete_filter_profile.clicked.connect(self.delete_filter_profile)
        _profile_row.addWidget(_profile_lbl)
        _profile_row.addWidget(self.filter_profile_combo, stretch=1)
        _profile_row.addWidget(self.btn_save_filter_profile)
        _profile_row.addWidget(self.btn_load_filter_profile)
        _profile_row.addWidget(self.btn_delete_filter_profile)
        filter_group_layout.addLayout(_profile_row)

        # Auto-Adjust button
        _adjust_row = QHBoxLayout()
        _adjust_row.setSpacing(6)
        self.btn_auto_adjust = QPushButton("AUTO-ADJUST")
        self.btn_auto_adjust.setMinimumHeight(32)
        self.btn_auto_adjust.setStyleSheet(
            "QPushButton { font-weight: bold; background-color: #4CAF50; color: white; "
            "border-radius: 4px; padding: 4px 12px; } "
            "QPushButton:hover { background-color: #45a049; } "
            "QPushButton:disabled { background-color: #666; }"
        )
        self.btn_auto_adjust.clicked.connect(self._run_auto_adjust)
        self.auto_adjust_status = QLabel("")
        self.auto_adjust_status.setObjectName("StatusLabel")
        self.auto_adjust_status.setWordWrap(True)
        _adjust_row.addWidget(self.btn_auto_adjust)
        _adjust_row.addWidget(self.auto_adjust_status, stretch=1)
        filter_group_layout.addLayout(_adjust_row)

        filter_group_layout.addStretch(1)

        # ── Filter Preview Group ────────────────────────────────────────────
        filter_preview_group = QGroupBox("Filter Preview")
        self._grp_filter_preview = filter_preview_group
        filter_preview_layout = QVBoxLayout(filter_preview_group)
        filter_preview_layout.setContentsMargins(10, 12, 10, 10)
        filter_preview_layout.setSpacing(8)
        filter_preview_layout.addWidget(self.btn_refresh_filter_preview)

        self.dialogue_filter_image_label = QLabel(get_translation("dialogue", "en"))
        self.menu_filter_image_label = QLabel(get_translation("menu", "en"))
        for _lbl in (self.dialogue_filter_image_label, self.menu_filter_image_label):
            _lbl.setMinimumSize(520, 220)
            _lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _lbl.setStyleSheet(
                "QLabel { background: #080c10; border: 1px solid #1e2630; "
                "color: #3a4a5a; border-radius: 10px; font-size: 11px; }"
            )
        _dlg_prev_lbl = QLabel("Dialogue")
        _dlg_prev_lbl.setObjectName("FieldLabel")
        _menu_prev_lbl = QLabel("Menu")
        _menu_prev_lbl.setObjectName("FieldLabel")
        filter_preview_layout.addWidget(_dlg_prev_lbl)
        filter_preview_layout.addWidget(self.dialogue_filter_image_label, stretch=1)
        filter_preview_layout.addWidget(_menu_prev_lbl)
        filter_preview_layout.addWidget(self.menu_filter_image_label, stretch=1)

        # Wrap left panel in a scrollable GroupBox for smaller screens
        filter_scroll_group = QGroupBox("Filter Settings")
        self._grp_filter_scroll = filter_scroll_group
        _fsg_layout = QVBoxLayout(filter_scroll_group)
        _fsg_layout.setContentsMargins(0, 4, 0, 0)
        _fsg_layout.setSpacing(0)
        _fsg_layout.addWidget(filter_group)

        filter_splitter = QSplitter(Qt.Orientation.Horizontal)
        filter_splitter.addWidget(filter_scroll_group)
        filter_splitter.addWidget(filter_preview_group)
        filter_splitter.setStretchFactor(0, 3)
        filter_splitter.setStretchFactor(1, 4)
        filter_splitter.setChildrenCollapsible(False)

        # ── Signal connections ──────────────────────────────────────────────
        self.filter_enabled_check.toggled.connect(self._on_opencv_setting_changed)
        self.filter_preset_combo.currentIndexChanged.connect(self._on_filter_preset_changed)
        self.show_advanced_check.toggled.connect(self._on_show_advanced_toggled)
        self.live_preview_check.toggled.connect(self._on_live_preview_toggled)
        self.filter_invert_check.toggled.connect(self._on_opencv_setting_changed)
        self.filter_blur_spin.valueChanged.connect(self._on_opencv_setting_changed)
        self.filter_threshold_spin.valueChanged.connect(self._on_opencv_setting_changed)
        self.filter_dilate_spin.valueChanged.connect(self._on_opencv_setting_changed)

        region_group = QGroupBox("Manual Regions")
        self._grp_region = region_group
        region_layout = QVBoxLayout(region_group)
        region_layout.setContentsMargins(8, 8, 8, 8)
        region_layout.setSpacing(4)

        button_row = QHBoxLayout()
        button_row.setSpacing(4)
        self.btn_pick_dialogue = QPushButton("Select Dialogue Region")
        self.btn_pick_menu = QPushButton("Select Menu Region")
        self.btn_test_once = QPushButton("Test OCR Once")
        self.btn_start = QPushButton("Start")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)

        self.btn_pick_dialogue.clicked.connect(lambda: self.pick_region("dialogue"))
        self.btn_pick_menu.clicked.connect(lambda: self.pick_region("menu"))
        self.btn_test_once.clicked.connect(self.run_test_once)
        self.btn_start.clicked.connect(self.start_capture)
        self.btn_stop.clicked.connect(self.stop_capture)

        button_row.addWidget(self.btn_pick_dialogue)
        button_row.addWidget(self.btn_pick_menu)
        button_row.addWidget(self.btn_test_once)
        button_row.addWidget(self.btn_start)
        button_row.addWidget(self.btn_stop)

        self.dialogue_region_label = QLabel()
        self.menu_region_label = QLabel()

        region_layout.addLayout(button_row)
        region_layout.addWidget(self.dialogue_region_label)
        region_layout.addWidget(self.menu_region_label)

        stabilizer_cfg = self.cfg.get("stabilizer", {})
        quick_capture_cfg = self.cfg.get("quick_capture", {})
        stability_group = QGroupBox("Stabilization and Quick Capture")
        self._grp_stability = stability_group
        stability_form = QFormLayout(stability_group)
        stability_form.setHorizontalSpacing(10)
        stability_form.setVerticalSpacing(4)

        self.stabilization_enabled_check = QCheckBox("Confirm repeated OCR before updating")
        self.stabilization_enabled_check.setChecked(bool(stabilizer_cfg.get("enabled", True)))
        self.stabilization_frames_spin = QSpinBox()
        self.stabilization_frames_spin.setRange(1, 5)
        self.stabilization_frames_spin.setValue(int(stabilizer_cfg.get("frames", 2)))
        self.stabilization_similarity_spin = QSpinBox()
        self.stabilization_similarity_spin.setRange(70, 100)
        self.stabilization_similarity_spin.setSuffix("%")
        self.stabilization_similarity_spin.setValue(int(stabilizer_cfg.get("similarity_percent", 88)))

        self.quick_capture_enabled_check = QCheckBox("Enable global hotkey")
        self.quick_capture_enabled_check.setChecked(bool(quick_capture_cfg.get("enabled", False)))
        self.quick_capture_modifier_combo = QComboBox()
        for label, value in HOTKEY_MODIFIERS:
            self.quick_capture_modifier_combo.addItem(label, value)
        self._set_combo_by_data(
            self.quick_capture_modifier_combo,
            str(quick_capture_cfg.get("modifier", "ctrl+shift")),
            "ctrl+shift",
        )
        self.quick_capture_key_combo = QComboBox()
        for key in HOTKEY_KEYS:
            self.quick_capture_key_combo.addItem(key, key)
        self._set_combo_by_data(
            self.quick_capture_key_combo,
            str(quick_capture_cfg.get("key", "Q")).upper(),
            "Q",
        )
        self.btn_quick_capture_now = QPushButton("Quick Select Now")
        self.quick_capture_status_label = QLabel("Hotkey idle")

        quick_capture_buttons = QWidget()
        quick_capture_buttons_layout = QHBoxLayout(quick_capture_buttons)
        quick_capture_buttons_layout.setContentsMargins(0, 0, 0, 0)
        quick_capture_buttons_layout.setSpacing(6)
        quick_capture_buttons_layout.addWidget(self.quick_capture_modifier_combo)
        quick_capture_buttons_layout.addWidget(self.quick_capture_key_combo)
        quick_capture_buttons_layout.addWidget(self.btn_quick_capture_now)

        stability_form.addRow(self._form_labels["stabilization"], self.stabilization_enabled_check)
        stability_form.addRow(self._form_labels["stable_frames"], self.stabilization_frames_spin)
        stability_form.addRow(self._form_labels["similarity"], self.stabilization_similarity_spin)
        stability_form.addRow(self._form_labels["quick_capture"], self.quick_capture_enabled_check)
        stability_form.addRow(self._form_labels["hotkey"], quick_capture_buttons)
        stability_form.addRow(self._form_labels["hotkey_status"], self.quick_capture_status_label)
        self.stabilization_enabled_check.toggled.connect(self._on_stabilization_settings_changed)
        self.stabilization_frames_spin.valueChanged.connect(self._on_stabilization_settings_changed)
        self.stabilization_similarity_spin.valueChanged.connect(self._on_stabilization_settings_changed)
        self.quick_capture_enabled_check.toggled.connect(self._on_quick_capture_settings_changed)
        self.quick_capture_modifier_combo.currentIndexChanged.connect(self._on_quick_capture_settings_changed)
        self.quick_capture_key_combo.currentIndexChanged.connect(self._on_quick_capture_settings_changed)
        self.btn_quick_capture_now.clicked.connect(self.start_quick_capture_selection)

        self.dialogue_translated_preview_label = QLabel("Dialogue (TR)")
        self.menu_translated_preview_label = QLabel("Menu (TR)")

        controls_tabs = QTabWidget()
        controls_tabs.setDocumentMode(True)

        main_tab = QWidget()
        main_tab_layout = QGridLayout(main_tab)
        main_tab_layout.setContentsMargins(6, 6, 6, 6)
        main_tab_layout.setHorizontalSpacing(6)
        main_tab_layout.setVerticalSpacing(6)
        main_tab_layout.addWidget(monitor_group, 0, 0)
        main_tab_layout.addWidget(region_group, 0, 1)
        main_tab_layout.addWidget(stability_group, 1, 0, 1, 2)
        main_tab_layout.setColumnStretch(0, 3)
        main_tab_layout.setColumnStretch(1, 2)

        filter_tab = QWidget()
        filter_tab_layout = QVBoxLayout(filter_tab)
        filter_tab_layout.setContentsMargins(6, 6, 6, 6)
        filter_tab_layout.setSpacing(6)
        filter_tab_layout.addWidget(filter_splitter, stretch=1)

        # ── Settings Tab ────────────────────────────────────────────────
        settings_tab = QWidget()
        settings_tab_layout = QGridLayout(settings_tab)
        settings_tab_layout.setContentsMargins(12, 10, 12, 10)
        settings_tab_layout.setHorizontalSpacing(12)
        settings_tab_layout.setVerticalSpacing(8)

        settings_col_left = QVBoxLayout()
        settings_col_left.setSpacing(12)
        settings_col_right = QVBoxLayout()
        settings_col_right.setSpacing(12)

        # General settings group
        _general_header = QLabel("GENERAL")
        _general_header.setObjectName("SettingsSectionHeader")
        settings_col_left.addWidget(_general_header)

        settings_general_group = QGroupBox("")
        self._grp_settings_general = settings_general_group
        settings_general_form = QFormLayout(settings_general_group)
        settings_general_form.setHorizontalSpacing(10)
        settings_general_form.setVerticalSpacing(6)
        settings_general_form.addRow(self._form_labels["theme_lang"], theme_row)
        settings_general_form.addRow(self._form_labels["ocr_engine"], self.ocr_engine_combo)
        settings_general_form.addRow(self._form_labels["ocr_backend"], self.backend_label)
        settings_col_left.addWidget(settings_general_group)

        # Preprocessing group
        _preproc_header = QLabel("IMAGE PREPROCESSING")
        _preproc_header.setObjectName("SettingsSectionHeader")
        settings_col_left.addWidget(_preproc_header)

        settings_preproc_group = QGroupBox("")
        self._grp_settings_preproc = settings_preproc_group
        settings_preproc_form = QFormLayout(settings_preproc_group)
        settings_preproc_form.setHorizontalSpacing(10)
        settings_preproc_form.setVerticalSpacing(6)
        settings_preproc_form.addRow(self._form_labels["preprocess_contrast"], self.preproc_contrast_slider)
        settings_preproc_form.addRow(self._form_labels["preprocess_sharpness"], self.preproc_sharpness_slider)
        settings_preproc_form.addRow(self._form_labels["preprocess_denoise"], self.preproc_denoise_slider)
        settings_preproc_form.addRow("", self.preproc_auto_enhance_check)
        settings_col_left.addWidget(settings_preproc_group)

        # System Info group
        _sysinfo_header = QLabel("SYSTEM INFO")
        _sysinfo_header.setObjectName("SettingsSectionHeader")
        settings_col_left.addWidget(_sysinfo_header)

        settings_system_group = QGroupBox("")
        self._grp_settings_system = settings_system_group
        settings_system_form = QFormLayout(settings_system_group)
        settings_system_form.setHorizontalSpacing(10)
        settings_system_form.setVerticalSpacing(6)
        settings_system_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        import platform as _platform
        _py_ver = _platform.python_version()
        _os_info = f"{_platform.system()} {_platform.release()}"
        _cv_ver = cv2.__version__ if cv2 is not None else "N/A"
        _mss_ver = "Installed" if mss else "Not installed"
        _pil_ver = Image.__version__ if Image and hasattr(Image, "__version__") else "N/A"
        _np_ver = np.__version__ if np is not None else "N/A"
        try:
            import pytesseract as _tess_chk  # noqa: F401
            _tess_ver = "Available"
        except Exception:
            _tess_ver = "Not installed"
        try:
            from winsdk.windows.media.ocr import OcrEngine  # noqa: F401
            _winrt_ver = "Available"
        except Exception:
            _winrt_ver = "Not installed"

        def _sys_row(label: str, value: str, ok: bool = True) -> tuple:
            _lbl = QLabel(label)
            _lbl.setMinimumWidth(100)
            _val = QLabel(value)
            _val.setObjectName("SysInfoValue")
            _val.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            _dot = QLabel("●")
            _dot.setFixedWidth(16)
            _dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _dot.setStyleSheet(f"color: {'#3a8a42' if ok else '#888'}; font-size: 10px;")
            _right = QHBoxLayout()
            _right.setContentsMargins(0, 0, 0, 0)
            _right.setSpacing(6)
            _right.addWidget(_dot)
            _right.addWidget(_val)
            _right.addStretch()
            _right_widget = QWidget()
            _right_widget.setLayout(_right)
            _right_widget.setStyleSheet("background: transparent;")
            return _lbl, _right_widget

        self.sys_info_python = QLabel(f"Python {_py_ver}")
        self.sys_info_os = QLabel(_os_info)
        self.sys_info_opencv = QLabel(f"OpenCV {_cv_ver}")
        self.sys_info_numpy = QLabel(f"NumPy {_np_ver}")
        self.sys_info_pil = QLabel(f"Pillow {_pil_ver}")
        self.sys_info_mss = QLabel(_mss_ver)
        self.sys_info_tesseract = QLabel(_tess_ver)
        self.sys_info_winrt = QLabel(_winrt_ver)
        self.sys_info_backend = QLabel(self.backend.name)

        settings_system_form.addRow(*_sys_row("Python", f"Python {_py_ver}", True))
        settings_system_form.addRow(*_sys_row("OS", _os_info, True))
        settings_system_form.addRow(*_sys_row("OpenCV", f"OpenCV {_cv_ver}", cv2 is not None))
        settings_system_form.addRow(*_sys_row("NumPy", f"NumPy {_np_ver}", np is not None))
        settings_system_form.addRow(*_sys_row("Pillow", f"Pillow {_pil_ver}", Image is not None))
        settings_system_form.addRow(*_sys_row("mss", _mss_ver, bool(mss)))
        settings_system_form.addRow(*_sys_row("Tesseract", _tess_ver, _tess_ver == "Available"))
        settings_system_form.addRow(*_sys_row("WinRT/WINRT", _winrt_ver, _winrt_ver == "Available"))
        settings_system_form.addRow(*_sys_row("Active Backend", self.backend.name, self.backend.name != "none"))
        settings_col_left.addWidget(settings_system_group)

        settings_col_left.addStretch()
        settings_tab_layout.addLayout(settings_col_left, 0, 0)

        # Translation group
        _translate_header = QLabel("TRANSLATION")
        _translate_header.setObjectName("SettingsSectionHeader")
        settings_col_right.addWidget(_translate_header)

        settings_translate_group = QGroupBox("")
        self._grp_settings_translate = settings_translate_group
        settings_translate_form = QFormLayout(settings_translate_group)
        settings_translate_form.setHorizontalSpacing(10)
        settings_translate_form.setVerticalSpacing(6)
        settings_translate_form.addRow(self._form_labels["auto_translate"], self.translation_enabled_check)
        settings_translate_form.addRow(self._form_labels["source_language"], self.translation_source_combo)
        settings_translate_form.addRow(self._form_labels["target_language"], self.translation_target_combo)
        settings_translate_form.addRow(self._form_labels["translate_engine"], self.translation_backend_combo)
        settings_translate_form.addRow("", self.btn_argos_packs)
        settings_col_right.addWidget(settings_translate_group)

        # Output Window settings group
        _output_header = QLabel("OUTPUT WINDOW")
        _output_header.setObjectName("SettingsSectionHeader")
        settings_col_right.addWidget(_output_header)

        settings_output_group = QGroupBox("")
        self._grp_settings_output = settings_output_group
        settings_output_form = QFormLayout(settings_output_group)
        settings_output_form.setHorizontalSpacing(10)
        settings_output_form.setVerticalSpacing(6)
        self.output_width_spin = QSpinBox()
        self.output_width_spin.setRange(400, 3840)
        self.output_width_spin.setValue(self.output_window._out_cfg.get("width", 1100))
        self.output_width_spin.valueChanged.connect(self._on_output_size_changed)
        self.output_height_spin = QSpinBox()
        self.output_height_spin.setRange(300, 2160)
        self.output_height_spin.setValue(self.output_window._out_cfg.get("height", 750))
        self.output_height_spin.valueChanged.connect(self._on_output_size_changed)
        _size_row = QHBoxLayout()
        _size_row.addWidget(self.output_width_spin)
        _size_row.addWidget(QLabel("x"))
        _size_row.addWidget(self.output_height_spin)
        self.output_opacity_spin = QSpinBox()
        self.output_opacity_spin.setRange(30, 100)
        self.output_opacity_spin.setValue(int(self.output_window._out_cfg.get("opacity", 0.95) * 100))
        self.output_opacity_spin.setSuffix("%")
        self.output_opacity_spin.valueChanged.connect(self._on_output_opacity_changed)
        self.output_auto_show_check = QCheckBox("")
        self.output_auto_show_check.setChecked(self.output_window._out_cfg.get("auto_show", True))
        self.output_auto_show_check.toggled.connect(self._on_output_auto_show_changed)
        self.output_save_pos_check = QCheckBox("")
        self.output_save_pos_check.setChecked(self.output_window._out_cfg.get("save_position", True))
        self.output_save_pos_check.toggled.connect(self._on_output_save_pos_changed)
        settings_output_form.addRow(self._form_labels["output_size"], _size_row)
        settings_output_form.addRow(self._form_labels["output_opacity"], self.output_opacity_spin)
        settings_output_form.addRow(self._form_labels["output_auto_show"], self.output_auto_show_check)
        settings_output_form.addRow(self._form_labels["output_save_pos"], self.output_save_pos_check)
        settings_col_right.addWidget(settings_output_group)

        # Performance Analysis group
        _perf_header = QLabel("PERFORMANCE")
        _perf_header.setObjectName("SettingsSectionHeader")
        settings_col_right.addWidget(_perf_header)

        settings_perf_group = QGroupBox("")
        self._grp_settings_perf = settings_perf_group
        settings_perf_form = QFormLayout(settings_perf_group)
        settings_perf_form.setHorizontalSpacing(10)
        settings_perf_form.setVerticalSpacing(6)
        self.perf_fps_label = QLabel("--")
        self.perf_fps_label.setObjectName("PerfValue")
        self.perf_fps_avg_label = QLabel("--")
        self.perf_fps_avg_label.setObjectName("PerfValue")
        self.perf_capture_label = QLabel("--")
        self.perf_capture_label.setObjectName("PerfValue")
        self.perf_ocr_label = QLabel("--")
        self.perf_ocr_label.setObjectName("PerfValue")
        self.perf_total_label = QLabel("--")
        self.perf_total_label.setObjectName("PerfValue")
        self.perf_frame_count_label = QLabel("--")
        self.perf_frame_count_label.setObjectName("PerfValue")
        settings_perf_form.addRow(self._form_labels["fps"], self.perf_fps_label)
        settings_perf_form.addRow(self._form_labels["avg_fps"], self.perf_fps_avg_label)
        settings_perf_form.addRow(self._form_labels["capture_time"], self.perf_capture_label)
        settings_perf_form.addRow(self._form_labels["ocr_time"], self.perf_ocr_label)
        settings_perf_form.addRow(self._form_labels["total_time"], self.perf_total_label)
        settings_perf_form.addRow(self._form_labels["frame_count"], self.perf_frame_count_label)
        settings_col_right.addWidget(settings_perf_group)

        settings_col_right.addStretch()
        settings_tab_layout.addLayout(settings_col_right, 0, 1)
        settings_tab_layout.setColumnStretch(0, 1)
        settings_tab_layout.setColumnStretch(1, 1)

        # ── About Tab ───────────────────────────────────────────────────
        about_tab = QWidget()
        about_scroll = QScrollArea()
        about_scroll.setWidgetResizable(True)
        about_scroll.setFrameShape(QFrame.Shape.NoFrame)
        about_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        about_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        about_container = QWidget()
        about_container.setStyleSheet("background: transparent;")
        about_tab_layout = QVBoxLayout(about_container)
        about_tab_layout.setContentsMargins(32, 16, 32, 16)
        about_tab_layout.setSpacing(12)

        # ── Hero Section ──
        _hero = QFrame()
        _hero.setObjectName("AboutHero")
        _hero.setFixedHeight(160)
        _hero_layout = QVBoxLayout(_hero)
        _hero_layout.setContentsMargins(24, 14, 24, 14)
        _hero_layout.setSpacing(4)
        _hero_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        _app_title = QLabel("SYNCRA")
        _app_title.setObjectName("AboutTitle")
        _app_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _hero_layout.addWidget(_app_title)

        _version_row = QHBoxLayout()
        _version_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _ver_badge = QLabel("v1.0.0")
        _ver_badge.setObjectName("AboutVersionBadge")
        _version_row.addWidget(_ver_badge)
        _hero_layout.addLayout(_version_row)

        _hero_desc = QLabel(
            "Real-time screen capture, OCR and translation tool.\n"
            "Optimized for game dialogs and menus."
        )
        _hero_desc.setObjectName("AboutDesc")
        _hero_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _hero_desc.setWordWrap(True)
        _hero_layout.addWidget(_hero_desc)

        about_tab_layout.addWidget(_hero)

        # ── Features Grid ──
        _feat_header = QLabel("FEATURES")
        _feat_header.setObjectName("AboutSectionHeader")
        about_tab_layout.addWidget(_feat_header)

        _feat_grid = QGridLayout()
        _feat_grid.setSpacing(10)

        _features = [
            ("OCR Engine", "WinRT & Tesseract\nDual backend support", "#4fc3f7"),
            ("Image Processing", "OpenCV powered\nAdvanced filtering pipeline", "#e7b85c"),
            ("Translation", "Google Translate\n& Argos Translate", "#9fd46d"),
            ("Quick Capture", "Global hotkey\nInstant region OCR", "#c084fc"),
            ("Stabilization", "Smart text gate\nDuplicate filtering", "#74d1c6"),
            ("Auto Adjust", "One-click optimal\nfilter settings", "#f472b6"),
        ]

        for i, (title, desc, accent) in enumerate(_features):
            row, col = divmod(i, 3)
            _card = QFrame()
            _card.setObjectName("AboutFeatureCard")
            _card.setMinimumHeight(60)
            _card_layout = QVBoxLayout(_card)
            _card_layout.setContentsMargins(12, 8, 12, 8)
            _card_layout.setSpacing(4)

            _card_accent = QFrame()
            _card_accent.setFixedWidth(3)
            _card_accent.setStyleSheet(f"background: {accent}; border-radius: 1px;")

            _card_title = QLabel(title)
            _card_title.setObjectName("AboutCardTitle")

            _card_desc = QLabel(desc)
            _card_desc.setObjectName("AboutCardDesc")
            _card_desc.setWordWrap(True)

            _inner = QHBoxLayout()
            _inner.setSpacing(10)
            _inner.addWidget(_card_accent)
            _inner_v = QVBoxLayout()
            _inner_v.setSpacing(2)
            _inner_v.addWidget(_card_title)
            _inner_v.addWidget(_card_desc)
            _inner.addLayout(_inner_v, stretch=1)
            _card_layout.addLayout(_inner)

            _feat_grid.addWidget(_card, row, col)

        about_tab_layout.addLayout(_feat_grid)

        # ── Hotkeys Section ──
        _hk_header = QLabel("HOTKEYS")
        _hk_header.setObjectName("AboutSectionHeader")
        about_tab_layout.addWidget(_hk_header)

        _hk_row = QHBoxLayout()
        _hk_row.setSpacing(10)

        _hotkeys = [
            ("Ctrl+Shift+Q", "Quick Capture", "#4fc3f7"),
        ]

        for keys, action, accent in _hotkeys:
            _hk_card = QFrame()
            _hk_card.setObjectName("AboutHotkeyCard")
            _hk_card_layout = QHBoxLayout(_hk_card)
            _hk_card_layout.setContentsMargins(12, 8, 12, 8)
            _hk_card_layout.setSpacing(10)

            _hk_key = QLabel(keys)
            _hk_key.setObjectName("AboutHotkeyKey")
            _hk_key.setStyleSheet(f"color: {accent};")
            _hk_card_layout.addWidget(_hk_key)

            _hk_action = QLabel(action)
            _hk_action.setObjectName("AboutHotkeyAction")
            _hk_card_layout.addWidget(_hk_action)
            _hk_card_layout.addStretch()

            _hk_row.addWidget(_hk_card)

        about_tab_layout.addLayout(_hk_row)

        # ── System Usage Section ──
        _usage_header = QLabel("SYSTEM USAGE")
        _usage_header.setObjectName("AboutSectionHeader")
        about_tab_layout.addWidget(_usage_header)

        _tc = get_theme_colors("dark")
        _usage_labels = {}
        _usage_bars = {}

        def _make_usage_card(title: str, accent: str) -> tuple:
            card = QFrame()
            card.setObjectName("AboutFeatureCard")
            card.setMinimumHeight(64)
            card_lay = QVBoxLayout(card)
            card_lay.setContentsMargins(16, 10, 16, 10)
            card_lay.setSpacing(4)

            top_row = QHBoxLayout()
            top_row.setSpacing(0)

            _accent_bar = QFrame()
            _accent_bar.setFixedWidth(3)
            _accent_bar.setStyleSheet(f"background: {accent}; border-radius: 1px;")
            top_row.addWidget(_accent_bar)
            top_row.addSpacing(10)

            _title_lbl = QLabel(title)
            _title_lbl.setStyleSheet(f"color: {_tc['text_muted']}; font-size: 11px; font-weight: 600; background: transparent; border: none;")
            top_row.addWidget(_title_lbl)
            top_row.addStretch()

            _value_lbl = QLabel("--")
            _value_lbl.setStyleSheet(f"color: {accent}; font-size: 13px; font-weight: 700; font-family: Consolas, monospace; background: transparent; border: none;")
            top_row.addWidget(_value_lbl)
            card_lay.addLayout(top_row)

            _bar = QSlider(Qt.Orientation.Horizontal)
            _bar.setRange(0, 100)
            _bar.setValue(0)
            _bar.setFixedHeight(4)
            _bar.setEnabled(False)
            _bar.setStyleSheet(f"""
                QSlider::groove:horizontal {{
                    border: none;
                    height: 4px;
                    background: {_tc['bg_secondary']};
                    border-radius: 2px;
                }}
                QSlider::sub-page:horizontal {{
                    background: {accent};
                    border-radius: 2px;
                }}
                QSlider::handle:horizontal {{ background: transparent; width: 0px; }}
                QSlider::add-page:horizontal {{ background: transparent; border-radius: 2px; }}
            """)
            card_lay.addWidget(_bar)

            return card, _value_lbl, _bar

        _cpu_card, _cpu_val, _cpu_bar = _make_usage_card("CPU Usage", "#4fc3f7")
        _mem_card, _mem_val, _mem_bar = _make_usage_card("Memory Usage", "#e7b85c")
        _ram_card, _ram_val, _ram_bar = _make_usage_card("System RAM", "#9fd46d")
        _core_card, _core_val, _core_bar = _make_usage_card("CPU Cores", "#c084fc")

        _usage_grid = QGridLayout()
        _usage_grid.setSpacing(10)
        _usage_grid.addWidget(_cpu_card, 0, 0)
        _usage_grid.addWidget(_mem_card, 0, 1)
        _usage_grid.addWidget(_ram_card, 1, 0)
        _usage_grid.addWidget(_core_card, 1, 1)

        _usage_labels = {"cpu": _cpu_val, "mem": _mem_val, "ram": _ram_val, "cores": _core_val}
        _usage_bars = {"cpu": _cpu_bar, "mem": _mem_bar, "ram": _ram_bar}
        self._about_usage_labels = _usage_labels
        about_tab_layout.addLayout(_usage_grid)

        def _update_about_usage():
            usage = _get_process_usage()
            if "cpu" in _usage_labels:
                _usage_labels["cpu"].setText(f"{usage['cpu_pct']}%")
                _usage_bars["cpu"].setValue(min(100, int(usage["cpu_pct"])))
            if "mem" in _usage_labels:
                _usage_labels["mem"].setText(f"{usage['mem_mb']:.0f} MB")
            if "ram" in _usage_labels:
                _used = usage.get("sys_ram_used_gb", 0)
                _total = usage["sys_ram_gb"]
                _pct = int((_used / _total * 100)) if _total > 0 else 0
                _usage_labels["ram"].setText(f"{_used:.1f} / {_total:.1f} GB")
                _usage_bars["ram"].setValue(_pct)
            if "cores" in _usage_labels:
                _usage_labels["cores"].setText(f"{usage['sys_cores']} cores")

        self._about_usage_timer = QTimer(self)
        self._about_usage_timer.timeout.connect(_update_about_usage)
        self._about_usage_timer.start(2000)
        _update_about_usage()

        # ── Credits / Footer ──
        about_tab_layout.addStretch()

        _footer = QLabel("Syncra OCR  |  Built with PyQt6 + OpenCV + WinRT")
        _footer.setObjectName("AboutFooter")
        _footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        about_tab_layout.addWidget(_footer)

        about_scroll.setWidget(about_container)
        about_main_layout = QVBoxLayout(about_tab)
        about_main_layout.setContentsMargins(0, 0, 0, 0)
        about_main_layout.addWidget(about_scroll)

        self._tabs = controls_tabs
        controls_tabs.addTab(main_tab, "Main")
        controls_tabs.addTab(filter_tab, "Filter")
        controls_tabs.addTab(settings_tab, "Settings")
        controls_tabs.addTab(about_tab, "About")

        root.addWidget(controls_tabs, stretch=1)

        self.setCentralWidget(central)

        self._refresh_screens()
        self._refresh_backend_labels()
        self._refresh_region_labels()
        self._refresh_filter_profiles_combo()
        self._on_translation_target_changed(self.translation_target_combo.currentIndex())
        active_profile = self.cfg.get("active_filter_profile", "")
        active_settings = self.cfg.get("filter_profiles", {}).get(active_profile, {})
        if isinstance(active_profile, str) and active_profile and isinstance(active_settings, dict):
            self._apply_opencv_settings(active_settings, refresh_preview=False, update_status=False)
        else:
            self.apply_filter_preset(refresh_preview=False, update_status=False)
        self._update_quick_capture_ui()
        self._install_window_shortcuts()
        QTimer.singleShot(0, self._update_global_hotkey_registration)
        self._refresh_ui_texts("en")

        if not mss:
            self._set_status("mss is missing. Install requirements and restart.")
        elif not Image:
            self._set_status("Pillow is missing. Install requirements and restart.")

    def closeEvent(self, event) -> None:
        self._is_closing = True
        self.stop_capture()
        if self._selector is not None:
            self._selector.close()
            self._selector = None
        if self._quick_selector is not None:
            self._quick_selector.close()
            self._quick_selector = None
        self.output_window.close()
        self.quick_translate_window.close()
        self._unregister_global_hotkey()
        self._current_ocr_worker = None
        self._current_quick_capture_worker = None
        self._active_workers.clear()
        self.backend.close()
        if self._capture:
            self._capture.close()
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._update_global_hotkey_registration)

    def _set_translation_target(self, code: str) -> None:
        normalized = (code or "tr").lower()
        for idx in range(self.translation_target_combo.count()):
            if self.translation_target_combo.itemData(idx) == normalized:
                self.translation_target_combo.setCurrentIndex(idx)
                return
        self.translation_target_combo.setCurrentIndex(0)

    def _refresh_backend_labels(self) -> None:
        self.backend_label.setText(self.backend.name)
        if hasattr(self, "cv_status_label"):
            self.cv_status_label.setText(
                "OpenCV ready" if self._opencv_filter_is_available() else "OpenCV not installed (fallback active)"
            )
        if self.backend.name == "none":
            self._set_status("No OCR backend ready. Install winsdk or pytesseract.")
        elif (
            self.translation_enabled_check.isChecked()
            and not self.translation_backend.is_ready()
        ):
            if self.translation_backend.name == "argos":
                self._set_status("Argos Translate not installed. Run: pip install argostranslate")
            else:
                self._set_status("Translation backend missing. Install deep-translator.")

    def _set_status(self, text: str) -> None:
        if self._is_closing:
            return
        self.status_label.setText(text)

    def _update_frame_info(self) -> None:
        if self._is_closing:
            return
        if not self.timer.isActive():
            return

        fps = 0.0
        if self._frame_times:
            avg = sum(self._frame_times) / len(self._frame_times)
            fps = 1.0 / avg if avg > 0 else 0.0

        total_ms = self._capture_ms + self._ocr_ms
        self.status_label.setText(f"{fps:.1f} FPS | {total_ms:.0f}ms/frame")
        self._update_perf_labels(fps, total_ms)

    def _update_perf_labels(self, fps: float = 0.0, total_ms: float = 0.0) -> None:
        if self._is_closing:
            return
        if not hasattr(self, "perf_fps_label"):
            return
        self.perf_fps_label.setText(f"{fps:.1f}")
        if self._frame_times:
            avg = sum(self._frame_times) / len(self._frame_times)
            avg_fps = 1.0 / avg if avg > 0 else 0.0
        else:
            avg_fps = 0.0
        self.perf_fps_avg_label.setText(f"{avg_fps:.1f}")
        self.perf_capture_label.setText(f"{self._capture_ms:.1f}")
        self.perf_ocr_label.setText(f"{self._ocr_ms:.1f}")
        self.perf_total_label.setText(f"{total_ms:.1f}")
        self.perf_frame_count_label.setText(str(len(self._frame_times)))

    def _refresh_screens(self) -> None:
        self.screens = QApplication.screens()
        self.capture_screen_combo.blockSignals(True)
        self.output_screen_combo.blockSignals(True)
        self.capture_screen_combo.clear()
        self.output_screen_combo.clear()

        for idx, screen in enumerate(self.screens):
            geo = screen.geometry()
            title = f"{idx}: {screen.name()} [{geo.x()},{geo.y()} {geo.width()}x{geo.height()}]"
            self.capture_screen_combo.addItem(title)
            self.output_screen_combo.addItem(title)

        capture_idx = int(self.cfg.get("capture_screen_index", 0))
        output_idx = int(self.cfg.get("output_screen_index", 0))

        capture_idx = min(max(capture_idx, 0), max(0, len(self.screens) - 1))
        output_idx = min(max(output_idx, 0), max(0, len(self.screens) - 1))

        self.capture_screen_combo.setCurrentIndex(capture_idx)
        self.output_screen_combo.setCurrentIndex(output_idx)

        self.capture_screen_combo.blockSignals(False)
        self.output_screen_combo.blockSignals(False)

        if self.screens:
            self.output_window.move_to_screen(self.screens[output_idx])
            self.quick_translate_window.move_to_screen(self.screens[output_idx])

    def _on_capture_screen_changed(self, idx: int) -> None:
        self.cfg["capture_screen_index"] = idx
        save_config(self.cfg)

    def _on_output_screen_changed(self, idx: int) -> None:
        self.cfg["output_screen_index"] = idx
        save_config(self.cfg)
        if 0 <= idx < len(self.screens):
            self.output_window.move_to_screen(self.screens[idx])
            self.quick_translate_window.move_to_screen(self.screens[idx])

    def _on_interval_changed(self, value: int) -> None:
        self.cfg["ocr_interval_ms"] = int(value)
        save_config(self.cfg)
        if self.timer.isActive():
            self.timer.setInterval(value)

    def _on_translation_enabled_changed(self, checked: bool) -> None:
        self.cfg["translation_enabled"] = bool(checked)
        save_config(self.cfg)
        if checked and self.translation_backend.name == "none":
            self._set_status("Translation enabled but no backend available.")
        elif checked:
            self._set_status("Translation enabled")
        else:
            self._set_status("Translation disabled")

    def _on_translation_target_changed(self, _index: int) -> None:
        target = self._current_translation_target()
        self.cfg["translation_target"] = target
        save_config(self.cfg)
        lang = target.upper()
        self.dialogue_translated_preview_label.setText(f"Dialogue ({lang})")
        self.menu_translated_preview_label.setText(f"Menu ({lang})")

    def _on_theme_changed(self, _index: int) -> None:
        theme = self.theme_combo.currentData() or "dark"
        self.cfg["theme"] = theme
        save_config(self.cfg)
        self.setStyleSheet(build_theme_stylesheet(theme))
        self.output_window.setStyleSheet(build_theme_stylesheet(theme))
        self.quick_translate_window.setStyleSheet(build_theme_stylesheet(theme))
        self._apply_theme_to_widgets(theme)

    def _apply_theme_to_widgets(self, theme: str) -> None:
        colors = get_theme_colors(theme)
        for window in [self, self.output_window, self.quick_translate_window]:
            if hasattr(window, "cv_status_label"):
                cv_ok = cv2 is not None and np is not None
                dot_color = colors["success"] if cv_ok else colors["error"]
                window.cv_status_label.setStyleSheet(f"color: {dot_color}; font-size: 12px;")

    def _on_language_changed(self, _index: int) -> None:
        self._refresh_ui_texts("en")

    def _refresh_ui_texts(self, lang: str) -> None:
        t = TRANSLATIONS.get(lang, TRANSLATIONS["en"])
        self.setWindowTitle(t.get("app_title", "Syncra OCR"))
        if hasattr(self, "btn_argos_packs"):
            self.btn_argos_packs.setText(t.get("manage_packs", "Manage Language Packs"))
        if hasattr(self, "btn_start"):
            self.btn_start.setText(t.get("start", "Start"))
        if hasattr(self, "btn_stop"):
            self.btn_stop.setText(t.get("stop", "Stop"))
        if hasattr(self, "btn_test_once"):
            self.btn_test_once.setText(t.get("test_ocr", "Test OCR"))
        if hasattr(self, "btn_pick_dialogue"):
            self.btn_pick_dialogue.setText(t.get("select_dialogue", "Select Dialogue Region"))
        if hasattr(self, "btn_pick_menu"):
            self.btn_pick_menu.setText(t.get("select_menu", "Select Menu Region"))
        if hasattr(self, "btn_auto_adjust"):
            self.btn_auto_adjust.setText(t.get("auto_adjust", "AUTO-ADJUST"))
        if hasattr(self, "btn_refresh_filter_preview"):
            self.btn_refresh_filter_preview.setText(t.get("refresh_preview", "Refresh Preview"))
        if hasattr(self, "btn_quick_capture_now"):
            self.btn_quick_capture_now.setText(t.get("quick_select_now", "Quick Select Now"))
        if hasattr(self, "btn_ocr_mode_direct"):
            self.btn_ocr_mode_direct.setText(t.get("direct", "Direct"))
        if hasattr(self, "btn_ocr_mode_filtered"):
            self.btn_ocr_mode_filtered.setText(t.get("filtered", "Filtered"))
        if hasattr(self, "filter_enabled_check"):
            self.filter_enabled_check.setText(t.get("enable_filter", "Enable filter"))
        if hasattr(self, "live_preview_check"):
            self.live_preview_check.setText(t.get("live_preview", "Live preview"))
        if hasattr(self, "show_advanced_check"):
            self.show_advanced_check.setText(t.get("show_advanced", "Show advanced settings"))
        if hasattr(self, "filter_invert_check"):
            self.filter_invert_check.setText(t.get("invert", "Invert output"))
        if hasattr(self, "_grp_monitor"):
            self._grp_monitor.setTitle(t.get("monitor_settings", "Monitor and OCR Settings"))
        if hasattr(self, "_grp_filter_preview"):
            self._grp_filter_preview.setTitle(t.get("filter_preview", "Filter Preview"))
        if hasattr(self, "_grp_filter_scroll"):
            self._grp_filter_scroll.setTitle(t.get("filter_settings", "Filter Settings"))
        if hasattr(self, "_grp_region"):
            self._grp_region.setTitle(t.get("manual_regions", "Manual Regions"))
        if hasattr(self, "_grp_stability"):
            self._grp_stability.setTitle(t.get("stabilization", "Stabilization and Quick Capture"))
        if hasattr(self, "_grp_settings_general"):
            self._grp_settings_general.setTitle(t.get("general", "General"))
        if hasattr(self, "_grp_settings_preproc"):
            self._grp_settings_preproc.setTitle(t.get("preprocessing", "Preprocessing"))
        if hasattr(self, "_grp_settings_translate"):
            self._grp_settings_translate.setTitle(t.get("translation_settings", "Translation"))
        if hasattr(self, "_grp_settings_perf"):
            self._grp_settings_perf.setTitle(t.get("performance", "Performance Analysis"))
        if hasattr(self, "_grp_settings_system"):
            self._grp_settings_system.setTitle(t.get("system_info", "System Info"))
        if hasattr(self, "_grp_settings_output"):
            self._grp_settings_output.setTitle(t.get("output_settings", "Output Window Settings"))
        if hasattr(self, "_tabs"):
            self._tabs.setTabText(0, t.get("main_tab", "Main"))
            self._tabs.setTabText(1, t.get("filter_tab", "Filter"))
            self._tabs.setTabText(2, t.get("settings_tab", "Settings"))
            self._tabs.setTabText(3, t.get("about_tab", "About"))
        if hasattr(self, "_lbl_theme"):
            self._lbl_theme.setText(t.get("theme", "Theme") + ":")
        if hasattr(self, "_form_labels"):
            fl = self._form_labels
            if "capture_screen" in fl:
                fl["capture_screen"].setText(t.get("capture_screen", "Capture screen"))
            if "output_screen" in fl:
                fl["output_screen"].setText(t.get("output_screen", "Output screen"))
            if "ocr_interval" in fl:
                fl["ocr_interval"].setText(t.get("ocr_interval", "OCR interval (ms)"))
            if "theme_lang" in fl:
                fl["theme_lang"].setText(t.get("theme", "Theme"))
            if "ocr_engine" in fl:
                fl["ocr_engine"].setText(t.get("ocr_engine", "OCR Engine"))
            if "preprocess_contrast" in fl:
                fl["preprocess_contrast"].setText(t.get("preprocess", "Preprocess") + ": " + t.get("contrast", "Contrast"))
            if "preprocess_sharpness" in fl:
                fl["preprocess_sharpness"].setText(t.get("preprocess", "Preprocess") + ": " + t.get("sharpness", "Sharpness"))
            if "preprocess_denoise" in fl:
                fl["preprocess_denoise"].setText(t.get("preprocess", "Preprocess") + ": " + t.get("denoise", "Denoise"))
            if "auto_translate" in fl:
                fl["auto_translate"].setText(t.get("auto_translate", "Auto translate"))
            if "target_language" in fl:
                fl["target_language"].setText(t.get("target_language", "Target language"))
            if "translate_engine" in fl:
                fl["translate_engine"].setText(t.get("translate_engine", "Translate engine"))
            if "source_language" in fl:
                fl["source_language"].setText(t.get("source_language", "Source language"))
            if "ocr_backend" in fl:
                fl["ocr_backend"].setText(t.get("ocr_backend", "OCR backend"))
            if "status" in fl:
                fl["status"].setText(t.get("status", "Status"))
            if "invert" in fl:
                fl["invert"].setText(t.get("invert", "Invert"))
            if "blur" in fl:
                fl["blur"].setText(t.get("blur", "Blur"))
            if "threshold" in fl:
                fl["threshold"].setText(t.get("threshold", "Threshold"))
            if "dilate" in fl:
                fl["dilate"].setText(t.get("dilate", "Dilate"))
            if "stabilization" in fl:
                fl["stabilization"].setText(t.get("stabilization", "Stabilization"))
            if "stable_frames" in fl:
                fl["stable_frames"].setText(t.get("stable_frames", "Stable frames"))
            if "similarity" in fl:
                fl["similarity"].setText(t.get("similarity", "Similarity"))
            if "quick_capture" in fl:
                fl["quick_capture"].setText(t.get("quick_capture", "Quick capture"))
            if "hotkey" in fl:
                fl["hotkey"].setText(t.get("hotkey", "Hotkey"))
            if "hotkey_status" in fl:
                fl["hotkey_status"].setText(t.get("hotkey_status", "Hotkey status"))
            if "fps" in fl:
                fl["fps"].setText(t.get("fps", "FPS"))
            if "avg_fps" in fl:
                fl["avg_fps"].setText(t.get("avg_fps", "Avg FPS"))
            if "capture_time" in fl:
                fl["capture_time"].setText(t.get("capture_time", "Capture (ms)"))
            if "ocr_time" in fl:
                fl["ocr_time"].setText(t.get("ocr_time", "OCR (ms)"))
            if "total_time" in fl:
                fl["total_time"].setText(t.get("total_time", "Total (ms)"))
            if "frame_count" in fl:
                fl["frame_count"].setText(t.get("frame_count", "Frames"))
            if "python" in fl:
                fl["python"].setText(t.get("python", "Python"))
            if "os" in fl:
                fl["os"].setText(t.get("os", "OS"))
            if "opencv_ver" in fl:
                fl["opencv_ver"].setText(t.get("opencv_ver", "OpenCV"))
            if "numpy_ver" in fl:
                fl["numpy_ver"].setText(t.get("numpy_ver", "NumPy"))
            if "pil_ver" in fl:
                fl["pil_ver"].setText(t.get("pil_ver", "Pillow"))
            if "mss_ver" in fl:
                fl["mss_ver"].setText(t.get("mss_ver", "mss"))
            if "tesseract_ver" in fl:
                fl["tesseract_ver"].setText(t.get("tesseract_ver", "Tesseract"))
            if "winrt_ver" in fl:
                fl["winrt_ver"].setText(t.get("winrt_ver", "WinRT"))
            if "active_backend" in fl:
                fl["active_backend"].setText(t.get("active_backend", "Active Backend"))
            if "output_size" in fl:
                fl["output_size"].setText(t.get("output_size", "Window Size"))
            if "output_opacity" in fl:
                fl["output_opacity"].setText(t.get("output_opacity", "Opacity"))
            if "output_auto_show" in fl:
                fl["output_auto_show"].setText(t.get("output_auto_show", "Auto Show"))
            if "output_save_pos" in fl:
                fl["output_save_pos"].setText(t.get("output_save_pos", "Save Position"))

    def _on_ocr_engine_changed(self, _index: int) -> None:
        engine = self.ocr_engine_combo.currentData() or "auto"
        self.cfg["ocr_engine"] = engine
        save_config(self.cfg)
        self.backend = build_backend(preference=engine)
        self._refresh_backend_labels()

    def _on_preprocessing_changed(self, *_args) -> None:
        self.cfg.setdefault("ocr_preprocessing", {})
        self.cfg["ocr_preprocessing"]["contrast"] = self.preproc_contrast_slider.value() / 100.0
        self.cfg["ocr_preprocessing"]["sharpness"] = self.preproc_sharpness_slider.value() / 100.0
        self.cfg["ocr_preprocessing"]["denoise"] = int(self.preproc_denoise_slider.value())
        self.cfg["ocr_preprocessing"]["auto_enhance"] = bool(self.preproc_auto_enhance_check.isChecked())
        save_config(self.cfg)

    def _on_output_size_changed(self) -> None:
        w = self.output_width_spin.value()
        h = self.output_height_spin.value()
        self.output_window.resize(w, h)
        self.output_window._out_cfg["width"] = w
        self.output_window._out_cfg["height"] = h
        self.output_window._save_output_cfg()

    def _on_output_opacity_changed(self, value: int) -> None:
        self.output_window.setWindowOpacity(value / 100.0)
        self.output_window._out_cfg["opacity"] = value / 100.0
        self.output_window._save_output_cfg()

    def _on_output_auto_show_changed(self, checked: bool) -> None:
        self.output_window._out_cfg["auto_show"] = checked
        self.output_window._save_output_cfg()

    def _on_output_save_pos_changed(self, checked: bool) -> None:
        self.output_window._out_cfg["save_position"] = checked
        self.output_window._save_output_cfg()

    def _run_auto_adjust(self) -> None:
        frame = getattr(self, "_last_dialogue_raw", None)
        if frame is None:
            self.auto_adjust_status.setText("No frame - start capture first")
            return

        if not self.backend or self.backend.name == "none":
            self.auto_adjust_status.setText("No OCR backend available")
            return

        self.btn_auto_adjust.setEnabled(False)
        self.btn_auto_adjust.setText("Testing...")
        self.auto_adjust_status.setText("Testing filter combinations...")
        QApplication.processEvents()

        worker = AutoAdjustWorker(self, frame)
        worker.signals.done.connect(self._on_auto_adjust_done)
        QThreadPool.globalInstance().start(worker)

    def _on_auto_adjust_done(self, success: bool, message: str) -> None:
        self.btn_auto_adjust.setEnabled(True)
        self.btn_auto_adjust.setText("AUTO-ADJUST")
        if success:
            self.auto_adjust_status.setText(message)
            self._reprocess_last_filter_previews()
        else:
            self.auto_adjust_status.setText(f"Failed: {message}")

    def _stabilizer_settings(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.stabilization_enabled_check.isChecked()),
            "frames": int(self.stabilization_frames_spin.value()),
            "similarity_percent": int(self.stabilization_similarity_spin.value()),
        }

    def _quick_capture_settings(self) -> dict[str, Any]:
        modifier = self.quick_capture_modifier_combo.currentData()
        key = self.quick_capture_key_combo.currentData()
        return {
            "enabled": bool(self.quick_capture_enabled_check.isChecked()),
            "modifier": modifier if isinstance(modifier, str) else "ctrl+shift",
            "key": (key if isinstance(key, str) else "Q").upper(),
        }

    def _on_stabilization_settings_changed(self, *_args) -> None:
        self.cfg["stabilizer"] = self._stabilizer_settings()
        save_config(self.cfg)
        self._dialogue_gate.reset()
        self._menu_gate.reset()
        frames = self.stabilization_frames_spin.value()
        if self.stabilization_enabled_check.isChecked():
            self._set_status(f"Stabilization enabled ({frames} frames)")
        else:
            self._set_status("Stabilization disabled")

    def _update_quick_capture_ui(self) -> None:
        enabled = self.quick_capture_enabled_check.isChecked()
        modifier = str(self.quick_capture_modifier_combo.currentData() or "ctrl+shift")
        key = str(self.quick_capture_key_combo.currentData() or "Q")
        hotkey_text = hotkey_display_text(modifier, key)
        self.quick_capture_modifier_combo.setEnabled(enabled)
        self.quick_capture_key_combo.setEnabled(enabled)
        self.quick_translate_window.set_hotkey_text(hotkey_text)
        self.output_window.set_hotkey_text(hotkey_text)

    def _install_window_shortcuts(self) -> None:
        if not self._window_shortcuts:
            for owner in (self, self.output_window, self.quick_translate_window):
                shortcut = QShortcut(QKeySequence(), owner)
                shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
                shortcut.activated.connect(self.start_quick_capture_selection)
                self._window_shortcuts.append(shortcut)
        sequence = QKeySequence(
            hotkey_sequence_text(
                str(self.quick_capture_modifier_combo.currentData() or "ctrl+shift"),
                str(self.quick_capture_key_combo.currentData() or "Q"),
            )
        )
        for shortcut in self._window_shortcuts:
            shortcut.setKey(sequence)
            shortcut.setEnabled(True)

    def _on_quick_capture_settings_changed(self, *_args) -> None:
        self.cfg["quick_capture"] = self._quick_capture_settings()
        save_config(self.cfg)
        self._update_quick_capture_ui()
        self._install_window_shortcuts()
        self._update_global_hotkey_registration()

    def _unregister_global_hotkey(self) -> None:
        if USER32 is not None and self._global_hotkey_registered:
            USER32.UnregisterHotKey(self._global_hotkey_hwnd, self._global_hotkey_id)
        self._global_hotkey_registered = False
        self._global_hotkey_hwnd = 0

    def _update_global_hotkey_registration(self) -> None:
        self._unregister_global_hotkey()
        if not self.quick_capture_enabled_check.isChecked():
            self.quick_capture_status_label.setText("Global off, window shortcut on")
            return
        if USER32 is None or wintypes is None:
            self.quick_capture_status_label.setText("Windows only")
            return

        hwnd = int(self.winId())
        if not hwnd:
            self.quick_capture_status_label.setText("Window handle not ready")
            return

        cfg = self._quick_capture_settings()
        hotkey_text = hotkey_display_text(cfg["modifier"], cfg["key"])
        ok = USER32.RegisterHotKey(
            hwnd,
            self._global_hotkey_id,
            hotkey_modifier_flags(cfg["modifier"]) | MOD_NOREPEAT,
            hotkey_virtual_key(cfg["key"]),
        )
        self._global_hotkey_registered = bool(ok)
        self._global_hotkey_hwnd = hwnd if self._global_hotkey_registered else 0
        if self._global_hotkey_registered:
            self.quick_capture_status_label.setText(f"Ready: {hotkey_text}")
        else:
            self.quick_capture_status_label.setText(f"Register failed: {hotkey_text}")

    def nativeEvent(self, eventType, message):
        try:
            event_name = eventType.decode() if isinstance(eventType, (bytes, bytearray)) else str(eventType)
            if USER32 is not None and wintypes is not None and event_name in {
                "windows_generic_MSG",
                "windows_dispatcher_MSG",
            }:
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and int(msg.wParam) == self._global_hotkey_id:
                    QTimer.singleShot(0, self.start_quick_capture_selection)
                    return True, 0
        except Exception:
            log_exception_text(traceback.format_exc())
        return False, 0

    def _on_filter_preset_changed(self, _index: int) -> None:
        preset = self.filter_preset_combo.currentData()
        if preset == "custom":
            return
        self.apply_filter_preset(refresh_preview=True, update_status=True)

    def _on_color_mode_changed(self, _index: int) -> None:
        color_mode = self.filter_color_combo.currentData() or "gray"
        self.cfg.setdefault("opencv", {})["color_mode"] = color_mode
        save_config(self.cfg)
        self._reprocess_last_filter_previews()

    def _on_show_advanced_toggled(self, checked: bool) -> None:
        self.advanced_filter_panel.setVisible(checked)
        self._save_current_settings_to_config()

    def _on_live_preview_toggled(self, _checked: bool) -> None:
        self._save_current_settings_to_config()

    def _on_opencv_setting_changed(self, *_args) -> None:
        if not self._profile_apply_in_progress:
            if self.filter_preset_combo.currentData() != "custom":
                self.filter_preset_combo.blockSignals(True)
                self._set_combo_by_data(self.filter_preset_combo, "custom", "custom")
                self.filter_preset_combo.blockSignals(False)
            self.cfg["active_filter_profile"] = ""
            self._refresh_filter_profiles_combo()
        self._save_current_settings_to_config()
        self._refresh_backend_labels()
        self._reprocess_last_filter_previews()

    def _refresh_region_labels(self) -> None:
        dialogue = list_to_qrect(self.cfg["regions"].get("dialogue", [0, 0, 0, 0]))
        menu = list_to_qrect(self.cfg["regions"].get("menu", [0, 0, 0, 0]))
        self.dialogue_region_label.setText(
            f"Dialogue region: x={dialogue.x()} y={dialogue.y()} w={dialogue.width()} h={dialogue.height()}"
        )
        self.menu_region_label.setText(
            f"Menu region: x={menu.x()} y={menu.y()} w={menu.width()} h={menu.height()}"
        )

    def _current_capture_screen(self):
        idx = self.capture_screen_combo.currentIndex()
        if 0 <= idx < len(self.screens):
            return self.screens[idx]
        if self.screens:
            return self.screens[0]
        return None

    def _current_output_screen(self):
        idx = self.output_screen_combo.currentIndex()
        if 0 <= idx < len(self.screens):
            return self.screens[idx]
        if self.screens:
            return self.screens[0]
        return None

    def _current_translation_target(self) -> str:
        code = self.translation_target_combo.currentData()
        if isinstance(code, str) and code:
            return code
        return "tr"

    def _current_translation_source(self) -> str:
        code = self.translation_source_combo.currentData()
        if isinstance(code, str) and code:
            return code
        return "auto"

    def _collect_opencv_settings(self) -> dict[str, Any]:
        preset = self.filter_preset_combo.currentData()
        if not isinstance(preset, str):
            preset = "subtitle_auto"

        return {
            "enabled": bool(self.filter_enabled_check.isChecked()),
            "ocr_mode": self._current_ocr_mode(),
            "preset": preset,
            "color_mode": str(self.filter_color_combo.currentData() or "gray"),
            "show_advanced": bool(self.show_advanced_check.isChecked()),
            "live_preview": bool(self.live_preview_check.isChecked()),
            "invert": bool(self.filter_invert_check.isChecked()),
            "blur": int(self.filter_blur_spin.value()),
            "threshold": int(self.filter_threshold_spin.value()),
            "dilate_iter": int(self.filter_dilate_spin.value()),
        }

    def _save_current_settings_to_config(self) -> None:
        self.cfg["opencv"] = self._collect_opencv_settings()
        save_config(self.cfg)

    def _set_combo_by_data(self, combo: QComboBox, value: str, fallback: str | None = None) -> None:
        idx = combo.findData(value)
        if idx < 0 and fallback is not None:
            idx = combo.findData(fallback)
        combo.setCurrentIndex(max(0, idx))

    # ── OCR mode ──────────────────────────────────────────────────────────

    def _set_ocr_mode(self, mode: str) -> None:
        """Toggle between 'direct' (raw image) and 'filtered' OCR modes."""
        is_direct = mode == "direct"
        self.btn_ocr_mode_direct.setChecked(is_direct)
        self.btn_ocr_mode_filtered.setChecked(not is_direct)
        # Show/hide the filter controls when in direct mode
        self.filter_enabled_check.setEnabled(not is_direct)
        self.cfg.setdefault("opencv", {})["ocr_mode"] = mode
        save_config(self.cfg)

    def _current_ocr_mode(self) -> str:
        return "direct" if self.btn_ocr_mode_direct.isChecked() else "filtered"

    # ── Translation backend ────────────────────────────────────────────────

    def _on_translation_backend_changed(self, _index: int) -> None:
        preference = self.translation_backend_combo.currentData() or "google"
        self.cfg["translation_backend"] = preference
        save_config(self.cfg)
        # Swap out the backend instance
        if preference == "argos":
            if self._argos_backend is None:
                self._argos_backend = ArgosTranslateBackend()
            self.translation_backend = self._argos_backend
        else:
            self.translation_backend = build_translation_backend("google")
        self.btn_argos_packs.setVisible(preference == "argos")
        self._refresh_backend_labels()

    def _on_translation_source_changed(self, _index: int) -> None:
        source = self.translation_source_combo.currentData() or "auto"
        self.cfg["translation_source"] = source
        save_config(self.cfg)

    def _open_argos_pack_manager(self) -> None:
        if self._argos_backend is None:
            self._argos_backend = ArgosTranslateBackend()
        # Recreate if source/target changed since last open
        src = self._current_translation_source()
        tgt = self._current_translation_target()
        if self._argos_pack_manager is None or (
            getattr(self._argos_pack_manager, "_source", None) != (src if src != "auto" else "ja")
            or getattr(self._argos_pack_manager, "_target", None) != tgt
        ):
            if self._argos_pack_manager is not None:
                self._argos_pack_manager.close()
            self._argos_pack_manager = ArgosPackageManagerDialog(
                self._argos_backend,
                source_lang=src,
                target_lang=tgt,
                parent=None,
            )
        self._argos_pack_manager.show()
        self._argos_pack_manager.raise_()
        self._argos_pack_manager.activateWindow()

    # ── Color pickers ──────────────────────────────────────────────────────

    def _filter_control_widgets(self) -> list[QWidget]:
        return [
            self.filter_enabled_check,
            self.filter_preset_combo,
            self.filter_color_combo,
            self.show_advanced_check,
            self.live_preview_check,
            self.filter_invert_check,
            self.filter_blur_spin,
            self.filter_threshold_spin,
            self.filter_dilate_spin,
        ]

    def _apply_opencv_settings(
        self,
        settings: dict[str, Any],
        refresh_preview: bool = True,
        update_status: bool = True,
        status_text: str | None = None,
    ) -> None:
        self._profile_apply_in_progress = True
        try:
            for ctrl in self._filter_control_widgets():
                ctrl.blockSignals(True)

            self.filter_enabled_check.setChecked(bool(settings.get("enabled", True)))
            self._set_combo_by_data(self.filter_preset_combo, str(settings.get("preset", "custom")), "custom")
            self.show_advanced_check.setChecked(bool(settings.get("show_advanced", False)))
            self.live_preview_check.setChecked(bool(settings.get("live_preview", True)))
            self.filter_invert_check.setChecked(bool(settings.get("invert", False)))
            self.filter_blur_spin.setValue(int(settings.get("blur", 3)))
            self.filter_threshold_spin.setValue(int(settings.get("threshold", 170)))
            self.filter_dilate_spin.setValue(int(settings.get("dilate_iter", 1)))
            self._set_combo_by_data(self.filter_color_combo, str(settings.get("color_mode", "gray")), "gray")
        finally:
            for ctrl in self._filter_control_widgets():
                ctrl.blockSignals(False)

        self.advanced_filter_panel.setVisible(self.show_advanced_check.isChecked())
        self._on_opencv_setting_changed()
        self._profile_apply_in_progress = False
        if refresh_preview:
            self.refresh_filter_preview()
        if update_status and status_text:
            self._set_status(status_text)

    def _refresh_filter_profiles_combo(self) -> None:
        selected = self.cfg.get("active_filter_profile", "")
        if not isinstance(selected, str):
            selected = ""
        self.filter_profile_combo.blockSignals(True)
        self.filter_profile_combo.clear()
        self.filter_profile_combo.addItem("No saved filter", "")
        for name in sorted(self.cfg.get("filter_profiles", {}).keys()):
            self.filter_profile_combo.addItem(name, name)
        self._set_combo_by_data(self.filter_profile_combo, selected, "")
        self.filter_profile_combo.blockSignals(False)

    def save_filter_profile(self) -> None:
        current_name = self.filter_profile_combo.currentData()
        seed = current_name if isinstance(current_name, str) and current_name else "My Filter"
        name, ok = QInputDialog.getText(self, APP_NAME, "Filter profile name:", text=seed)
        if not ok:
            return
        normalized = name.strip()
        if not normalized:
            return

        settings = self._collect_opencv_settings()
        settings["preset"] = "custom"
        self.filter_preset_combo.blockSignals(True)
        self._set_combo_by_data(self.filter_preset_combo, "custom", "custom")
        self.filter_preset_combo.blockSignals(False)
        self.cfg["opencv"] = dict(settings)
        self.cfg.setdefault("filter_profiles", {})[normalized] = settings
        self.cfg["active_filter_profile"] = normalized
        save_config(self.cfg)
        self._refresh_filter_profiles_combo()
        self._set_status(f"Saved filter profile: {normalized}")

    def load_filter_profile(self) -> None:
        name = self.filter_profile_combo.currentData()
        if not isinstance(name, str) or not name:
            return
        settings = self.cfg.get("filter_profiles", {}).get(name)
        if not isinstance(settings, dict):
            return
        self.cfg["active_filter_profile"] = name
        save_config(self.cfg)
        self._apply_opencv_settings(
            settings,
            refresh_preview=True,
            update_status=True,
            status_text=f"Loaded filter profile: {name}",
        )

    def delete_filter_profile(self) -> None:
        name = self.filter_profile_combo.currentData()
        if not isinstance(name, str) or not name:
            return
        profiles = self.cfg.get("filter_profiles", {})
        if not isinstance(profiles, dict) or name not in profiles:
            return
        profiles.pop(name, None)
        if self.cfg.get("active_filter_profile") == name:
            self.cfg["active_filter_profile"] = ""
        save_config(self.cfg)
        self._refresh_filter_profiles_combo()
        self._set_status(f"Deleted filter profile: {name}")

    def _filter_presets(self) -> dict[str, dict[str, Any]]:
        return FILTER_PRESETS

    def apply_filter_preset(self, refresh_preview: bool = True, update_status: bool = True) -> None:
        preset = self.filter_preset_combo.currentData()
        if preset == "custom":
            return
        if not isinstance(preset, str):
            preset = "subtitle_auto"
        settings = self._filter_presets().get(preset, self._filter_presets()["subtitle_auto"])
        merged = self._collect_opencv_settings()
        merged.update(settings)
        merged["preset"] = preset
        self._apply_opencv_settings(
            merged,
            refresh_preview=refresh_preview,
            update_status=update_status,
            status_text=f"Filter preset applied: {preset}",
        )

    def _opencv_filter_enabled_effective(self) -> bool:
        return (
            self.filter_enabled_check.isChecked()
            and cv2 is not None
            and np is not None
        )

    def _opencv_filter_is_available(self) -> bool:
        return cv2 is not None and np is not None

    def _screen_pixmap(self, screen) -> QPixmap | None:
        if not self._capture or not screen:
            return None
        geo = screen.geometry()
        try:
            shot = self._capture.grab(
                {
                    "left": int(geo.x()),
                    "top": int(geo.y()),
                    "width": int(geo.width()),
                    "height": int(geo.height()),
                }
            )
            image = QImage(shot.bgra, shot.width, shot.height, shot.width * 4, QImage.Format.Format_BGRA8888).copy()
            return QPixmap.fromImage(image)
        except Exception:
            return None

    def pick_region(self, key: str) -> None:
        screen = self._current_capture_screen()
        if not screen:
            QMessageBox.warning(self, APP_NAME, "No active screen found.")
            return

        background = self._screen_pixmap(screen)
        title = f"Select {key} region: drag, Enter=accept, Esc=cancel"
        selector = RegionSelector(screen, title=title, background=background)
        self._selector = selector

        def on_selected(rect: QRect) -> None:
            self.cfg["regions"][key] = qrect_to_list(rect)
            save_config(self.cfg)
            self._reset_stabilizers()
            self._refresh_region_labels()
            QMessageBox.information(
                self,
                "Saved",
                f"{key} = x:{rect.x()} y:{rect.y()} w:{rect.width()} h:{rect.height()}",
            )
            self._selector = None

        def on_canceled() -> None:
            self._selector = None

        selector.selected.connect(on_selected)
        selector.canceled.connect(on_canceled)
        selector.showFullScreen()
        selector.activateWindow()

    def _stabilizer_runtime_values(self) -> tuple[bool, int, float]:
        enabled = bool(self.stabilization_enabled_check.isChecked())
        frames = int(self.stabilization_frames_spin.value())
        similarity = max(0.7, min(1.0, self.stabilization_similarity_spin.value() / 100.0))
        return enabled, frames, similarity

    def _reset_stabilizers(self) -> None:
        self._dialogue_gate.reset()
        self._menu_gate.reset()

    def _apply_live_results(
        self,
        dialogue_text: str,
        menu_text: str,
        dialogue_translated: str,
        menu_translated: str,
        force_immediate: bool = False,
    ) -> None:
        enabled, frames, similarity = self._stabilizer_runtime_values()
        dialogue_state = self._dialogue_gate.push(
            dialogue_text,
            dialogue_translated,
            enabled=enabled and not force_immediate,
            required_frames=frames,
            similarity_threshold=similarity,
        )
        menu_state = self._menu_gate.push(
            menu_text,
            menu_translated,
            enabled=enabled and not force_immediate,
            required_frames=frames,
            similarity_threshold=similarity,
        )

        committed_dialogue = dialogue_state["source"]
        committed_menu = menu_state["source"]
        committed_dialogue_tr = dialogue_state["translated"]
        committed_menu_tr = menu_state["translated"]

        preview_dialogue = dialogue_text or committed_dialogue
        preview_menu = menu_text or committed_menu
        preview_dialogue_tr = committed_dialogue_tr or dialogue_translated
        preview_menu_tr = committed_menu_tr or menu_translated
        output_status = None

        if not force_immediate and enabled and (not dialogue_state["stable"] or not menu_state["stable"]):
            progress = []
            if not dialogue_state["stable"]:
                progress.append(f"dialogue {dialogue_state['pending_hits']}/{dialogue_state['required_frames']}")
            if not menu_state["stable"]:
                progress.append(f"menu {menu_state['pending_hits']}/{menu_state['required_frames']}")
            output_status = "Stabilizing"
            self._set_status("Stabilizing " + ", ".join(progress))
        elif committed_dialogue_tr or committed_menu_tr:
            self._set_status("OCR + translation updated")
        elif committed_dialogue or committed_menu:
            self._set_status("OCR updated")
        else:
            self._set_status("Running - no text detected")

        self.output_window.update_text(
            preview_dialogue,
            preview_menu,
            preview_dialogue_tr,
            preview_menu_tr,
            self._current_translation_target(),
            status_override=output_status,
        )

    def _resume_live_after_quick_capture(self) -> None:
        if self._quick_capture_resume_live and not self.timer.isActive() and self.btn_stop.isEnabled():
            self.timer.start(self.interval_spin.value())
        self._quick_capture_resume_live = False

    def _hide_windows_for_quick_capture(self, capture_screen) -> None:
        self._quick_hidden_windows = []
        if not capture_screen:
            return
        capture_geo = capture_screen.geometry()
        for window in (self.output_window, self.quick_translate_window):
            if not window.isVisible():
                continue
            center = window.frameGeometry().center()
            if capture_geo.contains(center):
                self._quick_hidden_windows.append(window)
                window.hide()
        if self._quick_hidden_windows:
            QApplication.processEvents()

    def _restore_windows_after_quick_capture(self) -> None:
        if not self._quick_hidden_windows:
            return
        for window in self._quick_hidden_windows:
            window.show()
            window.raise_()
        self._quick_hidden_windows = []
        QApplication.processEvents()

    def start_quick_capture_selection(self) -> None:
        if self._quick_capture_running or self._quick_selector is not None:
            return
        if self.backend.name == "none":
            QMessageBox.warning(self, APP_NAME, "No OCR backend available for quick capture.")
            return
        if not self._capture or not Image:
            QMessageBox.warning(self, APP_NAME, "Capture backend is not available.")
            return

        screen = self._current_capture_screen()
        if not screen:
            QMessageBox.warning(self, APP_NAME, "No active capture screen found.")
            return

        self._quick_capture_resume_live = self.timer.isActive()
        if self._quick_capture_resume_live:
            self.timer.stop()

        self._hide_windows_for_quick_capture(screen)
        output_screen = self._current_output_screen()
        if output_screen:
            self.quick_translate_window.move_to_screen(output_screen)
        self.quick_translate_window.set_status("Selecting")
        if self.quick_translate_window not in self._quick_hidden_windows:
            self.quick_translate_window.show()
            self.quick_translate_window.raise_()

        background = self._screen_pixmap(screen)
        title = "Quick capture: drag a region, Enter=translate, Esc=cancel"
        selector = RegionSelector(screen, title=title, background=background)
        self._quick_selector = selector

        def on_selected(rect: QRect) -> None:
            self._quick_selector = None
            self._run_quick_capture(rect)

        def on_canceled() -> None:
            self._quick_selector = None
            self._restore_windows_after_quick_capture()
            self.quick_translate_window.set_status("Cancelled")
            self._resume_live_after_quick_capture()

        selector.selected.connect(on_selected)
        selector.canceled.connect(on_canceled)
        selector.showFullScreen()
        selector.activateWindow()

    def _run_quick_capture(self, rect: QRect) -> None:
        try:
            bundle = self._grab_region_bundle(rect)
        except Exception as exc:
            self._restore_windows_after_quick_capture()
            self.quick_translate_window.set_status(f"Capture error: {exc}")
            self._resume_live_after_quick_capture()
            return

        if not bundle:
            self._restore_windows_after_quick_capture()
            self.quick_translate_window.set_status("Capture failed")
            self._resume_live_after_quick_capture()
            return

        candidates: list[Any] = []
        seen_keys: set[tuple[Any, ...]] = set()

        def add_candidate(image_obj) -> None:
            if image_obj is None or Image is None:
                return
            key = (getattr(image_obj, "mode", None), getattr(image_obj, "size", None), image_obj.tobytes())
            if key in seen_keys:
                return
            seen_keys.add(key)
            candidates.append(image_obj)

        raw_pil = bundle.get("raw_pil")
        primary = bundle.get("ocr_image")
        add_candidate(primary)
        if raw_pil is not None:
            add_candidate(raw_pil)
            if ImageOps is not None:
                grayscale = ImageOps.grayscale(raw_pil)
                add_candidate(ImageOps.autocontrast(grayscale, cutoff=1))
                add_candidate(ImageOps.autocontrast(grayscale, cutoff=0))
                add_candidate(ImageOps.invert(ImageOps.autocontrast(grayscale, cutoff=1)))

        self._quick_capture_running = True
        self.quick_translate_window.set_status("Processing")
        worker = QuickCaptureWorker(
            self.backend,
            self.translation_backend,
            candidates,
            self.translation_enabled_check.isChecked(),
            self._current_translation_target(),
            source_lang=self._current_translation_source(),
        )
        worker.signals.done.connect(self._on_quick_capture_done, Qt.ConnectionType.QueuedConnection)
        self._current_quick_capture_worker = worker
        self._active_workers.append(worker)
        self.thread_pool.start(worker)

    def _on_quick_capture_done(self, source_text: str, translated_text: str, error_text: str) -> None:
        self._quick_capture_running = False
        worker = self._current_quick_capture_worker
        self._current_quick_capture_worker = None
        if worker is not None:
            self._release_worker(worker)
        if self._is_closing:
            return
        if error_text:
            self._restore_windows_after_quick_capture()
            self.quick_translate_window.set_status("OCR error")
            self._set_status(f"Quick capture error: {error_text.strip()}")
            self._resume_live_after_quick_capture()
            return

        output_screen = self._current_output_screen()
        if output_screen:
            self.quick_translate_window.move_to_screen(output_screen)
        self._restore_windows_after_quick_capture()
        self.quick_translate_window.show()
        self.quick_translate_window.raise_()
        self.quick_translate_window.update_text(source_text, translated_text, self._current_translation_target())
        if translated_text:
            self.quick_translate_window.set_status("Translated")
            self._set_status("Quick capture translated")
        elif source_text:
            self.quick_translate_window.set_status("OCR Only")
            self._set_status("Quick capture OCR only")
        else:
            self.quick_translate_window.set_status("No text (fallback tried)")
            self._set_status("Quick capture found no text")
        self._resume_live_after_quick_capture()

    def _grab_region_bundle(self, region: QRect) -> dict[str, Any] | None:
        if not self._capture:
            return None
        screen = self._current_capture_screen()
        if not screen:
            return None
        geo = screen.geometry()

        left = int(geo.x() + region.x())
        top = int(geo.y() + region.y())
        width = int(region.width())
        height = int(region.height())

        if width <= 0 or height <= 0:
            return None

        shot = self._capture.grab(
            {
                "left": left,
                "top": top,
                "width": width,
                "height": height,
            }
        )

        if not Image:
            return None

        raw_rgb = None
        if np is not None:
            raw_rgb = np.frombuffer(shot.rgb, dtype=np.uint8).reshape((shot.height, shot.width, 3))

        pil_raw = Image.frombytes("RGB", shot.size, shot.rgb)

        if self._current_ocr_mode() == "direct":
            pil_for_ocr = self._fast_enhance_for_ocr(pil_raw)
            pil_for_ocr = self._upscale_for_ocr(pil_for_ocr, target_height=128)
            return {
                "ocr_image": pil_for_ocr,
                "preview": raw_rgb,
                "raw_rgb": raw_rgb,
                "raw_pil": pil_raw,
            }

        if self._opencv_filter_enabled_effective() and raw_rgb is not None:
            best, preview = self._smart_ocr_filter(raw_rgb)
            pil_for_ocr = self._pil_from_np(best)
            pil_for_ocr = self._upscale_for_ocr(pil_for_ocr, target_height=128)
            return {
                "ocr_image": pil_for_ocr,
                "preview": preview,
                "raw_rgb": raw_rgb,
                "raw_pil": pil_raw,
            }

        fallback = self._preprocess_image(pil_raw)
        fallback = self._upscale_for_ocr(fallback, target_height=128)
        preview = np.array(fallback) if fallback is not None and np is not None else None
        return {
            "ocr_image": fallback,
            "preview": preview,
            "raw_rgb": raw_rgb,
            "raw_pil": pil_raw,
        }

    def _grab_region_processed(self, region: QRect):
        bundle = self._grab_region_bundle(region)
        if not bundle:
            return None, None, None
        return bundle["ocr_image"], bundle["preview"], bundle["raw_rgb"]

    def _smart_ocr_filter(self, rgb_frame):
        cfg = self.cfg.get("opencv", {})
        return smart_ocr_filter(rgb_frame, cfg)

    def _auto_adjust_filter(self, rgb_frame):
        if rgb_frame is None or np is None or not self.backend:
            return False, "No frame or backend"

        gray = cv2.cvtColor(rgb_frame, cv2.COLOR_RGB2GRAY)

        best_score = -1
        best_params = {}

        blur_range = [0, 1, 2, 3, 5]
        threshold_range = [100, 120, 140, 160, 170, 180, 200, 220]
        dilate_range = [0, 1, 2]

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))

        for blur in blur_range:
            blurred = gray
            if blur > 0:
                ksize = blur * 2 + 1
                blurred = cv2.GaussianBlur(gray, (ksize, ksize), 0)

            for threshold in threshold_range:
                _, binary = cv2.threshold(blurred, threshold, 255, cv2.THRESH_BINARY)

                for dilate in dilate_range:
                    result = binary
                    if dilate > 0:
                        result = cv2.dilate(binary, kernel, iterations=dilate)

                    pil_img = Image.fromarray(result, mode="L")
                    pil_img = self._upscale_for_ocr(pil_img, target_height=128)

                    try:
                        text = self.backend.recognize(pil_img)
                    except Exception:
                        text = ""

                    score = self._score_ocr_result(text)

                    if score > best_score:
                        best_score = score
                        best_params = {
                            "blur": blur,
                            "threshold": threshold,
                            "dilate_iter": dilate,
                        }

        if not best_params:
            return False, "No valid results"

        self.cfg.setdefault("opencv", {}).update(best_params)
        self.filter_blur_spin.setValue(best_params["blur"])
        self.filter_threshold_spin.setValue(best_params["threshold"])
        self.filter_dilate_spin.setValue(best_params["dilate_iter"])

        save_config(self.cfg)
        self._reprocess_last_filter_previews()

        return True, f"Best: blur={best_params['blur']}, threshold={best_params['threshold']}, dilate={best_params['dilate_iter']} (score={best_score})"

    @staticmethod
    def _score_ocr_result(text: str) -> int:
        return score_ocr_result(text)

    def _upscale_for_ocr(self, pil_image, target_height=128):
        return upscale_for_ocr(pil_image, target_height)

    def _pil_from_np(self, array):
        return pil_from_np(array)

    def _enhance_image(self, image: Image.Image, settings: dict = None) -> Image.Image:
        return enhance_image(image, settings)

    def _fast_enhance_for_ocr(self, image: Image.Image) -> Image.Image:
        return fast_enhance_for_ocr(image)

    def _preprocess_image(self, image):
        if image is None:
            return None

        enhanced = self._enhance_image(image)

        if not ImageOps:
            return enhanced

        gray = ImageOps.grayscale(enhanced)
        boosted = ImageOps.autocontrast(gray, cutoff=1)

        if self.filter_enabled_check.isChecked():
            threshold = int(self.filter_threshold_spin.value())
            boosted = boosted.point(lambda p, t=threshold: 255 if p >= t else 0)
            if self.filter_invert_check.isChecked():
                boosted = ImageOps.invert(boosted)

        return boosted

    def _set_filter_preview_image(self, label: QLabel, image_array) -> None:
        if image_array is None or np is None:
            return
        if len(image_array.shape) == 2:
            h, w = image_array.shape
            qimg = QImage(
                image_array.data,
                w,
                h,
                w,
                QImage.Format.Format_Grayscale8,
            ).copy()
        else:
            h, w, _ = image_array.shape
            qimg = QImage(
                image_array.data,
                w,
                h,
                w * 3,
                QImage.Format.Format_RGB888,
            ).copy()

        pix = QPixmap.fromImage(qimg).scaled(
            label.width(),
            label.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(pix)

    def _reprocess_last_filter_previews(self) -> None:
        if self._last_dialogue_raw is not None:
            if self._opencv_filter_enabled_effective():
                d_best, d_view = self._smart_ocr_filter(self._last_dialogue_raw)
                d_view = d_best
            else:
                d_pil = self._preprocess_image(self._pil_from_np(self._last_dialogue_raw))
                d_view = np.array(d_pil) if d_pil is not None and np is not None else None
            self._set_filter_preview_image(self.dialogue_filter_image_label, d_view)
        if self._last_menu_raw is not None:
            if self._opencv_filter_enabled_effective():
                m_best, m_view = self._smart_ocr_filter(self._last_menu_raw)
                m_view = m_best
            else:
                m_pil = self._preprocess_image(self._pil_from_np(self._last_menu_raw))
                m_view = np.array(m_pil) if m_pil is not None and np is not None else None
            self._set_filter_preview_image(self.menu_filter_image_label, m_view)

    def refresh_filter_preview(self) -> None:
        regions = self._regions_ready()
        if not regions:
            self._set_status("Select valid dialogue and menu regions first")
            return
        dialogue_region, menu_region = regions
        dialogue_img, dialogue_preview, dialogue_raw = self._grab_region_processed(dialogue_region)
        menu_img, menu_preview, menu_raw = self._grab_region_processed(menu_region)
        if dialogue_img is None or menu_img is None:
            self._set_status("Failed to capture region for preview")
            return
        self._last_dialogue_raw = dialogue_raw
        self._last_menu_raw = menu_raw
        self._set_filter_preview_image(self.dialogue_filter_image_label, dialogue_preview)
        self._set_filter_preview_image(self.menu_filter_image_label, menu_preview)
        self._set_status("Filter preview refreshed")

    def _regions_ready(self) -> tuple[QRect, QRect] | None:
        dialogue = list_to_qrect(self.cfg["regions"].get("dialogue", [0, 0, 0, 0]))
        menu = list_to_qrect(self.cfg["regions"].get("menu", [0, 0, 0, 0]))
        if not is_valid_region(dialogue) or not is_valid_region(menu):
            return None
        return dialogue, menu

    def start_capture(self) -> None:
        if self._quick_capture_running:
            self._set_status("Quick capture is running")
            return
        if self.backend.name == "none":
            QMessageBox.warning(
                self,
                APP_NAME,
                "No OCR backend available. Install winsdk or pytesseract.",
            )
            return

        regions = self._regions_ready()
        if not regions:
            QMessageBox.warning(
                self,
                APP_NAME,
                "Please select both dialogue and menu regions first.",
            )
            return

        if not self._capture:
            QMessageBox.warning(self, APP_NAME, "mss is not available.")
            return

        if self.translation_enabled_check.isChecked() and self.translation_backend.name == "none":
            QMessageBox.warning(
                self,
                APP_NAME,
                "Translation backend is not available. Install deep-translator.",
            )
            return

        output_screen = self._current_output_screen()
        if output_screen:
            self.output_window.move_to_screen(output_screen)

        self._reset_stabilizers()
        if self.output_window._out_cfg.get("auto_show", True):
            self.output_window.show()
            self.output_window.raise_()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._frame_count = 0
        self._frame_times.clear()
        self._last_tick_time = 0.0
        self._capture_ms = 0.0
        self._ocr_ms = 0.0
        self.timer.start(self.interval_spin.value())
        self._set_status("Running")

    def stop_capture(self) -> None:
        if self.timer.isActive():
            self.timer.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._ocr_running = False
        self._quick_capture_resume_live = False
        self._reset_stabilizers()
        self._frame_count = 0
        self._frame_times.clear()
        self._last_tick_time = 0.0
        self._capture_ms = 0.0
        self._ocr_ms = 0.0
        self._set_status("Stopped")
        self._update_perf_labels(0.0, 0.0)

    def run_test_once(self) -> None:
        if self._ocr_running:
            return
        if self.backend.name == "none":
            QMessageBox.warning(
                self,
                APP_NAME,
                "No OCR backend available. Install winsdk or pytesseract.",
            )
            return
        if self.translation_enabled_check.isChecked() and self.translation_backend.name == "none":
            QMessageBox.warning(
                self,
                APP_NAME,
                "Translation backend is not available. Install deep-translator.",
            )
            return
        self._test_mode = True
        self._tick()

    def _tick(self) -> None:
        if self._ocr_running or self._quick_capture_running or self._is_closing:
            return

        now = time.time()
        if self._last_tick_time > 0:
            delta = now - self._last_tick_time
            self._frame_times.append(delta)
            if len(self._frame_times) > 30:
                self._frame_times.pop(0)
        self._last_tick_time = now

        regions = self._regions_ready()
        if not regions:
            self._set_status("Waiting for valid regions")
            return

        dialogue_region, menu_region = regions

        t0 = time.time()
        try:
            dialogue_img, dialogue_preview, dialogue_raw = self._grab_region_processed(dialogue_region)
            menu_img, menu_preview, menu_raw = self._grab_region_processed(menu_region)
        except Exception as exc:
            self._set_status(f"Capture error: {exc}")
            return
        self._capture_ms = (time.time() - t0) * 1000

        if dialogue_img is None or menu_img is None:
            self._set_status("Capture failed for one or more regions")
            return

        self._last_dialogue_raw = dialogue_raw
        self._last_menu_raw = menu_raw
        if self.live_preview_check.isChecked():
            self._set_filter_preview_image(self.dialogue_filter_image_label, dialogue_preview)
            self._set_filter_preview_image(self.menu_filter_image_label, menu_preview)

        self._frame_count += 1
        self._update_frame_info()

        self._ocr_running = True
        self._ocr_start_time = time.time()
        worker = OCRWorker(
            self.backend,
            self.translation_backend,
            dialogue_img,
            menu_img,
            self.translation_enabled_check.isChecked(),
            self._current_translation_target(),
            source_lang=self._current_translation_source(),
        )
        worker.signals.done.connect(self._on_worker_done, Qt.ConnectionType.QueuedConnection)
        self._current_ocr_worker = worker
        self._active_workers.append(worker)
        self.thread_pool.start(worker)

    def _on_worker_done(
        self,
        dialogue_text: str,
        menu_text: str,
        dialogue_translated: str,
        menu_translated: str,
        error_text: str,
    ) -> None:
        self._ocr_ms = (time.time() - getattr(self, "_ocr_start_time", time.time())) * 1000
        self._ocr_running = False
        was_test = getattr(self, "_test_mode", False)
        self._test_mode = False
        worker = self._current_ocr_worker
        self._current_ocr_worker = None
        if worker is not None:
            self._release_worker(worker)
        if self._is_closing:
            return

        if error_text:
            self._set_status(f"OCR error: {error_text.strip()}")
            if was_test:
                QMessageBox.warning(self, APP_NAME, f"OCR error:\n{error_text.strip()}")
            return

        if was_test:
            self._show_test_result_dialog(
                dialogue_text, menu_text, dialogue_translated, menu_translated
            )
            return

        self._apply_live_results(
            dialogue_text,
            menu_text,
            dialogue_translated,
            menu_translated,
            force_immediate=not self.timer.isActive(),
        )
        self._update_frame_info()

    def _release_worker(self, worker: QRunnable) -> None:
        if worker in self._active_workers:
            self._active_workers.remove(worker)

    def _show_test_result_dialog(
        self,
        dialogue_text: str,
        menu_text: str,
        dialogue_translated: str,
        menu_translated: str,
    ) -> None:
        dialog = OCRTestResultDialog(
            self,
            dialogue_text=dialogue_text,
            menu_text=menu_text,
            dialogue_translated=dialogue_translated,
            menu_translated=menu_translated,
            translation_enabled=self.translation_enabled_check.isChecked(),
            target_lang=self._current_translation_target(),
            ocr_time_ms=self._ocr_ms,
            capture_time_ms=self._capture_ms,
            backend_name=self.backend.name,
        )
        dialog.exec()


class OCRTestResultDialog(QDialog):
    def __init__(
        self,
        parent=None,
        dialogue_text: str = "",
        menu_text: str = "",
        dialogue_translated: str = "",
        menu_translated: str = "",
        translation_enabled: bool = False,
        target_lang: str = "en",
        ocr_time_ms: float = 0.0,
        capture_time_ms: float = 0.0,
        backend_name: str = "unknown",
    ):
        super().__init__(parent)
        self.setWindowTitle("OCR Test Result")
        self.setMinimumSize(520, 420)
        self.setMaximumSize(700, 600)
        self.setModal(False)

        tc = get_theme_colors("dark")
        self.setStyleSheet(f"""
            QDialog {{
                background: {tc['bg_primary']};
                color: {tc['text_primary']};
            }}
            QLabel#DialogTitle {{
                color: {tc['accent']};
                font-size: 16px;
                font-weight: 800;
            }}
            QLabel#DialogSubtitle {{
                color: {tc['text_muted']};
                font-size: 11px;
            }}
            QLabel#SectionLabel {{
                color: {tc['group_title']};
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 1px;
            }}
            QLabel#TextLabel {{
                color: {tc['text_primary']};
                font-size: 13px;
                background: {tc['bg_secondary']};
                border: 1px solid {tc['border']};
                border-radius: 8px;
                padding: 10px 12px;
            }}
            QLabel#EmptyLabel {{
                color: {tc['text_muted']};
                font-size: 12px;
                font-style: italic;
                background: {tc['bg_secondary']};
                border: 1px solid {tc['border']};
                border-radius: 8px;
                padding: 10px 12px;
            }}
            QLabel#TimeLabel {{
                color: {tc['text_secondary']};
                font-size: 11px;
            }}
            QPushButton {{
                padding: 6px 16px;
                background: {tc['bg_secondary']};
                color: {tc['text_secondary']};
                border: 1px solid {tc['border']};
                border-radius: 6px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {tc['bg_elevated']};
                border-color: {tc['border_active']};
                color: {tc['text_primary']};
            }}
            QPushButton#BtnCopy {{
                background: {tc['accent_bg']};
                color: {tc['accent']};
                border-color: {tc['accent_bg']};
                font-weight: 700;
            }}
            QPushButton#BtnCopy:hover {{
                background: {tc['accent']};
                color: {tc['bg_primary']};
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("OCR Test Result")
        title.setObjectName("DialogTitle")
        subtitle = QLabel(f"{backend_name.upper()}  |  {ocr_time_ms:.0f}ms OCR  |  {capture_time_ms:.0f}ms capture")
        subtitle.setObjectName("DialogSubtitle")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(subtitle)
        root.addLayout(header)

        sep = QFrame()
        sep.setObjectName("Separator")
        sep.setFixedHeight(1)
        root.addWidget(sep)

        has_dialogue = bool(dialogue_text.strip())
        has_menu = bool(menu_text.strip())

        if has_dialogue:
            d_header = QHBoxLayout()
            d_label = QLabel("DIALOGUE")
            d_label.setObjectName("SectionLabel")
            d_header.addWidget(d_label)
            d_header.addStretch()
            if has_dialogue:
                d_copy = QPushButton("Copy")
                d_copy.setObjectName("BtnCopy")
                d_copy.setFixedWidth(60)
                d_copy.clicked.connect(lambda: QApplication.clipboard().setText(dialogue_text))
                d_header.addWidget(d_copy)
            root.addLayout(d_header)

            d_text = QLabel(dialogue_text.strip() if dialogue_text.strip() else "(no text detected)")
            d_text.setObjectName("TextLabel" if dialogue_text.strip() else "EmptyLabel")
            d_text.setWordWrap(True)
            d_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            root.addWidget(d_text)

            if translation_enabled and dialogue_translated.strip():
                dt_label = QLabel("TRANSLATED")
                dt_label.setObjectName("SectionLabel")
                root.addWidget(dt_label)
                dt_text = QLabel(dialogue_translated.strip())
                dt_text.setObjectName("TextLabel")
                dt_text.setWordWrap(True)
                dt_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                root.addWidget(dt_text)

        if has_menu:
            m_header = QHBoxLayout()
            m_label = QLabel("MENU")
            m_label.setObjectName("SectionLabel")
            m_header.addWidget(m_label)
            m_header.addStretch()
            m_copy = QPushButton("Copy")
            m_copy.setObjectName("BtnCopy")
            m_copy.setFixedWidth(60)
            m_copy.clicked.connect(lambda: QApplication.clipboard().setText(menu_text))
            m_header.addWidget(m_copy)
            root.addLayout(m_header)

            m_text = QLabel(menu_text.strip() if menu_text.strip() else "(no text detected)")
            m_text.setObjectName("TextLabel" if menu_text.strip() else "EmptyLabel")
            m_text.setWordWrap(True)
            m_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            root.addWidget(m_text)

            if translation_enabled and menu_translated.strip():
                mt_label = QLabel("TRANSLATED")
                mt_label.setObjectName("SectionLabel")
                root.addWidget(mt_label)
                mt_text = QLabel(menu_translated.strip())
                mt_text.setObjectName("TextLabel")
                mt_text.setWordWrap(True)
                mt_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                root.addWidget(mt_text)

        if not has_dialogue and not has_menu:
            empty = QLabel("No text detected in any region.\nTry adjusting filter settings or region selection.")
            empty.setObjectName("EmptyLabel")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            root.addWidget(empty)

        root.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        all_text = dialogue_text + "\n" + menu_text
        all_translated = dialogue_translated + "\n" + menu_translated
        copy_all = QPushButton("Copy All")
        copy_all.setObjectName("BtnCopy")
        copy_all.clicked.connect(lambda: QApplication.clipboard().setText(
            all_translated.strip() if translation_enabled and all_translated.strip() else all_text.strip()
        ))
        btn_row.addWidget(copy_all)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)


def main() -> int:
    def _handle_exception(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        show_fatal_error(text)

    sys.excepthook = _handle_exception
    set_windows_app_user_model_id()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    icon = build_app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    if not acquire_single_instance_lock():
        QMessageBox.warning(
            None,
            APP_NAME,
            "Syncra is already running.\n\nClose the existing window before starting a second instance.",
        )
        return 0

    try:
        win = MainWindow()
        win.show()
    except Exception:
        show_fatal_error(traceback.format_exc())
        release_single_instance_lock()
        return 1

    try:
        return app.exec()
    finally:
        release_single_instance_lock()


if __name__ == "__main__":
    raise SystemExit(main())
