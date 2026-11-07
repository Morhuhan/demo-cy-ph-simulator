"""Экран тренажёра (/expo): страница, ассеты, наставник и журнал прохождений.

Логика прохождения живёт в браузере. Здесь проверяется то, что даёт стенд:
файлы отдаются и не выходят за свою папку, наставник получает состояние
этапа и честно отказывает без ключа, итог ложится в журнал.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from libs.config import MentorConfig, load_config, load_sops
from libs.stand import SharedResources
from run_local import STAND_KEY_ENV, build_app
from services.instructor_ui import app as ui_app
from services.instructor_ui.mentor import AskRequest, Mentor, MentorError, build_user_message

STATE = {
    "step": 1,
    "steps_total": 5,
    "step_title": "Открытие люка-лаза",
    "question": "Заземление подключено. Следующее действие?",
    "options": ["Открыть крышку люка-лаза", "Сообщить в операторную о готовности"],
    "done": ["Заземление цистерны"],
    "last_error": "Стоп. Докладывать о готовности рано.",
    "errors": 1,
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(STAND_KEY_ENV, "demo")
    monkeypatch.setattr(ui_app, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(ui_app, "expo_control", ui_app.ExpoControl())
    config = load_config("configs/app-ppe-live.yaml")
    config.storage.url = f"sqlite+aiosqlite:///{tmp_path / 'expo.sqlite3'}"
    config.voice.warmup = False
    with TestClient(build_app(SharedResources.load(config))) as c:
        yield c


def test_page_and_scene_are_served(client):
    assert client.get("/expo", follow_redirects=False).headers["location"] == "/expo/"
    page = client.get("/expo/")
    assert page.status_code == 200
    assert "scene3d.js" in page.text
    assert client.get("/expo/scene3d.js").status_code == 200
    assert client.get("/expo/lessons.js").status_code == 200
    assert client.get("/expo/models/layout.json").status_code == 200
    assert client.get("/expo/vendor/three/build/three.module.js").status_code == 200


def test_assets_do_not_leave_their_folder(client):
    assert client.get("/expo/../app.py").status_code == 404
    assert client.get("/expo/%2e%2e/app.py").status_code == 404
    assert client.get("/expo/nothing.js").status_code == 404


def test_without_key_mentor_refuses_honestly(client, monkeypatch):
    """Нет ключа — 503, и экран отвечает подсказкой этапа сам."""
    monkeypatch.delenv("AI_MENTOR_KEY", raising=False)
    assert client.get("/expo/state").json()["mentor"] is False
    response = client.post("/expo/ask", json={"question": "Сколько длится налив?", "state": STATE})
    assert response.status_code == 503


def test_result_is_appended_to_the_log(client, tmp_path):
    body = {"steps_done": 5, "steps_total": 5, "errors": 1, "ppe_violations": 0, "asks": 2, "seconds": 154}
    first = client.post("/expo/result", json=body).json()
    second = client.post("/expo/result", json=body).json()
    assert (first["count"], second["count"]) == (1, 2)
    lines = (tmp_path / "expo-results.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["errors"] == 1 and record["seconds"] == 154 and "ts" in record


def test_journal_is_served_latest_first(client):
    for errors in (3, 0):
        body = {"steps_done": 5, "steps_total": 5, "errors": errors, "ppe_violations": 0, "asks": 0,
                "seconds": 60, "lesson": "top-fill", "mode": "exam", "passed": not errors}
        client.post("/expo/result", json=body)
    journal = client.get("/expo/results?limit=1").json()
    assert journal["count"] == 2
    assert [(x["errors"], x["mode"], x["passed"]) for x in journal["items"]] == [(0, "exam", True)]


def test_pult_controls_the_screen(client):
    """Пульт меняет занятие и отдаёт команды, экран получает их по /expo/live."""
    assert client.get("/expo/instructor").status_code == 200
    with client.websocket_connect("/expo/live") as screen:
        first = screen.receive_json()
        assert first["type"] == "control" and first["state"]["lesson"] == "top-fill"

        state = client.post("/expo/control", json={"mode": "exam", "view": "eye"}).json()["state"]
        assert (state["lesson"], state["mode"], state["view"]) == ("top-fill", "exam", "eye")
        pushed = screen.receive_json()
        assert pushed["state"] == state and pushed["command"] is None

        for command in ("start", "pause", "resume"):
            client.post("/expo/control", json={"command": command})
            assert screen.receive_json()["command"] == command

        client.post("/expo/status", json={"phase": "run", "lesson": "top-fill", "mode": "review", "step": 2, "steps_total": 16,
                                          "step_title": "Заземление", "helmet": True, "present": True})
        status = screen.receive_json()
        assert status["type"] == "status" and status["status"]["step_title"] == "Заземление"

    assert client.get("/expo/control").json()["status"]["phase"] == "run"
    assert client.post("/expo/control", json={"mode": "review"}).json()["state"]["mode"] == "review"
    assert client.post("/expo/control", json={"mode": "free"}).status_code == 422
    assert client.post("/expo/control", json={"command": "shutdown"}).status_code == 422


def mentor(handler, **config) -> Mentor:
    return Mentor(MentorConfig(base_url="https://llm.test/v1", **config), transport=httpx.MockTransport(handler))


def answer(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_concept_screens_talk_through_stand(client):
    assert client.get("/expo/concept.html").status_code == 200
    with client.websocket_connect("/expo/concept/live") as pult, client.websocket_connect("/expo/concept/live") as screen:
        screen.send_text('{"hello": 1}')
        assert pult.receive_text() == '{"hello": 1}'
        pult.send_text('{"a": "next"}')
        assert screen.receive_text() == '{"a": "next"}'


def test_mentor_gets_regulation_and_current_step(monkeypatch):
    monkeypatch.setenv("AI_MENTOR_KEY", "test")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["auth"] = request.headers["Authorization"]
        return answer(" Сначала  откройте люк. ")

    body = AskRequest(question="А что потом?", state={**STATE, "notes": "Крышка откидывается вверх до фиксации."})
    text, model = asyncio.run(mentor(handler, model="fast").ask(load_sops()["kft-01"], body))

    assert (text, model) == ("Сначала откройте люк.", "fast")
    assert seen["auth"] == "test"
    user = seen["messages"][1]["content"]
    assert "этап 2 из 5: Открытие люка-лаза" in user
    assert "Докладывать о готовности рано" in user
    assert "Крышка откидывается вверх" in user
    assert "заземлени" in user.lower()  # текст регламента приложен


def test_silent_model_hands_over_to_fallback(monkeypatch):
    """Посетитель ждёт стоя: первая не ответила — спрашиваем запасную."""
    monkeypatch.setenv("AI_MENTOR_KEY", "test")

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"] == "fast":
            raise httpx.ReadTimeout("slow", request=request)
        return answer("Подключите заземление.")

    text, model = asyncio.run(mentor(handler, model="fast", fallback_models=["spare"]).ask(
        load_sops()["kft-01"], AskRequest(question="Что дальше?", state=STATE)))
    assert (text, model) == ("Подключите заземление.", "spare")


def test_mentor_failure_is_an_error_not_an_answer(monkeypatch):
    monkeypatch.setenv("AI_MENTOR_KEY", "test")
    with pytest.raises(MentorError, match="500"):
        asyncio.run(mentor(lambda r: httpx.Response(500)).ask(
            load_sops()["kft-01"], AskRequest(question="?", state=STATE)))


def test_prompt_marks_paused_scenario():
    body = AskRequest(question="Почему стоим?", state={**STATE, "helmet": False})
    assert "не обнаружена" in build_user_message(load_sops()["kft-01"], body)
