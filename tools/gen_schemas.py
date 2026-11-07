"""Генерация JSON Schema контрактов обмена.

Файлы в contracts/ не пишутся руками — они порождаются из pydantic-моделей и
из машиночитаемого СОП. Смысл в том, что контракт, который отдаётся CV-команде,
физически не может разойтись с кодом, который его принимает: разошёлся бы —
сгенерировался бы другой файл.

Перечисления переменных и допустимых значений подставляются из configs/sop/*.yaml.
Поэтому добавление нового наблюдаемого состояния — это правка YAML и повторный
запуск этой команды, без единой строки Python.

    python -m tools.gen_schemas
    python -m tools.gen_schemas --check    # только проверить актуальность
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from libs.config import load_config, load_sop, project_root
from libs.contracts import (
    Observation,
    SessionSnapshot,
    SopSpec,
    SpeakRequest,
    StateChange,
    StreamHealth,
    Verdict,
    VoiceCommand,
)
from services.cv_ingress.wire import ObjectStateEvent, ServiceEvent

OUTPUT_DIR = "contracts"

#: Что генерируем и зачем. Порядок — от границы с CV-командой внутрь системы.
EXPORTS: list[tuple[str, type, str]] = [
    (
        "cv-object-state",
        ObjectStateEvent,
        "Событие CV-модуля о состоянии объекта, как оно приходит на сокет /cv. "
        "Это контракт смежника, а не внутренний: он описывает то, что стенд "
        "принимает от модуля распознавания.",
    ),
    (
        "cv-service-event",
        ServiceEvent,
        "Служебное сообщение CV-модуля о собственном состоянии, сокет /cv.",
    ),
    (
        "observation",
        Observation,
        "Наблюдение CV. Это граница ответственности с командой компьютерного зрения.",
    ),
    ("stream-health", StreamHealth, "Сообщение CV о состоянии видеопотока."),
    ("state-change", StateChange, "Подтверждённая смена состояния — выход сглаживания."),
    ("verdict", Verdict, "Решение сценарного движка."),
    ("session-snapshot", SessionSnapshot, "Полное состояние сеанса тренировки."),
    ("speak-request", SpeakRequest, "Задание голосовому ассистенту."),
    ("voice-command", VoiceCommand, "Распознанная реплика оператора."),
    ("sop", SopSpec, "Схема машиночитаемого СОП (YAML-файлы в configs/sop/)."),
]


def build_schema(model: type, title: str, description: str, sop) -> dict[str, Any]:
    schema = model.model_json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = title
    schema["description"] = description

    if model is Observation:
        _inject_vocabulary(schema, sop)

    return schema


def _inject_vocabulary(schema: dict[str, Any], sop) -> None:
    """Подставить в схему наблюдения словарь переменных из СОП.

    Без этого схема разрешала бы любую строку в `variable` и `value`, и
    опечатка на стороне CV выяснилась бы только на интеграции. С перечислениями
    её ловит валидатор на первом же сообщении.
    """
    variables = sorted(sop.observables)
    values = sorted({value for obs in sop.observables.values() for value in obs.values})

    properties = schema.setdefault("properties", {})
    properties["variable"] = {
        **properties.get("variable", {}),
        "enum": variables,
        "description": (
            "Наблюдаемая переменная состояния. Перечисление порождено из "
            f"configs/sop/{sop.id}.yaml, секция observables."
        ),
    }

    value_property = properties.get("value", {})
    value_property.pop("anyOf", None)
    properties["value"] = {
        **value_property,
        "type": ["string", "null"],
        "enum": [*values, None],
        "description": (
            "Значение переменной; null при unclear=true. Допустимые значения "
            "зависят от переменной — таблица соответствия в docs/contracts.md."
        ),
    }

    schema["x-observables"] = {
        name: {
            "title": observable.title,
            "description": observable.description,
            "initial": observable.initial,
            "values": {value: meta.title for value, meta in observable.values.items()},
        }
        for name, observable in sop.observables.items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Генерация JSON Schema контрактов")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Не писать файлы, а проверить, что они соответствуют коду",
    )
    args = parser.parse_args(argv)

    config = load_config()
    sop = load_sop(config.sop_path)
    output_dir: Path = project_root() / OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    stale: list[str] = []
    for name, model, description in EXPORTS:
        schema = build_schema(model, name, description, sop)
        body = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
        path = output_dir / f"{name}.schema.json"

        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != body:
                stale.append(path.name)
            continue

        path.write_text(body, encoding="utf-8")
        print(f"  {path.relative_to(project_root())}")

    if args.check:
        if stale:
            print("схемы устарели: " + ", ".join(stale))
            print("выполните: python -m tools.gen_schemas")
            return 1
        print("схемы актуальны")
        return 0

    print(f"\nсгенерировано схем: {len(EXPORTS)} (сценарий {sop.scenario_code})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
