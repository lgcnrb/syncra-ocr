from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from PIL import Image


class OCRBackendBase(ABC):
    name: str = "none"

    def __init__(self) -> None:
        self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    @abstractmethod
    def recognize(self, image: Image.Image) -> str:
        ...

    def close(self) -> None:
        pass


class WinRtOCRBackend(OCRBackendBase):
    name = "winrt"

    def __init__(self) -> None:
        super().__init__()
        self._engine = None
        self._runner = None
        try:
            from winsdk.windows.media.ocr import OcrEngine
            self._engine = OcrEngine.try_create_from_user_profile_languages()
            self._runner = AsyncLoopRunner()
            self._ready = self._engine is not None
        except Exception:
            self._ready = False

    def is_ready(self) -> bool:
        return self._ready and self._engine is not None

    def recognize(self, image: Image.Image) -> str:
        if not self.is_ready():
            return ""
        try:
            return asyncio.get_event_loop().run_until_complete(self._recognize_async(image))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(self._recognize_async(image))
        except Exception:
            return ""

    async def _recognize_async(self, image: Image.Image) -> str:
        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        if image.mode != "RGBA":
            image = image.convert("RGBA")
        data = image.tobytes()
        width, height = image.size

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(data)
        await writer.store_async()
        stream.seek(0)

        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.getSoftware_bitmap_async()

        result = await self._engine.recognize_async(bitmap)
        return result.text or ""

    def close(self) -> None:
        self._runner = None
        self._engine = None


class TesseractBackend(OCRBackendBase):
    name = "tesseract"

    def __init__(self) -> None:
        super().__init__()
        self._pytesseract = None
        try:
            import pytesseract
            self._pytesseract = pytesseract
            self._ready = True
        except Exception:
            self._ready = False

    def is_ready(self) -> bool:
        return self._ready and self._pytesseract is not None

    def recognize(self, image: Image.Image) -> str:
        if not self.is_ready() or not self._pytesseract:
            return ""
        try:
            return self._pytesseract.image_to_string(image).strip()
        except Exception:
            return ""


class AsyncLoopRunner:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None

    def get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop


class DummyBackend(OCRBackendBase):
    name = "none"

    def recognize(self, image: Image.Image) -> str:
        return ""


OCR_ENGINE_PRIORITY = ["winrt", "tesseract"]


def build_backend(preference: str = "auto") -> OCRBackendBase:
    engines: dict[str, OCRBackendBase] = {
        "winrt": WinRtOCRBackend(),
        "tesseract": TesseractBackend(),
    }

    if preference != "auto":
        selected = engines.get(preference, engines["winrt"])
        if selected.is_ready():
            return selected

    for engine_name in OCR_ENGINE_PRIORITY:
        engine = engines[engine_name]
        if engine.is_ready():
            return engine

    return DummyBackend()
