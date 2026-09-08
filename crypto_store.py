from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any


APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "CommandManager"
STORE_FILE = APP_DIR / "profiles.json"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


CRYPTPROTECT_UI_FORBIDDEN = 0x01


def _blob_from_bytes(data: bytes) -> DATA_BLOB:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _bytes_from_blob(blob: DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def encrypt_text(value: str) -> str:
    data = value.encode("utf-8")
    input_blob = _blob_from_bytes(data)
    output_blob = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise ctypes.WinError()
    return base64.b64encode(_bytes_from_blob(output_blob)).decode("ascii")


def decrypt_text(value: str) -> str:
    data = base64.b64decode(value.encode("ascii"))
    input_blob = _blob_from_bytes(data)
    output_blob = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise ctypes.WinError()
    return _bytes_from_blob(output_blob).decode("utf-8")


def load_profiles() -> list[dict[str, Any]]:
    if not STORE_FILE.exists():
        return []
    with STORE_FILE.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    return payload.get("profiles", [])


def save_profiles(profiles: list[dict[str, Any]]) -> None:
    payload = _load_payload()
    payload["profiles"] = profiles
    _save_payload(payload)


def load_commands() -> list[dict[str, Any]]:
    return _load_payload().get("commands", [])


def save_commands(commands: list[dict[str, Any]]) -> None:
    payload = _load_payload()
    payload["commands"] = commands
    _save_payload(payload)


def load_editors() -> list[dict[str, Any]]:
    return _load_payload().get("editors", [])


def save_editors(editors: list[dict[str, Any]]) -> None:
    payload = _load_payload()
    payload["editors"] = editors
    _save_payload(payload)


def load_favorites() -> dict[str, list[str]]:
    favorites = _load_payload().get("favorites", {})
    return {
        "local_paths": list(favorites.get("local_paths", [])),
        "remote_paths": list(favorites.get("remote_paths", [])),
    }


def save_favorites(favorites: dict[str, list[str]]) -> None:
    payload = _load_payload()
    payload["favorites"] = {
        "local_paths": list(favorites.get("local_paths", [])),
        "remote_paths": list(favorites.get("remote_paths", [])),
    }
    _save_payload(payload)


def _load_payload() -> dict[str, Any]:
    if not STORE_FILE.exists():
        return {"profiles": [], "commands": [], "editors": [], "favorites": {"local_paths": [], "remote_paths": []}}
    with STORE_FILE.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    payload.setdefault("profiles", [])
    payload.setdefault("commands", [])
    payload.setdefault("editors", [])
    payload.setdefault("favorites", {"local_paths": [], "remote_paths": []})
    return payload


def _save_payload(payload: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with STORE_FILE.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def profile_id(name: str, host: str, username: str) -> str:
    key = f"{name}\0{host}\0{username}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()[:16]


def command_id(name: str, path: str) -> str:
    key = f"{name}\0{path}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()[:16]
