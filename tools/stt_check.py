"""Проверка распознавания речи через Hugging Face на фразах самого стенда.

    python -m tools.stt_check

Гоняет заранее озвученные реплики через тот же движок, что работает на
занятии, и печатает, что распознано, какая команда из этого выделена и за
сколько. Токен берётся из переменной HF_TOKEN — как и у стенда, из .env.

Отдельно проверяется короткая команда. Whisper сам определяет язык, и на
фразе в одно-два слова он может ошибиться — а доклад о готовности как раз
такая фраза. Её кусок вырезается из конца подсказки «…например „к работе
готов“».
"""

from __future__ import annotations

import io
import sys
import time
import wave

from libs.config import load_config, project_root
from services.voice_assistant.stt import HfRouterStt, match_intent
from services.voice_assistant.tts import PrebakedTts

FULL = "Наденьте камеру и доложите о готовности к работе."
HINT = "Доложите голосом — например «к работе готов»."


def voiced(phrase: str) -> bytes | None:
    path = project_root() / "assets" / "voice" / f"{PrebakedTts.key_for(phrase)}.wav"
    return path.read_bytes() if path.exists() else None


def tail(audio: bytes, seconds: float) -> bytes:
    with wave.open(io.BytesIO(audio)) as source:
        params = source.getparams()
        frames = source.readframes(source.getnframes())
    keep = int(seconds * params.framerate) * params.sampwidth * params.nchannels
    out = io.BytesIO()
    with wave.open(out, "wb") as target:
        target.setparams(params)
        target.writeframes(frames[-keep:])
    return out.getvalue()


def main() -> int:
    config = load_config().voice.stt
    stt = HfRouterStt(config)
    if not stt.available():
        print(f"[ !! ] переменная {config.token_env} не задана — положить токен в .env стенда")
        return 1
    print(f"модель: {config.hf_model}")

    samples: list[tuple[str, bytes]] = []
    full = voiced(FULL)
    if full:
        samples.append(("фраза стенда целиком", full))
    hint = voiced(HINT)
    if hint:
        for seconds in (1.3, 1.0):
            samples.append((f"короткая команда, {seconds} с", tail(hint, seconds)))
    if not samples:
        print("[ !! ] нет заранее озвученных реплик в assets/voice")
        return 1

    failed = False
    for title, audio in samples:
        started = time.perf_counter()
        try:
            result = stt.transcribe(audio)
        except Exception as error:  # noqa: BLE001 — показать причину и идти дальше
            print(f"[ !! ] {title}: {error}")
            failed = True
            continue
        elapsed = time.perf_counter() - started
        intent, _ = match_intent(result.text)
        print(f"[ OK ] {title}: «{result.text}» → {intent.value}, {elapsed:.1f} с")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
