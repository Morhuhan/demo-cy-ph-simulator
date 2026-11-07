"""Разбор занятия по шаблону — реализация по умолчанию.

Работает всегда: без сети, без ключей, без видеокарты. Это важно не только для
изолированного контура по п. 1.3 ТЗ, но и для показа: если на площадке отвалится
интернет, разбор всё равно сформируется, просто суше.
"""

from __future__ import annotations

from .base import DebriefEvent, DebriefInput, minutes

_KIND_TITLES = {
    "MISMATCH": "нарушение",
    "TIMEOUT": "шаг не выполнен за отведённое время",
    "UNCLEAR": "система не разобрала действие",
}


class TemplateResponder:
    name = "template"

    async def compose(self, data: DebriefInput) -> str:
        return "\n\n".join(part for part in self._parts(data) if part)

    # ------------------------------------------------------------------

    def _parts(self, data: DebriefInput) -> list[str]:
        return [
            self._headline(data),
            self._problems(data),
            self._closing(data),
        ]

    def _headline(self, data: DebriefInput) -> str:
        who = data.trainee_name or "Обучаемый"
        outcome = (
            "последовательность выполнена полностью"
            if data.completed
            else f"пройдено шагов: {data.steps_done} из {data.steps_total}"
        )
        errors = (
            "замечаний нет"
            if not data.errors
            else f"замечаний: {data.errors}"
        )
        return (
            f"{who}, сценарий «{data.scenario}». "
            f"Длительность {minutes(data.duration_sec)}, {outcome}, {errors}."
        )

    def _problems(self, data: DebriefInput) -> str:
        problems = data.problems()
        if not problems:
            return ""

        lines = ["Что пошло не так:"]
        for event in problems:
            lines.append(f"• {self._problem_line(event)}")
        return "\n".join(lines)

    def _problem_line(self, event: DebriefEvent) -> str:
        kind = _KIND_TITLES.get(event.kind, event.kind.lower())
        head = f"{minutes(event.at_sec)} — «{event.step_title}», {kind}."
        body = event.message.strip()
        tail = ""
        if event.consequence:
            # Последствие берётся из регламента, а не придумывается: на разборе
            # обучаемому важно понимать не «нельзя», а «почему нельзя».
            tail = f" Последствие по регламенту: {event.consequence.strip()}"
        return f"{head} {body}{tail}".strip()

    def _closing(self, data: DebriefInput) -> str:
        if data.state == "aborted":
            return "Занятие остановлено инструктором до завершения сценария."
        if not data.completed:
            return "Сценарий не доведён до конца."
        if not data.errors:
            return "Замечаний нет, порядок действий соблюдён."

        critical = [e for e in data.problems() if e.severity == "critical"]
        if critical:
            steps = ", ".join(sorted({f"«{e.step_title}»" for e in critical}))
            return f"Повторить в первую очередь: {steps}."
        return "Сценарий доведён до конца, замечания некритические."
