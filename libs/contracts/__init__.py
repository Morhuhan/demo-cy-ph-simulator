"""Единый источник правды по контрактам обмена.

Все сервисы импортируют модели отсюда. JSON Schema в каталоге `contracts/`
генерируется из этих моделей (`python -m tools.gen_schemas`) и физически не
может разойтись с кодом — именно эти файлы отдаются CV-команде.
"""

from .common import SCHEMA_VERSION, BBox, Envelope, new_id, utc_now
from .observation import Observation, StreamHealth, StreamState
from .session import (
    CommandName,
    Mode,
    SessionCommand,
    SessionSnapshot,
    SessionState,
    StepStatus,
    StepView,
)
from .sop import (
    Expectation,
    Invariant,
    Observable,
    ObservableValue,
    ScenarioPhrases,
    SopSpec,
    SopStep,
    StepPhrases,
)
from .speech import (
    Priority,
    SpeakRequest,
    SpokenEvent,
    UtteranceKind,
    VoiceCommand,
    VoiceIntent,
)
from .state import StateChange, StateQuality
from .verdict import ReasonCode, Severity, Verdict, VerdictKind

__all__ = [
    "SCHEMA_VERSION",
    "BBox",
    "CommandName",
    "Envelope",
    "Expectation",
    "Invariant",
    "Mode",
    "Observable",
    "ObservableValue",
    "Observation",
    "Priority",
    "ReasonCode",
    "ScenarioPhrases",
    "SessionCommand",
    "SessionSnapshot",
    "SessionState",
    "Severity",
    "SopSpec",
    "SopStep",
    "SpeakRequest",
    "SpokenEvent",
    "StateChange",
    "StateQuality",
    "StepPhrases",
    "StepStatus",
    "StepView",
    "StreamHealth",
    "StreamState",
    "UtteranceKind",
    "Verdict",
    "VerdictKind",
    "VoiceCommand",
    "VoiceIntent",
    "new_id",
    "utc_now",
]
