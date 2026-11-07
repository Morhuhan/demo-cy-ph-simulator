"""Голос обучаемого: фраза с микрофона превращается в команду сценария.

Распознавание — Whisper на серверах Hugging Face. Сеть здесь подменена:
проверяется, что стенд правильно спрашивает и правильно понимает ответ, а не
то, как хорошо Whisper знает русский.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from libs.config import SttConfig, load_config
from libs.stand import SharedResources
from run_local import STAND_KEY_ENV, build_app
from services.voice_assistant.stt import HfRouterStt, Transcription, create_stt

WAV = b"RIFF....WAVEfmt "


def hf_stt(handler, **config) -> HfRouterStt:
    return HfRouterStt(SttConfig(engine="hf", **config), transport=httpx.MockTransport(handler))


def test_phrase_goes_to_fal_in_russian_with_prompt(monkeypatch):
    """Язык — явно: без него короткая команда с тихого микрофона уходит в английский."""
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"text": " К работе готов."})

    result = hf_stt(handler, prompt="заземление, люк-лаз").transcribe(WAV)

    assert result.text == "К работе готов."
    assert seen["url"].endswith("/fal-ai/fal-ai/whisper")
    assert seen["auth"] == "Bearer hf_test"
    assert seen["body"]["language"] == "ru"
    assert seen["body"]["prompt"] == "заземление, люк-лаз"
    assert base64.b64decode(seen["body"]["audio_url"].split(",", 1)[1]) == WAV


def test_fal_refusal_falls_back_to_hf_inference_in_russian(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "fal-ai" in str(request.url):
            return httpx.Response(503, text="busy")
        body = json.loads(request.content)
        assert body["parameters"]["generate_kwargs"]["language"] == "ru"
        assert base64.b64decode(body["inputs"]) == WAV
        return httpx.Response(200, json={"text": " Вариант 2."})

    assert hf_stt(handler).transcribe(WAV).text == "Вариант 2."
    assert seen[-1].endswith("/hf-inference/models/openai/whisper-large-v3-turbo")


def test_hf_refusal_is_an_error_not_an_empty_phrase(monkeypatch):
    """Отказ сервиса и тишина — разные вещи: первое надо показать инструктору."""
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    stt = hf_stt(lambda request: httpx.Response(401, json={"error": "Invalid credentials"}))

    with pytest.raises(RuntimeError, match="401"):
        stt.transcribe(WAV)


def test_without_token_commands_stay_buttons(monkeypatch):
    """Нет токена — стенд работает, а распознавание честно выключено."""
    monkeypatch.delenv("HF_TOKEN", raising=False)
    stt = create_stt(SttConfig(engine="hf"))

    assert stt.accepts_audio is False


# ----------------------------------------------------------------------
# Приём фразы стендом
# ----------------------------------------------------------------------


class FakeStt:
    name = "fake"
    accepts_audio = True

    def __init__(self, text: str) -> None:
        self.text = text
        self.received: bytes | None = None

    def available(self) -> bool:
        return True

    def transcribe(self, audio: bytes) -> Transcription:
        self.received = audio
        return Transcription(text=self.text, confidence=1.0, engine=self.name)


@pytest.fixture
def stand(tmp_path, monkeypatch):
    monkeypatch.setenv(STAND_KEY_ENV, "demo")

    def make(stt) -> TestClient:
        config = load_config("configs/app-ppe-live.yaml")
        config.storage.url = f"sqlite+aiosqlite:///{tmp_path / 'voice.sqlite3'}"
        config.voice.warmup = False
        shared = SharedResources.load(config)
        shared.stt = stt
        return TestClient(build_app(shared))

    return make


def test_spoken_report_becomes_a_command(stand):
    stt = FakeStt("К работе готов.")
    with stand(stt) as client:
        response = client.post(
            "/voice/listen", content=WAV, headers={"Content-Type": "audio/wav"}
        )

    assert response.status_code == 200
    assert response.json()["intent"] == "ready_report"
    assert stt.received == WAV


def test_microphone_check_does_not_reach_the_scenario(stand):
    """Проверка микрофона: команда распознана, но в сценарий не ушла.

    Иначе, проверяя микрофон посреди занятия, инструктор сам себе засчитал
    бы доклад о готовности.
    """
    with stand(FakeStt("К работе готов.")) as client:
        client.get("/voice/state")  # поднимает занятие стенда
        voice = client.app.state.stands.peek("demo").voice

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("пробная фраза ушла в сценарий")

        voice.submit_transcript = forbidden
        response = client.post("/voice/listen?dry_run=true", content=WAV)

    assert response.status_code == 200
    assert response.json()["intent"] == "ready_report"
    assert response.json()["transcript"] == "К работе готов."


def test_silence_is_not_a_command(stand):
    with stand(FakeStt("")) as client:
        response = client.post("/voice/listen", content=WAV)

    assert response.json() == {"intent": "unknown", "confidence": 0.0, "transcript": ""}


def test_listen_without_recognition_says_so(stand, monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with stand(create_stt(SttConfig(engine="hf"))) as client:
        state = client.get("/voice/state").json()
        response = client.post("/voice/listen", content=WAV)

    assert state["server_stt"] is False
    assert response.status_code == 503
    assert "не настроено" in json.loads(response.text)["detail"]
