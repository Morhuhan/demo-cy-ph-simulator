"""Распознавание коротких команд оператора.

Голосовой канал работает в обе стороны. Наружу — оценки и инструкции. Внутрь —
«повтори», «помощь» и доклад о готовности к наливу: седьмой шаг СОП камерой не
наблюдается в принципе, и закрыть его может только речь.

Намерение определяется по ключевым словам, а не моделью. Для трёх команд это
надёжнее: список закрыт, слова заданы регламентом, а ошибка распознавания
«готов к наливу» стоит дороже, чем отказ понять нестандартную формулировку.
Место под замену обозначено интерфейсом `SttEngine`.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from libs.config import SttConfig
from libs.contracts import VoiceIntent

log = logging.getLogger(__name__)


@dataclass
class Transcription:
    text: str
    confidence: float
    engine: str


class SttEngine(ABC):
    name: str = "unknown"
    #: Принимает ли движок звук. Браузерный получает уже готовый текст.
    accepts_audio: bool = True

    @abstractmethod
    def transcribe(self, audio: bytes) -> Transcription: ...

    def available(self) -> bool:
        return True


class BrowserStt(SttEngine):
    """Распознавание выполняет браузер инструктора, сюда приходит готовый текст.

    Работает без единой зависимости и без доступа контейнера к микрофону.
    """

    name = "browser"
    accepts_audio = False

    def transcribe(self, audio: bytes) -> Transcription:  # pragma: no cover
        raise NotImplementedError("браузерная реализация принимает готовый текст, а не аудио")


class FasterWhisperStt(SttEngine):
    """Локальное распознавание. Поставочный путь, когда микрофон у вычислителя."""

    name = "faster-whisper"

    def __init__(self, config: SttConfig) -> None:
        self._config = config
        self._model = None

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("загрузка модели faster-whisper %s…", self._config.model_size)
            self._model = WhisperModel(self._config.model_size, device="cpu", compute_type="int8")
        return self._model

    def transcribe(self, audio: bytes) -> Transcription:
        import io

        model = self._ensure_model()
        segments, _info = model.transcribe(
            io.BytesIO(audio), language=self._config.language, vad_filter=True
        )
        parts = list(segments)
        text = " ".join(segment.text.strip() for segment in parts)
        confidence = (
            sum(segment.avg_logprob for segment in parts) / len(parts) if parts else -5.0
        )
        # avg_logprob лежит примерно в [-5, 0]; приводим к [0, 1] для контракта.
        normalised = max(0.0, min(1.0, 1.0 + confidence / 5.0))
        return Transcription(text=text, confidence=normalised, engine=self.name)


class HfRouterStt(SttEngine):
    """Распознавание Whisper на серверах Hugging Face.

    Фраза уходит на маршрутизатор HF одним запросом и возвращается текстом.
    Уверенности этот маршрут не отдаёт — она и не нужна: команду из текста
    выделяет разбор ключевых слов ниже, со своей уверенностью.

    Язык передаётся явно. Без него Whisper угадывает язык по самой фразе, и
    короткая команда с тихого микрофона уходит в английский: «Вариант Б» на
    замере 21.10 вернулся как «What are you on?».

    Основной провайдер — fal (как в call-analyzer), запасной — hf-inference:
    отказ одного не должен лишать посетителя голоса.
    """

    name = "hf-whisper"

    def __init__(self, config: SttConfig, transport: httpx.BaseTransport | None = None) -> None:
        self._config = config
        self._transport = transport
        self._client: httpx.Client | None = None
        self._last_used = 0.0

    def _http(self) -> httpx.Client:
        # Соединение с маршрутизатором держится открытым между фразами: новое
        # TLS-рукопожатие стоило ~3 с из 4 (замер 21.10), сам fal отвечает за 0,8.
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                transport=self._transport,
                timeout=self._config.timeout_sec,
                limits=httpx.Limits(keepalive_expiry=600),
            )
        self._last_used = time.monotonic()
        return self._client

    def warm(self) -> None:
        """Открыть соединение заранее, если оно простаивало: первая фраза не ждёт рукопожатия."""
        if not self.available() or time.monotonic() - self._last_used < 50:
            return
        try:
            self._http().get(self._config.fal_url.split("/fal-ai")[0] + "/")
        except httpx.HTTPError as exc:
            log.info("прогрев соединения распознавания не удался: %s", type(exc).__name__)

    @property
    def _token(self) -> str:
        return os.getenv(self._config.token_env, "").strip()

    def available(self) -> bool:
        return bool(self._token)

    def transcribe(self, audio: bytes) -> Transcription:
        headers = {"Authorization": f"Bearer {self._token}"}
        client = self._http()
        if self._config.hf_provider == "fal":
            try:
                text = self._fal(client, headers, audio)
                return Transcription(text=text, confidence=1.0 if text else 0.0, engine="hf-fal")
            except (RuntimeError, httpx.HTTPError) as exc:
                log.warning("распознавание fal не удалось, повтор через hf-inference: %s", exc)
        text = self._hf_inference(client, headers, audio)
        return Transcription(text=text, confidence=1.0 if text else 0.0, engine=self.name)

    def _fal(self, client: httpx.Client, headers: dict, audio: bytes) -> str:
        # fal принимает звук ссылкой data:; WAV он разбирает и под меткой mpeg —
        # с меткой audio/wav отвечает 422 «Unsupported data URL».
        payload = {
            "audio_url": "data:audio/mpeg;base64," + base64.b64encode(audio).decode(),
            "task": "transcribe",
            "language": self._config.language,
            "chunk_level": "segment",
        }
        if self._config.prompt:
            payload["prompt"] = self._config.prompt
        response = client.post(self._config.fal_url, json=payload, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"fal ответил {response.status_code}: {response.text[:200]}")
        return str(response.json().get("text", "")).strip()

    def _hf_inference(self, client: httpx.Client, headers: dict, audio: bytes) -> str:
        url = f"{self._config.hf_url.rstrip('/')}/{self._config.hf_model}"
        payload = {
            "inputs": base64.b64encode(audio).decode(),
            "parameters": {"generate_kwargs": {"language": self._config.language, "task": "transcribe"}},
        }
        response = client.post(url, json=payload, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"HF ответил {response.status_code}: {response.text[:200]}")
        return str(response.json().get("text", "")).strip()


#: Ключевые слова намерений. Порядок важен: доклад о готовности проверяется
#: первым, потому что фраза «готов, повтори маршрут» не должна стать «повтори».
_PATTERNS: list[tuple[VoiceIntent, re.Pattern[str]]] = [
    (
        VoiceIntent.READY_REPORT,
        re.compile(
            r"\b(готов\w*|готовност\w+|к\s+наливу|операторн\w+|доклад\w*|принял)\b",
            re.IGNORECASE,
        ),
    ),
    (VoiceIntent.REPEAT, re.compile(r"\b(повтор\w*|ещё\s+раз|еще\s+раз|не\s+понял)\b", re.IGNORECASE)),
    (
        VoiceIntent.HELP,
        re.compile(r"\b(помощ\w*|помоги\w*|подскаж\w*|что\s+дальше|как\s+делать)\b", re.IGNORECASE),
    ),
]


def match_intent(text: str) -> tuple[VoiceIntent, float]:
    """Намерение по тексту реплики и уверенность в нём."""
    cleaned = " ".join(text.lower().split())
    if not cleaned:
        return VoiceIntent.UNKNOWN, 0.0

    for intent, pattern in _PATTERNS:
        if pattern.search(cleaned):
            # Короткая точная команда надёжнее длинной фразы, где слово могло
            # прозвучать в другом смысле.
            confidence = 0.95 if len(cleaned.split()) <= 4 else 0.75
            return intent, confidence

    return VoiceIntent.UNKNOWN, 0.0


def create_stt(config: SttConfig) -> SttEngine:
    requested = config.engine.lower()
    if requested == "off":
        return BrowserStt()
    if requested == "hf":
        hf = HfRouterStt(config)
        if hf.available():
            return hf
        # Не падаем: без распознавания стенд работает, команды остаются
        # кнопками инструктора. Но молчать об этом нельзя.
        log.warning(
            "распознавание через Hugging Face не включено: не задана переменная %s",
            config.token_env,
        )
        return BrowserStt()
    if requested in {"auto", "faster-whisper"}:
        whisper = FasterWhisperStt(config)
        if whisper.available():
            return whisper
        if requested == "faster-whisper":
            raise RuntimeError(
                "запрошен faster-whisper, но он не установлен: pip install -e \".[voice]\""
            )
        log.info("faster-whisper не найден — распознавание команд остаётся в браузере")
        return BrowserStt()
    raise ValueError(f"неизвестный движок распознавания: {config.engine}")
