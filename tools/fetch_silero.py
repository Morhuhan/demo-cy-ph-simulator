"""Разовое скачивание модели Silero для серверного синтеза речи.

Вынесено из старта сервиса намеренно. Тренажёр по п. 6.3 ТЗ работает в
изолированном контуре без интернета, поэтому качать что-либо при подъёме стенда
нельзя: на площадке Заказчика это означало бы зависший запуск. Модель
выкачивается один раз осознанной командой — на машине разработчика или при
подготовке образа.

    python -m tools.fetch_silero
"""

from __future__ import annotations

import sys

from libs.config import load_config
from services.voice_assistant.tts import SileroTts


def main() -> int:
    config = load_config()

    try:
        import torch
    except ImportError:
        print('torch не установлен. Сначала: pip install -e ".[voice]"')
        return 1

    if SileroTts.model_cached(torch):
        print(f"модель уже скачана: {torch.hub.get_dir()}")
        return 0

    print(f"загрузка модели Silero '{config.voice.silero.model_id}' (около 38 МБ)…")
    engine = SileroTts(config.voice)
    engine._ensure_model()

    sample = engine.synthesize("Проверка синтеза речи. Заземление подключено.")
    print(
        f"готово: {len(sample.audio or b'')} байт, "
        f"{sample.duration_sec:.1f} с, синтез {sample.synth_ms:.0f} мс"
    )
    print("теперь voice.engine=auto будет использовать серверный синтез")
    return 0


if __name__ == "__main__":
    sys.exit(main())
