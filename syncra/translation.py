from __future__ import annotations

from abc import ABC, abstractmethod


class TranslationBackendBase(ABC):
    name: str = "none"

    def __init__(self) -> None:
        self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    @abstractmethod
    def translate(self, text: str, source: str = "en", target: str = "tr") -> str:
        ...

    def close(self) -> None:
        pass


class GoogleTranslateBackend(TranslationBackendBase):
    name = "google"

    def __init__(self) -> None:
        super().__init__()
        self._translator = None
        try:
            from deep_translator import GoogleTranslator
            self._translator = GoogleTranslator
            self._ready = True
        except Exception:
            self._ready = False

    def is_ready(self) -> bool:
        return self._ready and self._translator is not None

    def translate(self, text: str, source: str = "en", target: str = "tr") -> str:
        if not self.is_ready() or not text or not text.strip():
            return ""
        try:
            translator = self._translator(source=source, target=target)
            result = translator.translate(text.strip())
            return result or ""
        except Exception:
            return ""


class ArgosTranslateBackend(TranslationBackendBase):
    name = "argos"

    def __init__(self) -> None:
        super().__init__()
        self._argos = None
        try:
            import argostranslate.package
            import argostranslate.translate
            self._argos = argostranslate
            self._ready = True
        except Exception:
            self._ready = False

    def is_ready(self) -> bool:
        return self._ready and self._argos is not None

    def translate(self, text: str, source: str = "en", target: str = "tr") -> str:
        if not self.is_ready() or not text or not text.strip():
            return ""
        try:
            from argostranslate import translate as argos_translate
            installed = argos_translate.get_installed_languages()
            src_lang = None
            tgt_lang = None
            for lang in installed:
                if lang.code == source:
                    src_lang = lang
                if lang.code == target:
                    tgt_lang = lang
            if src_lang and tgt_lang:
                result = tgt_lang.get_translation(src_lang).translate(text.strip())
                return result or ""
            return ""
        except Exception:
            return ""


class DummyTranslationBackend(TranslationBackendBase):
    name = "none"

    def translate(self, text: str, source: str = "en", target: str = "tr") -> str:
        return ""


def build_translation_backend(preference: str = "google") -> TranslationBackendBase:
    backends = {
        "google": GoogleTranslateBackend,
        "argos": ArgosTranslateBackend,
    }

    if preference in backends:
        backend = backends[preference]()
        if backend.is_ready():
            return backend

    google = GoogleTranslateBackend()
    if google.is_ready():
        return google

    return DummyTranslationBackend()
