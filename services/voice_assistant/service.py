"""Голосовой ассистент: очередь реплик, кэш с прогревом, замер сквозной задержки.

Три вещи, без которых голос на тренажёре не работает:

* **Очередь с прерыванием.** Реплики не должны накладываться, но критическое
  предупреждение обязано перебить рассказ о предыдущем шаге, а не ждать своей
  очереди. Приоритет INTERRUPT чистит очередь и обрывает текущую реплику.
* **Кэш с прогревом.** Все постоянные фразы СОП синтезируются при старте.
  Иначе первая же реплика оплачивала бы загрузку модели из бюджета п. 6.2 ТЗ.
* **Замер сквозной задержки.** От метки кадра, вызвавшего вердикт, до начала
  звучания. Это и есть величина, которую проверяют на приёмке.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from libs.bus import SPEAK_REQUESTS, SPOKEN_EVENTS, VOICE_COMMANDS, Bus
from libs.config import AppConfig
from libs.contracts import (
    Priority,
    SopSpec,
    SpeakRequest,
    SpokenEvent,
    UtteranceKind,
    VoiceCommand,
    utc_now,
)

from .speakable import speakable
from .stt import create_stt, match_intent
from .tts import BrowserTts, Synthesis, TtsEngine, create_tts

log = logging.getLogger(__name__)

SERVICE_NAME = "voice_assistant"

#: Как маршрут находит службу своего зрителя — см. libs/stand.py.
Resolve = Callable[[Request], Awaitable["VoiceAssistantService"]]
CONSUMER_GROUP = "voice-assistant"

#: Сколько последних реплик держать доступными по HTTP для проигрывания.
AUDIO_STORE_SIZE = 32

#: Предел одной фразы с микрофона. Страница режет запись на шести секундах,
#: в WAV 16 кГц это меньше 200 КБ; запас на случай другой частоты.
MAX_PHRASE_BYTES = 1_000_000

#: Реакция на действие оператора. Такая реплика не имеет права стоять в очереди:
#: норматив п. 6.2 ТЗ отсчитывается от завершения действия до начала звучания.
_REACTIVE = frozenset(
    {UtteranceKind.CONFIRMATION, UtteranceKind.WARNING, UtteranceKind.CRITICAL}
)

#: Реплики, которые можно оборвать на полуслове. Инструкция, которую оператор
#: уже выполнил, не просто бесполезна — она мешает услышать оценку сделанного.
_INTERRUPTIBLE = frozenset({UtteranceKind.INSTRUCTION, UtteranceKind.HINT})


def _normalise(text: str) -> str:
    return " ".join(text.split()).lower()


class VoiceAssistantService:
    def __init__(
        self,
        bus: Bus,
        sop: SopSpec,
        config: AppConfig,
        *,
        tts: TtsEngine | None = None,
        stt=None,
        cache: dict[str, Synthesis] | None = None,
    ) -> None:
        self._bus = bus
        self._sop = sop
        self._config = config

        # Движки можно передать снаружи: занятий на стенде несколько, а модель
        # синтеза весит сотни мегабайт и состояния занятия не хранит —
        # поднимать её копию на каждого зрителя незачем.
        self.tts: TtsEngine = tts or create_tts(config.voice)
        self.stt = stt or create_stt(config.voice.stt)

        # Кэш реплик может быть общим на стенд: звук зависит только от текста.
        self._cache: dict[str, Synthesis] = {} if cache is None else cache
        self._audio: OrderedDict[str, bytes] = OrderedDict()
        self._queue: deque[SpeakRequest] = deque()
        self._arrived = asyncio.Event()
        self._interrupt = asyncio.Event()
        self._current: SpeakRequest | None = None
        self._interrupted_id: str | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self.last_spoken: SpokenEvent | None = None

    # ------------------------------------------------------------------

    async def start(self) -> None:
        log.info("голосовой ассистент: синтез '%s', распознавание '%s'", self.tts.name, self.stt.name)
        self._tasks = [
            asyncio.create_task(self._consume(), name="voice-consume"),
            asyncio.create_task(self._worker(), name="voice-worker"),
        ]
        if self._config.voice.warmup:
            # Прогрев идёт фоном: подъём стенда не должен ждать синтеза трёх
            # десятков фраз. Реплики, прозвучавшие до конца прогрева,
            # синтезируются по требованию и тоже попадают в кэш.
            self._tasks.append(asyncio.create_task(self._warmup_async(), name="voice-warmup"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def switch_scenario(self, sop: SopSpec) -> None:
        """Сменить сценарий: у нового свои постоянные фразы.

        Прогрев уходит в фон — переключение не должно ждать синтеза.
        """
        self._sop = sop
        if self._config.voice.warmup:
            self._tasks.append(
                asyncio.create_task(self._warmup_async(), name="voice-warmup")
            )

    async def _warmup_async(self) -> None:
        try:
            await asyncio.to_thread(self._warmup)
        except Exception:  # pragma: no cover
            log.exception("прогрев кэша реплик не удался — фразы будут синтезироваться на лету")

    def _warmup(self) -> None:
        """Синтезировать все постоянные фразы сценария заранее."""
        phrases: list[str] = [
            self._sop.phrases.welcome,
            self._sop.phrases.finished,
            self._sop.phrases.aborted,
            self._sop.phrases.unclear,
            self._sop.phrases.stream_lost,
            self._sop.phrases.help_fallback,
        ]
        for step in self._sop.steps:
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
        phrases.extend(invariant.message for invariant in self._sop.invariants)

        for phrase in phrases:
            text = " ".join(phrase.split())
            try:
                self._cache[_normalise(text)] = self.tts.synthesize(text)
            except Exception:  # pragma: no cover - прогрев не должен ронять старт
                log.exception("не удалось прогреть фразу: %.40s…", text)

        log.info("прогрев кэша реплик: %d фраз", len(self._cache))

    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        async for request in self._bus.subscribe(
            SPEAK_REQUESTS, SpeakRequest, group=CONSUMER_GROUP
        ):
            self.enqueue(request)

    def enqueue(self, request: SpeakRequest) -> None:
        if request.priority is Priority.INTERRUPT:
            # Критическое предупреждение не может ждать: всё, что стояло в
            # очереди, относилось к обстановке до нарушения и уже неактуально.
            dropped = len(self._queue)
            self._queue.clear()
            self._queue.append(request)
            # Запоминаем, что именно оборвали: это уходит в SpokenEvent и
            # позволяет на разборе увидеть, какую реплику оператор не дослушал.
            self._interrupted_id = self._current.event_id if self._current else None
            self._interrupt.set()
            if dropped or self._interrupted_id:
                log.info(
                    "прерывание: снято реплик из очереди — %d, оборвана текущая: %s",
                    dropped,
                    bool(self._interrupted_id),
                )
        else:
            if len(self._queue) >= self._config.voice.queue_max:
                # Копить нельзя: реплика, произнесённая с опозданием на полминуты,
                # уже относится не к тому шагу.
                stale = self._queue.popleft()
                log.warning("очередь переполнена, снята реплика: %.40s…", stale.text)

            if request.kind in _REACTIVE:
                # Оценка действия идёт вперёд очереди и обрывает инструкцию,
                # если та ещё звучит. Иначе подтверждение шага дожидалось бы
                # конца инструкции к этому же шагу и приезжало на секунды позже
                # норматива — при формально исправной системе.
                self._queue.appendleft(request)
                if self._current is not None and self._current.kind in _INTERRUPTIBLE:
                    self._interrupted_id = self._current.event_id
                    self._interrupt.set()
            else:
                self._queue.append(request)
        self._arrived.set()

    async def _worker(self) -> None:
        while True:
            if not self._queue:
                self._arrived.clear()
                await self._arrived.wait()
                continue

            request = self._queue.popleft()
            self._interrupt.clear()
            self._current = request

            try:
                result = await self._speak(request)
            except Exception:  # pragma: no cover
                log.exception("сбой синтеза реплики")
                self._current = None
                continue

            # Держим паузу на время звучания, чтобы реплики не накладывались.
            # Прерывание будит нас досрочно.
            try:
                await asyncio.wait_for(self._interrupt.wait(), timeout=result.duration_sec)
            except TimeoutError:
                pass
            self._current = None

    async def _speak(self, request: SpeakRequest) -> _SpokenResult:
        key = _normalise(request.text)
        cached = self._cache.get(key)
        if cached is not None:
            synthesis, cache_hit = cached, True
        else:
            synthesis = await self._synthesize(request.text)
            self._cache[key] = synthesis
            cache_hit = False

        if synthesis.audio is not None:
            self._audio[request.event_id] = synthesis.audio
            while len(self._audio) > AUDIO_STORE_SIZE:
                self._audio.popitem(last=False)

        event = SpokenEvent(
            session_id=request.session_id,
            request_id=request.event_id,
            text=request.text,
            kind=request.kind,
            engine=synthesis.engine,
            cache_hit=cache_hit,
            synth_ms=synthesis.synth_ms,
            duration_sec=synthesis.duration_sec,
            end_to_end_ms=self._end_to_end_ms(request),
            interrupted=self._interrupted_id,
            verdict_id=request.verdict_id,
        )
        self._interrupted_id = None
        self.last_spoken = event
        await self._bus.publish(SPOKEN_EVENTS, event)
        return _SpokenResult(event=event, duration_sec=synthesis.duration_sec)

    async def _synthesize(self, text: str) -> Synthesis:
        """Синтез с деградацией.

        Отказ модели — испорченный кэш, нехватка памяти, пропавший файл весов —
        не должен обрывать голосовой канал целиком: без голоса тренажёр
        перестаёт быть тренажёром. Переходим на браузерный синтез и продолжаем
        работать, записав причину в журнал.
        """
        try:
            return await asyncio.to_thread(self.tts.synthesize, text)
        except Exception:
            if isinstance(self.tts, BrowserTts):
                raise
            log.exception(
                "синтез через '%s' отказал — голос переключён на браузер инструктора",
                self.tts.name,
            )
            self.tts = BrowserTts()
            # Дальше заводим свой кэш: реплики браузерного синтеза приходят
            # без звука, и в общем кэше они заглушили бы остальные занятия,
            # у которых движок исправен.
            self._cache = {}
            return await asyncio.to_thread(self.tts.synthesize, text)

    def _end_to_end_ms(self, request: SpeakRequest) -> float | None:
        """От метки кадра, породившего вердикт, до начала реплики (п. 6.2 ТЗ)."""
        if not request.source_frame_ts:
            return None
        try:
            frame_ts = datetime.fromisoformat(request.source_frame_ts)
        except ValueError:  # pragma: no cover
            return None
        return max(0.0, (utc_now() - frame_ts).total_seconds() * 1000)

    # ------------------------------------------------------------------

    async def submit_transcript(self, transcript: str, session_id: str | None) -> VoiceCommand:
        """Реплика оператора, распознанная браузером или пультом."""
        intent, confidence = match_intent(transcript)
        command = VoiceCommand(
            session_id=session_id,
            intent=intent,
            transcript=transcript,
            confidence=confidence,
            engine=self.stt.name,
        )
        await self._bus.publish(VOICE_COMMANDS, command)
        return command

    async def render(self, text: str) -> Synthesis | None:
        """Звук фразы для страницы, которая говорит сама (выставочный экран).

        Мимо очереди реплик: у экрана своя логика, занятие стенда он не ведёт.
        Кэш общий с репликами сценария — звук зависит только от текста.
        Без серверного синтеза — None, страница скажет голосом браузера.
        """
        text = speakable(text)
        key = _normalise(text)
        cached = self._cache.get(key)
        if cached is not None and cached.audio:
            return cached
        if isinstance(self.tts, BrowserTts):
            return None
        async with _RENDER_LOCK:
            synthesis = await self._synthesize(text)
        if not synthesis.audio:
            return None
        self._cache[key] = synthesis
        return synthesis

    def audio_of(self, request_id: str) -> bytes | None:
        return self._audio.get(request_id)

    @property
    def queue_depth(self) -> int:
        return len(self._queue)


class _SpokenResult:
    __slots__ = ("event", "duration_sec")

    def __init__(self, event: SpokenEvent, duration_sec: float) -> None:
        self.event = event
        self.duration_sec = duration_sec


# ----------------------------------------------------------------------


#: Фоновые задачи маршрутов: держим ссылку, пока не закончатся.
_BACKGROUND: set[asyncio.Task] = set()

#: Модель синтеза одна на стенд; фразы экрана синтезируются по очереди.
_RENDER_LOCK = asyncio.Lock()


class SayRequest(BaseModel):
    text: str = Field(min_length=1, max_length=900)


class TranscriptRequest(BaseModel):
    transcript: str
    session_id: str | None = None


def create_router(resolve: Resolve) -> APIRouter:
    """Маршруты голоса.

    Занятий на стенде несколько, и очередь реплик у каждого своя: `resolve`
    находит ту службу, что принадлежит зрителю этого запроса.
    """
    router = APIRouter(prefix="/voice", tags=["voice"])

    @router.get("/state")
    async def state(request: Request) -> dict:
        service = await resolve(request)
        # Страница спрашивает состояние периодически — держим соединение
        # распознавания тёплым, чтобы первая фраза не ждала рукопожатия.
        warm = getattr(service.stt, "warm", None)
        if warm is not None:
            task = asyncio.create_task(asyncio.to_thread(warm))
            _BACKGROUND.add(task)
            task.add_done_callback(_BACKGROUND.discard)
        return {
            "tts_engine": service.tts.name,
            "stt_engine": service.stt.name,
            "queue_depth": service.queue_depth,
            "cached_phrases": len(service._cache),
            "browser_speech": service.tts.name == "browser",
            "server_stt": service.stt.accepts_audio,
        }

    @router.post("/say")
    async def say(body: SayRequest, request: Request) -> Response:
        """Фраза — в WAV голосом стенда. 503 — серверного синтеза нет."""
        service = await resolve(request)
        synthesis = await service.render(body.text)
        if synthesis is None:
            raise HTTPException(status_code=503, detail="серверный синтез речи не включён")
        return Response(synthesis.audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @router.get("/audio/{request_id}.wav")
    async def audio(request_id: str, request: Request) -> Response:
        service = await resolve(request)
        data = service.audio_of(request_id)
        if data is None:
            raise HTTPException(status_code=404, detail="аудио не найдено или устарело")
        return Response(content=data, media_type="audio/wav")

    @router.post("/listen")
    async def listen(
        request: Request, session_id: str | None = None, dry_run: bool = False
    ) -> dict:
        """Фраза с микрофона обучаемого: звук, затем текст, затем команда.

        Речь от тишины отделяет страница, и она же молчит, пока говорит стенд.
        Сюда приходит одна фраза в WAV.

        `dry_run` — проверка микрофона: фраза распознаётся и разбирается, но
        в сценарий не уходит. Можно говорить что угодно, занятию это не мешает.
        """
        service = await resolve(request)
        if not service.stt.accepts_audio:
            raise HTTPException(status_code=503, detail="распознавание речи на стенде не настроено")
        audio = await request.body()
        if not audio:
            raise HTTPException(status_code=400, detail="пустая запись")
        if len(audio) > MAX_PHRASE_BYTES:
            raise HTTPException(status_code=413, detail="запись длиннее любой команды")
        try:
            transcription = await asyncio.to_thread(service.stt.transcribe, audio)
        except Exception as exc:  # сеть, токен, лимиты — всё это причина, а не авария
            log.warning("распознавание не удалось: %s", exc)
            raise HTTPException(status_code=502, detail=f"распознавание не удалось: {exc}") from exc
        if not transcription.text:
            return {"intent": "unknown", "confidence": 0.0, "transcript": ""}
        if dry_run:
            intent, confidence = match_intent(transcription.text)
            return {"intent": intent, "confidence": confidence, "transcript": transcription.text}
        command = await service.submit_transcript(transcription.text, session_id)
        return {
            "intent": command.intent,
            "confidence": command.confidence,
            "transcript": command.transcript,
        }

    @router.post("/transcript")
    async def transcript(body: TranscriptRequest, request: Request) -> dict:
        service = await resolve(request)
        command = await service.submit_transcript(body.transcript, body.session_id)
        return {
            "intent": command.intent,
            "confidence": command.confidence,
            "transcript": command.transcript,
        }

    return router
