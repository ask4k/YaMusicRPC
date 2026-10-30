#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Yandex Music -> Discord Rich Presence 2.0.

Возможности:
  * иконка в системном трее: пауза, перезапуск, автозапуск, выход;
  * автозапуск с Windows (реестр HKCU, без прав администратора);
  * сборка в один .exe без консоли (build.py);
  * переживает перезапуск Discord (переподключение с backoff);
  * кэш обложек в SQLite — один трек загружается один раз;
  * если ImgBB недоступен (в РФ путь /1/upload часто блокируется),
    автоматически используется резервный хостинг uguu.se;
  * статус "Слушает" и кнопка "Открыть в Яндекс Музыке";
  * работает под pythonw: всё пишется в yandex_music_rpc.log;
  * пауза, радио-потоки и несколько медиа-сессий обрабатываются корректно;
  * защита от запуска двух копий одновременно.

Запуск:                  python yandex_music_rpc.py          (трей)
С консолью:              python yandex_music_rpc.py --console
Проверить окружение:     python yandex_music_rpc.py --check
Один апдейт и выход:     python yandex_music_rpc.py --once
Автозапуск без трея:     python yandex_music_rpc.py --install-autostart / --uninstall-autostart
Сборка .exe:             python build.py
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import ctypes
import json
import logging
import logging.handlers
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from pypresence import ActivityType, AioPresence
from pypresence.exceptions import ServerError
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

try:
    import winreg
except ImportError:  # не Windows
    winreg = None  # type: ignore[assignment]


def app_dir() -> Path:
    """Папка для лога и кэша: рядом с .exe при сборке, иначе рядом со скриптом."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = app_dir()
LOG_FILE = BASE_DIR / "yandex_music_rpc.log"
CACHE_FILE = BASE_DIR / "covers.sqlite3"
CONFIG_FILE = BASE_DIR / "config.json"

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "YandexMusicRPC"

POLL_INTERVAL = 1.0        # как часто проверять медиа-сессию
RESYNC_INTERVAL = 30.0     # раз в 30 с обновляем статус, чтобы пережить перезапуск Discord
DISCORD_TIMEOUT = 10.0     # потолок ожидания ответа Discord
MAX_TEXT_CHARS = 128       # лимит полей activity в Discord (символы, не байты)
MAX_IMAGE_BYTES = 32 * 1024 * 1024
UPLOAD_TIMEOUT = (5, 8)    # connect/read для загрузки обложки
UPLOAD_COOLDOWN = 600.0    # пауза после серии неудачных загрузок
TEMP_URL_TTL = 7200        # сколько верим ссылке временного хостинга (файл живёт ~3 ч)
MUTEX_NAME = "Local\\YandexMusicRPC2"

log = logging.getLogger("yandex-rpc")
_mutex_handle: int | None = None


# --------------------------------------------------------------------------- #
# Логирование и конфигурация
# --------------------------------------------------------------------------- #

def setup_logging(verbose: bool) -> None:
    """Пишем в файл всегда, в консоль — только если она вообще есть."""
    log.setLevel(logging.DEBUG)
    log.propagate = False

    file_format = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(file_format)
        file_handler.setLevel(logging.DEBUG)
        log.addHandler(file_handler)
    except OSError:
        pass

    if sys.stdout is not None:
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        log.addHandler(console)


def load_credentials() -> tuple[str, str]:
    """CLIENT_ID и ключ ImgBB: сначала переменные окружения, затем config.json."""
    client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
    imgbb_key = os.getenv("IMGBB_API_KEY", "").strip()

    if (not client_id or not imgbb_key) and CONFIG_FILE.is_file():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            client_id = client_id or str(data.get("client_id") or "").strip()
            imgbb_key = imgbb_key or str(data.get("imgbb_key") or "").strip()
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("Не удалось прочитать %s: %s", CONFIG_FILE.name, exc)

    return client_id, imgbb_key


def acquire_single_instance() -> bool:
    """Не даём двум копиям одновременно дёргать один Discord IPC."""
    global _mutex_handle
    if sys.platform != "win32":
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_bool

    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


# --------------------------------------------------------------------------- #
# Автозапуск с Windows
# --------------------------------------------------------------------------- #

def autostart_command() -> str:
    """Команда, которая будет запускаться при входе в систему."""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable)}"'

    script = Path(__file__).resolve()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = pythonw if pythonw.is_file() else Path(sys.executable)
    return f'"{launcher}" "{script}"'


def is_autostart_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
    except OSError:
        return False
    # Запись есть, но ссылается на старый путь (пересобрали exe / переименовали папку).
    return str(value).strip() == autostart_command()


def set_autostart(enabled: bool) -> bool:
    """Включает/выключает автозапуск. Возвращает True при успехе."""
    if winreg is None:
        log.warning("Автозапуск поддерживается только в Windows")
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_command()
                )
            else:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(key, AUTOSTART_NAME)
    except OSError as exc:
        log.warning("Не удалось изменить автозапуск: %s", exc)
        return False

    log.info("Автозапуск %s", "включён" if enabled else "выключен")
    return True


# --------------------------------------------------------------------------- #
# Кэш обложек
# --------------------------------------------------------------------------- #

class CoverCache:
    """Хранит соответствие трека и уже загруженной ссылки на обложку."""

    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path, timeout=5)
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS covers (
                    key        TEXT PRIMARY KEY,
                    url        TEXT NOT NULL,
                    title      TEXT NOT NULL,
                    artist     TEXT NOT NULL,
                    album      TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # База от прежней версии могла не иметь колонки expires_at.
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(covers)")}
            if "expires_at" not in columns:
                self._conn.execute(
                    "ALTER TABLE covers ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def build_key(title: str, artist: str, album: str) -> str:
        def norm(value: str) -> str:
            return re.sub(r"\s+", " ", (value or "").strip()).casefold()

        return "\x1f".join((norm(artist), norm(album), norm(title)))

    def get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT url, expires_at FROM covers WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        url, expires_at = row
        if expires_at and expires_at <= int(time.time()):
            return None  # ссылка на временном хостинге уже протухла
        return url

    def put(
        self,
        key: str,
        url: str,
        title: str,
        artist: str,
        album: str,
        *,
        ttl: int = 0,
    ) -> None:
        now = int(time.time())
        expires_at = now + ttl if ttl else 0
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO covers (key, url, title, artist, album, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    url = excluded.url,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (key, url, title, artist, album, now, expires_at),
            )

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()


class BaseUploader:
    name = "?"

    #: True — ссылки живут долго и кэшируются навсегда,
    #: False — файл удаляется хостингом через несколько часов.
    permanent = False

    def probe(self) -> bool:
        """Быстрая проверка доступности сервиса без реальной загрузки."""
        return True

    def upload(self, image: bytes) -> str | None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class ImgBBUploader(BaseUploader):
    name = "ImgBB"
    permanent = True

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._http = requests.Session()

    def probe(self) -> bool:
        try:
            # Именно путь загрузки: у части провайдеров корень сайта открыт,
            # а /1/upload висит без ответа. Любой ответ означает, что хостинг жив.
            response = self._http.get(
                "https://api.imgbb.com/1/upload",
                timeout=(4, 4),
                stream=True,
            )
            response.close()
            return True
        except requests.RequestException:
            return False
        except Exception:
            return False

    def upload(self, image: bytes) -> str | None:
        encoded = base64.b64encode(image).decode("ascii")
        response = self._http.post(
            "https://api.imgbb.com/1/upload",
            data={"key": self._api_key, "image": encoded},
            timeout=UPLOAD_TIMEOUT,
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        url = (response.json().get("data") or {}).get("url")
        if not url:
            raise RuntimeError("в ответе нет ссылки")
        return url

    def close(self) -> None:
        self._http.close()


class UguuUploader(BaseUploader):
    """Временный хостинг: файлы живут ~3 часа, зато работает без ключа."""

    name = "uguu.se"
    permanent = False

    def __init__(self) -> None:
        self._http = requests.Session()

    def upload(self, image: bytes) -> str | None:
        response = self._http.post(
            "https://uguu.se/upload?output=text",
            files={"files[]": ("cover.png", image, "image/png")},
            timeout=UPLOAD_TIMEOUT,
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        url = response.text.strip()
        if not url.startswith("http"):
            raise RuntimeError(f"неожиданный ответ: {url[:120]!r}")
        return url

    def close(self) -> None:
        self._http.close()


class UploaderChain:
    """Пробует загрузчики по порядку, падающие временно отключает."""

    def __init__(self, uploaders: list[BaseUploader]) -> None:
        self._uploaders = [u for u in uploaders if u is not None]
        self._failures: dict[str, int] = {}
        self._disabled_until: dict[str, float] = {}

    def probe(self) -> list[tuple[str, bool]]:
        """Отключает недоступные сервисы до первого трека, чтобы не ждать таймаут."""
        results = []
        for uploader in self._uploaders:
            ok = uploader.probe()
            results.append((uploader.name, ok))
            if not ok:
                self._disabled_until[uploader.name] = time.monotonic() + UPLOAD_COOLDOWN
        return results

    def _available(self, uploader: BaseUploader) -> bool:
        return time.monotonic() >= self._disabled_until.get(uploader.name, 0.0)

    def upload(self, image: bytes) -> tuple[str, int] | None:
        """Возвращает (url, ttl_seconds). ttl=0 — ссылка постоянная."""
        for uploader in self._uploaders:
            if not self._available(uploader):
                continue
            try:
                url = uploader.upload(image)
            except Exception as exc:
                failures = self._failures.get(uploader.name, 0) + 1
                self._failures[uploader.name] = failures
                if failures >= 2:
                    self._disabled_until[uploader.name] = (
                        time.monotonic() + UPLOAD_COOLDOWN
                    )
                    log.warning(
                        "%s недоступен (%s: %s). Отключаю на %.0f минут.",
                        uploader.name,
                        type(exc).__name__,
                        exc,
                        UPLOAD_COOLDOWN / 60,
                    )
                else:
                    log.warning("%s: загрузка не удалась (%s)", uploader.name, exc)
                continue

            if url:
                self._failures[uploader.name] = 0
                ttl = 0 if uploader.permanent else TEMP_URL_TTL
                log.debug("Обложка загружена через %s: %s", uploader.name, url)
                return url, ttl

        return None

    def close(self) -> None:
        for uploader in self._uploaders:
            with contextlib.suppress(Exception):
                uploader.close()


# --------------------------------------------------------------------------- #
# Медиа-сессии Windows
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Track:
    title: str
    artist: str
    album: str
    playing: bool
    position: float
    end: float
    info: Any


def is_yandex_session(session: Any) -> bool:
    app = (session.source_app_user_model_id or "").casefold()
    return "yandex" in app or "яндекс" in app


async def read_track(session: Any) -> Track | None:
    info = await session.try_get_media_properties_async()
    if info is None:
        return None

    playback = session.get_playback_info()
    timeline = session.get_timeline_properties()

    title = (info.title or "").strip()
    artist = (info.artist or "").strip()
    album = (info.album_title or "").strip()
    playing = bool(playback and playback.playback_status == PlaybackStatus.PLAYING)

    position = 0.0
    end = 0.0
    if timeline is not None:
        with contextlib.suppress(Exception):
            position = max(0.0, timeline.position.total_seconds())
        with contextlib.suppress(Exception):
            end = max(0.0, timeline.end_time.total_seconds())

    if not title and not playing:
        return None

    return Track(
        title=title,
        artist=artist,
        album=album,
        playing=playing,
        position=position,
        end=end,
        info=info,
    )


async def read_thumbnail(info: Any) -> bytes | None:
    """Читает обложку из медиа-сессии. Учитывает реальный размер прочитанных байт."""
    if info is None or info.thumbnail is None:
        return None

    stream = await info.thumbnail.open_read_async()
    try:
        if stream.size <= 0:
            return None

        buffer = Buffer(stream.size)
        result = await stream.read_async(buffer, buffer.capacity, InputStreamOptions.NONE)
        length = getattr(result, "length", None)
        if length is None:
            length = buffer.capacity
        if length <= 0:
            return None

        reader = DataReader.from_buffer(buffer)
        data = bytearray(length)
        reader.read_bytes(data)  # winsdk не возвращает число байт — режем сами
        return bytes(data)
    finally:
        with contextlib.suppress(Exception):
            stream.close()


class MediaWatcher:
    def __init__(self) -> None:
        self._manager: Any | None = None

    async def _get_manager(self) -> Any:
        if self._manager is None:
            self._manager = await MediaManager.request_async()
        return self._manager

    async def current_session(self) -> Any | None:
        try:
            manager = await self._get_manager()
            sessions = [s for s in manager.get_sessions() if is_yandex_session(s)]
        except Exception as exc:
            log.debug("Медиа-менеджер недоступен: %s", exc)
            self._manager = None
            return None

        if not sessions:
            return None

        # Из нескольких сессий Яндекса выбираем ту, что сейчас играет.
        for session in sessions:
            try:
                playback = session.get_playback_info()
                if playback and playback.playback_status == PlaybackStatus.PLAYING:
                    return session
            except Exception:
                continue
        return sessions[0]


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #

class DiscordPresence:
    def __init__(self, client_id: str) -> None:
        self._client_id = client_id
        self._rpc: AioPresence | None = None
        self._retry_at = 0.0
        self._backoff = 3.0
        self._generation = 0
        self._buttons_ok = True
        self._last_connect_error: str | None = None

    @property
    def generation(self) -> int:
        """Растёт при каждом успешном подключении — сигнал «статус надо переотправить»."""
        return self._generation

    @property
    def needs_resend(self) -> bool:
        """True, если соединения нет (Discord закрылся/перезапустился)."""
        rpc = self._rpc
        if rpc is None:
            return True
        writer = getattr(rpc, "sock_writer", None)
        reader = getattr(rpc, "sock_reader", None)
        try:
            if writer is not None and writer.is_closing():
                return True
            if reader is not None and reader.at_eof():
                return True
        except Exception:
            return True
        return False

    async def _ensure_connected(self) -> bool:
        if self._rpc is not None:
            if self.needs_resend:
                rpc, self._rpc = self._rpc, None
                await self._destroy(rpc)
            else:
                return True

        now = time.monotonic()
        if now < self._retry_at:
            return False

        rpc = AioPresence(self._client_id, connection_timeout=5, response_timeout=5)
        try:
            await asyncio.wait_for(rpc.connect(), timeout=8)
        except Exception as exc:
            await self._destroy(rpc)
            message = f"{type(exc).__name__}: {exc}"
            if message != self._last_connect_error:
                log.warning(
                    "Discord недоступен (%s). Следующая попытка через %.0f с.",
                    message,
                    self._backoff,
                )
                self._last_connect_error = message
            self._retry_at = now + self._backoff
            self._backoff = min(self._backoff * 1.8, 30.0)
            return False

        self._rpc = rpc
        self._generation += 1
        self._retry_at = 0.0
        self._backoff = 3.0
        self._last_connect_error = None
        log.info("Подключено к Discord")
        return True

    @staticmethod
    async def _destroy(rpc: AioPresence) -> None:
        """Закрывает IPC-соединение, не трогая event loop (в pypresence close() этого не умеет)."""
        with contextlib.suppress(Exception):
            rpc.send_data(2, {"v": 1, "client_id": rpc.client_id})
        writer = getattr(rpc, "sock_writer", None)
        reader = getattr(rpc, "sock_reader", None)
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=2)
        if reader is not None:
            with contextlib.suppress(Exception):
                reader.feed_eof()

    async def _drop(self, reason: Exception) -> bool:
        log.warning(
            "Связь с Discord потеряна (%s: %s) — переподключаюсь",
            type(reason).__name__,
            reason,
        )
        rpc, self._rpc = self._rpc, None
        if rpc is not None:
            await self._destroy(rpc)
        self._retry_at = time.monotonic() + 2.0
        return False

    async def update(self, activity: dict[str, Any]) -> bool:
        if not await self._ensure_connected():
            return False
        assert self._rpc is not None

        try:
            await asyncio.wait_for(self._rpc.update(**activity), timeout=DISCORD_TIMEOUT)
            return True
        except ServerError as exc:
            # Discord мог не принять кнопку — пробуем без неё и больше не мешаем.
            if activity.get("buttons") and self._buttons_ok:
                self._buttons_ok = False
                log.warning("Discord отклонил кнопку (%s), продолжаю без неё", exc)
                without_buttons = {k: v for k, v in activity.items() if k != "buttons"}
                try:
                    await asyncio.wait_for(
                        self._rpc.update(**without_buttons), timeout=DISCORD_TIMEOUT
                    )
                    return True
                except Exception as retry_exc:
                    return await self._drop(retry_exc)
            return await self._drop(exc)
        except Exception as exc:
            return await self._drop(exc)

    async def clear(self) -> bool:
        if self._rpc is None:
            return True
        try:
            await asyncio.wait_for(self._rpc.clear(), timeout=DISCORD_TIMEOUT)
            return True
        except Exception as exc:
            rpc, self._rpc = self._rpc, None
            if rpc is not None:
                await self._destroy(rpc)
            self._retry_at = time.monotonic() + 2.0
            log.debug("Не удалось очистить статус (%s: %s)", type(exc).__name__, exc)
            return False

    async def close(self, clear_activity: bool = True) -> None:
        rpc, self._rpc = self._rpc, None
        if rpc is None:
            return
        if clear_activity:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(rpc.clear(), timeout=DISCORD_TIMEOUT)
        await self._destroy(rpc)


# --------------------------------------------------------------------------- #
# Основной цикл
# --------------------------------------------------------------------------- #

def clip(value: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Убирает переводы строк и обрезает по лимиту символов Discord."""
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


class Runner:
    def __init__(
        self,
        cache: CoverCache,
        uploaders: UploaderChain | None,
        presence: DiscordPresence,
        watcher: MediaWatcher,
    ) -> None:
        self.cache = cache
        self.uploaders = uploaders
        self.presence = presence
        self.watcher = watcher

        self._last_key: tuple[str, str, str, bool] | None = None
        self._active = False
        self._last_sent_at = 0.0
        self._sent_generation = -1

        # Читается из потока трея — обычные bool достаточно.
        self.paused = False
        self.current_label = ""
        self.online: bool | None = None  # None — ещё не было ни одной попытки

    def reset(self) -> None:
        """Сбрасывает запомненный трек, чтобы статус отправился заново."""
        self._last_key = None

    async def run(self) -> None:
        log.info("Слежу за Yandex Music (опрос раз в %.0f с)", POLL_INTERVAL)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Непредвиденная ошибка в цикле наблюдения")
            await asyncio.sleep(POLL_INTERVAL)

    async def tick(self) -> bool:
        if self.paused:
            await self._clear_status()
            return False

        session = await self.watcher.current_session()
        if session is None:
            await self._clear_status()
            return False

        try:
            track = await read_track(session)
        except Exception as exc:
            log.debug("Не удалось прочитать медиа-сессию: %s", exc)
            await self._clear_status()
            return False

        if track is None:
            await self._clear_status()
            return False

        key = (track.title, track.artist, track.album, track.playing)
        now = time.monotonic()
        connection_lost = self._active and self.presence.needs_resend
        reconnected = self._active and self.presence.generation != self._sent_generation
        resync = (
            key == self._last_key
            and self._active
            and now - self._last_sent_at >= RESYNC_INTERVAL
        )

        # Если Discord закрылся и трек не менялся — тоже переотправляем статус.
        if (
            key == self._last_key
            and self._active
            and not reconnected
            and not resync
            and not connection_lost
        ):
            return True

        cover_url = await self._cover_for(track)
        activity = self._build_activity(track, cover_url)

        if not await self.presence.update(activity):
            self.online = False
            return False
        self.online = True

        if key != self._last_key:
            suffix = "" if track.playing else " (на паузе)"
            self.current_label = f"{track.artist or 'Неизвестный'} — {track.title or 'трек'}{suffix}"
            log.info("%s", self.current_label)

        self._last_key = key
        self._active = True
        self._last_sent_at = now
        self._sent_generation = self.presence.generation
        return True

    async def _clear_status(self) -> None:
        if not self._active and self._last_key is None:
            self.current_label = ""
            return
        if await self.presence.clear():
            if self._active:
                log.info("Музыка не играет — статус очищен")
            self._last_key = None
            self._active = False
            self._sent_generation = -1
            self.current_label = ""

    async def _cover_for(self, track: Track) -> str | None:
        key = CoverCache.build_key(track.title, track.artist, track.album)
        cached = self.cache.get(key)
        if cached:
            log.debug("Обложка из кэша: %s", cached)
            return cached

        if self.uploaders is None:
            return None

        image = await read_thumbnail(track.info)
        if not image:
            log.debug("Обложка у трека отсутствует")
            return None
        if len(image) > MAX_IMAGE_BYTES:
            log.warning("Обложка слишком большая (%d байт), пропускаю", len(image))
            return None

        uploaded = await asyncio.to_thread(self.uploaders.upload, image)
        if uploaded is None:
            return None

        url, ttl = uploaded
        self.cache.put(key, url, track.title, track.artist, track.album, ttl=ttl)
        return url

    @staticmethod
    def _build_activity(track: Track, cover_url: str | None) -> dict[str, Any]:
        activity: dict[str, Any] = {
            "activity_type": ActivityType.LISTENING,
            "details": clip(track.title or "Неизвестный трек"),
            "state": clip(track.artist or "Неизвестный исполнитель"),
            "large_image": cover_url,
            "large_text": clip(track.album or "Yandex Music"),
        }

        # У радио и рекламы end_time == 0 — прогресс-бар им не нужен.
        if track.playing and track.end > 0 and track.end > track.position:
            start_ts = int(time.time() - track.position)
            end_ts = start_ts + int(track.end - track.position)
            if end_ts > start_ts:
                activity["start"] = start_ts
                activity["end"] = end_ts

        search = " ".join(part for part in (track.artist, track.title) if part).strip()
        if search:
            url = build_search_url(search)
            if url:
                activity["buttons"] = [
                    {"label": "Открыть в Яндекс Музыке", "url": url}
                ]

        return activity


def build_search_url(search: str, limit: int = 250) -> str | None:
    """Ссылка на поиск в Яндекс Музыке, гарантированно влезающая в лимит Discord.

    Кириллица в percent-кодировке занимает 6 символов на букву, поэтому длинный
    трек приходится обрезать, иначе Discord отклонит кнопку целиком.
    """
    base = "https://music.yandex.ru/search?"
    for cut in range(len(search), 0, -2):
        query = search[:cut].rstrip()
        if not query:
            break
        url = base + urllib.parse.urlencode({"text": query})
        if len(url) <= limit:
            return url
    return None


# --------------------------------------------------------------------------- #
# Иконка и трей
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Config:
    client_id: str
    imgbb_key: str


def draw_icon(state: str = "running", size: int = 64) -> Any:
    """Рисует иконку в трее: жёлтая — работает, серая — пауза, красная — нет Discord."""
    from PIL import Image, ImageDraw

    palette = {
        "running": ((255, 219, 77, 255), (25, 25, 25, 255)),
        "paused": ((158, 158, 158, 255), (255, 255, 255, 255)),
        "offline": ((211, 47, 47, 255), (255, 255, 255, 255)),
        "waiting": ((97, 97, 97, 255), (255, 255, 255, 255)),
    }
    background, foreground = palette.get(state, palette["running"])

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * 0.24), fill=background
    )

    scale = size / 64
    draw.ellipse(
        (14 * scale, 38 * scale, 30 * scale, 54 * scale), fill=foreground
    )
    draw.rectangle(
        (27 * scale, 12 * scale, 33 * scale, 47 * scale), fill=foreground
    )
    draw.polygon(
        [
            (33 * scale, 12 * scale),
            (48 * scale, 20 * scale),
            (48 * scale, 28 * scale),
            (33 * scale, 20 * scale),
        ],
        fill=foreground,
    )
    return image


def save_icon_file(path: Path) -> None:
    """Сохраняет многоразмерный .ico для сборки exe."""
    draw_icon("running", 256).save(
        path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


class PresenceService:
    """Держит asyncio-цикл в отдельном потоке и даёт трею управлять состоянием."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task[None] | None = None
        self._runner: Runner | None = None
        self._presence: DiscordPresence | None = None
        self._paused = threading.Event()
        self._ready = threading.Event()

    @property
    def runner(self) -> Runner | None:
        return self._runner

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._thread_main, name="presence-service", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=15):
            log.warning("Сервис долго стартует — продолжаю в фоне")

    def pause(self) -> None:
        self._paused.set()
        if self._runner is not None:
            self._runner.paused = True
        log.debug("Наблюдение приостановлено")

    def resume(self) -> None:
        self._paused.clear()
        if self._runner is not None:
            self._runner.paused = False
            self._runner.reset()
        log.debug("Наблюдение возобновлено")

    def icon_state(self) -> str:
        if self.paused:
            return "paused"
        if self._runner is None or self._runner.online is None:
            return "waiting"
        return "running" if self._runner.online else "offline"

    def tooltip(self) -> str:
        title = "Yandex Music RPC"
        if self.paused:
            return f"{title} — пауза"
        if self._runner is None or self._runner.online is None:
            return f"{title} — запускается…"
        if not self._runner.online:
            return f"{title} — Discord не найден"
        if self._runner.current_label:
            return f"{title}\n{self._runner.current_label}"
        return f"{title} — музыка не играет"

    def stop(self, timeout: float = 10.0) -> None:
        loop, thread = self._loop, self._thread
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            with contextlib.suppress(Exception):
                future.result(timeout=timeout)
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None
        self._loop = None

    async def _shutdown(self) -> None:
        self._paused.set()
        if self._runner is not None:
            self._runner.paused = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except asyncio.CancelledError:
            log.debug("Сервис остановлен")
        except Exception:
            log.exception("Сервис упал")
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _main(self) -> None:
        self._task = asyncio.current_task()

        uploaders = (
            UploaderChain([ImgBBUploader(self._config.imgbb_key), UguuUploader()])
            if self._config.imgbb_key
            else UploaderChain([UguuUploader()])
        )
        cache = CoverCache(CACHE_FILE)
        presence = DiscordPresence(self._config.client_id)
        runner = Runner(cache, uploaders, presence, MediaWatcher())
        self._runner, self._presence = runner, presence
        self._ready.set()

        try:
            for name, ok in await asyncio.to_thread(uploaders.probe):
                log.info(
                    "Хостинг обложек %s: %s", name, "доступен" if ok else "недоступен"
                )
            await runner.run()
        finally:
            with contextlib.suppress(Exception):
                await presence.close(clear_activity=True)
            uploaders.close()
            cache.close()
            self._ready.set()


class TrayApp:
    """Иконка в системном трее с меню управления."""

    def __init__(self, service: PresenceService, config: Config) -> None:
        import pystray

        self._pystray = pystray
        self._config = config
        self._service = service
        self._updates_stop = threading.Event()
        self._last_state: str | None = None
        self._last_title: str | None = None

        self.icon = pystray.Icon(
            "yandex-music-rpc",
            draw_icon("waiting"),
            "Yandex Music RPC",
            menu=self._build_menu(),
        )

    def _build_menu(self) -> Any:
        menu = self._pystray.Menu
        item = self._pystray.MenuItem
        return menu(
            item(self._track_text, None, enabled=False),
            menu.SEPARATOR,
            item(
                lambda _: "Продолжить" if self._service.paused else "Пауза",
                self._on_toggle_pause,
            ),
            item("Перезапустить", self._on_restart),
            item(
                "Автозапуск с Windows",
                self._on_toggle_autostart,
                checked=lambda _: is_autostart_enabled(),
            ),
            menu.SEPARATOR,
            item("Открыть лог", self._on_open_log),
            item("Выход", self._on_quit),
        )

    def _track_text(self, _item: Any = None) -> str:
        if self._service.paused:
            return "Пауза"
        runner = self._service.runner
        if runner is not None and runner.current_label:
            label = runner.current_label
            return label if len(label) <= 60 else label[:59] + "…"
        return "Музыка не играет"

    def run(self) -> None:
        threading.Thread(target=self._update_loop, name="tray-updater", daemon=True).start()
        self.icon.run(setup=self._on_ready)

    def _on_ready(self, icon: Any) -> None:
        # При своём setup pystray не показывает иконку сам — включаем вручную.
        try:
            icon.visible = True
        except Exception:
            log.exception("Не удалось показать иконку в трее")
            return
        log.info("Иконка в трее создана. Меню — правым кликом.")

    def _update_loop(self) -> None:
        while not self._updates_stop.wait(1.0):
            state = self._service.icon_state()
            if state != self._last_state:
                self._last_state = state
                with contextlib.suppress(Exception):
                    self.icon.icon = draw_icon(state)

            title = self._service.tooltip()
            if title != self._last_title:
                self._last_title = title
                with contextlib.suppress(Exception):
                    self.icon.title = title[:127]

    def _on_toggle_pause(self, _icon: Any, _item: Any) -> None:
        if self._service.paused:
            self._service.resume()
        else:
            self._service.pause()
        with contextlib.suppress(Exception):
            self.icon.update_menu()

    def _on_restart(self, _icon: Any, _item: Any) -> None:
        threading.Thread(target=self._restart, name="tray-restart", daemon=True).start()

    def _restart(self) -> None:
        log.info("Перезапуск сервиса по команде из трея")
        self._service.stop()
        self._service = PresenceService(self._config)
        self._service.start()
        with contextlib.suppress(Exception):
            self.icon.update_menu()

    def _on_toggle_autostart(self, _icon: Any, _item: Any) -> None:
        set_autostart(not is_autostart_enabled())
        with contextlib.suppress(Exception):
            self.icon.update_menu()

    def _on_open_log(self, _icon: Any, _item: Any) -> None:
        target = LOG_FILE if LOG_FILE.exists() else BASE_DIR
        with contextlib.suppress(OSError):
            os.startfile(str(target))  # noqa: S606 — открываем проводник/блокнот

    def _on_quit(self, _icon: Any, _item: Any) -> None:
        threading.Thread(target=self._quit, name="tray-quit", daemon=True).start()

    def _quit(self) -> None:
        log.info("Выход по команде из трея")
        self._updates_stop.set()
        self._service.stop()
        with contextlib.suppress(Exception):
            self.icon.stop()


def run_tray(config: Config) -> int:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        log.warning(
            "Для трея нужны pystray и Pillow (pip install pystray pillow). "
            "Запускаю в консольном режиме."
        )
        return asyncio.run(run_bot(config, once=False))

    service = PresenceService(config)
    service.start()
    app = TrayApp(service, config)
    try:
        app.run()
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
    finally:
        app._updates_stop.set()
        service.stop()
    return 0


# --------------------------------------------------------------------------- #
# Диагностика и точка входа
# --------------------------------------------------------------------------- #

async def diagnose() -> int:
    try:
        manager = await MediaManager.request_async()
        sessions = list(manager.get_sessions())
    except Exception as exc:
        log.error("Не удалось получить медиа-сессии: %s", exc)
        return 1

    if not sessions:
        log.info("Активных медиа-сессий нет. Запусти музыку и попробуй снова.")
    else:
        log.info("Найдено медиа-сессий: %d", len(sessions))
        statuses = {
            PlaybackStatus.PLAYING: "играет",
            PlaybackStatus.PAUSED: "пауза",
            PlaybackStatus.STOPPED: "остановлено",
        }
        for index, session in enumerate(sessions, 1):
            app = session.source_app_user_model_id or "?"
            try:
                info = await session.try_get_media_properties_async()
                title = info.title or "?"
                artist = info.artist or "?"
            except Exception as exc:
                log.info("[%d] %s — не удалось прочитать (%s)", index, app, exc)
                continue
            try:
                playback = session.get_playback_info()
                status = statuses.get(playback.playback_status, "?")
            except Exception:
                status = "?"
            marker = "  <-- будет использоваться" if is_yandex_session(session) else ""
            log.info("[%d] %s | %s — %s | %s%s", index, app, artist, title, status, marker)

    pipe = None
    try:
        from pypresence.utils import get_ipc_path

        pipe = get_ipc_path()
    except Exception:
        pass
    log.info("Discord IPC: %s", pipe or "не найден (Discord запущен?)")

    _, imgbb_key = load_credentials()
    chain = UploaderChain(
        [ImgBBUploader(imgbb_key), UguuUploader()] if imgbb_key else [UguuUploader()]
    )
    try:
        for name, ok in await asyncio.to_thread(chain.probe):
            log.info(
                "Хостинг обложек %s: %s",
                name,
                "доступен" if ok else "НЕДОСТУПЕН — будет использован следующий",
            )
    finally:
        chain.close()
    return 0


async def run_bot(config: Config, *, once: bool) -> int:
    uploaders: UploaderChain | None = None
    if config.imgbb_key:
        uploaders = UploaderChain([ImgBBUploader(config.imgbb_key), UguuUploader()])
    else:
        uploaders = UploaderChain([UguuUploader()])

    # Проверяем хостинги заранее, чтобы первый трек не ждал таймаут.
    for name, ok in await asyncio.to_thread(uploaders.probe):
        log.info("Хостинг обложек %s: %s", name, "доступен" if ok else "недоступен")

    cache = CoverCache(CACHE_FILE)
    presence = DiscordPresence(config.client_id)
    runner = Runner(cache, uploaders, presence, MediaWatcher())

    try:
        if once:
            # Discord иногда не отдаёт IPC-соединение с первой попытки.
            for attempt in range(1, 4):
                if await runner.tick():
                    log.info("Статус отправлен.")
                    return 0
                if attempt < 3:
                    log.debug("Повтор через 3 с (попытка %d из 3)", attempt + 1)
                    await asyncio.sleep(3)
            log.info("Статус не отправлен: нет активного трека или Discord недоступен.")
            return 1

        await runner.run()
        return 0
    finally:
        # В режиме --once статус должен остаться в профиле после выхода.
        await presence.close(clear_activity=not once)
        uploaders.close()
        cache.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Yandex Music -> Discord Rich Presence 2.0")
    parser.add_argument("--check", action="store_true", help="показать окружение и выйти")
    parser.add_argument("--once", action="store_true", help="отправить статус один раз и выйти")
    parser.add_argument("--console", action="store_true", help="без трея, работа в консоли")
    parser.add_argument(
        "--install-autostart", action="store_true", help="включить автозапуск и выйти"
    )
    parser.add_argument(
        "--uninstall-autostart", action="store_true", help="выключить автозапуск и выйти"
    )
    parser.add_argument(
        "--make-icon", type=Path, help="сохранить .ico с иконкой трея и выйти"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="подробный вывод в консоль")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    if args.make_icon:
        save_icon_file(args.make_icon)
        log.info("Иконка сохранена: %s", args.make_icon)
        return 0

    if args.check:
        return asyncio.run(diagnose())

    if args.install_autostart or args.uninstall_autostart:
        enabled = set_autostart(args.install_autostart)
        if enabled:
            log.info("%s", autostart_command())
        return 0 if enabled else 1

    client_id, imgbb_key = load_credentials()
    if not client_id:
        log.error(
            "CLIENT_ID не найден. Укажи его в config.json, rpc.py "
            "или задай переменную окружения DISCORD_CLIENT_ID."
        )
        return 1
    if not imgbb_key:
        log.warning("IMGBB_API_KEY не найден — обложки будут через резервный хостинг.")

    if not acquire_single_instance():
        log.error("Программа уже запущена — второй экземпляр не нужен.")
        return 1

    config = Config(client_id, imgbb_key)
    try:
        if args.once:
            return asyncio.run(run_bot(config, once=True))
        if args.console or sys.platform != "win32":
            return asyncio.run(run_bot(config, once=False))
        return run_tray(config)
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
        return 0
    except Exception:
        log.exception("Критическая ошибка")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
