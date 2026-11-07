"""Источник наблюдений. Сейчас — эмулятор, потом реальный модуль CV."""

from .detector import Detector, DetectorMessage, RtspDetector
from .emulator import EmulatorDetector, EmulatorState
from .runs import EmulatorRun, ExpectedVerdict, NoiseProfile, load_run, load_runs
from .service import CvAdapterService, create_router

__all__ = [
    "CvAdapterService",
    "Detector",
    "DetectorMessage",
    "EmulatorDetector",
    "EmulatorRun",
    "EmulatorState",
    "ExpectedVerdict",
    "NoiseProfile",
    "RtspDetector",
    "create_router",
    "load_run",
    "load_runs",
]
