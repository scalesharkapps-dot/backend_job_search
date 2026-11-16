"""Storage backend selection."""

from __future__ import annotations

from app.core.config import get_settings
from app.services.storage.base import StorageBackend, StorageError, build_object_path


def get_storage() -> StorageBackend:
    """Return the configured backend (FastAPI dependency; overridable in tests)."""
    if get_settings().STORAGE_BACKEND == "supabase":
        from app.services.storage.supabase import SupabaseStorage

        return SupabaseStorage()
    from app.services.storage.local import LocalStorage

    return LocalStorage()


__all__ = ["StorageBackend", "StorageError", "build_object_path", "get_storage"]
