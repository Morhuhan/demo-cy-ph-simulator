"""Отчёт о занятии документом Word.

Формат выбран под адресата: отчёт распечатывают, подшивают и прикладывают к
журналу обучения. CSV-выгрузка по п. 5.4 ТЗ остаётся отдельно — она про сводку
по многим занятиям, а здесь разбор одного.

Документ собирается из уже сохранённых данных: вердикты движка и составленный
по ним разбор. Ничего нового здесь не вычисляется.
"""

from __future__ import annotations

import io
from datetime import datetime

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from libs.debrief import Debrief, DebriefInput, minutes

#: Как называть вердикты в документе. Внутренние коды в отчёт не выносим.
_KIND_TITLES = {
    "MATCH": "Выполнено",
    "MISMATCH": "Нарушение",
    "UNCLEAR": "Не разобрано",
    "TIMEOUT": "Не уложился",
}

_STATE_TITLES = {
    "finished": "завершено",
    "aborted": "остановлено инструктором",
    "running": "не завершено",
    "paused": "приостановлено",
    "idle": "не начато",
}


def build_report(
    data: DebriefInput,
    debrief: Debrief,
    *,
    started_at: datetime | None = None,
) -> bytes:
    """Собрать документ и вернуть его байтами."""
    document = Document()
    _set_base_font(document)

    document.add_heading("Отчёт о занятии на тренажёре", level=0)

    _summary_table(document, data, started_at)

    document.add_heading("Разбор занятия", level=1)
    for block in debrief.text.split("\n\n"):
        for line in block.split("\n"):
            if line.strip():
                document.add_paragraph(line.strip())

    if data.events:
        document.add_heading("Журнал занятия", level=1)
        _journal_table(document, data)

    _footer(document, debrief)
    return _to_bytes(document)


# ----------------------------------------------------------------------


def _set_base_font(document: Document) -> None:
    style = document.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)


def _summary_table(
    document: Document, data: DebriefInput, started_at: datetime | None
) -> None:
    rows = [
        ("Обучаемый", data.trainee_name or "—"),
        ("Сценарий", data.scenario),
        ("Режим занятия", data.mode),
        ("Дата и время", started_at.astimezone().strftime("%d.%m.%Y %H:%M") if started_at else "—"),
        ("Длительность", minutes(data.duration_sec)),
        ("Пройдено шагов", f"{data.steps_done} из {data.steps_total}"),
        ("Замечаний", str(data.errors)),
        ("Состояние", _STATE_TITLES.get(data.state, data.state)),
    ]

    table = document.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for name, value in rows:
        cells = table.add_row().cells
        cells[0].text = name
        cells[1].text = value
        cells[0].paragraphs[0].runs[0].bold = True


def _journal_table(document: Document, data: DebriefInput) -> None:
    table = document.add_table(rows=1, cols=4)
    table.style = "Table Grid"

    header = table.rows[0].cells
    for index, title in enumerate(("Время", "Шаг", "Оценка", "Комментарий")):
        header[index].text = title
        header[index].paragraphs[0].runs[0].bold = True

    for event in data.events:
        cells = table.add_row().cells
        cells[0].text = minutes(event.at_sec)
        cells[1].text = event.step_title
        cells[2].text = _KIND_TITLES.get(event.kind, event.kind)
        cells[3].text = event.message.strip()


def _footer(document: Document, debrief: Debrief) -> None:
    """Подпись о том, чем составлен разбор.

    Указывается честно: если текст писала языковая модель, читатель отчёта
    должен об этом знать. Оценки в любом случае вынесены системой, а не
    моделью, — об этом здесь же.
    """
    source = (
        "языковой моделью по журналу занятия"
        if debrief.engine == "llm"
        else "автоматически по журналу занятия"
    )
    note = document.add_paragraph()
    note.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = note.add_run(
        f"Разбор составлен {source}. Оценки шагов вынесены системой "
        f"по описанию сценария и в разборе не пересматриваются."
    )
    run.italic = True
    run.font.size = Pt(9)

    if debrief.fallback_reason:
        reason = document.add_paragraph()
        run = reason.add_run(f"Языковая модель недоступна: {debrief.fallback_reason}.")
        run.italic = True
        run.font.size = Pt(9)


def _to_bytes(document: Document) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def file_name(data: DebriefInput, started_at: datetime | None) -> str:
    stamp = started_at.astimezone().strftime("%Y-%m-%d_%H-%M") if started_at else "отчёт"
    who = (data.trainee_name or "занятие").replace(" ", "_").replace(".", "")
    return f"{stamp}_{who}.docx"
