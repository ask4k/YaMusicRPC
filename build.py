#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборка Yandex Music RPC в .exe без консольного окна.

Использование:
    python build.py            # собрать dist/YandexMusicRPC/ (папка)
    python build.py --onefile  # один файл (медленнее стартует)
    python build.py --clean    # удалить артефакты сборки

Требуется: pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
APP_NAME = "YandexMusicRPC"
ICON_FILE = BASE_DIR / "app.ico"
MODULE_NAME = "yandex_music_rpc"
ENTRY_POINT = BASE_DIR / f"{MODULE_NAME}.py"


def ensure_icon() -> Path:
    if ICON_FILE.exists():
        return ICON_FILE
    try:
        module = __import__(MODULE_NAME)
        module.save_icon_file(ICON_FILE)
    except Exception as exc:
        print(f"! Не удалось создать иконку: {exc}")
        return ICON_FILE
    print(f"+ Иконка создана: {ICON_FILE.name}")
    return ICON_FILE


def load_credentials() -> tuple[str, str]:
    """Читает настройки из той же программы, чтобы не дублировать логику."""
    module = __import__(MODULE_NAME)
    return module.load_credentials()


def write_config(target_dir: Path) -> None:
    """Кладёт config.json рядом с .exe, иначе собранная программа не найдёт CLIENT_ID."""
    try:
        client_id, imgbb_key = load_credentials()
    except Exception as exc:
        print(f"! Не удалось прочитать настройки: {exc}")
        return

    if not client_id:
        print(
            "! CLIENT_ID не найден — config.json не создан. "
            "Создай его рядом с .exe вручную по образцу config.example.json."
        )
        return

    target = target_dir / "config.json"
    payload = {"client_id": client_id, "imgbb_key": imgbb_key}
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"+ Настройки записаны: {target}")


def build(onefile: bool) -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller не установлен. Выполни: pip install pyinstaller")
        return 1

    icon = ensure_icon()
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",  # .exe без консоли
        "--name",
        APP_NAME,
        "--distpath",
        str(BASE_DIR / "dist"),
        "--workpath",
        str(BASE_DIR / "build"),
        "--specpath",
        str(BASE_DIR),
    ]

    if icon.exists():
        args += ["--icon", str(icon)]

    args.append("--onefile" if onefile else "--onedir")

    # winsdk и pypresence подтягиваются динамически — указываем явно.
    for module in ("winsdk", "pypresence", "pystray", "PIL", "requests"):
        args += ["--hidden-import", module]

    args.append(str(ENTRY_POINT))

    print("Сборка:", " ".join(args))
    result = subprocess.run(args, cwd=BASE_DIR)
    if result.returncode != 0:
        print("! Сборка не удалась")
        return result.returncode

    target = BASE_DIR / "dist" / (APP_NAME + (".exe" if onefile else ""))
    # Для --onedir config.json нужен внутри папки с exe, для --onefile — рядом с ним.
    config_dir = target if target.is_dir() else target.parent
    write_config(config_dir)
    print(f"\n+ Готово: {target}")
    print("  Скопируй папку целиком (или один .exe) и запусти — появится иконка в трее.")
    print("  Права администратора не нужны.")
    return 0


def cleanup() -> None:
    for path in (BASE_DIR / "build", BASE_DIR / "dist"):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    spec = BASE_DIR / f"{APP_NAME}.spec"
    if spec.exists():
        spec.unlink()


def main() -> int:
    # Без этого в cmd.exe/терминале кириллица превращается в кракозябры.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Сборка Yandex Music RPC")
    parser.add_argument("--onefile", action="store_true", help="один файл вместо папки")
    parser.add_argument("--clean", action="store_true", help="удалить артефакты и выйти")
    args = parser.parse_args()

    if args.clean:
        cleanup()
        print("Артефакты сборки удалены")
        return 0

    return build(args.onefile)


if __name__ == "__main__":
    raise SystemExit(main())
