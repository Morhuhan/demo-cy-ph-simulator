"""Разбор занятия языковой моделью.

Обращение к OpenAI-совместимому эндпоинту. Модель получает готовые вердикты и
пересказывает их — оценки она не выносит и не меняет: это дело сценарного
движка, и на приёмке предъявляются именно его решения.

Отсюда и построение запроса: в нём перечислены уже принятые решения, а в
инструкции прямо запрещено добавлять свои. Разбор — пересказ, а не вторая
экспертиза.
"""

from __future__ import annotations

import json
import logging

import httpx

from libs.config import DebriefConfig

from .base import DebriefInput, minutes

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты — инструктор производственного обучения. Тебе дают журнал занятия на \
тренажёре: список уже вынесенных системой оценок. Напиши короткий разбор \
для инструктора на русском языке.

Требования:
— 3–6 предложений, сплошным текстом, без списков и заголовков;
— опирайся только на переданные факты; ничего не додумывай;
— не выноси собственных оценок и не меняй уже принятые: количество нарушений \
в разборе должно совпадать с переданным;
— если нарушения были, назови их и следствие по регламенту своими словами;
— закончи одной рекомендацией, на что обратить внимание;
— деловой тон, без канцелярита и без обращения к обучаемому на «ты»."""


class LlmError(RuntimeError):
    """Модель недоступна или ответила не тем. Разбор составит шаблон."""


class LlmResponder:
    name = "llm"

    def __init__(self, config: DebriefConfig) -> None:
        self._config = config

    async def compose(self, data: DebriefInput) -> str:
        key = self._config.api_key()
        if not key:
            raise LlmError(f"не задана переменная окружения {self._config.api_key_env}")

        payload = {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_message(data)},
            ],
        }
        headers = {
            "Authorization": f"Bearer {key}" if self._config.use_bearer else key,
            "Content-Type": "application/json",
        }
        url = self._config.base_url.rstrip("/") + "/chat/completions"

        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_sec) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise LlmError(f"модель не ответила за {self._config.timeout_sec:.0f} с") from exc
        except httpx.HTTPError as exc:
            raise LlmError(f"нет связи с моделью: {type(exc).__name__}") from exc

        if response.status_code == 401:
            raise LlmError("ключ доступа отклонён")
        if response.status_code == 429:
            raise LlmError("превышен лимит запросов к модели")
        if response.status_code != 200:
            raise LlmError(f"модель вернула HTTP {response.status_code}")

        try:
            body = response.json()
            text = body["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            raise LlmError("ответ модели неожиданной структуры") from exc

        text = (text or "").strip()
        if not text:
            raise LlmError("модель вернула пустой ответ")

        usage = body.get("usage") or {}
        log.info(
            "разбор занятия составлен моделью %s (токенов: %s)",
            self._config.model,
            usage.get("total_tokens", "?"),
        )
        return text


def build_user_message(data: DebriefInput) -> str:
    """Факты занятия для модели.

    Передаются только вердикты и их контекст. Ни сырых наблюдений, ни ФИО в
    свободной форме: чем меньше уходит наружу, тем спокойнее с п. 2.3 о защите
    персональных данных.
    """
    lines = [
        f"Сценарий: {data.scenario}",
        f"Режим занятия: {data.mode}",
        f"Длительность: {minutes(data.duration_sec)}",
        f"Пройдено шагов: {data.steps_done} из {data.steps_total}",
        f"Нарушений зафиксировано: {data.errors}",
        "",
    ]

    if not data.events:
        lines.append("Оценок в журнале нет.")
        return "\n".join(lines)

    lines.append("Журнал оценок:")
    for event in data.events:
        parts = [
            f"{minutes(event.at_sec)}",
            f"шаг «{event.step_title}»",
            f"оценка {event.kind}",
        ]
        if event.severity != "info":
            parts.append(f"тяжесть {event.severity}")
        parts.append(event.message.strip())
        if event.consequence:
            parts.append(f"последствие по регламенту: {event.consequence.strip()}")
        lines.append("— " + "; ".join(parts))

    return "\n".join(lines)
