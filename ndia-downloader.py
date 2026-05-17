import logging
import queue
import re
import subprocess
import threading
import uuid
from datetime import date
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

# ---------------------------------------------------------------------------
# Configuration — edit these values before running
# ---------------------------------------------------------------------------

SOURCE_ACCOUNT_URL  = "https://sragpstondia.blob.core.windows.net"
SOURCE_CONTAINER    = "upload"
INCLUDE_OPTIONS    = None   # None for default (exclude deleted blobs), or list of: "deleted", "metadata", "snapshots", "versions", "tags"

DEST_ACCOUNT_URL    = "https://sra1pstagdatatemp.blob.core.windows.net"
DEST_CONTAINER      = "json"

# Each entry: (compiled regex, destination prefix inside DEST_CONTAINER)
# Patterns are tested in order; first match wins.
REGEX_ROUTES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ncc-agent-event",   re.IGNORECASE), "amazonConnect/ndia/agentevents"),
    (re.compile(r"ncc-contact-trace",   re.IGNORECASE), "amazonConnect/ndia/contacts"),
    (re.compile(r"^\d\d:\d\d:\d\d.+json$", re.IGNORECASE), "amazonConnect/ndia/eval"),
    (re.compile(r"ocs-ncc-", re.IGNORECASE), "amazonConnect/ndia/metrics")
]

# Destination prefix for files that match no pattern
CATCH_ALL_PREFIX = "amazonConnect/ndia/unknown"

TEMP_DIR    = Path(r"E:\blob-temp")       # local staging area for downloads
ARCHIVE_DIR = Path(r"E:\blob-archive")    # 7z archives written here
LOG_DIR     = Path(r"E:\blob-logs")       # log files written here

DOWNLOAD_WORKERS  = 8
UPLOAD_WORKERS    = 16

# Number of files batched into a single 7z call by the archive worker.
# Larger values mean fewer 7z invocations (faster overall) at the cost of
# holding more temp files in memory between flushes.
ARCHIVE_BATCH_SIZE = 500

# Set to True only when you are confident the pipeline is working correctly.
# While False, source blobs are left intact after a successful upload.
DELETE_SOURCE = True

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _configure_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"ndia-downloader_{date.today():%Y-%m-%d}.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s — %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thread-safe counters
# ---------------------------------------------------------------------------

class _Counters:
    def __init__(self):
        self._lock          = threading.Lock()
        self.downloaded     = 0
        self.uploaded       = 0
        self.archived       = 0
        self.archive_failed = 0
        self.failed         = 0
        self.skipped        = 0

    def inc(self, field: str, n: int = 1):
        with self._lock:
            setattr(self, field, getattr(self, field) + n)

counters = _Counters()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_clients() -> tuple:
    """Return (source_container_client, dest_container_client) using MSI."""
    credential = DefaultAzureCredential()
    source_client = (
        BlobServiceClient(SOURCE_ACCOUNT_URL, credential=credential)
        .get_container_client(SOURCE_CONTAINER)
    )
    dest_client = (
        BlobServiceClient(DEST_ACCOUNT_URL, credential=credential)
        .get_container_client(DEST_CONTAINER)
    )
    return source_client, dest_client


def get_dest_prefix(blob_name: str) -> str:
    """Return the destination prefix for a blob name based on REGEX_ROUTES."""
    for pattern, prefix in REGEX_ROUTES:
        if pattern.search(blob_name):
            return prefix
    return CATCH_ALL_PREFIX


def get_archive_path() -> Path:
    """Return today's 7z archive path, creating ARCHIVE_DIR if needed."""
    return ARCHIVE_DIR / f"archive_{date.today():%Y-%m-%d}.zip"


def safe_local_path(blob_name: str) -> Path:
    """
    Map a blob name to a flat filename in TEMP_DIR that is safe on NTFS.
    '/' path separators are collapsed to '__'; all other characters that are
    illegal on NTFS ( \\ : * ? " < > | ) are replaced with '_'.
    """
    # Flatten path separators first so we don't create subdirectories
    safe_name = blob_name.replace("/", "__")
    # Replace remaining NTFS-illegal characters
    safe_name = re.sub(r'[\\:*?"<>|]', "_", safe_name)
    return TEMP_DIR / safe_name


# ---------------------------------------------------------------------------
# Download worker
# ---------------------------------------------------------------------------

def download_worker(
    source_client,
    download_q: queue.Queue,
    upload_q: queue.Queue,
):
    while True:
        blob_name = download_q.get()
        if blob_name is None:           # sentinel — shut down
            download_q.task_done()
            break

        local_path = safe_local_path(blob_name)
        try:
            blob_client = source_client.get_blob_client(blob_name)
            with open(local_path, "wb") as fh:
                stream = blob_client.download_blob()
                stream.readinto(fh)
            log.info("Downloaded: %s", blob_name)
            counters.inc("downloaded")
            upload_q.put((local_path, blob_name))
        except Exception:
            log.exception("Failed to download: %s", blob_name)
            counters.inc("failed")
            # Remove partial file if it exists
            if local_path.exists():
                local_path.unlink(missing_ok=True)
        finally:
            download_q.task_done()


# ---------------------------------------------------------------------------
# Upload worker
# ---------------------------------------------------------------------------

def upload_worker(
    source_client,
    dest_client,
    upload_q: queue.Queue,
    archive_q: queue.Queue,
):
    while True:
        item = upload_q.get()
        if item is None:                # sentinel — shut down
            upload_q.task_done()
            break

        local_path, blob_name = item
        # Build destination blob path: regex targets original blob name,
        # but upload uses safe local filename (Spark-safe)
        safe_filename = local_path.name
        dest_prefix   = get_dest_prefix(blob_name)
        dest_blob_name = f"{dest_prefix}/{safe_filename}"

        try:
            # Upload
            dest_blob_client = dest_client.get_blob_client(dest_blob_name)
            with open(local_path, "rb") as fh:
                dest_blob_client.upload_blob(fh, overwrite=True)

            # Verify by size
            local_size = local_path.stat().st_size
            remote_size = dest_blob_client.get_blob_properties().size

            if local_size != remote_size:
                raise ValueError(
                    f"Size mismatch for {blob_name}: "
                    f"local={local_size}, remote={remote_size}"
                )

            log.info("Uploaded and verified: %s → %s", blob_name, dest_blob_name)

            # Hand the (local file, source blob name) pair to the archive
            # worker. Source deletion only happens there, after the file is
            # confirmed archived — preventing data loss if archiving fails.
            archive_q.put((local_path, blob_name))
            counters.inc("uploaded")

        except Exception:
            log.exception("Failed to upload/verify '%s' → '%s'", blob_name, dest_blob_name)
            counters.inc("failed")
            # Leave local temp file and source blob intact for investigation
        finally:
            upload_q.task_done()


# ---------------------------------------------------------------------------
# Archive worker
# ---------------------------------------------------------------------------

def archive_worker(archive_q: queue.Queue, source_client):
    """
    Single dedicated thread. Accumulates (local_path, blob_name) tuples and
    calls 7z once per batch using a listfile (-i@).

    On SUCCESS: deletes source blobs (if DELETE_SOURCE) then removes local
    temp files — in that order so a blob is never deleted before the archive
    copy is confirmed.

    On FAILURE: logs every filename in the failed batch (so nothing is
    silently lost), increments archive_failed, and leaves temp files and
    source blobs untouched for investigation.

    Shutdown sentinel: None.
    """
    batch: list[tuple[Path, str]] = []

    def flush():
        if not batch:
            return
        archive_path = get_archive_path()
        # Unique listfile per batch to avoid collisions if multiple instances run
        listfile     = TEMP_DIR / f"_7z_listfile_{uuid.uuid4().hex[:8]}.txt"
        local_paths  = [p for p, _ in batch]
        blob_names   = [n for _, n in batch]

        listfile.write_text(
            "\n".join(str(p) for p in local_paths), encoding="utf-8"
        )
        try:
            result = subprocess.run(
                [
                    "7z", "a",
                    "-tzip",
                    "-mm=Deflate",
                    "-mx=1",
                    f"-i@{listfile}",
                    str(archive_path),
                ],
                check=True,
                capture_output=True,
            )
            log.info("Archived batch of %d files → %s", len(batch), archive_path.name)
            counters.inc("archived", len(batch))

            # Archive confirmed — now safe to delete source blobs and temp files.
            # Batch-delete in chunks of 256 (Azure SDK hard limit per request),
            # so the number of HTTP calls = ceil(batch_size / 256).
            successfully_deleted = set()
            if DELETE_SOURCE:
                _AZURE_DELETE_CHUNK = 256
                for i in range(0, len(blob_names), _AZURE_DELETE_CHUNK):
                    chunk = blob_names[i : i + _AZURE_DELETE_CHUNK]
                    try:
                        responses = source_client.delete_blobs(*chunk)
                        for resp, name in zip(responses, chunk):
                            if resp.status_code not in (200, 202, 204):
                                log.error(
                                    "Failed to delete source blob (HTTP %d): %s",
                                    resp.status_code, name,
                                )
                                counters.inc("failed")
                            else:
                                log.debug("Deleted source: %s", name)
                                successfully_deleted.add(name)
                        log.info(
                            "Batch-deleted %d source blobs (%d/%d)",
                            len(chunk), min(i + _AZURE_DELETE_CHUNK, len(blob_names)), len(blob_names),
                        )
                    except Exception:
                        log.exception(
                            "Batch delete failed for chunk starting at index %d (%d blobs)", i, len(chunk)
                        )
                        for name in chunk:
                            log.error("  DELETE FAILED: %s", name)
                        counters.inc("failed", len(chunk))
            else:
                log.debug("DELETE_SOURCE=False — %d source blobs kept", len(batch))
                # If not deleting source, we still clean up temp files
                successfully_deleted = set(blob_names)

            # Only unlink temp files for blobs that were successfully deleted (or all if not deleting)
            for local_path, blob_name in batch:
                if blob_name in successfully_deleted:
                    local_path.unlink(missing_ok=True)
                else:
                    log.warning(
                        "Temp file NOT removed (source blob delete failed): %s",
                        local_path.name,
                    )

        except subprocess.CalledProcessError as exc:
            stderr_text = exc.stderr.decode(errors="replace").strip() if exc.stderr else "(none)"
            log.error(
                "7z failed archiving batch of %d files. 7z stderr: %s",
                len(batch), stderr_text,
            )
            log.error("The following %d files were NOT archived (source blobs and temp files preserved):", len(batch))
            for blob_name in blob_names:
                log.error("  ARCHIVE FAILED: %s", blob_name)
            counters.inc("archive_failed", len(batch))
        except Exception:
            log.exception("Unexpected error archiving batch of %d files", len(batch))
            log.error("The following %d files were NOT archived (source blobs and temp files preserved):", len(batch))
            for blob_name in blob_names:
                log.error("  ARCHIVE FAILED: %s", blob_name)
            counters.inc("archive_failed", len(batch))
        finally:
            listfile.unlink(missing_ok=True)
            batch.clear()

    while True:
        item = archive_q.get()
        if item is None:        # sentinel — flush remainder and exit
            archive_q.task_done()
            flush()
            break
        batch.append(item)
        archive_q.task_done()
        if len(batch) >= ARCHIVE_BATCH_SIZE:
            flush()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    _configure_logging()
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=== Run configuration ===")
    log.info("  SOURCE       : %s / %s", SOURCE_ACCOUNT_URL, SOURCE_CONTAINER)
    log.info("  DESTINATION  : %s / %s", DEST_ACCOUNT_URL, DEST_CONTAINER)
    log.info("  TEMP_DIR     : %s", TEMP_DIR)
    log.info("  ARCHIVE_DIR  : %s", ARCHIVE_DIR)
    log.info("  LOG_DIR      : %s", LOG_DIR)
    log.info("  WORKERS      : download=%d  upload=%d", DOWNLOAD_WORKERS, UPLOAD_WORKERS)
    log.info("  BATCH SIZE   : %d", ARCHIVE_BATCH_SIZE)
    log.info("  DELETE_SOURCE: %s", DELETE_SOURCE)
    log.info("=========================")
    log.info("Connecting to Azure storage accounts...")
    source_client, dest_client = build_clients()

    # Populate the download queue
    download_q: queue.Queue = queue.Queue(maxsize=DOWNLOAD_WORKERS * 4)
    upload_q: queue.Queue   = queue.Queue(maxsize=UPLOAD_WORKERS * 4)
    # Bound the archive queue so uploads back-pressure rather than
    # accumulating unbounded paths in memory.
    archive_q: queue.Queue  = queue.Queue(maxsize=UPLOAD_WORKERS * 4)

    # Archive thread is non-daemon: if the main thread crashes it must still
    # finish flushing its current batch before the process exits.
    archive_thread = threading.Thread(
        target=archive_worker,
        args=(archive_q, source_client),
        name="archive",
        daemon=False,
    )
    archive_thread.start()

    # Launch upload pool
    upload_threads = []
    for i in range(UPLOAD_WORKERS):
        t = threading.Thread(
            target=upload_worker,
            args=(source_client, dest_client, upload_q, archive_q),
            name=f"upload-{i}",
            daemon=True,
        )
        t.start()
        upload_threads.append(t)

    # Launch download pool
    download_threads = []
    for i in range(DOWNLOAD_WORKERS):
        t = threading.Thread(
            target=download_worker,
            args=(source_client, download_q, upload_q),
            name=f"download-{i}",
            daemon=True,
        )
        t.start()
        download_threads.append(t)

    # Enumerate all blobs and feed the download queue.
    # The queue has a maxsize so this naturally back-pressures enumeration
    # rather than loading all blob names into memory at once.
    blob_count = 0
    log.info("Enumerating blobs in %s / %s ...", SOURCE_ACCOUNT_URL, SOURCE_CONTAINER)
    try:
        for blob in source_client.list_blobs(include=INCLUDE_OPTIONS):
            download_q.put(blob.name)   # blocks if queue is full (back-pressure)
            blob_count += 1
            if blob_count % 10_000 == 0:
                log.info("Queued %d blobs so far...", blob_count)
        log.info("Enumeration complete. Total blobs: %d", blob_count)
    except Exception:
        log.exception(
            "Enumeration failed after %d blobs — signalling workers to stop cleanly",
            blob_count,
        )
    finally:
        # Always send sentinels so no worker is left hanging
        for _ in range(DOWNLOAD_WORKERS):
            download_q.put(None)

    # Wait for all downloads to finish, then signal upload workers
    for t in download_threads:
        t.join()
    log.info("All download workers finished.")

    for _ in range(UPLOAD_WORKERS):
        upload_q.put(None)

    for t in upload_threads:
        t.join()
    log.info("All upload workers finished.")

    # Signal archive worker to flush and exit
    archive_q.put(None)
    archive_thread.join()
    log.info("Archive worker finished.")

    log.info("=== Final summary ===")
    log.info("  Blobs enumerated : %d", blob_count)
    log.info("  Downloaded       : %d", counters.downloaded)
    log.info("  Uploaded+verified: %d", counters.uploaded)
    log.info("  Archived         : %d", counters.archived)
    log.info("  Archive failures : %d", counters.archive_failed)
    log.info("  Failures         : %d", counters.failed)
    log.info("  Skipped          : %d", counters.skipped)
    if counters.archive_failed > 0:
        log.warning(
            "%d file(s) failed to archive — source blobs and temp files "
            "were preserved. Search the log for 'ARCHIVE FAILED' to identify them.",
            counters.archive_failed,
        )
    log.info("=====================")


if __name__ == "__main__":
    main()
