# Контракты обмена

Все форматы описаны pydantic-моделями в `libs/contracts/`. JSON-схемы в
`contracts/` **генерируются** из них и из машиночитаемого СОП:

```bash
python -m tools.gen_schemas          # сгенерировать
python -m tools.gen_schemas --check  # проверить актуальность (для CI)
```

Смысл в том, что контракт, отдаваемый CV-команде, физически не может разойтись
с кодом, который его принимает. Разошёлся бы — сгенерировался бы другой файл.

## Общие правила

Каждое сообщение содержит `schema_version`, `event_id` и `ts`. Потребитель
обязан отбрасывать сообщения с несовместимой мажорной версией — это уже
реализовано в `libs/bus/base.py:decode`.

Всё время — **UTC, tz-aware**. Наивные `datetime` запрещены: сообщения приходят
из разных процессов, а по ним считается задержка для приёмки.

Сообщение, не прошедшее валидацию, логируется и пропускается. Один кривой пакет
от смежников не должен ронять сервис.

## Наблюдение — граница с CV-командой

Поток `cv.observations`, схема `contracts/observation.schema.json`.

```json
{
  "schema_version": "1.0",
  "event_id": "3f2a...",
  "ts": "2026-07-29T12:00:04.512000Z",
  "source": "cv-yolo-v1",
  "camera_id": "chest-01",
  "post_id": "С-4",
  "frame_ts": "2026-07-29T12:00:04.480000Z",
  "variable": "pipe_position",
  "value": "seated",
  "confidence": 0.83,
  "unclear": false,
  "bbox": {"x": 0.41, "y": 0.22, "w": 0.18, "h": 0.44}
}
```

Что важно соблюдать:

* **`frame_ts` — время кадра, а не время отправки.** От него считается задержка
  из п. 6.2 ТЗ. Если поставить сюда `now()`, измерение потеряет смысл.
* **Сглаживать на стороне CV не нужно.** Нужен сырой поток: пороги калибруются
  сценарной частью, и делать это надо, не трогая CV. Ожидаемый темп — около
  15 сообщений в секунду на переменную.
* **`unclear: true` — штатная ситуация**, а не ошибка. Объект перекрыт, засветка,
  вне кадра. При `unclear: true` поле `value` обязано быть `null`, при
  `unclear: false` — обязано быть заполнено. Это проверяется валидатором.
* **`confidence`** — уверенность модели в значении, `0..1`.
* **`post_id`** — номер поста с таблички, если распознан. Посты в ряд одинаковые,
  и без него непонятно, у какого стоит оператор.

### Словарь наблюдаемых состояний

Перечисления в схеме порождены из `configs/sop/kft-01.yaml`, секция `observables`.
Добавление нового состояния — правка YAML и повторная генерация, без единой
строки Python.

| Переменная | Значение | Смысл |
|---|---|---|
| `grounding_lamp` | `off` | лампа прибора заземления не горит |
| | `green` | горит зелёным, цепь замкнута |
| `hatch_cover` | `closed` | крышка люка-лаза закрыта |
| | `open` | открыта |
| `pipe_position` | `stowed` | труба в исходном положении |
| | `over_hatch` | над горловиной |
| | `seated` | опущена до упора крышки в горловину |
| | `bottom` | опущена до дна цистерны |
| `clamp_weight` | `released` | груз механизма прижатия отжат |
| | `pressed` | прижат |

**Внимание CV-команде:** `over_hatch` и `seated` визуально различаются
несколькими сантиметрами высоты трубы. Это самое трудное место сценария, здесь
ожидаются низкая уверенность и дребезг. Присылайте как есть — гашением дребезга
занимается `event_processor`.

## Состояние видеопотока

Поток `cv.health`, схема `contracts/stream-health.schema.json`.

```json
{"source": "cv-yolo-v1", "camera_id": "chest-01",
 "state": "lost", "reason": "обрыв RTSP-соединения"}
```

`state`: `online` | `degraded` | `lost`. Об отказах на своей стороне CV сообщает
явно, а не молча замолкает. Молчание источника движок тоже отследит — по
сторожевому таймеру, но с задержкой и без причины.

## Подтверждённое состояние

Поток `state.changes`, выход сглаживания. Публикуется **только при изменении**.

```json
{
  "variable": "pipe_position",
  "value": "seated",
  "previous_value": "over_hatch",
  "quality": "confirmed",
  "confidence": 0.84,
  "agreement": 0.88,
  "first_seen_ts": "2026-07-29T12:00:23.000000Z",
  "confirmed_ts": "2026-07-29T12:00:24.000000Z",
  "observations": 17
}
```

`quality`: `confirmed` (можно принимать решение) | `unclear` (согласия не хватает)
| `stale` (наблюдений давно нет). Задержка сглаживания — это
`confirmed_ts - first_seen_ts`.

## Вердикт

Поток `engine.verdicts`, схема `contracts/verdict.schema.json`.

```json
{
  "session_id": "9c1e...",
  "step_id": "hatch",
  "step_index": 2,
  "kind": "MISMATCH",
  "severity": "critical",
  "reason_code": "precondition_violated",
  "confidence": 0.91,
  "message": "Шаг «Открыть крышку люка-лаза» выполнен без обязательного…",
  "details": {"missing_steps": ["grounding"], "consequence": "…"},
  "source_event_id": "…",
  "latency_ms": 867.0
}
```

| Поле | Значения |
|---|---|
| `kind` | `MATCH`, `MISMATCH`, `UNCLEAR`, `TIMEOUT` |
| `severity` | `info`, `warning`, `critical` |
| `reason_code` | `step_completed`, `precondition_violated`, `out_of_order`, `unexpected_state`, `low_confidence`, `stream_lost`, `step_timeout` |

`reason_code` отделён от `message` потому, что текст меняется — в том числе если
реплики начнёт формулировать LLM, — а отчётность и метрики должны опираться на
стабильный код.

`latency_ms` — от `first_seen_ts` исходного состояния до вердикта. Включает окно
подтверждения, и это честно: с точки зрения оператора действие завершилось
тогда, когда состояние стало видно.

## Голосовой канал

`voice.speak` — задание ассистенту, `voice.spoken` — факт начала воспроизведения
с замером сквозной задержки, `voice.commands` — распознанная реплика оператора.

`kind` реплики: `instruction`, `confirmation`, `warning`, `critical`, `hint`,
`report`. `priority`: `normal` | `interrupt`.

Намерения оператора: `repeat`, `help`, `ready_report`, `unknown`.

## Машиночитаемый СОП

Схема `contracts/sop.schema.json`, файлы в `configs/sop/`. Регламент — это
данные, а не код (п. 1.3 вопросника Заказчика).

```yaml
steps:
  - id: hatch
    title: Открыть крышку люка-лаза
    timeout_sec: 90
    expect:
      - variable: hatch_cover
        equals: open
    requires: [grounding]
    phrases:
      instruction: Откройте крышку люка-лаза.
      success: Крышка люка-лаза открыта.
      violation: Стоп. Вы открываете люк, не подключив заземление…
```

Ключевое поле — **`requires`**. Это не «рекомендуемый порядок», а предусловие:
нарушение `requires` у шага, чьё предусловие помечено `critical`, останавливает
сценарий. Ссылки между шагами и допустимость значений проверяются при загрузке —
ошибка в регламенте вылезет при старте, а не в середине тренировки.

`timeout_sec` задаётся на каждый шаг отдельно: опускание трубы лебёдкой — минуты,
открытие люка — секунды. Общий тайм-аут давал бы ложные `TIMEOUT`.

## Потоки шины

| Поток | Сообщение | Публикует |
|---|---|---|
| `cv.observations` | `Observation` | cv_adapter |
| `cv.health` | `StreamHealth` | cv_adapter |
| `state.changes` | `StateChange` | event_processor |
| `engine.verdicts` | `Verdict` | scenario_engine |
| `engine.session` | `SessionSnapshot` | scenario_engine |
| `engine.commands` | `SessionCommand` | instructor_ui |
| `voice.speak` | `SpeakRequest` | scenario_engine |
| `voice.spoken` | `SpokenEvent` | voice_assistant |
| `voice.commands` | `VoiceCommand` | voice_assistant, cv_adapter |

Один поток — один тип сообщения. Смешивать типы нельзя: потребители разбирают
payload строго по объявленной модели.
