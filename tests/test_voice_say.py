"""Голос выставочного экрана: фраза — в WAV голосом стенда.

Синтез подменён: проверяется маршрут, кэш и подготовка текста, а не то, как
звучит Silero.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from libs.config import load_config
from libs.stand import SharedResources
from run_local import STAND_KEY_ENV, build_app
from services.voice_assistant.speakable import number_words, speakable
from services.voice_assistant.tts import BrowserTts, Synthesis, TtsEngine


class FakeTts(TtsEngine):
    name = "fake"

    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize(self, text: str) -> Synthesis:
        self.texts.append(text)
        return Synthesis(audio=b"RIFF" + text.encode(), duration_sec=1.0, synth_ms=1.0, engine=self.name)


@pytest.fixture
def stand(tmp_path, monkeypatch):
    monkeypatch.setenv(STAND_KEY_ENV, "demo")

    def make(tts) -> TestClient:
        config = load_config("configs/app-ppe-live.yaml")
        config.storage.url = f"sqlite+aiosqlite:///{tmp_path / 'say.sqlite3'}"
        config.voice.warmup = False
        shared = SharedResources.load(config)
        shared.tts = tts
        return TestClient(build_app(shared))

    return make


def test_phrase_comes_back_as_audio_and_is_cached(stand):
    tts = FakeTts()
    with stand(tts) as client:
        first = client.post("/voice/say", json={"text": "Налив ЛВЖ, этап 2."})
        second = client.post("/voice/say", json={"text": "Налив  ЛВЖ, этап 2."})

    assert first.status_code == 200 and first.headers["content-type"] == "audio/wav"
    assert second.content == first.content
    assert tts.texts == ["Налив эл вэ жэ, этап два."]


def test_without_server_voice_page_speaks_itself(stand):
    with stand(BrowserTts()) as client:
        assert client.post("/voice/say", json={"text": "Каска обнаружена."}).status_code == 503


@pytest.mark.parametrize(
    "number, words",
    [(0, "ноль"), (21, "двадцать один"), (112, "сто двенадцать"), (2000, "две тысячи"),
     (1205, "одна тысяча двести пять"), (60000, "шестьдесят тысяч")],
)
def test_numbers_in_words(number, words):
    assert number_words(number) == words


def test_speakable_reads_signs_units_and_ranges():
    assert speakable("от -40 до +60 °C") == "от минус сорок до плюс шестьдесят градусов"
    assert speakable("откачано 50%") == "откачано пятьдесят процентов"
    assert speakable("10–20 минут") == "десять – двадцать минут"
    assert speakable("Это СИЗ") == "Это сиз"
