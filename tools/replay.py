"""Приёмочный прогон: эмулятор -> сглаживание -> движок, в ускоренном времени.

Это исполняемая форма критерия из п. 5 вопросника Заказчика: приёмка не по
демонстрации на экране, а по тому, что заданный поток событий даёт заданную
последовательность вердиктов. Прогоны детерминированы, поэтому результат не
зависит ни от того, кто стоит перед камерой, ни от скорости машины.

    python -m tools.replay              # все прогоны
    python -m tools.replay jitter-pipe  # один
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from libs.config import AppConfig, load_config, load_sop
from libs.contracts import Mode, Observation, SopSpec, StateChange, Verdict, VerdictKind
from services.cv_adapter.emulator import EmulatorDetector
from services.cv_adapter.runs import EmulatorRun, load_runs
from services.event_processor.smoothing import Smoother
from services.scenario_engine.engine import ScenarioEngine

EPOCH = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)


@dataclass
class ReplayResult:
    run_id: str
    verdicts: list[Verdict]
    changes: list[StateChange]
    speech: list[str]
    latencies_ms: list[float]
    errors: int
    passed: bool
    diff: list[str]

    @property
    def max_latency_ms(self) -> float:
        return max(self.latencies_ms, default=0.0)


def replay(
    run: EmulatorRun,
    sop: SopSpec,
    config: AppConfig,
    *,
    mode: Mode = Mode.TRAINING,
    seed: int = 7,
) -> ReplayResult:
    clock = {"now": EPOCH}
    detector = EmulatorDetector(sop, config.emulator, seed=seed, clock=lambda: clock["now"])
    smoother = Smoother(config.smoothing)
    engine = ScenarioEngine(sop, config.engine)

    verdicts: list[Verdict] = []
    changes: list[StateChange] = []
    speech: list[str] = []

    def collect(output) -> None:
        verdicts.extend(output.verdicts)
        speech.extend(request.text for request in output.speech)

    collect(engine.start(mode=mode, trainee_name="Проверочный прогон", now=clock["now"]))
    detector.start_run(run)

    period = 1.0 / config.emulator.rate_hz
    for _ in range(int(run.duration_sec / period)):
        clock["now"] = clock["now"] + timedelta(seconds=period)
        now = clock["now"]

        for message in detector.tick():
            if isinstance(message, Observation):
                change = smoother.observe(message)
                if change is not None:
                    changes.append(change)
                    collect(engine.on_state_change(change, now=now))

        for stale in smoother.check_stale(now):
            changes.append(stale)
            collect(engine.on_state_change(stale, now=now))

        for intent in detector.take_pending_voice():
            from libs.contracts import VoiceCommand

            collect(
                engine.on_voice(
                    VoiceCommand(intent=intent, transcript="", confidence=0.95, engine="emulator"),
                    now=now,
                )
            )

        collect(engine.tick(now))

    latencies = [v.latency_ms for v in verdicts if v.latency_ms is not None]
    passed, diff = compare(run, verdicts)
    return ReplayResult(
        run_id=run.id,
        verdicts=verdicts,
        changes=changes,
        speech=speech,
        latencies_ms=latencies,
        errors=engine.errors_count,
        passed=passed,
        diff=diff,
    )


def compare(run: EmulatorRun, verdicts: list[Verdict]) -> tuple[bool, list[str]]:
    """Сверка с ожидаемой последовательностью из YAML прогона.

    Сравниваются только вердикты MATCH/MISMATCH/TIMEOUT: UNCLEAR по своей
    природе зависит от случайного шума и в приёмочный критерий не входит.
    """
    if not run.expected:
        return True, []

    significant = [v for v in verdicts if v.kind is not VerdictKind.UNCLEAR]
    actual = [(v.step_id, v.kind.value, v.reason_code.value) for v in significant]
    expected = [
        (item.step, item.kind.value, item.reason.value if item.reason else None)
        for item in run.expected
    ]

    diff: list[str] = []
    for index in range(max(len(actual), len(expected))):
        got = actual[index] if index < len(actual) else None
        want = expected[index] if index < len(expected) else None
        if got is None:
            diff.append(f"  [{index}] ожидалось {want}, но вердикта нет")
            continue
        if want is None:
            diff.append(f"  [{index}] лишний вердикт {got}")
            continue
        if got[0] != want[0] or got[1] != want[1]:
            diff.append(f"  [{index}] ожидалось {want}, получено {got}")
            continue
        if want[2] is not None and got[2] != want[2]:
            diff.append(f"  [{index}] шаг {got[0]}: причина {got[2]}, ожидалась {want[2]}")

    return not diff, diff


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Приёмочный прогон сценариев")
    parser.add_argument("run", nargs="*", help="Идентификаторы прогонов; по умолчанию все")
    parser.add_argument("--verbose", "-v", action="store_true", help="Печатать все вердикты")
    parser.add_argument("--mode", default="training", choices=[m.value for m in Mode])
    args = parser.parse_args(argv)

    config = load_config()
    sop = load_sop(config.sop_path)
    runs = load_runs(config.emulator.runs_dir, scenario=sop.id)
    selected = args.run or list(runs)

    failures = 0
    print(f"Сценарий: {sop.scenario_code} — {sop.title}")
    print(f"Бюджет задержки: {config.latency_budget.total_ms} мс\n")

    for run_id in selected:
        run = runs.get(run_id)
        if run is None:
            print(f"!! прогон '{run_id}' не найден")
            failures += 1
            continue

        result = replay(run, sop, config, mode=Mode(args.mode))
        mark = "OK  " if result.passed else "ОШИБ"
        budget = config.latency_budget.total_ms
        within = "в бюджете" if result.max_latency_ms <= budget else "ПРЕВЫШЕН"

        print(
            f"[{mark}] {run.id:24} вердиктов {len(result.verdicts):2}  "
            f"ошибок {result.errors}  задержка макс {result.max_latency_ms:6.0f} мс ({within})"
        )

        if args.verbose or not result.passed:
            for verdict in result.verdicts:
                latency = f"{verdict.latency_ms:5.0f}мс" if verdict.latency_ms else "     —"
                print(
                    f"       {verdict.step_id:12} {verdict.kind.value:8} "
                    f"{verdict.severity.value:8} {latency}  {verdict.message}"
                )
        if not result.passed:
            failures += 1
            print("       расхождения с ожидаемым:")
            for line in result.diff:
                print(f"     {line}")
        print()

    print("Итог:", "все прогоны прошли" if not failures else f"провалено {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
