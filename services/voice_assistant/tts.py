"""Синтез речи за абстракцией (п. 1.2 вопросника Заказчика).

Две реализации:

* `SileroTts` — поставочная. Русская модель Silero, работает локально и
  офлайн, что требует п. 6.1.3 ТЗ. Тянет torch, поэтому вынесена в extras.
* `BrowserTts` — синтез средствами браузера инструктора. Ноль зависимостей,
  работает на чистой машине сразу. Аудио не рождается на сервере: наружу
  уходит текст, произносит его вкладка браузера.

Выбор `auto` берёт Silero, если torch доступен, иначе браузер. Это позволяет
поднять стенд одной командой на машине без GPU и без выкачивания моделей —
и при этом не даёт поставке зависеть от браузера.
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
import time
import wave
from pathlib import Path
from abc import ABC, abstractmethod
from dataclasses import dataclass

from libs.config import VoiceConfig

log = logging.getLogger(__name__)

#: Средний темп синтезированной русской речи, знаков в секунду. Нужен, чтобы
#: выдерживать паузу между репликами, когда длительность аудио неизвестна
#: (браузерный синтез не сообщает её серверу).
CHARS_PER_SECOND = 14.0
MIN_UTTERANCE_SEC = 1.0


@dataclass
class Synthesis:
    """Результат синтеза одной реплики."""

    audio: bytes | None
    """WAV-данные. None — если произносит браузер."""

    duration_sec: float
    synth_ms: float
    engine: str


def estimate_duration_sec(text: str) -> float:
    return max(MIN_UTTERANCE_SEC, len(text) / CHARS_PER_SECOND)


class TtsEngine(ABC):
    name: str = "unknown"

    @abstractmethod
    def synthesize(self, text: str) -> Synthesis: ...

    def available(self) -> bool:
        return True


class BrowserTts(TtsEngine):
    """Реплика уходит текстом, произносит её браузер инструктора.

    Осознанный компромисс демо-стенда: docker-контейнер на Windows не имеет
    доступа к звуковой карте хоста, а требование «docker compose up на чистой
    машине» из п. 3.1 вопросника важнее, чем серверный синтез на демонстрации.
    На пилоте с гарнитурой оператора место занимает SileroTts.
    """

    name = "browser"

    def synthesize(self, text: str) -> Synthesis:
        return Synthesis(
            audio=None,
            duration_sec=estimate_duration_sec(text),
            synth_ms=0.0,
            engine=self.name,
        )


class SileroTts(TtsEngine):
    """Локальный синтез Silero. Модель загружается один раз при первом обращении."""

    name = "silero"

    def __init__(self, config: VoiceConfig) -> None:
        self._config = config
        self._model = None
        self._sample_rate = config.silero.sample_rate

    def available(self) -> bool:
        """Готов ли Silero работать прямо сейчас, без обращения к сети.

        Мало наличия torch: при первом вызове torch.hub полез бы качать 38 МБ
        из интернета. Для системы, которая по п. 6.3 ТЗ обязана работать в
        изолированном контуре, неявная загрузка на старте недопустима — стенд
        либо повис бы, либо не поднялся. Поэтому проверяем именно наличие уже
        скачанной модели, а выкачивание вынесено в отдельный шаг:

            python -m tools.fetch_silero
        """
        try:
            import torch
        except ImportError:
            return False
        return self.model_cached(torch)

    @staticmethod
    def model_cached(torch_module=None) -> bool:
        from pathlib import Path

        if torch_module is None:
            try:
                import torch as torch_module  # type: ignore[no-redef]
            except ImportError:
                return False
        hub_dir = Path(torch_module.hub.get_dir())
        return (hub_dir / "snakers4_silero-models_master").exists()

    def _ensure_model(self):
        if self._model is not None:
            return self._model

        import torch

        log.info("загрузка модели Silero %s…", self._config.silero.model_id)
        model, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-models",
            model="silero_tts",
            language="ru",
            speaker=self._config.silero.model_id,
            trust_repo=True,
        )
        model.to(torch.device("cpu"))
        self._model = model
        return model

    def synthesize(self, text: str) -> Synthesis:
        import io
        import wave

        started = time.perf_counter()
        model = self._ensure_model()
        audio = model.apply_tts(
            text=text,
            speaker=self._config.silero.speaker,
            sample_rate=self._sample_rate,
        )

        samples = (audio * 32767).short().numpy().tobytes()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self._sample_rate)
            wav.writeframes(samples)

        duration = len(audio) / self._sample_rate
        return Synthesis(
            audio=buffer.getvalue(),
            duration_sec=duration,
            synth_ms=(time.perf_counter() - started) * 1000,
            engine=self.name,
        )


class PrebakedTts(TtsEngine):
    """Заранее синтезированные реплики из файлов.

    Постоянные фразы сценария известны заранее, поэтому их можно озвучить
    один раз Silero и положить рядом с проектом. На площадке без видеокарты
    голос тогда остаётся тем же самым: не зависит ни от наличия torch, ни от
    того, какие голоса стоят в системе у зрителя.

    Фразы, которых в наборе нет, уходят в запасной движок.
    """

    name = "prebaked"

    def __init__(self, directory: Path, fallback: TtsEngine) -> None:
        self._dir = directory
        self._fallback = fallback

    @staticmethod
    def key_for(text: str) -> str:
        normalised = " ".join(text.split()).lower()
        return hashlib.sha1(normalised.encode("utf-8")).hexdigest()

    def available(self) -> bool:
        return self._dir.is_dir() and any(self._dir.glob("*.wav"))

    def synthesize(self, text: str) -> Synthesis:
        started = time.perf_counter()
        file = self._dir / f"{self.key_for(text)}.wav"
        if not file.exists():
            return self._fallback.synthesize(text)

        audio = file.read_bytes()
        return Synthesis(
            audio=audio,
            duration_sec=_wav_duration_sec(audio) or estimate_duration_sec(text),
            synth_ms=(time.perf_counter() - started) * 1000,
            engine=self.name,
        )


def _wav_duration_sec(audio: bytes) -> float | None:
    """Длительность WAV по заголовку — нужна, чтобы выдержать паузу между репликами."""
    try:
        with wave.open(io.BytesIO(audio)) as handle:
            return handle.getnframes() / float(handle.getframerate())
    except (wave.Error, EOFError, ZeroDivisionError):
        return None


class SerialTts(TtsEngine):
    """Синтез по одному за раз.

    Движок один на весь стенд, а занятий идёт несколько: модель синтеза не
    рассчитана на одновременные вызовы из разных потоков. Очередь здесь ничего
    не стоит — реплики и так произносятся последовательно, а ожидание чужой
    фразы измеряется миллисекундами: почти всё берётся из кэша.
    """

    def __init__(self, inner: TtsEngine) -> None:
        self._inner = inner
        self._lock = threading.Lock()

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._inner.name

    @property
    def inner(self) -> TtsEngine:
        return self._inner

    def available(self) -> bool:
        return self._inner.available()

    def synthesize(self, text: str) -> Synthesis:
        with self._lock:
            return self._inner.synthesize(text)


def create_tts(config: VoiceConfig) -> TtsEngine:
    requested = config.engine.lower()

    if requested == "browser":
        return _with_prebaked(config, BrowserTts())

    if requested in {"auto", "silero"}:
        silero = SileroTts(config)
        if silero.available():
            return silero
        if requested == "silero":
            raise RuntimeError(
                "запрошен Silero, но он не готов. Установите зависимости "
                '(pip install -e ".[voice]") и скачайте модель: python -m tools.fetch_silero'
            )
        engine = _with_prebaked(config, BrowserTts())
        if engine.name == PrebakedTts.name:
            log.info("модель Silero не найдена — озвучиваю заранее синтезированными репликами")
        else:
            log.info(
                "модель Silero не найдена локально — синтез уходит в браузер инструктора. "
                "Чтобы включить серверный синтез: python -m tools.fetch_silero"
            )
        return engine

    raise ValueError(f"неизвестный движок синтеза: {config.engine}")


def _with_prebaked(config: VoiceConfig, fallback: TtsEngine) -> TtsEngine:
    """Подложить заранее синтезированные реплики, если они есть."""
    from libs.config import project_root

    prebaked = PrebakedTts(project_root() / config.prebaked_dir, fallback)
    return prebaked if prebaked.available() else fallback
