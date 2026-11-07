"""Занятие как отдельный экземпляр стенда и реестр таких занятий.

Стенд открывают по ссылке, и одновременно на него заходят несколько человек:
кто-то из заказчика смотрит проверку СИЗ, кто-то параллельно гоняет налив.
Занятие у каждого своё — со своим сценарием, журналом, голосом и записью.

Поэтому службы не одиночки: на каждого зрителя поднимается свой комплект со
своей шиной в памяти. События одного занятия физически не могут попасть в
чужое — не потому, что их отфильтровали, а потому, что шина другая.

Общим остаётся то, что дорого поднимать и нечего разделять: модель синтеза
речи (иначе на каждого зрителя грузилась бы своя) и база занятий — у записей
в ней и так есть идентификатор сеанса.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from libs.bus import Bus, InMemoryBus
from libs.config import AppConfig, load_config, load_sop, load_sops
from libs.contracts import SessionState, SopSpec
from services.cv_adapter.service import CvAdapterService
from services.cv_ingress.service import CvIngressService
from services.event_processor.service import EventProcessorService
from services.instructor_ui.app import InstructorUiService
from services.scenario_engine.service import ScenarioEngineService
from services.storage_api.repository import Repository
from services.storage_api.service import StorageService
from services.voice_assistant.service import VoiceAssistantService
from services.voice_assistant.stt import SttEngine, create_stt
from services.voice_assistant.tts import SerialTts, TtsEngine, create_tts

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SharedResources:
    """То, что одно на весь стенд, сколько бы занятий ни шло одновременно."""

    config: AppConfig
    sops: dict[str, SopSpec]
    default_sop: SopSpec
    tts: TtsEngine
    stt: SttEngine
    #: Синтезированные реплики. Текст один и тот же у всех занятий, звук по
    #: нему получается тот же самый — синтезировать его каждому заново значило
    #: бы тратить и время прогрева, и память на копии.
    phrases: dict = field(default_factory=dict)
    #: Журнал занятий. База одна на стенд, и пул соединений к ней тоже.
    repository: Repository | None = None

    @classmethod
    def load(cls, config: AppConfig | None = None) -> SharedResources:
        config = config or load_config()
        sop = load_sop(config.sop_path)
        sops = load_sops()
        sops.setdefault(sop.id, sop)
        # Синтез поднимается один раз на процесс: модель Silero весит сотни
        # мегабайт, и держать её копию на каждого зрителя нельзя. Сама она
        # состояния занятия не хранит — ей отдают текст, она возвращает звук.
        return cls(
            config=config,
            sops=sops,
            default_sop=sop,
            tts=SerialTts(create_tts(config.voice)),
            stt=create_stt(config.voice.stt),
            repository=Repository(config.storage, sop, sops),
        )


class Stand:
    """Полный комплект служб одного занятия."""

    def __init__(self, shared: SharedResources, *, stand_id: str) -> None:
        self.id = stand_id
        self.shared = shared
        self.sop = shared.default_sop
        config = shared.config

        # Шина своя у каждого занятия — это и есть граница между зрителями.
        self.bus: Bus = InMemoryBus()

        self.cv = CvAdapterService(self.bus, self.sop, config)
        self.ingress = CvIngressService(self.bus, self.sop, config)
        self.processor = EventProcessorService(self.bus, config)
        self.engine = ScenarioEngineService(self.bus, self.sop, config)
        self.voice = VoiceAssistantService(
            self.bus, self.sop, config, tts=shared.tts, stt=shared.stt, cache=shared.phrases
        )
        self.storage = StorageService(
            self.bus, self.sop, config, shared.sops, repository=shared.repository
        )
        self.ui = InstructorUiService(self.bus, self.sop, config)

        self.ui.scenarios = shared.sops
        self.ui.on_switch = self.switch_scenario
        self.engine.on_session_change = self._on_session_change

    # ------------------------------------------------------------------
    # Связи между службами, о которых знает только точка сборки
    # ------------------------------------------------------------------

    def _on_session_change(self, session_id, state) -> None:
        """Источник наблюдений идёт в ногу с занятием.

        Эмулятор помечает голосовые команды идентификатором текущего сеанса —
        в реальной системе это делает служба распознавания речи. Он же
        останавливается вместе с занятием: пауза замораживает и сюжет в кадре,
        остановка прекращает прогон, иначе инструктор нажал бы «Стоп», а
        «видео» продолжало бы идти.
        """
        self.cv.bind_session(session_id)
        self.cv.streaming = state is SessionState.RUNNING and self.cv.enabled_for_scenario
        if state is SessionState.PAUSED:
            self.cv.detector.pause()
            self.processor.pause()
        elif state is SessionState.RUNNING:
            self.processor.resume()
            self.cv.detector.resume()
        elif state in (SessionState.ABORTED, SessionState.FINISHED, SessionState.IDLE):
            # Сброс тоже прекращает сюжет: иначе прогон прошлого занятия
            # доигрывал бы в новый сеанс и приносил в него чужие вердикты.
            self.processor.resume()
            self.cv.detector.resume()
            self.cv.detector.stop_run()
            # Запись удерживает последнее состояние после конца съёмки, чтобы
            # занятие шло своим ходом. Но пережить само занятие это удержание
            # не должно: иначе следующий сеанс начинался бы с уже надетой
            # каской и засчитывал проверку, которой не было.
            self.ingress.player.stop()
            self.ingress.reset()

    def apply_source(self, sop: SopSpec) -> None:
        """Источник наблюдений — свойство сюжета, а не настройки запуска.

        У проверки СИЗ есть записи реальной съёмки, и стенд работает от них.
        У налива записей нет — работает эмулятор. Включить оба сразу нельзя:
        они публикуют по одной переменной и мешают друг другу.
        """
        self.ingress.switch_scenario(sop)
        self.cv.enabled_for_scenario = not self.ingress.has_recordings

    async def switch_scenario(self, sop_id: str) -> None:
        """Сменить сюжет. Затрагивает четыре службы, порядок важен."""
        target = self.shared.sops[sop_id]
        # Сначала выбирается источник, и только потом эмулятору сообщают
        # сценарий: иначе он поднимался бы или молчал по прошлому сюжету.
        self.apply_source(target)
        await self.cv.switch_scenario(target)
        # Буфер сглаживания держит переменные прошлого сценария. Если его не
        # сбросить, они перестанут наблюдаться, через stale_after_sec будут
        # объявлены устаревшими, и движок примет это за потерю видеопотока.
        self.processor.reset()
        await self.engine.switch_scenario(target)
        await self.voice.switch_scenario(target)
        self.ui.switch_scenario(target)
        self.sop = target
        log.info("занятие %s: сюжет %s", self.id, target.scenario_code)

    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self.bus.start()
        # Порядок важен: потребители должны подписаться раньше, чем эмулятор
        # начнёт публиковать, иначе первые наблюдения уйдут в никуда.
        await self.storage.start()
        await self.processor.start()
        await self.engine.start()
        await self.voice.start()
        await self.ui.start()
        await self.ingress.start()
        self.apply_source(self.sop)
        await self.cv.start()
        # Занятие ещё не началось — снимать нечего.
        self.cv.streaming = False
        log.info("занятие %s поднято: сюжет %s", self.id, self.sop.scenario_code)

    async def stop(self) -> None:
        for service in (
            self.cv,
            self.ingress,
            self.ui,
            self.voice,
            self.engine,
            self.processor,
            self.storage,
        ):
            await service.stop()
        await self.bus.close()
        log.info("занятие %s закрыто", self.id)

    @property
    def viewers(self) -> int:
        return self.ui.hub.viewers


@dataclass
class StandRegistry:
    """Кто на стенде сейчас занимается и чьё занятие кому показывать.

    Ключ занятия хранится у зрителя в cookie: у одного человека несколько
    вкладок — одно занятие на всех, у разных людей — разные.
    """

    shared: SharedResources
    #: Сколько занятий держать одновременно. Ограничение не от жадности:
    #: каждое занятие — это фоновые задачи и буферы, а стенд живёт на общей
    #: машине рядом с чужими сайтами.
    limit: int = 12
    #: Через сколько закрывать занятие, от которого ушли. Пятнадцати минут
    #: хватает отлучиться и вернуться, не потеряв журнал.
    idle_ttl_sec: float = 900.0

    _stands: dict[str, Stand] = field(default_factory=dict, init=False)
    _seen: dict[str, float] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _reaper: asyncio.Task[None] | None = field(default=None, init=False)

    async def get(self, key: str) -> Stand:
        """Занятие этого зрителя. Первое обращение поднимает новое."""
        self._seen[key] = time.monotonic()
        stand = self._stands.get(key)
        if stand is not None:
            return stand

        async with self._lock:
            stand = self._stands.get(key)
            if stand is not None:
                return stand
            if len(self._stands) >= self.limit:
                # Прежде чем отказывать, освобождаем то, что уже брошено.
                await self._collect(force=True)
            if len(self._stands) >= self.limit:
                raise StandLimitReached(self.limit)
            stand = Stand(self.shared, stand_id=key[:8])
            await stand.start()
            self._stands[key] = stand
            log.info("занятий на стенде: %d", len(self._stands))
            return stand

    def peek(self, key: str) -> Stand | None:
        return self._stands.get(key)

    @property
    def count(self) -> int:
        return len(self._stands)

    async def start(self) -> None:
        self._reaper = asyncio.create_task(self._reap_loop(), name="stands-reaper")

    async def stop(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        for key in list(self._stands):
            await self._close(key)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self._collect()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - уборка не должна ронять стенд
                log.exception("сбой при уборке занятий")

    #: Отсрочка, когда мест больше нет. Занятие, у которого только что
    #: открыли страницу, ещё не успело подключить сокет, и закрывать его
    #: нельзя — иначе места освобождались бы за счёт тех, кто только пришёл.
    GRACE_SEC = 90.0

    async def _collect(self, *, force: bool = False) -> None:
        """Закрыть занятия, от которых ушли.

        Открытая вкладка держит занятие сама: по ней идёт поток событий.
        Брошенным считаем то, где нет ни одного зрителя и давно не было
        обращений. Когда мест не осталось, срок ожидания укорачивается, но
        не до нуля.
        """
        now = time.monotonic()
        # Отсрочка укорачивает ожидание, но не удлиняет его: если занятия
        # и так закрываются быстро, теснота не повод ждать дольше.
        limit_sec = min(self.GRACE_SEC, self.idle_ttl_sec) if force else self.idle_ttl_sec
        for key, stand in list(self._stands.items()):
            if stand.viewers > 0:
                self._seen[key] = now
                continue
            if now - self._seen.get(key, now) >= limit_sec:
                await self._close(key)

    async def _close(self, key: str) -> None:
        stand = self._stands.pop(key, None)
        self._seen.pop(key, None)
        if stand is not None:
            await stand.stop()


class StandLimitReached(RuntimeError):
    """Стенд держит столько занятий, сколько может."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"на стенде уже {limit} занятий")
        self.limit = limit
