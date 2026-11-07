"""Перевод событий CV-модуля во внутренний контракт.

Единственное место, где живут расхождения между контрактом смежника и
контрактом движка. Всё остальное ниже по течению работает с Observation
и StreamHealth и про WebSocket ничего не знает.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from libs.config import CvIngressConfig
from libs.contracts import BBox, Observation, StreamHealth, StreamState, utc_now

from .wire import ObjectStateEvent, ObservationStatus, ServiceEvent, ServiceStatus

log = logging.getLogger(__name__)


@dataclass
class TranslationStats:
    """Счётчики для диагностики стыковки.

    Отдаются в интерфейс инструктора: на интеграции нужно видеть не только
    что события идут, но и какого качества контракт с той стороны.
    """

    events: int = 0
    service_events: int = 0
    rejected: int = 0
    without_frame_ts: int = 0
    unknown_objects: dict[str, int] = field(default_factory=dict)
    subjects: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "events": self.events,
            "service_events": self.service_events,
            "rejected": self.rejected,
            "without_frame_ts": self.without_frame_ts,
            "unknown_objects": dict(self.unknown_objects),
            "subjects": sorted(self.subjects),
        }


class SubjectSelector:
    """Выбор проверяемого сотрудника, когда в кадре несколько человек.

    CV присваивает subject_id по номеру трека и о том, кто из людей проходит
    проверку, не знает. Правило по умолчанию — ближайший к камере, то есть
    с наибольшей площадью рамки: сотрудник, предъявляющий себя на проверку,
    занимает кадр целиком, случайный прохожий позади — нет.

    Площадь сравнивается в тех единицах, в каких пришла рамка. Люди в одном
    кадре меряются одной линейкой, поэтому для выбора размер кадра не нужен.

    Без рамки выбирается первый появившийся субъект: для демонстрации, где
    в кадре один человек, этого достаточно.
    """

    def __init__(self) -> None:
        self._areas: dict[str, float] = {}
        self._first: str | None = None

    def reset(self) -> None:
        self._areas.clear()
        self._first = None

    def observe(self, subject_id: str, area: float | None) -> None:
        if self._first is None:
            self._first = subject_id
        if area is not None:
            self._areas[subject_id] = area

    def primary(self) -> str | None:
        if self._areas:
            return max(self._areas, key=lambda subject: self._areas[subject])
        return self._first


class Translator:
    def __init__(self, config: CvIngressConfig, *, known_variables: set[str]) -> None:
        self._config = config
        self._known = known_variables
        self.stats = TranslationStats()
        self.subjects = SubjectSelector()
        self._warned_frame_ts = False
        #: Момент нашего времени, которому соответствует нулевая секунда съёмки.
        #: Ставится по первому событию: у источника собственный отсчёт от начала
        #: записи, и без общей точки отсчёта задержку не измерить.
        self._frame_epoch: datetime | None = None

    # ------------------------------------------------------------------

    def reset_frame_epoch(self) -> None:
        """Забыть привязку отсчёта съёмки: следующая запись начнётся заново."""
        self._frame_epoch = None

    def shift_frame_epoch(self, seconds: float) -> None:
        """Сдвинуть точку отсчёта съёмки — запись стояла эти секунды.

        Пока показ замер, часы записи не идут, а наше время идёт. Без сдвига
        следующее событие пришло бы с временем кадра из прошлого, и система
        сочла бы съёмку отставшей: оценила бы задержку в секунды простоя и
        объявила потерю сигнала на ровном месте.
        """
        if self._frame_epoch is not None:
            self._frame_epoch += timedelta(seconds=seconds)

    def object_state(self, event: ObjectStateEvent) -> Observation | StreamHealth | None:
        """Событие состояния объекта → наблюдение.

        NO_DATA переводится не в наблюдение, а в отказ источника: по описанию
        CV это отсутствие данных для наблюдения, то есть техническая проблема,
        а не «объекта нет в кадре». Смешивать их нельзя — сценарий реагирует
        на них по-разному.
        """
        self.stats.events += 1

        variable = self._config.variable_for(event.object)
        if variable not in self._known:
            count = self.stats.unknown_objects.get(event.object, 0)
            self.stats.unknown_objects[event.object] = count + 1
            if count == 0:
                log.warning(
                    "CV прислал объект '%s' (переменная '%s'), которого нет в СОП — событие "
                    "отброшено. Проверьте cv_ingress.objects в конфигурации.",
                    event.object,
                    variable,
                )
            self.stats.rejected += 1
            return None

        if event.subject_id is not None:
            self.stats.subjects.add(event.subject_id)
            self.subjects.observe(event.subject_id, self._area(event.bbox))
            primary = self.subjects.primary()
            if primary is not None and event.subject_id != primary:
                # Состояние другого человека в кадре к проверке не относится.
                self.stats.rejected += 1
                return None

        if event.observation_status is ObservationStatus.NO_DATA:
            return StreamHealth(
                source=self._source(event),
                camera_id=event.camera_id,
                state=StreamState.LOST,
                reason="CV сообщил об отсутствии данных для наблюдения",
            )

        recognized = event.observation_status is ObservationStatus.RECOGNIZED
        return Observation(
            source=self._source(event),
            camera_id=event.camera_id,
            frame_ts=self._frame_ts(event),
            variable=variable,
            value=event.state if recognized else None,
            confidence=event.confidence if recognized else 0.0,
            unclear=not recognized,
            bbox=self._bbox(event),
        )

    def service(self, event: ServiceEvent) -> StreamHealth:
        self.stats.service_events += 1
        lost = event.status is ServiceStatus.NO_DATA
        return StreamHealth(
            source=f"cv:{event.camera_id}",
            camera_id=event.camera_id,
            state=StreamState.LOST if lost else StreamState.ONLINE,
            reason=_SERVICE_REASONS.get(event.reason, event.reason),
        )

    # ------------------------------------------------------------------

    def _source(self, event: ObjectStateEvent) -> str:
        return f"cv:{event.model_version}"

    def _frame_ts(self, event: ObjectStateEvent) -> datetime:
        """Время кадра, а при его отсутствии — время отправки.

        Подмена отмечается в счётчиках: пока CV не передаёт время кадра,
        измеренная задержка занижена на время, проведённое событием в
        стабилизаторе и в сети, и норматив 2 с из п. 6 ТЗ по ней не проверяется.
        """
        if event.frame_ts is not None:
            return event.frame_ts

        if event.frame_timestamp_sec is not None:
            # Привязываем отсчёт съёмки к нашему времени по первому событию.
            # Дальше положение кадра переводится в абсолютное время линейно,
            # поэтому задержка считается честно и внутри записи, и на живом
            # потоке.
            if self._frame_epoch is None:
                self._frame_epoch = utc_now() - timedelta(
                    seconds=event.frame_timestamp_sec
                )
            return self._frame_epoch + timedelta(seconds=event.frame_timestamp_sec)

        self.stats.without_frame_ts += 1
        if not self._warned_frame_ts:
            self._warned_frame_ts = True
            log.warning(
                "CV не передаёт frame_ts — используется timestamp (время отправки). "
                "Замер задержки по п. 6 ТЗ на этих данных недостоверен."
            )
        return event.timestamp

    def _xywh(self, raw: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        """Рамка в виде (x, y, ширина, высота) — в тех же единицах, в каких пришла."""
        if self._config.bbox_format == "xyxy":
            x1, y1, x2, y2 = raw
            return min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)
        return raw

    def _area(self, raw: tuple[float, float, float, float] | None) -> float | None:
        if raw is None:
            return None
        _, _, w, h = self._xywh(raw)
        return abs(w * h)

    def _bbox(self, event: ObjectStateEvent) -> BBox | None:
        if event.bbox is None:
            return None
        x, y, w, h = self._xywh(event.bbox)

        if not self._config.bbox_normalized:
            if event.frame_width is None or event.frame_height is None:
                # Пиксели без размера кадра в доли кадра не перевести. Рамка
                # нужна только для отладки, поэтому теряем её, а не наблюдение.
                return None
            x, w = x / event.frame_width, w / event.frame_width
            y, h = y / event.frame_height, h / event.frame_height

        try:
            return BBox(x=_clamp(x), y=_clamp(y), w=_clamp(w, low=1e-6), h=_clamp(h, low=1e-6))
        except ValueError:
            log.debug("рамка %s не укладывается в кадр — отброшена", event.bbox)
            return None


def _clamp(value: float, *, low: float = 0.0) -> float:
    return max(low, min(1.0, value))


_SERVICE_REASONS = {
    "stream_lost": "CV сообщил о потере видеопотока",
    "stream_restored": "CV сообщил о восстановлении видеопотока",
}
