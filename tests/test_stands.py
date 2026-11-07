"""Несколько занятий на одном стенде.

Стенд открывают по ссылке сразу несколько человек. Проверяется главное: их
занятия не пересекаются. Один переключает сюжет и запускает прогон — у второго
ничего не меняется, потому что службы у них разные, а не потому, что события
кто-то отфильтровал.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from libs.config import load_config
from libs.stand import SharedResources, StandLimitReached, StandRegistry
from run_local import (
    STAND_COOKIE,
    STAND_KEY_ENV,
    STAND_QUERY,
    WS_NO_STAND_KEY,
    build_app,
)


@pytest.fixture
def shared(tmp_path):
    config = load_config()
    # База на время проверки своя, речь не греем: прогрев синтезировал бы
    # три десятка фраз на каждое занятие и растянул бы проверку на минуты.
    config.storage.url = f"sqlite+aiosqlite:///{tmp_path / 'stands.sqlite3'}"
    config.voice.warmup = False
    return SharedResources.load(config)


@pytest.fixture
async def stand_app(shared):
    app = build_app(shared)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://stand"
    ) as _warm:
        # Поднимаем и опускаем стенд через настоящий жизненный цикл приложения.
        async with app.router.lifespan_context(app):
            yield app


def viewer(app) -> httpx.AsyncClient:
    """Отдельный зритель: своя cookie, значит и занятие своё."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://stand"
    )


@pytest.mark.asyncio
async def test_viewers_get_their_own_session(stand_app):
    async with viewer(stand_app) as first, viewer(stand_app) as second:
        # Ключ занятия выдаёт страница — как у браузера.
        await first.get("/")
        await second.get("/")
        await first.get("/scenario")
        await second.get("/scenario")
        assert first.cookies.get(STAND_COOKIE) != second.cookies.get(STAND_COOKIE)
        assert stand_app.state.stands.count == 2

        # Один переключает сюжет — у второго остаётся прежний.
        await first.post("/scenarios/ppe-01")
        assert (await first.get("/scenario")).json()["id"] == "ppe-01"
        assert (await second.get("/scenario")).json()["id"] != "ppe-01"


@pytest.mark.asyncio
async def test_session_of_one_viewer_does_not_touch_another(stand_app):
    async with viewer(stand_app) as first, viewer(stand_app) as second:
        await first.get("/")
        await second.get("/")
        await first.post("/scenarios/ppe-01")
        await second.post("/scenarios/ppe-01")

        await first.post(
            "/session/start", json={"mode": "training", "trainee_name": "Первый"}
        )
        await first.post("/cv/logs/real-donning/play")
        await asyncio.sleep(1.2)

        assert (await first.get("/cv/status")).json()["replay"] == "real-donning"
        # У второго зрителя показ не начинался.
        assert (await second.get("/cv/status")).json()["replay"] is None

        # И остановка у второго не трогает чужой показ.
        await second.post("/cv/logs/stop")
        assert (await first.get("/cv/status")).json()["replay"] == "real-donning"


@pytest.mark.asyncio
async def test_same_viewer_keeps_one_session_across_tabs(stand_app):
    async with viewer(stand_app) as tab:
        await tab.get("/")
        await tab.get("/scenario")
        key = tab.cookies.get(STAND_COOKIE)
        assert stand_app.state.stands.count == 1

        # Вторая вкладка того же человека приходит с тем же ключом.
        async with viewer(stand_app) as other_tab:
            other_tab.cookies.set(STAND_COOKIE, key)
            await other_tab.get("/scenario")
            assert stand_app.state.stands.count == 1


@pytest.mark.asyncio
async def test_request_without_key_does_not_create_session(stand_app):
    """Обращение мимо страницы занятие не поднимает.

    Иначе любой обход стенда заводил бы по занятию на запрос и вытеснял тех,
    кто действительно смотрит.
    """
    async with viewer(stand_app) as stranger:
        response = await stranger.get("/cv/status")
        assert response.status_code == 409
        assert stand_app.state.stands.count == 0


@pytest.mark.asyncio
async def test_registry_refuses_when_full(shared):
    """Мест конечное число: отказ честный, а не молчаливое вытеснение чужого."""
    registry = StandRegistry(shared, limit=1)
    try:
        await registry.get("первый")
        with pytest.raises(StandLimitReached):
            await registry.get("второй")
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_registry_closes_abandoned_session(shared):
    """Занятие, от которого ушли, освобождает место."""
    registry = StandRegistry(shared, limit=1, idle_ttl_sec=0.0)
    try:
        await registry.get("ушедший")
        assert registry.count == 1

        # Вкладок нет, срок вышел — место освобождается для следующего.
        stand = await registry.get("пришедший")
        assert registry.count == 1
        assert registry.peek("ушедший") is None
        assert stand is registry.peek("пришедший")
    finally:
        await registry.stop()


# ----------------------------------------------------------------------
# Ключ занятия для смежника: у CV-модуля cookie нет
# ----------------------------------------------------------------------


CV_EVENT = json.dumps(
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
        "bbox": [324.0, 204.7, 450.5, 318.7],
        "frame_timestamp_sec": 0.33,
    }
)


@pytest.fixture
def ppe_shared(tmp_path):
    """Стенд в той же конфигурации, в какой к нему подключается CV-модуль."""
    config = load_config("configs/app-ppe-live.yaml")
    config.storage.url = f"sqlite+aiosqlite:///{tmp_path / 'ppe.sqlite3'}"
    config.voice.warmup = False
    return SharedResources.load(config)


def wait_for_events(client: TestClient, url: str, expected: int) -> dict:
    """Дождаться, пока приёмник разберёт отправленное.

    Сокет и запрос состояния — разные задачи одного цикла, порядок между ними
    не определён. Ждём результат, а не угадываем задержку.
    """
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        status = client.get(url).json()
        if status["events"] >= expected:
            return status
        time.sleep(0.05)
    raise AssertionError(f"событий так и не пришло: ожидалось {expected}")


def test_cv_module_names_the_session_in_the_url(ppe_shared):
    """Смежник подключается без cookie и указывает занятие адресом."""
    app = build_app(ppe_shared)
    with TestClient(app) as client:
        with client.websocket_connect(f"/cv?{STAND_QUERY}=demo") as socket:
            socket.send_text(CV_EVENT)
            status = wait_for_events(client, f"/cv/status?{STAND_QUERY}=demo", 1)

        assert status["subjects"] == ["person-1"]
        assert app.state.stands.count == 1


def test_socket_without_key_is_closed_not_crashed(ppe_shared):
    """Без ключа занятия складывать события некуда — отказ должен быть внятным."""
    app = build_app(ppe_shared)
    with TestClient(app) as client:
        with client.websocket_connect("/cv") as socket:
            with pytest.raises(WebSocketDisconnect) as refusal:
                socket.receive_text()

        assert refusal.value.code == WS_NO_STAND_KEY
        assert app.state.stands.count == 0


def test_shared_key_joins_browser_and_cv_module(ppe_shared, monkeypatch):
    """Общий ключ стенда: занятие одно, порядок запуска не важен.

    Ради этого он и заведён — на показе никто не переписывает ключ из
    интерфейса инструктора в конфигурацию модуля распознавания.
    """
    monkeypatch.setenv(STAND_KEY_ENV, "demo")
    app = build_app(ppe_shared)
    with TestClient(app) as client:
        # Модуль распознавания подключился первым, ключа не указал.
        with client.websocket_connect("/cv") as socket:
            socket.send_text(CV_EVENT)
            # Инструктор открыл страницу — и попал в то же занятие.
            page = client.get("/")
            assert page.cookies.get(STAND_COOKIE) == "demo"
            status = wait_for_events(client, "/cv/status", 1)

        assert status["subjects"] == ["person-1"]
        assert app.state.stands.count == 1


def test_broken_shared_key_is_refused_at_startup(monkeypatch):
    """Опечатка в ключе развела бы браузер и модуль по разным занятиям."""
    monkeypatch.setenv(STAND_KEY_ENV, "демо стенд")
    with pytest.raises(ValueError, match=STAND_KEY_ENV):
        build_app()
