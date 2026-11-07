"""Проигрывание записанного журнала CV-модуля.

Запись, снятая на реальной съёмке, подаётся в систему тем же путём, что и живое
подключение: тот же разбор контракта, тот же перевод, то же удержание
состояния. Разница только в источнике строк — файл вместо сокета.

Ради этого демонстрация идёт на настоящем выходе нейросети, но воспроизводимо:
без видеокарты на площадке, без риска, что модуль не запустится на показе, и с
одинаковым результатом от прогона к прогону.

Темп задаёт `frame_timestamp_sec` — положение кадра в ролике. Поэтому события
ложатся на видеозапись секунда в секунду, и её можно показывать рядом.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CvLog:
    """Записанный журнал одного прогона CV-модуля."""

    id: str
    path: Path
    lines: list[str]
    duration_sec: float
    events: int
    subjects: list[str]
    video: Path | None = None
    """Ролик с отрисовкой рядом с журналом. Их пишет один прогон CV-модуля,
    поэтому время в событиях совпадает с положением в ролике."""

    scenario: str | None = None
    """Сценарий, к которому относится запись. Ею определяется источник
    наблюдений: у сценария с записями стенд работает от них, а не от эмулятора."""

    title: str = ""
    description: str | None = None

    pause_at_sec: float | None = None
    """Где остановить съёмку и дождаться реакции системы.

    Нужно для показа: сотрудник стоит без каски, запись замирает, система
    выдерживает отведённое на шаг время и объявляет нарушение — и только
    потом съёмка продолжается. Пауза не подделывает вердикт: система всё это
    время получает настоящие наблюдения «каска не надета» и решает сама.
    """




def load_logs(directory: str | Path) -> dict[str, CvLog]:
    """Прочитать все записи из каталога.

    Разбирается только то, что нужно для показа и темпа: сами строки уходят в
    приёмник нетронутыми, чтобы запись проходила ровно тот же контроль
    контракта, что и живое сообщение.

    Названия и привязка к сценарию берутся из index.yaml рядом: описание
    демонстрационного материала — это данные, а не код. Он же определяет
    состав показа: рядом лежат и другие съёмки, но в списке только
    перечисленные, иначе на стенд попадало бы всё, что когда-либо положили
    в каталог.
    """
    logs: dict[str, CvLog] = {}
    path = Path(directory)
    if not path.exists():
        return logs

    manifest = path / "index.yaml"
    meta: dict[str, dict] = {}
    default_scenario: str | None = None
    if manifest.exists():
        raw = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        default_scenario = raw.get("scenario")
        for item in raw.get("recordings") or []:
            if item.get("id"):
                meta[item["id"]] = item

    for file in sorted(path.glob("*.jsonl")):
        if meta and file.stem not in meta:
            continue
        lines = [line for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            continue

        stamps: list[float] = []
        subjects: list[str] = []
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            stamp = payload.get("frame_timestamp_sec")
            if isinstance(stamp, int | float):
                stamps.append(float(stamp))
            subject = payload.get("subject_id")
            if subject and subject not in subjects:
                subjects.append(subject)

        video = file.with_suffix(".mp4")
        entry = meta.get(file.stem, {})
        logs[file.stem] = CvLog(
            id=file.stem,
            path=file,
            video=video if video.exists() else None,
            scenario=entry.get("scenario", default_scenario),
            title=entry.get("title") or file.stem,
            description=entry.get("description"),
            pause_at_sec=entry.get("pause_at_sec"),
            lines=lines,
            duration_sec=max(stamps) if stamps else 0.0,
            events=len(lines),
            subjects=subjects,
        )
    return logs


class CvLogPlayer:
    """Подаёт строки записи в приёмник в темпе исходной съёмки."""

    def __init__(self, handle_line, on_hold_end=None) -> None:
        self._handle_line = handle_line
        #: Кому сообщить, сколько простояла съёмка: часы записи на это время
        #: остановились, и общий отсчёт надо сдвинуть.
        self._on_hold_end = on_hold_end
        self._started_at: float | None = None
        #: Запись кончилась, но сцена — нет. Строки в файле обрываются на
        #: последней смене состояния, а сотрудник как стоял в каске, так и
        #: стоит. Поэтому после последнего события приёмник продолжает
        #: удерживать состояние, пока показ не остановят: иначе занятие
        #: посреди хода упёрлось бы в «потерян видеопоток».
        self._finished = False
        self._task: asyncio.Task[None] | None = None
        self.current: CvLog | None = None
        #: Номер запуска. По нему интерфейс отличает повторный показ той же
        #: записи от продолжения текущего — иначе ролик остался бы в конце.
        self.run = 0
        self._resume = asyncio.Event()
        self._held_total = 0.0
        self._hold_started: float | None = None
        #: Предел ожидания реакции. Если система почему-то промолчала, съёмка
        #: продолжится сама: замерший навсегда кадр на показе хуже, чем
        #: пропущенная реплика.
        self._hold_limit_sec = 40.0

    @property
    def active(self) -> bool:
        """Жив ли показ — в том числе когда строки кончились, а сцена держится."""
        return self._task is not None and not self._task.done()

    @property
    def playing(self) -> bool:
        """Идёт ли сама съёмка. По ней подтягивается ролик и блокируются кнопки."""
        return self.active and not self._finished

    @property
    def position_sec(self) -> float:
        """Сколько секунд записи проиграно — по нему подтягивается ролик.

        Время ожидания реакции из отсчёта вычитается: пока съёмка стоит,
        ролик рядом тоже должен стоять.
        """
        if self._started_at is None or not self.playing:
            return 0.0
        now = asyncio.get_event_loop().time()
        held = self._held_total
        if self._hold_started is not None:
            held += now - self._hold_started
        return max(0.0, now - self._started_at - held)

    @property
    def holding(self) -> bool:
        return self._hold_started is not None

    def release(self) -> None:
        """Система отреагировала — съёмка продолжается."""
        self._resume.set()

    def start(self, log_entry: CvLog) -> None:
        self.stop()
        self.current = log_entry
        self.run += 1
        self._started_at = asyncio.get_event_loop().time()
        self._finished = False
        self._resume.clear()
        self._held_total = 0.0
        self._hold_started = None
        self._task = asyncio.create_task(self._play(log_entry), name="cv-log-replay")
        log.info("проигрывание записи '%s': %d событий", log_entry.id, log_entry.events)

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _play(self, log_entry: CvLog) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        pause_at = log_entry.pause_at_sec
        try:
            for line in log_entry.lines:
                stamp = self._stamp_of(line)
                # Замираем в самой точке паузы, а не на следующем событии:
                # иначе кадр застыл бы уже в момент, который надо показать
                # после реакции системы.
                if pause_at is not None and (stamp is None or stamp > pause_at):
                    await self._wait_until(pause_at, started, loop)
                    await self._hold(loop)
                    pause_at = None
                await self._wait_for(line, started, loop)
                await self._handle_line(line)

            # Строки кончились. Удержание в приёмнике продолжает публиковать
            # последнее состояние, поэтому сценарий доводится до конца так же,
            # как на живом потоке: шаг с докладом ждёт оператора, а не упирается
            # в тишину источника.
            self._finished = True
            log.info("запись '%s' проиграна, состояние удерживается", log_entry.id)
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            log.info("проигрывание записи '%s' прервано", log_entry.id)
            raise

    async def _hold(self, loop) -> None:
        """Замереть до реакции системы."""
        self._hold_started = loop.time()
        log.info("съёмка приостановлена — ждём реакции системы")
        try:
            await asyncio.wait_for(self._resume.wait(), timeout=self._hold_limit_sec)
            log.info("система отреагировала — съёмка продолжается")
        except TimeoutError:
            log.warning("реакции не было %.0f с — продолжаю съёмку", self._hold_limit_sec)
        finally:
            held = loop.time() - self._hold_started
            self._held_total += held
            self._hold_started = None
            if self._on_hold_end is not None:
                self._on_hold_end(held)

    @staticmethod
    def _stamp_of(line: str) -> float | None:
        try:
            stamp = json.loads(line).get("frame_timestamp_sec")
        except json.JSONDecodeError:
            return None
        return float(stamp) if isinstance(stamp, int | float) else None

    async def _wait_until(self, position: float, started: float, loop) -> None:
        delay = started + position + self._held_total - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _wait_for(self, line: str, started: float, loop) -> None:
        stamp = self._stamp_of(line)
        if stamp is None:
            return
        # Время ожидания реакции не считается временем съёмки.
        delay = started + stamp + self._held_total - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
