"""Точка входа отдельного сервиса — так они запускаются в docker-compose.

    python -m services.main scenario_engine

Каждый сервис поднимается своим процессом и общается с остальными только через
шину. Те, у кого есть HTTP-интерфейс, дополнительно слушают свой порт.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager

import uvicorn
from fastapi import APIRouter, FastAPI

from libs.bus import Bus, bus_from_env
from libs.config import load_config, load_sop

log = logging.getLogger("kft01")

SERVICES = (
    "cv_adapter",
    "cv_ingress",
    "event_processor",
    "scenario_engine",
    "voice_assistant",
    "storage_api",
    "instructor_ui",
)


def _build(name: str, bus: Bus, sop, config) -> tuple[object, APIRouter | None]:
    """Собрать сервис и его HTTP-роутер, если он есть."""
    match name:
        case "cv_adapter":
            from services.cv_adapter.service import CvAdapterService, create_router

            service = CvAdapterService(bus, sop, config)
            return service, create_router(service)

        case "cv_ingress":
            from services.cv_ingress.service import CvIngressService
            from services.cv_ingress.service import create_router as ingress_router

            service = CvIngressService(bus, sop, config)
            return service, ingress_router(service)

        case "event_processor":
            from services.event_processor.service import EventProcessorService

            return EventProcessorService(bus, config), None

        case "scenario_engine":
            from services.scenario_engine.service import ScenarioEngineService

            return ScenarioEngineService(bus, sop, config), None

        case "voice_assistant":
            from services.voice_assistant.service import VoiceAssistantService, create_router

            service = VoiceAssistantService(bus, sop, config)
            return service, create_router(service)

        case "storage_api":
            from services.storage_api.service import StorageService, create_router

            service = StorageService(bus, sop, config)
            return service, create_router(service)

        case "instructor_ui":
            from services.instructor_ui.app import (
                InstructorUiService,
                attach_proxy,
                create_router,
            )

            service = InstructorUiService(bus, sop, config)
            router = create_router(service)
            service._proxies = {  # type: ignore[attr-defined]
                "/emulator": os.getenv("CV_ADAPTER_URL", ""),
                "/voice": os.getenv("VOICE_URL", ""),
                "/api": os.getenv("STORAGE_URL", ""),
            }
            service._attach_proxy = attach_proxy  # type: ignore[attr-defined]
            return service, router

    raise ValueError(f"неизвестный сервис: {name}")


def build_app(name: str) -> FastAPI:
    config = load_config()
    sop = load_sop(config.sop_path)
    bus = bus_from_env()
    service, router = _build(name, bus, sop, config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await bus.start()
        await service.start()  # type: ignore[attr-defined]
        log.info("сервис %s запущен", name)
        try:
            yield
        finally:
            await service.stop()  # type: ignore[attr-defined]
            await bus.close()

    app = FastAPI(title=f"КФТ-01 — {name}", lifespan=lifespan)
    if router is not None:
        app.include_router(router)

    # Интерфейс инструктора в раздельной сборке проксирует чужие префиксы,
    # чтобы клиентский код не знал, собран стенд в один процесс или в шесть.
    proxies = getattr(service, "_proxies", {})
    for prefix, target in proxies.items():
        if target:
            service._attach_proxy(app, prefix, target)  # type: ignore[attr-defined]
            log.info("проксирование %s -> %s", prefix, target)

    return app


async def _run_headless(name: str) -> None:
    """Сервис без HTTP: живёт до сигнала остановки."""
    config = load_config()
    sop = load_sop(config.sop_path)
    bus = bus_from_env()
    service, _ = _build(name, bus, sop, config)

    await bus.start()
    await service.start()  # type: ignore[attr-defined]
    log.info("сервис %s запущен", name)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await service.stop()  # type: ignore[attr-defined]
        await bus.close()


HTTP_SERVICES = {"cv_adapter", "cv_ingress", "voice_assistant", "storage_api", "instructor_ui"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Запуск отдельного сервиса КФТ-01")
    parser.add_argument("service", choices=SERVICES)
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"))
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.service in HTTP_SERVICES:
        uvicorn.run(
            build_app(args.service), host=args.host, port=args.port, log_level=args.log_level
        )
    else:
        asyncio.run(_run_headless(args.service))


if __name__ == "__main__":
    main()
