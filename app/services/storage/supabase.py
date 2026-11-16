"""Supabase Storage backend (private buckets, signed URLs).

Uses the Storage REST API with the service-role key, server-side only. Buckets
must be created as PRIVATE (see docs/PRODUCTION_DEPLOYMENT.md).
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.services.http import get_http_client
from app.services.storage.base import StorageBackend, StorageError, validate_object_path

logger = logging.getLogger(__name__)


class SupabaseStorage(StorageBackend):
    name = "supabase"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.supabase_url or not settings.SUPABASE_SERVICE_ROLE_KEY:
            raise StorageError(
                "Supabase Storage is not configured.", code="storage_not_configured"
            )
        self.base = f"{settings.supabase_url}/storage/v1"
        key = settings.SUPABASE_SERVICE_ROLE_KEY.get_secret_value()
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        self.public_base = settings.supabase_url

    async def _call(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        client = get_http_client("supabase_storage", 30.0)
        headers = {**self.headers, **kwargs.pop("headers", {})}  # type: ignore[dict-item]
        try:
            return await client.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            logger.warning("Supabase Storage %s failed: %s", method, type(exc).__name__)
            raise StorageError() from exc

    async def put(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        validate_object_path(path)
        response = await self._call(
            "POST",
            f"{self.base}/object/{bucket}/{quote(path)}",
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "false"},
        )
        if response.status_code >= 300:
            logger.warning("Supabase Storage upload status=%s", response.status_code)
            raise StorageError()

    async def get(self, bucket: str, path: str) -> bytes:
        validate_object_path(path)
        response = await self._call("GET", f"{self.base}/object/authenticated/{bucket}/{quote(path)}")
        if response.status_code == 404 or response.status_code == 400:
            raise StorageError("File not found.", code="file_missing", status_code=404)
        if response.status_code >= 300:
            raise StorageError()
        return response.content

    async def delete(self, bucket: str, path: str) -> None:
        validate_object_path(path)
        response = await self._call(
            "DELETE", f"{self.base}/object/{bucket}", json={"prefixes": [path]}
        )
        if response.status_code >= 300 and response.status_code != 404:
            raise StorageError()

    async def signed_url(
        self, bucket: str, path: str, expires_in: int, download_name: str | None = None
    ) -> str:
        validate_object_path(path)
        response = await self._call(
            "POST",
            f"{self.base}/object/sign/{bucket}/{quote(path)}",
            json={"expiresIn": expires_in},
        )
        if response.status_code >= 300:
            raise StorageError()
        signed = (response.json() or {}).get("signedURL") or (response.json() or {}).get("signedUrl")
        if not signed:
            raise StorageError()
        url = f"{self.base}{signed}" if signed.startswith("/object") else f"{self.public_base}{signed}"
        if download_name:
            url += ("&" if "?" in url else "?") + "download=" + quote(download_name)
        return url
