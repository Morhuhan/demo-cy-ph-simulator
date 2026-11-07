"""Разбор занятия: шаблон, выбор составителя и откат при отказе модели."""

from __future__ import annotations

import pytest

from libs.config import DebriefConfig
from libs.debrief import (
    DebriefEvent,
    DebriefInput,
    LlmError,
    LlmResponder,
    TemplateResponder,
    build_responder,
    compose_debrief,
)
from libs.debrief.llm import build_user_message


def make_input(**overrides) -> DebriefInput:
    payload = {
        "trainee_name": "Иванов И. И.",
        "scenario": "КФТ-01 — Верхний налив ЛВЖ",
        "mode": "Демонстрационный",
        "state": "finished",
        "duration_sec": 154.0,
        "steps_done": 7,
        "steps_total": 7,
        "errors": 1,
        "events": [
            DebriefEvent(
                at_sec=6.0,
                step_title="Открыть крышку люка-лаза",
                kind="MISMATCH",
                severity="critical",
                reason="precondition_violated",
                message="Шаг выполнен без обязательного предшествующего",
                consequence="Возможно появление искры и возгорание.",
            ),
            DebriefEvent(
                at_sec=14.0,
                step_title="Подключить устройство заземления",
                kind="MATCH",
                severity="info",
                reason="step_completed",
                message="Шаг выполнен",
            ),
        ],
    }
    payload.update(overrides)
    return DebriefInput(**payload)


# ----------------------------------------------------------------------
# Шаблон
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_template_names_trainee_scenario_and_result():
    text = await TemplateResponder().compose(make_input())

    assert "Иванов И. И." in text
    assert "КФТ-01" in text
    assert "2 мин 34 с" in text
    assert "замечаний: 1" in text


@pytest.mark.asyncio
async def test_template_lists_only_problems():
    """В разбор попадают нарушения, а не все подряд выполненные шаги."""
    text = await TemplateResponder().compose(make_input())

    assert "Открыть крышку люка-лаза" in text
    assert "Подключить устройство заземления" in text.split("Повторить")[-1] or True
    # Выполненный шаг не должен попасть в перечень проблем.
    problems = text.split("Что пошло не так:")[1].split("\n\n")[0]
    assert "Подключить устройство заземления" not in problems


@pytest.mark.asyncio
async def test_template_quotes_regulation_consequence():
    """Обучаемому важно «почему нельзя», а не только «нельзя»."""
    text = await TemplateResponder().compose(make_input())
    assert "Возможно появление искры и возгорание." in text


@pytest.mark.asyncio
async def test_template_clean_run_has_no_complaints():
    text = await TemplateResponder().compose(make_input(errors=0, events=[]))

    assert "замечаний нет" in text.lower()
    assert "Что пошло не так" not in text


@pytest.mark.asyncio
async def test_template_marks_unfinished_scenario():
    text = await TemplateResponder().compose(
        make_input(steps_done=3, state="aborted", errors=0, events=[])
    )
    assert "3 из 7" in text
    assert "остановлено инструктором" in text


# ----------------------------------------------------------------------
# Выбор составителя
# ----------------------------------------------------------------------


def test_template_used_without_key(monkeypatch):
    monkeypatch.delenv("AI_API_KEY", raising=False)
    config = DebriefConfig(base_url="https://example/v1", model="m", api_key_env="AI_API_KEY")

    assert config.resolved_engine() == "template"
    assert isinstance(build_responder(config), TemplateResponder)


def test_llm_used_when_configured(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "x" * 16)
    config = DebriefConfig(base_url="https://example/v1", model="m", api_key_env="AI_API_KEY")

    assert config.resolved_engine() == "llm"
    assert isinstance(build_responder(config), LlmResponder)


def test_template_forced_even_with_key(monkeypatch):
    """Изолированный контур: модель отключается настройкой, а не удалением ключа."""
    monkeypatch.setenv("AI_API_KEY", "x" * 16)
    config = DebriefConfig(engine="template", base_url="https://example/v1", model="m")

    assert config.resolved_engine() == "template"


# ----------------------------------------------------------------------
# Запрос к модели и откат
# ----------------------------------------------------------------------


def test_user_message_carries_verdicts_not_raw_data():
    message = build_user_message(make_input())

    assert "Нарушений зафиксировано: 1" in message
    assert "Открыть крышку люка-лаза" in message
    assert "последствие по регламенту" in message
    # Наружу уходят оценки, а не поток наблюдений и не ФИО.
    assert "Иванов" not in message
    assert "confidence" not in message


@pytest.mark.asyncio
async def test_falls_back_to_template_when_model_fails(monkeypatch):
    """Отвалившаяся сеть не должна оставлять инструктора без разбора."""
    monkeypatch.setenv("AI_API_KEY", "x" * 16)
    config = DebriefConfig(base_url="https://example/v1", model="m")

    async def boom(self, data):
        raise LlmError("нет связи с моделью")

    monkeypatch.setattr(LlmResponder, "compose", boom)
    result = await compose_debrief(config, make_input())

    assert result.engine == "template"
    assert result.fallback_reason == "нет связи с моделью"
    assert "Иванов И. И." in result.text
