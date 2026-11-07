# Один образ на все шесть сервисов: они делят библиотеки контрактов и шины,
# а различаются только точкой входа. Держать шесть почти одинаковых образов
# значило бы шесть раз собирать одно и то же и рисковать их расхождением.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Слой зависимостей отдельно от кода: правка сервиса не пересобирает pip.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
        "pydantic>=2.9" "pyyaml>=6.0" "fastapi>=0.115" "uvicorn[standard]>=0.32" \
        "sqlalchemy[asyncio]>=2.0" "alembic>=1.14" "aiosqlite>=0.20" "redis>=5.2" "httpx>=0.28" \
        "python-docx>=1.1"

COPY libs ./libs
COPY services ./services
COPY tools ./tools
COPY configs ./configs
COPY migrations ./migrations
COPY assets ./assets
COPY alembic.ini run_local.py ./

RUN mkdir -p /app/data

# Сервис выбирается командой в docker-compose.yml. Стенд одним процессом
# (run_local.py) — в docker-compose.stand.yml: точка входа там переопределена.
ENTRYPOINT ["python", "-m", "services.main"]
