"""Загрузка конфигурации и машиночитаемого СОП.

Всё, что можно вынести из кода, вынесено в configs/. Правило простое: если
величину захочется подкрутить на приёмке или на полигоне — её место в YAML.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from libs.contracts import SopSpec


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else project_root() / path


class SmoothingProfile(BaseModel):
    """Параметры сглаживания одной переменной.

    Смысл величин — в docs/architecture.md, раздел про гашение дребезга.
    """

    model_config = ConfigDict(extra="forbid")

    confirm_window_sec: float = Field(
        default=0.9,
        gt=0,
        description=(
            "Скользящее окно, по которому принимается решение. Прямо входит в бюджет "
            "задержки п. 6.2 ТЗ: состояние не может быть подтверждено быстрее окна."
        ),
    )
    min_confidence: float = Field(
        default=0.55, ge=0, le=1, description="Средняя уверенность по наблюдениям-победителям"
    )
    min_agreement: float = Field(
        default=0.7,
        ge=0,
        le=1,
        description=(
            "Доля наблюдений окна, согласных со значением. Это и есть гашение "
            "дребезга: пока модель мечется между двумя состояниями, ни одно не "
            "набирает нужной доли, и система молчит."
        ),
    )
    min_hold_sec: float = Field(
        default=0.4,
        ge=0,
        description=(
            "Минимальное время удержания подтверждённого состояния перед сменой. "
            "Нижний порог против пинг-понга; на нормальном ходу сценария не влияет "
            "на задержку, потому что шаги разделены секундами."
        ),
    )
    stale_after_sec: float = Field(
        default=4.0,
        gt=0,
        description="Через сколько без наблюдений считать переменную устаревшей",
    )
    unclear_after_sec: float = Field(
        default=2.5,
        gt=0,
        description=(
            "Сколько неопределённость должна продержаться, прежде чем о ней сообщать. "
            "Обычный переход между состояниями занимает секунду-полторы и сам по себе "
            "неопределённость — сообщать о нём нельзя. А вот труба, зависшая на границе "
            "на восемь секунд, — это уже реальное «не разобрал»."
        ),
    )
    unclear_report_sec: float = Field(
        default=3.0,
        gt=0,
        description="Повторять сообщение о неопределённости не чаще этого",
    )
    min_observations: int = Field(
        default=5, ge=1, description="Минимум наблюдений в окне — защита от редкого потока"
    )


class SmoothingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: SmoothingProfile = Field(default_factory=SmoothingProfile)
    variables: dict[str, SmoothingProfile] = Field(default_factory=dict)

    def profile_for(self, variable: str) -> SmoothingProfile:
        return self.variables.get(variable, self.defaults)


class EngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_mode: str = "training"
    unclear_cooldown_sec: float = Field(
        default=6.0,
        gt=0,
        description="Не чаще этого выдавать вердикт UNCLEAR, чтобы не забивать журнал",
    )
    low_confidence_grace_sec: float = Field(
        default=1.5,
        gt=0,
        description=(
            "Пауза перед зачётом шага, чья уверенность ниже порога min_confidence. "
            "В момент подтверждения окно ещё содержит кадры самого перехода, и "
            "уверенность занижена; за эту паузу она обычно дорастает. Порог шага "
            "поэтому откладывает зачёт, а не отменяет его: блокировать обучаемого "
            "из-за собственной неуверенности система не должна."
        ),
    )
    stream_lost_after_sec: float = Field(
        default=4.0, gt=0, description="Молчание источника дольше этого — считаем поток потерянным"
    )
    announce_unclear_in_testing: bool = Field(
        default=False, description="В тестовом режиме о неразобранном молчим"
    )


class SileroConfig(BaseModel):
    # protected_namespaces снят: поле называется model_id по терминологии Silero,
    # а не по терминологии pydantic.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_id: str = "v4_ru"
    speaker: str = "eugene"
    sample_rate: int = 24000


class SttConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    engine: str = Field(default="auto", description="auto | faster-whisper | hf | off")
    model_size: str = "small"
    language: str = "ru"

    # Распознавание на серверах Hugging Face (engine: hf). Звук фразы уходит
    # наружу; в конфигурации только имя переменной окружения с токеном.
    hf_model: str = Field(
        default="openai/whisper-large-v3-turbo",
        description=(
            "Модель распознавания. Замер 26.09 на фразах стенда: около 3,3 с на "
            "фразу, почти всё — сеть; полная large-v3 медленнее на десятые доли."
        ),
    )
    hf_url: str = Field(
        default="https://router.huggingface.co/hf-inference/models",
        description="Маршрутизатор HF, провайдер hf-inference — запасной путь.",
    )
    # Основной путь — fal-ai/whisper (large-v3) синхронным запросом: так же
    # распознаёт call-analyzer. Замер 21.10 на фразах посетителя с тихого
    # микрофона: 0,8–0,9 с, подсказка терминов чинит «до земления» →
    # «заземление». 3,7–11 с из замера 26.09 давала очередь fal, а не он сам.
    hf_provider: str = Field(default="fal", description="fal | hf-inference")
    fal_url: str = "https://router.huggingface.co/fal-ai/fal-ai/whisper"
    prompt: str = Field(
        default="",
        description=(
            "Подсказка движку — перечень терминов через запятую, без связных фраз: "
            "законченное предложение движок норовит «дочитать» и обрывает текст."
        ),
    )
    token_env: str = Field(default="HF_TOKEN", description="Переменная окружения с токеном HF")
    timeout_sec: float = Field(default=15.0, gt=0)


class VoiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: str = Field(
        default="auto",
        description=(
            "auto | silero | browser. auto — silero, если установлен torch, "
            "иначе синтез в браузере инструктора."
        ),
    )
    silero: SileroConfig = Field(default_factory=SileroConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    cache_dir: str = "data/tts-cache"
    prebaked_dir: str = Field(
        default="assets/voice",
        description=(
            "Заранее синтезированные постоянные реплики. Кладутся рядом с "
            "проектом и озвучивают площадку без видеокарты тем же голосом, "
            "что и стенд разработчика: python -m tools.bake_voice"
        ),
    )
    warmup: bool = Field(
        default=True, description="Синтезировать постоянные фразы СОП при старте"
    )
    queue_max: int = Field(default=8, ge=1, description="Глубина очереди реплик")


class RetentionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessions_days: int = 90
    observations_days: int = 7
    video_days: int = 30


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = "sqlite+aiosqlite:///data/kft01.sqlite3"
    store_observations: bool = Field(
        default=False,
        description=(
            "Писать ли сырой поток наблюдений. По умолчанию нет: 15 сообщений/с на "
            "переменную быстро растят базу. Включается на время сбора датасета."
        ),
    )
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


class PrivacyConfig(BaseModel):
    """Заготовки под 152-ФЗ (п. 2.3 вопросника Заказчика).

    Это флаги требований, а не их реализация. Размытие лиц технически исполняет
    CV-модуль — здесь фиксируется, что система его требует, чтобы на приёмке
    ответственность была явной.
    """

    model_config = ConfigDict(extra="forbid")

    consent_required: bool = True
    blur_faces_required: bool = True
    store_raw_video: bool = True
    video_ring_buffer_sec: int = Field(default=300, description="Глубина кольцевого буфера видео")


class LatencyBudget(BaseModel):
    """Разделение норматива 2 с из п. 6.2 ТЗ по звеньям тракта.

    Разделено заранее и явно, чтобы на приёмке не выяснять, из-за чьей части
    не уложились. Окно сглаживания вынесено отдельной строкой сознательно: это
    самая крупная статья расхода во всём тракте, и она принадлежит сценарной
    части, а не CV. Спорить об этом надо сейчас, а не на испытаниях.
    """

    model_config = ConfigDict(extra="forbid")

    cv_ms: int = Field(default=500, description="Захват кадра, инференс, публикация в шину")
    smoothing_ms: int = Field(
        default=1100, description="Окно подтверждения состояния — см. smoothing.confirm_window_sec"
    )
    engine_ms: int = Field(default=100, description="Сверка со СОП и формирование вердикта")
    voice_ms: int = Field(default=300, description="От вердикта до начала звучания реплики")
    total_ms: int = Field(default=2000, description="Норматив п. 6.2 ТЗ")


class EmulatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    runs_dir: str = "configs/runs"
    default_run: str | None = None
    rate_hz: float = Field(default=15.0, gt=0, description="Темп потока наблюдений")
    camera_id: str = "chest-01"
    post_id: str | None = Field(
        default=None,
        description=(
            "Номер поста налива. Свойство площадки КФТ-01; в сценариях, не "
            "привязанных к посту, отсутствует."
        ),
    )


class DebriefConfig(BaseModel):
    """Разбор занятия текстом.

    Формируется после занятия, на сохранённых вердиктах. Реального времени не
    требует и в норматив задержки не входит — поэтому это единственное место,
    куда языковую модель можно пустить, не трогая принятие решений.

    Граница жёсткая: модель пересказывает уже вынесенные вердикты и не выносит
    своих. Оценка занятия остаётся за движком, иначе один и тот же прогон давал
    бы разные результаты и приёмочная метрика потеряла бы смысл.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    engine: str = Field(
        default="auto",
        description=(
            "auto | llm | template. auto — языковая модель, если задан ключ, "
            "иначе связный текст по шаблону."
        ),
    )

    base_url: str = Field(default="", description="OpenAI-совместимый эндпоинт")
    model: str = Field(default="", description="Идентификатор модели у провайдера")
    api_key_env: str = Field(
        default="AI_API_KEY",
        description=(
            "Имя переменной окружения с ключом. Сам ключ в конфигурации не "
            "хранится: она лежит в репозитории, ключ — нет."
        ),
    )
    use_bearer: bool = Field(
        default=True, description="Ключ уходит как 'Bearer <ключ>' или как есть"
    )
    timeout_sec: float = Field(default=60.0, gt=0)
    max_tokens: int = Field(default=700, gt=0)

    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "").strip()

    def resolved_engine(self) -> str:
        """Чем формировать разбор с учётом того, что доступно на площадке."""
        if self.engine == "template":
            return "template"
        if not (self.base_url and self.model and self.api_key()):
            return "template"
        return "llm"


class MentorConfig(BaseModel):
    """ИИ-наставник выставочного экрана: ответы на вопросы посетителя.

    Посетитель ждёт ответа стоя, поэтому модель выбрана по скорости: на замере
    20.10.2026 gigachat-2-pro отвечала за 0,9–1,4 с и не путала этапы.
    Не ответила за timeout_sec — вопрос уходит запасной. Без ключа экран
    отвечает по тексту СОП сам.

    Граница та же, что у разбора: модель объясняет, но не оценивает. Верно ли
    выбран шаг, решает сценарий экрана, а не она.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    engine: str = Field(default="auto", description="auto | llm | template")
    base_url: str = Field(default="https://gptunnel.ru/v1", description="OpenAI-совместимый эндпоинт")
    api_key_env: str = Field(default="AI_MENTOR_KEY", description="Переменная окружения с ключом")
    use_bearer: bool = False
    model: str = "gigachat-2-pro"
    fallback_models: list[str] = Field(default_factory=lambda: ["gemini-2.5-flash"])
    timeout_sec: float = Field(default=5.0, gt=0, description="На одну модель")
    max_tokens: int = Field(default=220, gt=0)

    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "").strip()


class CvIngressConfig(BaseModel):
    """Приём событий реального CV-модуля.

    Контракт CV отличается от внутреннего — разбор расхождений в
    docs/integration-cv.md. Здесь только то, что нужно подкрутить на стыковке,
    не трогая код: соответствие имён объектов, темп переопубликации и трактовка
    bbox.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Выключен по умолчанию: демонстрационный стенд работает от эмулятора",
    )
    path: str = Field(default="/cv", description="Путь WebSocket-эндпоинта, куда стучится CV")

    republish_hz: float = Field(
        default=12.0,
        gt=0,
        description=(
            "Темп переопубликации удерживаемого состояния. CV присылает только "
            "подтверждённые изменения, а сглаживание ниже по течению ждёт "
            "непрерывного потока — ingress держит последнее известное значение "
            "и повторяет его с этой частотой. Подробнее в docs/integration-cv.md."
        ),
    )

    logs_dir: str = Field(
        default="configs/cv-logs",
        description=(
            "Каталог записанных журналов CV-модуля. Запись проигрывается тем же "
            "путём, что и живое подключение, — демонстрация идёт на реальном "
            "выходе нейросети, но воспроизводимо."
        ),
    )

    hold_timeout_sec: float = Field(
        default=5.0,
        gt=0,
        description=(
            "Сколько удерживать последнее состояние без единого события от CV. "
            "Работает только вместе с heartbeat_expected: без признака жизни "
            "молчание неотличимо от неподвижного кадра, и обрывать по нему нельзя."
        ),
    )

    heartbeat_expected: bool = Field(
        default=False,
        description=(
            "Обещает ли CV-модуль периодический признак жизни. Пока не обещает, "
            "молчание источника не считается потерей потока: события приходят "
            "только на смену состояния, и неподвижный кадр законно молчит сколько "
            "угодно. Включать вместе с heartbeat на стороне CV — тогда "
            "hold_timeout_sec снова сторожит зависший модуль, а не спокойную сцену."
        ),
    )

    objects: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Соответствие поля object из события CV переменной СОП. Пусто — "
            "имена совпадают и используются как есть."
        ),
        examples=[{"helmet": "helmet"}],
    )

    bbox_normalized: bool = Field(
        default=True,
        description=(
            "Считать ли координаты bbox нормализованными к [0..1]. Формат "
            "на стороне CV не зафиксирован — пункт на согласование."
        ),
    )
    bbox_format: str = Field(
        default="xywh",
        description="xywh — (x, y, ширина, высота); xyxy — два угла",
    )

    def variable_for(self, object_name: str) -> str:
        return self.objects.get(object_name, object_name)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sop_path: str = "configs/sop/kft-01.yaml"
    smoothing: SmoothingConfig = Field(default_factory=SmoothingConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    latency_budget: LatencyBudget = Field(default_factory=LatencyBudget)
    emulator: EmulatorConfig = Field(default_factory=EmulatorConfig)
    cv_ingress: CvIngressConfig = Field(default_factory=CvIngressConfig)
    debrief: DebriefConfig = Field(default_factory=DebriefConfig)
    mentor: MentorConfig = Field(default_factory=MentorConfig)

    def sop_file(self) -> Path:
        return _resolve(self.sop_path)


def load_dotenv(path: str | Path = ".env") -> None:
    """Подтянуть переменные из .env, не затирая уже заданные в окружении.

    В docker-сборке это делает compose. При локальном запуске иначе пришлось бы
    выставлять ключи руками в каждой оболочке.
    """
    file = _resolve(path)
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


def load_config(path: str | Path | None = None) -> AppConfig:
    load_dotenv()
    config_path = _resolve(path or os.getenv("KFT_CONFIG", "configs/app.yaml"))
    if not config_path.exists():
        return AppConfig()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)


def load_sop(path: str | Path) -> SopSpec:
    """Разбор и валидация машиночитаемого СОП.

    Ошибка здесь — это ошибка регламента, а не кода: битые ссылки между шагами
    или неизвестные значения переменных вылезут при старте, а не в середине
    тренировки.
    """
    sop_path = _resolve(path)
    data = yaml.safe_load(sop_path.read_text(encoding="utf-8"))
    return SopSpec.model_validate(data)


def load_sops(directory: str | Path = "configs/sop") -> dict[str, SopSpec]:
    """Все сценарии из каталога, ключ — идентификатор.

    Интерфейс инструктора позволяет переключать сценарий на ходу, поэтому
    разбираются все описания сразу: ошибка в любом из них должна вылезти при
    старте стенда, а не в момент переключения на глазах у зрителей.
    """
    sops: dict[str, SopSpec] = {}
    for file in sorted(_resolve(directory).glob("*.yaml")):
        sop = load_sop(file)
        sops[sop.id] = sop
    return sops


@lru_cache(maxsize=1)
def app_config() -> AppConfig:
    return load_config()


def data_dir() -> Path:
    directory = project_root() / "data"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


__all__ = [
    "AppConfig",
    "CvIngressConfig",
    "DebriefConfig",
    "EmulatorConfig",
    "EngineConfig",
    "LatencyBudget",
    "MentorConfig",
    "PrivacyConfig",
    "SmoothingConfig",
    "SmoothingProfile",
    "StorageConfig",
    "VoiceConfig",
    "app_config",
    "data_dir",
    "load_config",
    "load_dotenv",
    "load_sop",
    "load_sops",
    "project_root",
]
