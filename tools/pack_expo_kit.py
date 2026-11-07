"""Сборка комплекта для выставки: стенд, cv, голос Silero, скрипты.

    python -m tools.pack_expo_kit

Берёт скрипты и cv из ../demo-kit, стенд из этого проекта, модель Silero из
кэша torch.hub. Кладёт папку ../expo-kit/demo и архив
../expo-kit.zip (распаковывается в C:\\ как C:\\demo).
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
KIT_SRC = PROJECT.parent / "demo-kit"
OUT = PROJECT.parent / "expo-kit"
ZIP = PROJECT.parent / "expo-kit.zip"
SCRIPTS = PROJECT / "tools" / "expo_kit"

STAND_PARTS = ["README.md", "alembic.ini", "pyproject.toml", "run_local.py",
               "assets", "configs", "contracts", "libs", "migrations", "services", "tools"]
SKIP = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".venv", "runs", "logs",
                              "config.laptop.yaml", "*.partial", ".git", "expo_kit", "pack_expo_kit.py")
ENV_KEYS = ("HF_TOKEN", "AI_MENTOR_KEY")


def silero_dir() -> Path:
    import torch

    path = Path(torch.hub.get_dir()) / "snakers4_silero-models_master"
    if not (path / "src" / "silero" / "model" / "v4_ru.pt").is_file():
        sys.exit(f"нет модели Silero в {path}: python -m tools.fetch_silero")
    return path


def env_lines() -> list[str]:
    found = {}
    for line in (PROJECT / ".env").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ENV_KEYS and value.strip():
            found[key.strip()] = value.strip()
    missing = [k for k in ENV_KEYS if k not in found]
    if missing:
        sys.exit(f"в .env нет {', '.join(missing)}")
    return [f"{k}={found[k]}" for k in ENV_KEYS]


def main() -> None:
    root = OUT / "demo"
    if OUT.exists():
        shutil.rmtree(OUT)
    root.mkdir(parents=True)

    for item in ("cv", "patches", "tools", "camtest.ps1", "check.ps1", "status.ps1", "stop.ps1"):
        src = KIT_SRC / item
        (shutil.copytree(src, root / item, ignore=SKIP) if src.is_dir() else shutil.copy2(src, root / item))
    for script in SCRIPTS.iterdir():
        shutil.copy2(script, root / script.name)

    stand = root / "stand"
    stand.mkdir()
    for part in STAND_PARTS:
        src = PROJECT / part
        (shutil.copytree(src, stand / part, ignore=SKIP) if src.is_dir() else shutil.copy2(src, stand / part))
    shutil.rmtree(stand / "configs" / "runs", ignore_errors=True)
    (stand / ".env").write_text("\n".join(env_lines()) + "\n", encoding="utf-8")
    shutil.copytree(silero_dir(), stand / "torch" / "hub" / "snakers4_silero-models_master", ignore=SKIP)

    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for file in sorted(root.rglob("*")):
            if file.is_file():
                z.write(file, file.relative_to(OUT).as_posix())
    files = sum(1 for f in root.rglob("*") if f.is_file())
    print(f"{ZIP}  {ZIP.stat().st_size / 2**20:.0f} МБ, файлов {files}")


if __name__ == "__main__":
    main()
