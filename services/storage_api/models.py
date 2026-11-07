"""Схема локальной базы данных.

SQLAlchemy 2 плюс Alembic взяты не ради красоты: п. 6.4 вопросника Заказчика
прямо спрашивает про переход на PostgreSQL. Здесь нет ни одного SQLite-специфичного
типа, поэтому смена диалекта — это смена строки подключения и прогон миграций.

Хранится всё, что нужно и для отчёта по п. 5.4 ТЗ, и для последующего
дообучения моделей: вердикты со ссылкой на породившее их состояние, состояния
со ссылкой на источник, реплики с замером сквозной задержки.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    """Сеанс тренировки — строка будущего отчёта."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sop_id: Mapped[str] = mapped_column(String(64), index=True)
    sop_title: Mapped[str] = mapped_column(String(255))
    scenario_code: Mapped[str] = mapped_column(String(32), default="")
    mode: Mapped[str] = mapped_column(String(16), index=True)
    state: Mapped[str] = mapped_column(String(16), index=True)
    trainee_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)

    errors_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_step_ids: Mapped[list] = mapped_column(JSON, default=list)
    steps_total: Mapped[int] = mapped_column(Integer, default=0)
    steps_done: Mapped[int] = mapped_column(Integer, default=0)

    verdicts: Mapped[list[VerdictRow]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_sessions_started", "started_at"),)


class VerdictRow(Base):
    """Решение движка. Основной материал приёмки и разбора тренировки."""

    __tablename__ = "verdicts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )

    step_id: Mapped[str] = mapped_column(String(64), index=True)
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    reason_code: Mapped[str] = mapped_column(String(32), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)

    source_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    session: Mapped[SessionRow] = relationship(back_populates="verdicts")


class StateChangeRow(Base):
    """Подтверждённая смена состояния оборудования."""

    __tablename__ = "state_changes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    variable: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quality: Mapped[str] = mapped_column(String(16), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    agreement: Mapped[float] = mapped_column(Float, default=0.0)
    observations: Mapped[int] = mapped_column(Integer, default=0)

    first_seen_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    source: Mapped[str] = mapped_column(String(64), default="")
    camera_id: Mapped[str] = mapped_column(String(64), default="")
    post_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class UtteranceRow(Base):
    """Произнесённая реплика вместе с замером сквозной задержки."""

    __tablename__ = "utterances"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    engine: Mapped[str] = mapped_column(String(32))
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    synth_ms: Mapped[float] = mapped_column(Float, default=0.0)
    end_to_end_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ObservationRow(Base):
    """Сырое наблюдение CV.

    Пишется только при storage.store_observations = true. По умолчанию выключено:
    пятнадцать сообщений в секунду на переменную растят базу быстрее, чем это
    оправдано. Включается на время сбора датасета для CV-команды.
    """

    __tablename__ = "observations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    source: Mapped[str] = mapped_column(String(64))
    camera_id: Mapped[str] = mapped_column(String(64))
    post_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    variable: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    unclear: Mapped[bool] = mapped_column(Boolean, default=False)
    frame_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
