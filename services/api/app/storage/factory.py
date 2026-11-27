"""Build the configured FileStorage (D-88). Every entrypoint uses this, never a backend class."""

from app.core.config import Settings
from app.storage.base import FileStorage
from app.storage.local import LocalFileStorage


def build_storage(settings: Settings) -> FileStorage:
    if settings.file_storage_backend == "supabase":
        from app.storage.supabase import SupabaseFileStorage

        # Settings validation already guarantees both values for this backend.
        if settings.supabase_url is None or settings.supabase_service_key is None:
            raise ValueError("FILE_STORAGE_BACKEND=supabase needs SUPABASE_URL and the key")
        return SupabaseFileStorage(
            settings.supabase_url,
            settings.supabase_service_key.get_secret_value(),
            settings.supabase_storage_bucket,
        )
    return LocalFileStorage(settings.storage_root)
