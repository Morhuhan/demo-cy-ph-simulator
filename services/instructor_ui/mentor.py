"""ИИ-наставник экрана тренажёра: ответ на вопрос обучаемого.

Все вопросы обучаемого — «что дальше», «почему ошибка», свободные — уходят
сюда. Модель получает текст регламента, справку по текущему этапу и
фактическое состояние прохождения, поэтому отвечает про то место, где
обучаемый сейчас стоит, а не общими словами.

Оценок модель не выносит: верно ли выбран шаг, решает сценарий экрана. Её
дело — объяснить.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
from pydantic import BaseModel, Field

from libs.config import MentorConfig
from libs.contracts import SopSpec

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты — ИИ-наставник киберфизического тренажёра. Оператор проходит \
упрощённый регламент верхнего налива ЛВЖ в вагон-цистерну: камера проверяет \
каску, на экране 3D-модель эстакады. Режимы: обзор — оборудование и операцию \
показывают по этапам, вопросов нет; обучение и экзамен — на каждом этапе \
оператор выбирает следующее действие из трёх вариантов. Режим указан в справке по этапу.

Как отвечать:
— по-русски, 1–2 коротких предложения: ответ произносится вслух синтезатором, \
поэтому числа пиши словами, без цифр, латиницы и сокращений;
— опирайся на текст регламента, справку по этапу и состояние прохождения, ничего не додумывай;
— «этот шаг» — всегда текущий этап из состояния, а не другой пункт регламента;
— не называй номер правильного варианта и не говори «это правильный выбор»; \
на прямую просьбу о подсказке можно объяснить, что требует регламент;
— не выноси оценок: верно или неверно выбран шаг, решает тренажёр;
— если вопрос не про операцию и безопасность, ответь одной фразой и предложи \
вернуться к текущему этапу, ничего не подсказывая;
— без списков, без markdown, на «вы»."""


class MentorState(BaseModel):
    """Что видит обучаемый: этап, вопрос на экране, последняя ошибка."""

    step: int = Field(ge=0, le=20)
    steps_total: int = Field(ge=1, le=20)
    step_title: str = Field(max_length=200)
    notes: str = Field(default="", max_length=1500, description="Справка экрана по текущему этапу")
    question: str = Field(default="", max_length=300)
    options: list[str] = Field(default_factory=list, max_length=4)
    done: list[str] = Field(default_factory=list, max_length=20)
    last_error: str | None = Field(default=None, max_length=600)
    errors: int = Field(default=0, ge=0)
    helmet: bool = True


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=400)
    state: MentorState


class MentorError(RuntimeError):
    """Модель недоступна. Экран ответит по тексту СОП."""


def regulation_text(sop: SopSpec) -> str:
    lines = [f"Регламент «{sop.title}»:"]
    for number, step in enumerate(sop.steps, 1):
        line = f"{number}. {step.title}."
        if step.phrases.hint:
            line += f" Как выполняется: {step.phrases.hint.strip()}"
        if step.consequence:
            line += f" Если нарушить: {step.consequence.strip()}"
        lines.append(line)
    return "\n".join(lines)


def build_user_message(sop: SopSpec, body: AskRequest) -> str:
    s = body.state
    parts = [
        regulation_text(sop),
        "",
        "Состояние прохождения:",
        f"— этап {s.step + 1} из {s.steps_total}: {s.step_title};",
        f"— выполнено: {', '.join(s.done) if s.done else 'пока ничего'};",
        f"— ошибок последовательности: {s.errors};",
        f"— каска: {'надета' if s.helmet else 'не обнаружена, сценарий приостановлен'};",
    ]
    if s.notes:
        parts.append(f"— справка по этапу «{s.step_title}»: {s.notes}")
    if s.question:
        parts.append(f"— на экране вопрос: «{s.question}»")
    if s.options:
        parts.append("— варианты: " + "; ".join(f"«{o}»" for o in s.options))
    if s.last_error:
        parts.append(f"— последняя ошибка: {s.last_error}")
    parts += ["", f"Вопрос обучаемого: «{body.question.strip()}»"]
    return "\n".join(parts)


# Соединение с провайдером держится открытым между вопросами: новое TLS-
# рукопожатие стоит секунды две, а ответ модели по готовому — меньше секунды.
_shared: dict[tuple[str, float], httpx.AsyncClient] = {}
_last_used: dict[tuple[str, float], float] = {}


def _client(config: MentorConfig) -> httpx.AsyncClient:
    key = (config.base_url, config.timeout_sec)
    client = _shared.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=config.timeout_sec,
            limits=httpx.Limits(keepalive_expiry=600),
        )
        _shared[key] = client
    _last_used[key] = time.monotonic()
    return client


class Mentor:
    def __init__(self, config: MentorConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._config = config
        self._transport = transport

    async def warm(self) -> None:
        """Открыть соединение заранее, если оно простаивало: первый вопрос обучаемого не ждёт рукопожатия."""
        key = (self._config.base_url, self._config.timeout_sec)
        if not self.available() or time.monotonic() - _last_used.get(key, 0) < 50:
            return
        try:
            await _client(self._config).get(
                self._config.base_url.rstrip("/") + "/models",
                headers={"Authorization": self._auth()},
            )
        except httpx.HTTPError as exc:
            log.info("наставник: прогрев соединения не удался: %s", type(exc).__name__)

    def _auth(self) -> str:
        key = self._config.api_key()
        return f"Bearer {key}" if self._config.use_bearer else key

    @property
    def models(self) -> list[str]:
        return [m for m in [self._config.model, *self._config.fallback_models] if m]

    def available(self) -> bool:
        if self._config.engine == "template":
            return False
        return bool(self._config.base_url and self.models and self._config.api_key())

    async def ask(self, sop: SopSpec, body: AskRequest) -> tuple[str, str]:
        """Ответ и модель, которая его дала. Не ответила одна — спрашиваем следующую."""
        if not self.available():
            raise MentorError("языковая модель не настроена")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(sop, body)},
        ]
        reasons = []
        if self._transport is not None:
            client = httpx.AsyncClient(timeout=self._config.timeout_sec, transport=self._transport)
        else:
            client = _client(self._config)
        try:
            for model in self.models:
                try:
                    return await self._ask_one(client, model, messages), model
                except MentorError as exc:
                    log.warning("наставник, %s: %s", model, exc)
                    reasons.append(f"{model}: {exc}")
        finally:
            if self._transport is not None:
                await client.aclose()
        raise MentorError("; ".join(reasons))

    async def _ask_one(self, client: httpx.AsyncClient, model: str, messages: list[dict]) -> str:
        headers = {"Authorization": self._auth(), "Content-Type": "application/json"}
        payload = {"model": model, "max_tokens": self._config.max_tokens, "messages": messages}
        url = self._config.base_url.rstrip("/") + "/chat/completions"
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise MentorError(f"не ответила за {self._config.timeout_sec:.0f} с") from exc
        except httpx.HTTPError as exc:
            raise MentorError(f"нет связи: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise MentorError(f"HTTP {response.status_code}")
        try:
            text = response.json()["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise MentorError("ответ неожиданной структуры") from exc
        text = " ".join((text or "").split())
        if not text:
            raise MentorError("пустой ответ")
        return text
