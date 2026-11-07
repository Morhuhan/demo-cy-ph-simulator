"""Локальный запуск всего стенда в одном процессе.

    python run_local.py

Нужен там, где нет Docker: все шесть сервисов поднимаются как asyncio-задачи в
одном процессе, шина — в памяти, HTTP — на одном порту. Код сервисов при этом
ровно тот же, что в поставке: разница только в точке сборки и в реализации
шины. Именно ради этого шина и спрятана за интерфейсом.

Стенд рассчитан на нескольких зрителей сразу: у каждого своё занятие со своим
сценарием, журналом и голосом. Кто к какому занятию относится, определяет ключ
в cookie, а сами занятия держит реестр — см. libs/stand.py.

Поставочный вариант — `docker compose up`, там каждый сервис отдельный
контейнер и Redis Streams.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from libs.stand import SharedResources, Stand, StandLimitReached, StandRegistry
from services.cv_adapter.service import create_router as cv_router
from services.cv_ingress.service import create_router as cv_ingress_router
from services.instructor_ui.app import create_router as ui_router
from services.storage_api.service import create_router as storage_router
from services.voice_assistant.service import create_router as voice_router

log = logging.getLogger("kft01")

#: Ключ занятия у зрителя. Одна вкладка или пять — занятие одно; у другого
#: человека cookie другая, и занятие у него тоже своё.
STAND_COOKIE = "kft_stand"
COOKIE_MAX_AGE = 12 * 3600

#: Ключ занятия в адресе. Так занятие указывает тот, у кого cookie нет вовсе:
#: CV-модуль подключается сокетом на ws://<хост>:<порт>/cv?stand=<ключ>.
STAND_QUERY = "stand"

#: Общий ключ на весь стенд, из окружения. Задан — и браузер, и CV-модуль
#: попадают в одно занятие, а порядок запуска перестаёт иметь значение. На
#: показе это избавляет от переписывания ключа из интерфейса в чужой конфиг.
STAND_KEY_ENV = "KFT_STAND_KEY"

#: Ключ попадает в журнал и в имена занятий, поэтому годятся только латиница,
#: цифры, дефис и подчёркивание.
_STAND_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Код закрытия сокета, когда занятие не указано. Свой код из прикладного
#: диапазона: 1008 сказал бы «нарушение политики» и не отличался бы от
#: прочих отказов.
WS_NO_STAND_KEY = 4401


class NoStandKey(RuntimeError):
    """Запрос пришёл без ключа занятия — страницу ещё не открывали."""


def _valid_key(key: str | None) -> str | None:
    """Ключ занятия, если он годится в качестве ключа."""
    return key if key and _STAND_KEY.match(key) else None


def _fixed_key() -> str | None:
    """Общий ключ стенда из окружения.

    Опечатка в нём тихо развела бы браузер и CV-модуль по разным занятиям,
    и на показе это выглядело бы как молчащий модуль распознавания. Поэтому
    негодный ключ — отказ при старте, а не предупреждение в журнале.
    """
    raw = os.getenv(STAND_KEY_ENV, "").strip()
    if not raw:
        return None
    key = _valid_key(raw)
    if key is None:
        raise ValueError(
            f"{STAND_KEY_ENV}={raw!r}: в ключе занятия допустимы латиница, "
            "цифры, дефис и подчёркивание"
        )
    return key


def build_app(shared: SharedResources | None = None) -> FastAPI:
    # Общие ресурсы можно передать снаружи — так проверка поднимает стенд
    # на временной базе и без прогрева речи.
    shared = shared or SharedResources.load()
    fixed_key = _fixed_key()
    registry = StandRegistry(shared)
    app_state: dict[str, object] = {"registry": registry}
    # Журнал занятий один на стенд: записи в нём и так разделены
    # идентификатором сеанса, а читаются одинаково для всех.
    repository = shared.repository

    async def stand_of(scope: Request | WebSocket) -> Stand:
        """Занятие этого зрителя.

        Ключ ищется в трёх местах. Общий ключ стенда, если он задан, перебивает
        всё: на показе занятие одно на всех. Дальше адрес — им занятие указывает
        тот, у кого cookie нет вовсе, то есть CV-модуль. И наконец cookie,
        которую выдаёт страница: так занятие находит браузер.

        Запрос без ключа занятие не поднимает — иначе любой случайный обход
        стенда заводил бы по занятию на запрос и вытеснял живых зрителей.
        """
        key = (
            fixed_key
            or _valid_key(scope.query_params.get(STAND_QUERY))
            or _valid_key(scope.cookies.get(STAND_COOKIE))
        )
        if not key:
            raise NoStandKey()
        return await registry.get(key)

    async def ui_of(scope):
        return (await stand_of(scope)).ui

    async def cv_of(scope):
        return (await stand_of(scope)).cv

    async def ingress_of(scope):
        return (await stand_of(scope)).ingress

    async def voice_of(scope):
        return (await stand_of(scope)).voice

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await repository.create_schema()
        await registry.start()
        log.info(
            "стенд поднят: сюжет по умолчанию %s, синтез '%s', занятий одновременно до %d",
            shared.default_sop.scenario_code,
            shared.tts.name,
            registry.limit,
        )
        try:
            yield
        finally:
            await registry.stop()

    app = FastAPI(title="КФТ-01 — киберфизический тренажёр", lifespan=lifespan)
    app.state.stands = app_state["registry"]

    @app.middleware("http")
    async def assign_stand(request: Request, call_next):
        """Выдать зрителю ключ занятия, если его ещё нет."""
        key = request.cookies.get(STAND_COOKIE)
        try:
            response = await call_next(request)
        except StandLimitReached as exc:
            log.warning("отказано в новом занятии: %s", exc)
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "Стенд сейчас занят: идёт предельное число занятий. "
                        "Попробуйте через несколько минут."
                    )
                },
            )
        if not key:
            response.set_cookie(
                STAND_COOKIE,
                fixed_key or uuid.uuid4().hex,
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                samesite="lax",
            )
        return response

    @app.exception_handler(NoStandKey)
    async def no_stand_key(scope: Request | WebSocket, _exc: NoStandKey):
        """Занятие не указано.

        У браузера это значит, что страницу ещё не открывали или открыли её до
        перезапуска стенда: ключ выдаёт страница, поэтому достаточно
        перезагрузиться. У сокета — что подключился смежник без ключа: cookie
        у него нет, занятие он указывает в адресе. Закрываем сразу и пишем в
        журнал, чего не хватило: на площадке это первое, куда смотрят.
        """
        if isinstance(scope, WebSocket):
            log.warning(
                "подключение к %s без ключа занятия: нужен %s?%s=<ключ> "
                "либо общий ключ стенда в переменной %s",
                scope.url.path,
                scope.url.path,
                STAND_QUERY,
                STAND_KEY_ENV,
            )
            await scope.accept()
            await scope.close(
                code=WS_NO_STAND_KEY,
                reason=f"занятие не указано: ?{STAND_QUERY}=<ключ>",
            )
            return None
        return JSONResponse(
            status_code=409,
            content={"detail": "Занятие не найдено, обновите страницу."},
        )

    app.include_router(ui_router(ui_of))
    app.include_router(cv_router(cv_of))
    app.include_router(cv_ingress_router(ingress_of, path=shared.config.cv_ingress.path))
    app.include_router(voice_router(voice_of))
    app.include_router(storage_router(repository, shared.config))
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальный запуск стенда КФТ-01")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"))
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"\n  Интерфейс инструктора:  http://{args.host}:{args.port}/\n")
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        asyncio.set_event_loop_policy(None)
