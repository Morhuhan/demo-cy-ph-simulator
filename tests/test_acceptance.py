"""Приёмочные прогоны.

Исполняемая форма критерия из п. 5 вопросника Заказчика: приёмка не по
демонстрации на экране, а по тому, что заданный поток событий даёт заданную
последовательность вердиктов. Ожидаемые последовательности лежат рядом со
сценариями — в configs/runs/*.yaml, секция `expected`.
"""

from __future__ import annotations

import pytest

from libs.contracts import Mode, VerdictKind
from tools.replay import replay

#: Несколько разных источников случайности: прогон, проходящий на одном
#: удачном шуме, ничего не доказывает.
SEEDS = (1, 7, 13, 42)


@pytest.mark.parametrize("seed", SEEDS)
def test_expected_verdicts(runs, sop, config, seed):
    for run in runs.values():
        result = replay(run, sop, config, seed=seed)
        assert result.passed, (
            f"прогон '{run.id}' (seed={seed}) разошёлся с ожидаемым:\n"
            + "\n".join(result.diff)
        )


@pytest.mark.parametrize("seed", SEEDS)
def test_latency_within_budget(runs, sop, config, seed):
    """Задержка от действия до вердикта укладывается в бюджет п. 6.2 ТЗ."""
    budget = config.latency_budget
    scenario_budget = budget.cv_ms + budget.smoothing_ms + budget.engine_ms

    for run in runs.values():
        result = replay(run, sop, config, seed=seed)
        assert result.max_latency_ms <= scenario_budget, (
            f"прогон '{run.id}': задержка {result.max_latency_ms:.0f} мс "
            f"превысила {scenario_budget} мс"
        )


def test_clean_run_has_no_errors(runs, sop, config):
    """На правильном прогоне система не должна выдумывать ошибок."""
    result = replay(runs["clean"], sop, config)
    assert result.errors == 0
    assert all(verdict.kind is VerdictKind.MATCH for verdict in result.verdicts)


def test_jitter_run_matches_clean_run(runs, sop, config):
    """Дребезг распознавания не должен менять оценку действий оператора.

    Оператор в обоих прогонах делает одно и то же и одинаково правильно.
    Разница только в качестве картинки — и она не должна доходить до вердиктов.
    """
    clean = replay(runs["clean"], sop, config)
    jitter = replay(runs["jitter-pipe"], sop, config)

    significant = lambda result: [  # noqa: E731
        (v.step_id, v.kind) for v in result.verdicts if v.kind is not VerdictKind.UNCLEAR
    ]
    assert significant(jitter) == significant(clean)
    assert jitter.errors == 0


def test_stream_loss_does_not_blame_operator(runs, sop, config):
    """Обрыв видеопотока не превращается в ошибку обучаемого."""
    result = replay(runs["stream-loss"], sop, config)
    assert result.errors == 0
    assert not [v for v in result.verdicts if v.kind is VerdictKind.MISMATCH]


def test_testing_mode_speaks_less(runs, sop, config):
    """Тестовый режим по п. 5.2 ТЗ — без подсказок и подтверждений."""
    training = replay(runs["clean"], sop, config, mode=Mode.TRAINING)
    testing = replay(runs["clean"], sop, config, mode=Mode.TESTING)

    assert len(testing.speech) < len(training.speech)
    # Оценка действий при этом одинаковая: режим влияет на голос, не на разбор.
    assert [v.kind for v in testing.verdicts] == [v.kind for v in training.verdicts]
