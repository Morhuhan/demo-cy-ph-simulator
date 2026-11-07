"""Интерфейс источника наблюдений (п. 1.2 вопросника Заказчика).

За этим интерфейсом сейчас стоит эмулятор, а после появления работы CV-команды
встанет реальный детектор. Ничто выше по течению — ни сглаживание, ни движок,
ни голос — про подмену не узнает.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from libs.contracts import Observation, StreamHealth

DetectorMessage = Observation | StreamHealth


class Detector(ABC):
    """Источник наблюдений за состоянием оборудования."""

    #: Идентификатор, попадающий в поле `source` каждого наблюдения.
    source_id: str = "unknown"

    @abstractmethod
    def stream(self) -> AsyncIterator[DetectorMessage]:
        """Бесконечный поток наблюдений и сообщений о здоровье потока."""

    async def start(self) -> None:  # pragma: no cover - по умолчанию нечего делать
        return None

    async def stop(self) -> None:  # pragma: no cover
        return None


class RtspDetector(Detector):
    """Заготовка под реальный источник: IP-камера по RTSP/ONVIF (п. 6.5 вопросника).

    Намеренно не реализована — это зона CV-команды. Класс существует, чтобы
    точка подключения была видна в коде, а не только в документации: захват
    кадров и инференс живут здесь, всё остальное в системе остаётся прежним.

    Что должна делать реализация:
      * подключение к RTSP с переподключением при обрыве;
      * сброс устаревших кадров вместо накопления очереди;
      * инференс модели и выдача Observation на каждую переменную из СОП;
      * StreamHealth при потере потока, засветке, обмерзании оптики.
    """

    source_id = "cv-rtsp"

    def __init__(self, rtsp_url: str, model_path: str | None = None) -> None:
        self.rtsp_url = rtsp_url
        self.model_path = model_path

    def stream(self) -> AsyncIterator[DetectorMessage]:  # pragma: no cover
        raise NotImplementedError(
            "Реальный детектор — зона CV-команды. Контракт, который он обязан "
            "соблюдать: contracts/observation.schema.json"
        )
