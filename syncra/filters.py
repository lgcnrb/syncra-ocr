from __future__ import annotations

from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps


FILTER_PRESETS: dict[str, dict[str, Any]] = {
    "subtitle_auto": {"invert": False, "blur": 3, "threshold": 180, "dilate_iter": 1, "color_mode": "gray"},
    "wuwa_dialogue": {"invert": False, "blur": 3, "threshold": 170, "dilate_iter": 1, "color_mode": "gray"},
    "anime_rpg": {"invert": False, "blur": 2, "threshold": 175, "dilate_iter": 1, "color_mode": "gray"},
    "white_text": {"invert": False, "blur": 2, "threshold": 180, "dilate_iter": 1, "color_mode": "white"},
    "white_yellow": {"invert": False, "blur": 3, "threshold": 180, "dilate_iter": 1, "color_mode": "white_yellow"},
    "high_contrast": {"invert": False, "blur": 3, "threshold": 140, "dilate_iter": 2, "color_mode": "gray"},
}

COLOR_MODES = {
    "gray": "Grayscale Threshold",
    "white": "White Text Only",
    "white_yellow": "White + Yellow Text",
    "custom_hsv": "Custom HSV Range",
}


def smart_ocr_filter(rgb_frame: np.ndarray, cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    if rgb_frame is None or cv2 is None or np is None:
        if rgb_frame is not None:
            return cv2.cvtColor(rgb_frame, cv2.COLOR_RGB2GRAY), cv2.cvtColor(rgb_frame, cv2.COLOR_RGB2GRAY)
        return np.zeros((100, 100), dtype=np.uint8), np.zeros((100, 100), dtype=np.uint8)

    invert = bool(cfg.get("invert", False))
    blur = int(cfg.get("blur", 3))
    threshold = int(cfg.get("threshold", 170))
    dilate = int(cfg.get("dilate_iter", 1))
    color_mode = str(cfg.get("color_mode", "gray"))

    if color_mode == "white":
        binary = _filter_color_range(rgb_frame, (0, 0, 200), (180, 50, 255))
    elif color_mode == "white_yellow":
        white = _filter_color_range(rgb_frame, (0, 0, 200), (180, 50, 255))
        yellow = _filter_color_range(rgb_frame, (15, 80, 150), (45, 255, 255))
        binary = cv2.bitwise_or(white, yellow)
    elif color_mode == "custom_hsv":
        h_min = int(cfg.get("h_min", 0))
        h_max = int(cfg.get("h_max", 180))
        s_min = int(cfg.get("s_min", 0))
        s_max = int(cfg.get("s_max", 255))
        v_min = int(cfg.get("v_min", 0))
        v_max = int(cfg.get("v_max", 255))
        binary = _filter_color_range(rgb_frame, (h_min, s_min, v_min), (h_max, s_max, v_max))
    else:
        gray = cv2.cvtColor(rgb_frame, cv2.COLOR_RGB2GRAY)
        if blur > 0:
            ksize = blur * 2 + 1
            gray = cv2.GaussianBlur(gray, (ksize, ksize), 0)
        _, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)

    if color_mode in ("white", "white_yellow", "custom_hsv"):
        if blur > 0:
            ksize = blur * 2 + 1
            binary = cv2.GaussianBlur(binary, (ksize, ksize), 0)
        _, binary = cv2.threshold(binary, threshold, 255, cv2.THRESH_BINARY)

    if dilate > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        binary = cv2.dilate(binary, kernel, iterations=dilate)

    if invert:
        cv2.bitwise_not(binary, binary)

    return binary, binary


def _filter_color_range(rgb_frame: np.ndarray, lower_hsv: tuple, upper_hsv: tuple) -> np.ndarray:
    hsv = cv2.cvtColor(rgb_frame, cv2.COLOR_RGB2HSV)
    lower = np.array(lower_hsv, dtype=np.uint8)
    upper = np.array(upper_hsv, dtype=np.uint8)
    mask = cv2.inRange(hsv, lower, upper)
    return mask


def upscale_for_ocr(pil_image: Image.Image, target_height: int = 128) -> Image.Image:
    if pil_image is None:
        return Image.new("L", (128, 32), 0)
    w, h = pil_image.size
    if h <= 0:
        return pil_image
    if h >= target_height:
        return pil_image
    scale = target_height / h
    new_w = int(w * scale)
    new_h = int(h * scale)
    return pil_image.resize((new_w, new_h), Image.Resampling.LANCZOS)


def fast_enhance_for_ocr(pil_image: Image.Image) -> Image.Image:
    if pil_image is None or cv2 is None:
        return pil_image or Image.new("L", (100, 30), 0)
    arr = np.array(pil_image.convert("L"))
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    arr = clahe.apply(arr)
    arr = cv2.convertScaleAbs(arr, alpha=1.2, beta=10)
    return Image.fromarray(arr, mode="L")


def enhance_image(pil_image: Image.Image, settings: dict | None = None) -> Image.Image:
    if pil_image is None:
        return pil_image
    settings = settings or {}
    contrast = float(settings.get("contrast", 1.2))
    sharpness = float(settings.get("sharpness", 1.0))
    denoise = int(settings.get("denoise", 0))
    auto_enhance = bool(settings.get("auto_enhance", True))

    if cv2 is not None and np is not None:
        arr = np.array(pil_image.convert("RGB"))
        if denoise > 0:
            arr = cv2.fastNlMeansDenoisingColored(arr, None, denoise, denoise, 7, 21)
        if contrast != 1.0:
            lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
            l_channel = lab[:, :, 0]
            clahe = cv2.createCLAHE(clipLimit=contrast * 2.0, tileGridSize=(8, 8))
            lab[:, :, 0] = clahe.apply(l_channel)
            arr = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
        if sharpness > 1.0:
            kernel = np.array([[-1, -1, -1], [-1, 9 + (sharpness - 1) * 5, -1], [-1, -1, -1]], dtype=np.float32)
            arr = cv2.filter2D(arr, -1, kernel)
        if auto_enhance:
            arr = np.clip(arr.astype(np.float32) * 1.1, 0, 255).astype(np.uint8)
        enhanced = Image.fromarray(arr)
    else:
        enhanced = pil_image
        if auto_enhance:
            enhanced = ImageOps.autocontrast(enhanced, cutoff=1)

    gray = ImageOps.grayscale(enhanced)
    return ImageOps.autocontrast(gray, cutoff=1)


def score_ocr_result(text: str) -> int:
    if not text:
        return 0
    text = text.strip()
    score = 0
    score += min(len(text), 200) * 2
    words = text.split()
    score += min(len(words), 50) * 5
    if text:
        alpha_count = sum(c.isalpha() for c in text)
        total = len(text)
        if total > 0:
            score += int((alpha_count / total) * 40)
    consecutive_spaces = text.count("  ")
    score -= consecutive_spaces * 10
    return max(0, score)


def pil_from_np(array: np.ndarray) -> Image.Image | None:
    if array is None:
        return None
    if len(array.shape) == 2:
        return Image.fromarray(array, mode="L")
    return Image.fromarray(array)
