"""Архив для CV-команды: стенд и модуль распознавания, всё для запуска в Docker.

    python -m tools.pack_stand                    # ../stand-docker.zip
    python -m tools.pack_stand --out D:/stand.zip
    python -m tools.pack_stand --no-weights       # без весов модели, ~20 МБ

Готовых образов не везём: собираются они из этих же файлов, а архив
исходников в десятки раз меньше и не привязан к архитектуре машины.

Стенд — по составу .dockerignore плюс то, что нужно человеку: инструкция,
патч для CV-модуля, схемы контракта. Модуль распознавания — из комплекта
demo-kit/cv (CV-Module с патчем видео) в папку cv-module/, туда же его
Dockerfile и веса. Баз занятий, .env с токенами и роликов из корня проекта
в архиве нет.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT_CV = ROOT.parent / "demo-kit" / "cv"

INCLUDE = [
    # То, из чего собирается образ.
    "Dockerfile",
    ".dockerignore",
    "docker-compose.stand.yml",
    "docker-compose.full.yml",
    "docker/cv-config.yaml",
    "pyproject.toml",
    "alembic.ini",
    "run_local.py",
    "libs",
    "services",
    "tools",
    "configs",
    "migrations",
    "assets",
    # То, что читает человек.
    "README.md",
    "contracts",
    "docs/stand-docker.md",
    "docs/integration-cv.md",
    "docs/contracts.md",
    "docs/cv-preview.patch",
]

#: Код модуля распознавания: что берётся из комплекта.
CV_INCLUDE = ["app.py", "requirements.txt", "config.yaml", "README.md", "src", "tests"]
CV_WEIGHTS = "models/yolo11m_safety.pt"

SKIP_PARTS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def collect(base: Path, entries: list[str]) -> list[Path]:
    found: list[Path] = []
    for entry in entries:
        path = base / entry
        if not path.exists():
            raise SystemExit(f"нет {path} — архив был бы неполным")
        candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        found += [
            p for p in candidates
            if not SKIP_PARTS.intersection(p.parts) and p.suffix not in SKIP_SUFFIXES
        ]
    return found


def entries(weights: bool) -> list[tuple[Path, str]]:
    """Пары «файл на диске → путь в архиве»."""
    pairs = [(p, p.relative_to(ROOT).as_posix()) for p in collect(ROOT, INCLUDE)]
    cv = collect(KIT_CV, CV_INCLUDE + ([CV_WEIGHTS] if weights else []))
    pairs += [(p, "cv-module/" + p.relative_to(KIT_CV).as_posix()) for p in cv]
    pairs += [
        (ROOT / "docker/cv.Dockerfile", "cv-module/Dockerfile"),
        (ROOT / "docker/cv.dockerignore", "cv-module/.dockerignore"),
    ]
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description="Архив стенда для CV-команды")
    parser.add_argument("--out", type=Path, default=ROOT.parent / "stand-docker.zip")
    parser.add_argument("--no-weights", action="store_true", help="без весов модели")
    args = parser.parse_args()

    selected = entries(weights=not args.no_weights)
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, name in selected:
            archive.write(path, f"stand/{name}")
        # Папка для роликов: том в docker-compose.full.yml. Пустой её нет —
        # Docker создал бы её сам, но от имени root.
        archive.writestr("stand/videos/README.txt", "Ролики для CV_SOURCE=/videos/<файл>.\n")

    size_mb = args.out.stat().st_size / 1024 / 1024
    print(f"{args.out}: файлов {len(selected)}, {size_mb:.1f} МБ")


if __name__ == "__main__":
    main()
