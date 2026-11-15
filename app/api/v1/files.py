"""Signed-download endpoint for the LOCAL storage backend (development only).

With Supabase Storage, signed URLs point directly at Supabase and this route
is unused.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response

from app.services.storage.local import LocalStorage, read_download_token

router = APIRouter(prefix="/files", tags=["Files"], include_in_schema=False)

_TYPES = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


@router.get("/{token}")
async def download(token: str) -> Response:
    data = read_download_token(token)
    bucket, path = str(data["b"]), str(data["p"])
    content = await LocalStorage().get(bucket, path)
    ext = path.rsplit(".", 1)[-1]
    headers = {"Cache-Control": "private, no-store"}
    if data.get("n"):
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(str(data['n']))}"
    return Response(content, media_type=_TYPES.get(ext, "application/octet-stream"), headers=headers)
