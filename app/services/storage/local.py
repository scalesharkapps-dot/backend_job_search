"""Local-filesystem storage for development and tests.

Files live under ``LOCAL_STORAGE_DIR`` (git-ignored). Downloads use HMAC-signed,
expiring tokens served by ``GET /api/v1/files/{token}``. Not for production.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import quote

from app.core.config import get_settings
from app.services.storage.base import StorageBackend, StorageError, validate_object_path


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload: bytes) -> str:
    key = get_settings().APP_SECRET_KEY.get_secret_value().encode()
    return _b64(hmac.new(key, payload, hashlib.sha256).digest())


def make_download_token(bucket: str, path: str, expires_in: int, download_name: str | None) -> str:
    payload = json.dumps(
        {"b": bucket, "p": path, "e": int(time.time()) + expires_in, "n": download_name},
        separators=(",", ":"),
    ).encode()
    return f"{_b64(payload)}.{_sign(payload)}"


def read_download_token(token: str) -> dict[str, object]:
    """Validate signature and expiry; raises StorageError(404) when invalid."""
    try:
        raw, signature = token.split(".", 1)
        payload = _unb64(raw)
    except (ValueError, TypeError):
        raise StorageError("Link is invalid.", code="invalid_link", status_code=404) from None
    if not hmac.compare_digest(_sign(payload), signature):
        raise StorageError("Link is invalid.", code="invalid_link", status_code=404)
    data = json.loads(payload)
    if int(data["e"]) < time.time():
        raise StorageError("Link has expired.", code="link_expired", status_code=410)
    return data


class LocalStorage(StorageBackend):
    name = "local"

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or get_settings().LOCAL_STORAGE_DIR).resolve()

    def _file(self, bucket: str, path: str) -> Path:
        validate_object_path(path)
        target = (self.root / bucket / path).resolve()
        if self.root not in target.parents:
            raise StorageError("Invalid storage path.", code="invalid_storage_path", status_code=400)
        return target

    async def put(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        target = self._file(bucket, path)

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            raise StorageError() from exc

    async def get(self, bucket: str, path: str) -> bytes:
        target = self._file(bucket, path)
        try:
            return await asyncio.to_thread(target.read_bytes)
        except FileNotFoundError:
            raise StorageError("File not found.", code="file_missing", status_code=404) from None
        except OSError as exc:
            raise StorageError() from exc

    async def delete(self, bucket: str, path: str) -> None:
        target = self._file(bucket, path)
        try:
            await asyncio.to_thread(target.unlink, True)
        except OSError as exc:
            raise StorageError() from exc

    async def check(self, bucket: str) -> dict[str, object]:
        def _probe() -> None:
            target = self.root / bucket
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".write-probe"
            probe.write_bytes(b"ok")
            probe.unlink()

        try:
            await asyncio.to_thread(_probe)
        except OSError as exc:
            raise StorageError() from exc
        return {"bucket": bucket, "exists": True}

    async def signed_url(
        self, bucket: str, path: str, expires_in: int, download_name: str | None = None
    ) -> str:
        self._file(bucket, path)
        settings = get_settings()
        token = make_download_token(bucket, path, expires_in, download_name)
        return f"{settings.PUBLIC_BASE_URL.rstrip('/')}{settings.API_V1_PREFIX}/files/{quote(token)}"
