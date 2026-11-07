"""Заранее синтезировать постоянные реплики всех сценариев.

    python -m tools.bake_voice

Постоянные фразы сценария известны из СОП, поэтому их можно озвучить один раз
Silero и положить рядом с проектом. Тогда на площадке без видеокарты голос
остаётся тем же самым: не зависит ни от наличия torch, ни от того, какие
голоса установлены в системе у зрителя.

Запускать после правки текстов в СОП — иначе изменённая фраза уйдёт в запасной
движок и прозвучит другим голосом.
"""

from __future__ import annotations

import argparse
import sys

from libs.config import load_config, load_sops, project_root
from libs.contracts import SopSpec
from services.voice_assistant.tts import PrebakedTts, SileroTts


def phrases_of(sop: SopSpec) -> list[str]:
    """Все постоянные реплики сценария — тот же набор, что греет ассистент."""
    phrases: list[str] = [
        sop.phrases.welcome,
        sop.phrases.finished,
        sop.phrases.aborted,
        sop.phrases.unclear,
        sop.phrases.stream_lost,
        sop.phrases.help_fallback,
    ]
    for step in sop.steps:
        phrases.extend(
            phrase
            for phrase in (
                step.phrases.instruction,
                step.phrases.success,
                step.phrases.violation,
                step.phrases.hint,
                step.phrases.timeout,
            )
            if phrase
        )
    phrases.extend(invariant.message for invariant in sop.invariants)
    return phrases


def main() -> int:
    parser = argparse.ArgumentParser(description="Синтез постоянных реплик в файлы")
    parser.add_argument("--config", default=None, help="путь к конфигурации")
    args = parser.parse_args()

    config = load_config(args.config)
    engine = SileroTts(config.voice)
    if not engine.available():
        print(
            "Silero недоступен. Установите зависимости и модель:\n"
            '  pip install -e ".[voice]"\n'
            "  python -m tools.fetch_silero",
            file=sys.stderr,
        )
        return 1

    target = project_root() / config.voice.prebaked_dir
    target.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    written = skipped = 0
    for sop in load_sops().values():
        print(f"{sop.scenario_code} — {sop.title}")
        for text in phrases_of(sop):
            clean = " ".join(text.split())
            if clean in seen:
                continue
            seen.add(clean)

            file = target / f"{PrebakedTts.key_for(clean)}.wav"
            if file.exists():
                skipped += 1
                continue

            synthesis = engine.synthesize(clean)
            if synthesis.audio is None:
                print(f"  пропущено (нет звука): {clean[:60]}")
                continue
            file.write_bytes(synthesis.audio)
            written += 1
            print(f"  {clean[:68]}")

    size = sum(f.stat().st_size for f in target.glob("*.wav")) / 1048576
    print(f"\nготово: записано {written}, уже было {skipped}, всего {size:.1f} МБ в {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
