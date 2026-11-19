"""Supabase Storage backend (private buckets, signed URLs).

Uses the Storage REST API with the service-role (secret) key, server-side only.
Buckets must be PRIVATE; ``scripts/setup_storage.py`` checks and creates them.

Error codes returned to clients (messages never contain configuration values):

* ``storage_not_configured`` - ``SUPABASE_URL`` or ``SUPABASE_SERVICE_ROLE_KEY`` missing/blank
* ``storage_bucket_missing`` - the configured bucket does not exist in the project
* ``storage_auth_failed``    - Supabase rejected the key (wrong/rotated key, or a public key was used)
* ``storage_unavailable``    - network error or any other upstream failure
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.services.http import get_http_client
from app.services.storage.base import StorageBackend, StorageError, validate_object_path

logger = logging.getLogger(__name__)

NOT_CONFIGURED_MESSAGE = "File uploads aren't available right now because the server's file storage isn't set up."


def missing_configuration() -> list[str]:
    """Names (never values) of the settings Supabase Storage needs but lacks."""
    settings = get_settings()
    missing = []
    if not (settings.supabase_url or "").strip():
        missing.append("SUPABASE_URL")
    key = settings.SUPABASE_SERVICE_ROLE_KEY
    if key is None or not key.get_secret_value().strip():
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    return missing


def _error_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _is_bucket_missing(response: httpx.Response) -> bool:
    # Supabase answers HTTP 400 with {"statusCode": "404", "error": "Bucket not found", "code": "NoSuchBucket"}.
    body = _error_body(response)
    text = f"{body.get('error', '')} {body.get('message', '')}".lower()
    return body.get("code") == "NoSuchBucket" or "bucket not found" in text


def _is_auth_error(response: httpx.Response) -> bool:
    # A wrong key yields HTTP 400 with {"statusCode": "403", "code": "AccessDenied", ...} (or a plain 401/403).
    body = _error_body(response)
    return (
        response.status_code in (401, 403)
        or str(body.get("statusCode")) in ("401", "403")
        or body.get("code") in ("AccessDenied", "InvalidJWT")
    )


def _upstream_error(response: httpx.Response, operation: str, bucket: str) -> StorageError:
    """Translate a failed Storage response into a client-safe StorageError (and log it for ops)."""
    if _is_bucket_missing(response):
        logger.error(
            "Supabase Storage bucket %r does not exist (run scripts/setup_storage.py --create)", bucket
        )
        return StorageError(
            "File storage is not set up correctly. Please try again later.", code="storage_bucket_missing"
        )
    if _is_auth_error(response):
        logger.error(
            "Supabase Storage rejected the server key on %s (status=%s). SUPABASE_SERVICE_ROLE_KEY must be "
            "the project's secret/service_role key, not the anon/publishable key.",
            operation,
            response.status_code,
        )
        return StorageError(
            "File storage is not set up correctly. Please try again later.", code="storage_auth_failed"
        )
    logger.warning("Supabase Storage %s failed status=%s", operation, response.status_code)
    return StorageError()


class SupabaseStorage(StorageBackend):
    name = "supabase"

    def __init__(self) -> None:
        missing = missing_configuration()
        if missing:
            logger.error("Supabase Storage is not configured; missing: %s", ", ".join(missing))
            raise StorageError(NOT_CONFIGURED_MESSAGE, code="storage_not_configured")
        settings = get_settings()
        self.base = f"{settings.supabase_url}/storage/v1"
        key = settings.SUPABASE_SERVICE_ROLE_KEY.get_secret_value().strip()  # type: ignore[union-attr]
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
            raise _upstream_error(response, "upload", bucket)

    async def get(self, bucket: str, path: str) -> bytes:
        validate_object_path(path)
        response = await self._call("GET", f"{self.base}/object/authenticated/{bucket}/{quote(path)}")
        if response.status_code < 300:
            return response.content
        if _is_bucket_missing(response) or _is_auth_error(response):
            raise _upstream_error(response, "download", bucket)
        if response.status_code in (400, 404):
            raise StorageError("File not found.", code="file_missing", status_code=404)
        raise _upstream_error(response, "download", bucket)

    async def delete(self, bucket: str, path: str) -> None:
        validate_object_path(path)
        response = await self._call(
            "DELETE", f"{self.base}/object/{bucket}", json={"prefixes": [path]}
        )
        if response.status_code >= 300 and (response.status_code != 404 or _is_bucket_missing(response)):
            raise _upstream_error(response, "delete", bucket)

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
            raise _upstream_error(response, "sign", bucket)
        body = _error_body(response)
        signed = body.get("signedURL") or body.get("signedUrl")
        if not signed:
            raise StorageError()
        url = f"{self.base}{signed}" if signed.startswith("/object") else f"{self.public_base}{signed}"
        if download_name:
            url += ("&" if "?" in url else "?") + "download=" + quote(download_name)
        return url

    async def check(self, bucket: str) -> dict[str, Any]:
        """Verify the key works and the bucket exists and is private (read-only)."""
        response = await self._call("GET", f"{self.base}/bucket/{quote(bucket)}")
        if response.status_code >= 300:
            raise _upstream_error(response, "bucket check", bucket)
        info = _error_body(response)
        if info.get("public"):
            logger.error("Supabase Storage bucket %r is PUBLIC; CVs must be in a private bucket", bucket)
            raise StorageError(
                "File storage is not set up correctly. Please try again later.", code="storage_bucket_public"
            )
        return {
            "bucket": bucket,
            "exists": True,
            "public": False,
            "file_size_limit": info.get("file_size_limit"),
            "allowed_mime_types": info.get("allowed_mime_types"),
        }

    async def create_bucket(
        self, bucket: str, *, file_size_limit: int, allowed_mime_types: list[str]
    ) -> None:
        """Create a PRIVATE bucket (used by scripts/setup_storage.py only)."""
        response = await self._call(
            "POST",
            f"{self.base}/bucket",
            json={
                "id": bucket,
                "name": bucket,
                "public": False,
                "file_size_limit": file_size_limit,
                "allowed_mime_types": allowed_mime_types,
            },
        )
        if response.status_code >= 300:
            raise _upstream_error(response, "bucket create", bucket)
