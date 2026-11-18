"""Check (and optionally create) the PRIVATE Supabase Storage buckets SharkHire needs.

    python scripts/setup_storage.py                # read-only check (uses .env / environment)
    python scripts/setup_storage.py --create       # create missing buckets as PRIVATE
    python scripts/setup_storage.py --roundtrip    # also upload/sign/download/delete one test object

Buckets come from SUPABASE_STORAGE_CV_BUCKET (default "cvs") and SUPABASE_STORAGE_MEDIA_BUCKET
(default "media"). Existing buckets are never modified or emptied; a PUBLIC bucket is reported as
an error (make it private in the dashboard). The round-trip writes one object under
"healthcheck/" in the CV bucket and deletes it again. Credentials are never printed.
Exit code 0 = everything OK, 1 = a problem was found.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

sys.path.insert(0, ".")
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.services.http import close_http_clients
from app.services.storage.supabase import SupabaseStorage, missing_configuration

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEST_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _buckets() -> list[tuple[str, int, list[str]]]:
    s = get_settings()
    return [
        (s.SUPABASE_STORAGE_CV_BUCKET, s.cv_max_bytes, [PDF_MIME, DOCX_MIME]),
        (s.SUPABASE_STORAGE_MEDIA_BUCKET, s.image_max_bytes, ["image/jpeg", "image/png", "image/webp"]),
    ]


async def run(create: bool, roundtrip: bool) -> int:
    settings = get_settings()
    print(f"STORAGE_BACKEND={settings.STORAGE_BACKEND}")
    if settings.STORAGE_BACKEND != "supabase":
        print("WARN  STORAGE_BACKEND is not 'supabase' - uploads go to the local disk (development only)")
    missing = missing_configuration()
    if missing:
        print(f"FAIL  missing configuration: {', '.join(missing)}")
        return 1
    storage = SupabaseStorage()
    ok = True
    for bucket, limit, mimes in _buckets():
        try:
            info = await storage.check(bucket)
            print(f"OK    bucket '{bucket}' exists, private, limit={info['file_size_limit']}, "
                  f"mime={info['allowed_mime_types']}")
        except AppError as exc:
            if exc.code == "storage_bucket_missing" and create:
                try:
                    await storage.create_bucket(bucket, file_size_limit=limit, allowed_mime_types=mimes)
                    await storage.check(bucket)
                    print(f"OK    bucket '{bucket}' created (private, limit={limit}, mime={mimes})")
                    continue
                except AppError as create_exc:
                    exc = create_exc
            hint = {
                "storage_bucket_missing": "re-run with --create, or create it in Dashboard > Storage (PRIVATE)",
                "storage_auth_failed": "SUPABASE_SERVICE_ROLE_KEY must be the secret / service_role key",
                "storage_bucket_public": "Dashboard > Storage > bucket > Edit: turn OFF 'Public bucket'",
            }.get(exc.code, "check SUPABASE_URL and network access")
            print(f"FAIL  bucket '{bucket}': {exc.code} - {hint}")
            ok = False

    if ok and roundtrip:
        bucket = settings.SUPABASE_STORAGE_CV_BUCKET
        path = f"healthcheck/{uuid.uuid4().hex}.pdf"
        try:
            await storage.put(bucket, path, TEST_PDF, PDF_MIME)
            url = await storage.signed_url(bucket, path, 60)
            data = await storage.get(bucket, path)
            assert data == TEST_PDF, "downloaded bytes differ"
            assert "token=" in url, "signed URL has no token"
            print("OK    round-trip upload / signed URL / download")
        except (AppError, AssertionError) as exc:
            print(f"FAIL  round-trip: {getattr(exc, 'code', exc)}")
            ok = False
        finally:
            try:
                await storage.delete(bucket, path)
                print("OK    round-trip test object deleted")
            except AppError as exc:
                print(f"WARN  could not delete test object {path}: {exc.code}")
    await close_http_clients()
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--create", action="store_true", help="create missing buckets as PRIVATE")
    parser.add_argument("--roundtrip", action="store_true", help="write/read/delete one test object")
    args = parser.parse_args()
    return asyncio.run(run(args.create, args.roundtrip))


if __name__ == "__main__":
    raise SystemExit(main())
