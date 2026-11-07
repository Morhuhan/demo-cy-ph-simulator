"""Смоук-тест поднятого стенда.

Проверяет живую систему целиком — через HTTP и шину, а не в обход них:
запускает сеанс, гоняет скриптованный прогон, дожидается вердиктов, сверяет
их с ожидаемыми из YAML прогона и проверяет задержку против норматива п. 6.2 ТЗ.

    python -m tools.smoke                       # прогон clean на localhost:8080
    python -m tools.smoke --run jitter-pipe --url http://localhost:8099
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from libs.config import load_config, load_sop
from services.cv_adapter.runs import load_runs


async def wait_for_stand(client: httpx.AsyncClient, timeout: float = 30.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            response = await client.get("/scenario")
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError("стенд не отвечает")


async def smoke(url: str, run_id: str, mode: str) -> int:
    config = load_config()
    sop = load_sop(config.sop_path)
    runs = load_runs(config.emulator.runs_dir, scenario=sop.id)
    run = runs.get(run_id)
    if run is None:
        print(f"прогон '{run_id}' не найден; доступны: {', '.join(runs)}")
        return 1

    # trust_env=False: стенд локальный, и обращения к нему не должны уходить
    # во внешний прокси, если он задан в окружении. На площадке Заказчика в
    # изолированном контуре это обязательное условие, а не удобство.
    async with httpx.AsyncClient(base_url=url, timeout=20.0, trust_env=False) as client:
        scenario = await wait_for_stand(client)
        print(f"стенд: {scenario['scenario_code']} — {scenario['title']}")

        voice = (await client.get("/voice/state")).json()
        print(f"голос: синтез {voice['tts_engine']}, распознавание {voice['stt_engine']}")

        await client.post("/emulator/reset")
        await client.post("/session/reset")
        await client.post(
            "/session/start", json={"mode": mode, "trainee_name": f"Смоук-тест «{run.title}»"}
        )
        await client.post(f"/emulator/runs/{run_id}/start")
        print(f"прогон '{run_id}' запущен, {run.duration_sec:.0f} с реального времени…")

        # Ждём столько же, сколько длится прогон, плюс запас на подтверждение
        # последнего состояния и на очередь реплик.
        await asyncio.sleep(run.duration_sec + 4)

        sessions = (await client.get("/api/sessions?limit=1")).json()
        if not sessions:
            print("сеанс не сохранён в базе")
            return 1
        session = sessions[0]
        summary = (await client.get(f"/api/sessions/{session['id']}/summary")).json()
        verdicts = (await client.get(f"/api/sessions/{session['id']}/verdicts")).json()

    print(
        f"\nсеанс {session['id'][:8]}  режим {session['mode']}  "
        f"шагов {session['steps_done']}/{session['steps_total']}  "
        f"ошибок {session['errors_count']}"
    )
    print("вердикты:")
    for verdict in verdicts:
        latency = f"{verdict['latency_ms']:.0f} мс" if verdict["latency_ms"] else "—"
        print(
            f"  {verdict['step_id']:12} {verdict['kind']:8} {verdict['severity']:8} "
            f"{latency:>8}  {verdict['message'][:70]}"
        )

    significant = [v for v in verdicts if v["kind"] != "UNCLEAR"]
    expected = [(item.step, item.kind.value) for item in run.expected]
    actual = [(v["step_id"], v["kind"]) for v in significant]

    latency = summary["latency"]
    budget = load_config().latency_budget.total_ms
    end_to_end = latency["end_to_end"]["max_ms"]

    print("\nзадержка:")
    print(f"  действие → вердикт, макс: {latency['verdict']['max_ms']:.0f} мс")
    print(f"  действие → голос,   макс: {end_to_end:.0f} мс (норматив {budget} мс)")

    problems = []
    if expected and actual != expected:
        problems.append(f"последовательность вердиктов: ожидалось {expected}, получено {actual}")
    if end_to_end > budget:
        problems.append(f"превышен норматив задержки: {end_to_end:.0f} > {budget} мс")
    if summary["verdicts"]["total"] == 0:
        problems.append("вердиктов нет вообще")

    print()
    if problems:
        for problem in problems:
            print(f"ПРОВАЛ: {problem}")
        return 1

    print("смоук-тест пройден")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Смоук-тест поднятого стенда КФТ-01")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--run", default="clean")
    parser.add_argument("--mode", default="training")
    args = parser.parse_args()
    return asyncio.run(smoke(args.url, args.run, args.mode))


if __name__ == "__main__":
    sys.exit(main())
