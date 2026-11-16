"""Storage abstraction for private files (CVs, profile photos, company logos)."""

from __future__ import annotations

import abc
import re
import uuid

from app.core.exceptions import AppError

_SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-/]*\.[a-z0-9]{2,5}$")


class StorageError(AppError):
    status_code = 503
    code = "storage_unavailable"
    message = "File storage is temporarily unavailable."


def build_object_path(kind: str, owner_id: uuid.UUID, extension: str) -> str:
    """Server-generated object key: ``<kind>/<owner uuid>/<random uuid>.<ext>``.

    Client filenames are never used in storage paths (prevents path traversal).
    """
    path = f"{kind}/{owner_id}/{uuid.uuid4().hex}.{extension.lower()}"
    validate_object_path(path)
    return path


def validate_object_path(path: str) -> str:
    if ".." in path or path.startswith("/") or "\\" in path or not _SAFE_PATH.match(path):
        raise StorageError("Invalid storage path.", code="invalid_storage_path", status_code=400)
    return path


class StorageBackend(abc.ABC):
    """Private object storage. Paths are always server-generated."""

    name: str = "abstract"

    @abc.abstractmethod
    async def put(self, bucket: str, path: str, data: bytes, content_type: str) -> None: ...

    @abc.abstractmethod
    async def get(self, bucket: str, path: str) -> bytes: ...

    @abc.abstractmethod
    async def delete(self, bucket: str, path: str) -> None:
        """Delete an object; deleting a missing object is not an error."""

    @abc.abstractmethod
    async def signed_url(
        self, bucket: str, path: str, expires_in: int, download_name: str | None = None
    ) -> str:
        """Return a short-lived URL granting read access to one object."""
