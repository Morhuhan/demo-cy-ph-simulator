"""Приём событий реального CV-модуля: перевод контракта и удержание состояния.

Здесь проверяется граница со смежником. Расхождения контрактов разобраны
в docs/integration-cv.md; тесты фиксируют, как именно каждое из них
обрабатывается, чтобы изменение на той стороне ломало тест, а не демонстрацию.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from libs.bus import InMemoryBus
from libs.config import CvIngressConfig, load_config, load_sop
from libs.contracts import Observation, StreamHealth, StreamState
from services.cv_ingress.service import CvIngressService
from services.cv_ingress.translate import SubjectSelector, Translator
from services.cv_ingress.wire import ObjectStateEvent, ServiceEvent

FRAME_TS = "2026-09-16T12:30:15.420000Z"
SENT_TS = "2026-09-16T12:30:16.000000Z"


def make_translator(**overrides) -> Translator:
    config = CvIngressConfig(enabled=True, **overrides)
    return Translator(config, known_variables={"helmet"})


def object_event(**overrides) -> ObjectStateEvent:
    payload = {
        "event_type": "object_state",
        "camera_id": "hmd-01",
        "timestamp": SENT_TS,
        "object": "helmet",
        "subject_id": "person-1",
        "state": "on",
        "confidence": 0.94,
        "observation_status": "RECOGNIZED",
        "model_version": "ppe-yolo11m-v1",
    }
    payload.update(overrides)
    return ObjectStateEvent.model_validate(payload)


# ----------------------------------------------------------------------
# Статусы наблюдения
# ----------------------------------------------------------------------


def test_recognized_becomes_observation():
    result = make_translator().object_state(object_event())

    assert isinstance(result, Observation)
    assert result.variable == "helmet"
    assert result.value == "on"
    assert result.confidence == pytest.approx(0.94)
    assert result.unclear is False
    assert result.source == "cv:ppe-yolo11m-v1"
    assert result.camera_id == "hmd-01"


@pytest.mark.parametrize("status", ["UNCLEAR", "NOT_VISIBLE"])
def test_unrecognized_statuses_become_unclear(status):
    """«Не разобрал» и «нет в кадре» — штатная неопределённость, а не отказ.

    Внутренний контракт различает их по-другому: unclear=true и value=None.
    Уверенность обнуляется — состояния нет, верить нечему.
    """
    result = make_translator().object_state(object_event(observation_status=status))

    assert isinstance(result, Observation)
    assert result.unclear is True
    assert result.value is None
    assert result.confidence == 0.0


def test_no_data_becomes_stream_failure():
    """NO_DATA — это отсутствие данных, то есть отказ источника.

    Принципиально отличается от NOT_VISIBLE: там объекта нет в кадре, здесь
    смотреть не на что вообще. Сценарий реагирует на них по-разному.
    """
    result = make_translator().object_state(object_event(observation_status="NO_DATA"))

    assert isinstance(result, StreamHealth)
    assert result.state is StreamState.LOST


# ----------------------------------------------------------------------
# Время кадра — пункт 1 списка на согласование
# ----------------------------------------------------------------------


def test_frame_ts_is_used_when_present():
    translator = make_translator()
    result = translator.object_state(object_event(frame_ts=FRAME_TS))

    assert isinstance(result, Observation)
    assert result.frame_ts.isoformat().startswith("2026-09-16T12:30:15.420")
    assert translator.stats.without_frame_ts == 0


def test_missing_frame_ts_falls_back_to_timestamp_and_is_counted():
    """Текущая версия CV времени кадра не передаёт.

    Подставляем время отправки, чтобы поток не встал, но считаем такие
    события: по ним норматив задержки из п. 6 ТЗ измерять нельзя.
    """
    translator = make_translator()
    result = translator.object_state(object_event())

    assert isinstance(result, Observation)
    assert result.frame_ts.isoformat().startswith("2026-09-16T12:30:16")
    assert translator.stats.without_frame_ts == 1


# ----------------------------------------------------------------------
# Словарь объектов
# ----------------------------------------------------------------------


def test_unknown_object_is_rejected_not_crashed():
    translator = make_translator()

    assert translator.object_state(object_event(object="gloves")) is None
    assert translator.stats.unknown_objects == {"gloves": 1}
    assert translator.stats.rejected == 1


def test_object_name_is_mapped_through_config():
    translator = Translator(
        CvIngressConfig(enabled=True, objects={"hard_hat": "helmet"}),
        known_variables={"helmet"},
    )
    result = translator.object_state(object_event(object="hard_hat"))

    assert isinstance(result, Observation)
    assert result.variable == "helmet"


# ----------------------------------------------------------------------
# Выбор субъекта: в кадре может быть несколько человек
# ----------------------------------------------------------------------


def test_closest_subject_wins():
    selector = SubjectSelector()
    selector.observe("person-1", 0.01)
    selector.observe("person-2", 0.30)

    assert selector.primary() == "person-2"


def test_passer_by_behind_does_not_take_over():
    """Регрессия на записи real-two-people, числа оттуда.

    Рамки пиксельные, в формате двух углов. Раньше площадь считалась как
    x2 * y2 — по координате угла, а не по размеру, — и дальний человек,
    стоящий правее и ниже в кадре, выходил «ближе» проверяемого втрое.
    """
    translator = make_translator(bbox_format="xyxy", bbox_normalized=False)

    near = translator.object_state(
        object_event(subject_id="person-1", bbox=[278.0, 28.0, 436.0, 144.0])
    )
    far = translator.object_state(
        object_event(subject_id="person-2", bbox=[604.0, 279.0, 654.0, 311.0], state="off")
    )

    assert isinstance(near, Observation)
    assert far is None
    assert translator.subjects.primary() == "person-1"


def test_first_subject_wins_without_bbox():
    selector = SubjectSelector()
    selector.observe("person-3", None)
    selector.observe("person-4", None)

    assert selector.primary() == "person-3"


def test_state_of_secondary_subject_is_ignored():
    """Прохожий позади проверяемого не должен влиять на вердикт."""
    translator = make_translator()

    near = translator.object_state(
        object_event(subject_id="person-1", bbox=[0.2, 0.2, 0.6, 0.7])
    )
    far = translator.object_state(
        object_event(subject_id="person-2", bbox=[0.0, 0.0, 0.05, 0.05], state="off")
    )

    assert isinstance(near, Observation)
    assert far is None
    assert translator.stats.subjects == {"person-1", "person-2"}


# ----------------------------------------------------------------------
# Рамка
# ----------------------------------------------------------------------


def test_bbox_xywh_is_passed_through():
    result = make_translator().object_state(object_event(bbox=[0.1, 0.2, 0.3, 0.4]))

    assert isinstance(result, Observation)
    assert result.bbox is not None
    assert (result.bbox.x, result.bbox.y, result.bbox.w, result.bbox.h) == pytest.approx(
        (0.1, 0.2, 0.3, 0.4)
    )


def test_bbox_xyxy_is_converted():
    translator = make_translator(bbox_format="xyxy")
    result = translator.object_state(object_event(bbox=[0.1, 0.2, 0.4, 0.6]))

    assert isinstance(result, Observation)
    assert result.bbox is not None
    assert (result.bbox.w, result.bbox.h) == pytest.approx((0.3, 0.4))


def test_pixel_bbox_is_scaled_by_frame_size():
    """Размер кадра приходит в событии — пиксели переводятся в доли кадра."""
    translator = make_translator(bbox_format="xyxy", bbox_normalized=False)
    result = translator.object_state(
        object_event(bbox=[320.0, 180.0, 960.0, 540.0], frame_width=1280, frame_height=720)
    )

    assert isinstance(result, Observation)
    assert result.bbox is not None
    assert (result.bbox.x, result.bbox.y, result.bbox.w, result.bbox.h) == pytest.approx(
        (0.25, 0.25, 0.5, 0.5)
    )


def test_pixel_bbox_is_dropped_not_fatal():
    """Пиксельные координаты без размера кадра не перевести.

    Рамка нужна для отладки, поэтому теряется она, а не наблюдение.
    """
    translator = make_translator(bbox_normalized=False)
    result = translator.object_state(object_event(bbox=[120.0, 80.0, 200.0, 240.0]))

    assert isinstance(result, Observation)
    assert result.bbox is None


# ----------------------------------------------------------------------
# Служебные события
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("NO_DATA", "stream_lost", StreamState.LOST),
        ("AVAILABLE", "stream_restored", StreamState.ONLINE),
    ],
)
def test_service_events_map_to_stream_health(status, reason, expected):
    event = ServiceEvent.model_validate(
        {
            "event_type": "cv_service",
            "camera_id": "hmd-01",
            "timestamp": SENT_TS,
            "status": status,
            "reason": reason,
        }
    )
    result = make_translator().service(event)

    assert result.state is expected
    assert result.reason


# ----------------------------------------------------------------------
# Устойчивость к чужому контракту
# ----------------------------------------------------------------------


def test_unknown_extra_field_does_not_break_parsing():
    """Смежник вправе добавить поле — это не повод отвергать сообщение.

    Обратное — отсутствие обязательного поля — ошибкой остаётся.
    """
    event = ObjectStateEvent.model_validate(
        {
            "event_type": "object_state",
            "camera_id": "hmd-01",
            "timestamp": SENT_TS,
            "object": "helmet",
            "subject_id": "person-1",
            "state": "on",
            "confidence": 0.9,
            "observation_status": "RECOGNIZED",
            "model_version": "v1",
            "track_id": 7,
        }
    )
    assert event.object == "helmet"


def test_naive_timestamp_is_rejected():
    """Время без часового пояса запрещено: по нему считается задержка."""
    with pytest.raises(ValueError):
        ObjectStateEvent.model_validate(
            {
                "event_type": "object_state",
                "camera_id": "hmd-01",
                "timestamp": "2026-09-16T12:30:16",
                "object": "helmet",
                "state": "on",
                "confidence": 0.9,
                "observation_status": "RECOGNIZED",
                "model_version": "v1",
            }
        )


def test_real_cv_payload_is_accepted():
    """Полезная нагрузка ровно в том виде, в каком её шлёт CV-модуль."""
    raw = json.dumps(
        {
            "event_type": "object_state",
            "camera_id": "hmd-01",
            "timestamp": "2026-09-16T12:30:15.420000Z",
            "object": "helmet",
            "subject_id": "person-1",
            "state": "on",
            "confidence": 0.94,
            "observation_status": "RECOGNIZED",
            "model_version": "ppe-yolo11m-v1",
            "bbox": [0.1, 0.2, 0.3, 0.4],
        }
    )
    event = ObjectStateEvent.model_validate_json(raw)
    result = make_translator().object_state(event)

    assert isinstance(result, Observation)
    assert result.value == "on"


# ----------------------------------------------------------------------
# Время кадра смещением — так его передаёт CV-модуль
# ----------------------------------------------------------------------


def test_frame_offset_is_anchored_to_our_clock():
    """CV шлёт положение кадра от начала съёмки, а не метку времени.

    У записи свой отсчёт, общего календаря с нами нет. Первое событие
    привязывает нулевую секунду съёмки к нашему времени, дальше перевод
    линейный — иначе задержка считалась бы от даты записи и выходила
    астрономической.
    """
    translator = make_translator()

    first = translator.object_state(object_event(frame_timestamp_sec=0.33))
    second = translator.object_state(object_event(frame_timestamp_sec=3.87))

    assert isinstance(first, Observation) and isinstance(second, Observation)
    delta = (second.frame_ts - first.frame_ts).total_seconds()
    assert delta == pytest.approx(3.54, abs=0.01)
    # Подстановки времени отправки не было.
    assert translator.stats.without_frame_ts == 0


def test_absolute_frame_time_wins_over_offset():
    """Если источник когда-нибудь начнёт слать метку времени, берём её."""
    translator = make_translator()
    result = translator.object_state(
        object_event(frame_ts=FRAME_TS, frame_timestamp_sec=99.0)
    )

    assert isinstance(result, Observation)
    assert result.frame_ts.isoformat().startswith("2026-09-16T12:30:15.420")


def test_paused_show_does_not_send_frames_from_the_past():
    """Пока показ замер, часы записи стоят — и отсчёт сдвигается вместе с ними.

    Без сдвига первое событие после паузы пришло бы с временем кадра из
    прошлого, и система сочла бы съёмку отставшей: объявила бы потерю сигнала
    ровно в тот момент, ради которого пауза и сделана.
    """
    translator = make_translator()
    first = translator.object_state(object_event(frame_timestamp_sec=1.0))

    # Съёмка простояла семь секунд, дальше запись продолжилась с 1.2 с.
    translator.shift_frame_epoch(7.0)
    second = translator.object_state(object_event(frame_timestamp_sec=1.2))

    assert isinstance(first, Observation) and isinstance(second, Observation)
    # Между кадрами прошло 0.2 с записи и 7 с простоя — столько же, сколько
    # у нас по часам. Иначе событие выглядело бы пришедшим из прошлого.
    delta = (second.frame_ts - first.frame_ts).total_seconds()
    assert delta == pytest.approx(7.2, abs=0.01)


def test_frame_epoch_resets_between_recordings():
    """Новая запись начинает отсчёт заново, иначе её события уедут в прошлое."""
    translator = make_translator()
    translator.object_state(object_event(frame_timestamp_sec=600.0))
    translator.reset_frame_epoch()

    result = translator.object_state(object_event(frame_timestamp_sec=0.5))
    assert isinstance(result, Observation)
    # После сброса кадр относится к настоящему, а не к десятой минуте прошлой записи.
    from libs.contracts import utc_now

    assert abs((utc_now() - result.frame_ts).total_seconds()) < 5


# ----------------------------------------------------------------------
# Удержание и молчание источника
# ----------------------------------------------------------------------


LIVE_EVENT = json.dumps(
    {
        "event_type": "object_state",
        "camera_id": "hmd-01",
        "timestamp": SENT_TS,
        "object": "helmet",
        "subject_id": "person-1",
        "state": "on",
        "confidence": 0.94,
        "observation_status": "RECOGNIZED",
        "model_version": "ppe-yolo11m-v1",
    }
)


def make_service(**overrides) -> CvIngressService:
    """Приёмник в той конфигурации, в какой к нему подключается CV-модуль."""
    config = load_config("configs/app-ppe-live.yaml")
    config.cv_ingress = config.cv_ingress.model_copy(update=overrides)
    return CvIngressService(InMemoryBus(), load_sop(config.sop_path), config)


async def test_static_scene_is_not_a_lost_stream():
    """Неподвижный кадр молчит законно.

    CV присылает события только на смену состояния: сотрудник стоит в каске —
    событий нет вовсе. Счесть это потерей потока значило бы оборвать проверку
    на ровном месте, а на шаге удержания каски — оборвать её гарантированно.

    Такт удержания вызывается напрямую: ждать своей очереди у фонового цикла
    в проверке незачем, а результат от этого не меняется.
    """
    service = make_service(hold_timeout_sec=0.05)
    await service.handle_message(LIVE_EVENT)
    await asyncio.sleep(0.1)
    await service._republish_tick()

    status = service.status()
    assert status["held"] == ["helmet"]
    assert status["stream_lost"] is False


async def test_silence_is_a_lost_stream_when_heartbeat_promised():
    """Когда смежник обещал признак жизни, молчание снова означает беду."""
    service = make_service(hold_timeout_sec=0.05, heartbeat_expected=True)
    await service.handle_message(LIVE_EVENT)
    await asyncio.sleep(0.1)
    await service._republish_tick()

    status = service.status()
    assert status["held"] == []
    assert status["stream_lost"] is True


class FakeSocket:
    """Сокет CV-модуля: отдаёт сообщения и отключается."""

    client = None

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)

    async def accept(self) -> None:
        pass

    async def receive_text(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        from fastapi import WebSocketDisconnect

        raise WebSocketDisconnect()


async def test_live_module_is_ignored_while_recording_plays():
    """Пока идёт тестовая запись, живой модуль молчит для стенда.

    Иначе два источника по одной переменной спорили бы, а «нет камеры» или
    отключение живого модуля оборвали бы показ записи.
    """
    service = make_service()
    service.player._task = asyncio.create_task(asyncio.sleep(10))
    try:
        await service.serve(FakeSocket([LIVE_EVENT]))
        assert service._held == {}
        assert service._stream_lost is False
    finally:
        service.player._task.cancel()


async def test_live_module_disconnect_is_a_lost_stream():
    service = make_service()
    await service.serve(FakeSocket([LIVE_EVENT]))
    assert service.status()["stream_lost"] is True
