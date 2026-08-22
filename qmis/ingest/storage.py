"""Storage abstraction for incoming weekly files.

The system watches "a place where files appear".  Today that place is a folder
on the server; tomorrow it is Google Drive, OneDrive or SharePoint because
someone in a meeting will decide so.  Everything downstream talks to
:class:`StorageBackend`, so swapping the platform means writing one new class
and changing one config line - not rebuilding the pipeline.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

EXCEL_SUFFIXES = (".xlsx", ".xlsm", ".xls")


@dataclass(frozen=True)
class RemoteFile:
    """A file the backend can see, described without leaking backend details."""

    uri: str
    name: str
    size: int
    modified_at: datetime
    backend: str


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


class StorageBackend(ABC):
    """Minimal contract every storage platform must satisfy."""

    name: str = "abstract"

    @abstractmethod
    def list_files(self, suffixes: Sequence[str] = EXCEL_SUFFIXES) -> list[RemoteFile]:
        """Files currently visible in the watched location, newest first."""

    @abstractmethod
    def fetch(self, uri: str, destination: Path) -> Path:
        """Copy/download ``uri`` to a local path and return it."""

    @abstractmethod
    def archive(self, uri: str) -> str | None:
        """Move a processed file out of the inbox. Returns the new URI."""

    def content_hash(self, uri: str, workdir: Path) -> str:
        """Hash of the file contents - the basis of duplicate detection."""
        local = self.fetch(uri, workdir / Path(uri).name)
        return sha256_file(local)


class LocalFolderStorage(StorageBackend):
    """Watches a folder on the server (or a synced Drive/OneDrive mount).

    A synced cloud folder is the cheapest possible "cloud integration": the
    Drive/OneDrive desktop client does the syncing and this class just sees
    files appear.  It buys the same workflow as an API integration with none of
    the OAuth maintenance, and it is replaceable the day that stops being true.
    """

    name = "local"

    def __init__(self, inbox: str | Path, archive_dir: str | Path | None = None):
        self.inbox = Path(inbox)
        self.archive_dir = Path(archive_dir) if archive_dir else self.inbox / "_processed"
        self.inbox.mkdir(parents=True, exist_ok=True)

    def list_files(self, suffixes: Sequence[str] = EXCEL_SUFFIXES) -> list[RemoteFile]:
        out: list[RemoteFile] = []
        for path in self.inbox.iterdir():
            if not path.is_file():
                continue
            if path.name.startswith("~$") or path.name.startswith("."):
                continue  # Excel lock files and hidden junk
            if suffixes and path.suffix.lower() not in suffixes:
                continue
            stat = path.stat()
            out.append(
                RemoteFile(
                    uri=str(path.resolve()),
                    name=path.name,
                    size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    backend=self.name,
                )
            )
        return sorted(out, key=lambda f: f.modified_at, reverse=True)

    def fetch(self, uri: str, destination: Path) -> Path:
        source = Path(uri)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() == destination.resolve():
            return destination
        shutil.copy2(source, destination)
        return destination

    def archive(self, uri: str) -> str | None:
        source = Path(uri)
        if not source.exists():
            return None
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        target = self.archive_dir / f"{source.stem}__{stamp}{source.suffix}"
        shutil.move(str(source), target)
        return str(target)

    def content_hash(self, uri: str, workdir: Path | None = None) -> str:
        return sha256_file(uri)


class InMemoryStorage(StorageBackend):
    """Backend used by tests and by the Streamlit upload widget."""

    name = "memory"

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self._archived: set[str] = set()

    def put(self, name: str, payload: bytes) -> RemoteFile:
        self._files[name] = payload
        return RemoteFile(
            uri=name,
            name=name,
            size=len(payload),
            modified_at=datetime.now(timezone.utc),
            backend=self.name,
        )

    def list_files(self, suffixes: Sequence[str] = EXCEL_SUFFIXES) -> list[RemoteFile]:
        now = datetime.now(timezone.utc)
        return [
            RemoteFile(uri=n, name=n, size=len(p), modified_at=now, backend=self.name)
            for n, p in self._files.items()
            if n not in self._archived and (not suffixes or Path(n).suffix.lower() in suffixes)
        ]

    def fetch(self, uri: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self._files[uri])
        return destination

    def archive(self, uri: str) -> str | None:
        self._archived.add(uri)
        return f"archived://{uri}"

    def content_hash(self, uri: str, workdir: Path | None = None) -> str:
        return sha256_bytes(self._files[uri])


class CloudDriveStorage(StorageBackend):
    """Placeholder for a native Drive / OneDrive / SharePoint integration.

    Deliberately not implemented: a synced folder already delivers the required
    workflow, and building an OAuth integration before the organisation has
    chosen its platform would be work thrown away.  When the platform is
    decided, implement the four methods below - nothing else changes.
    """

    name = "cloud"

    def __init__(self, provider: str, folder_id: str, credentials: dict | None = None):
        self.provider = provider
        self.folder_id = folder_id
        self.credentials = credentials or {}

    def _unimplemented(self) -> None:
        raise NotImplementedError(
            f"{self.provider} storage backend is not configured. Use LocalFolderStorage "
            "against a synced Drive/OneDrive folder, or implement this backend."
        )

    def list_files(self, suffixes: Sequence[str] = EXCEL_SUFFIXES) -> list[RemoteFile]:
        self._unimplemented()
        return []

    def fetch(self, uri: str, destination: Path) -> Path:
        self._unimplemented()
        return destination

    def archive(self, uri: str) -> str | None:
        self._unimplemented()
        return None


def build_storage(config: dict) -> StorageBackend:
    """Factory driven by settings.yaml -> ``storage`` block."""
    kind = str(config.get("backend", "local")).lower()
    if kind == "local":
        return LocalFolderStorage(
            config.get("inbox", os.environ.get("QMIS_INBOX", "data/inbox")),
            config.get("archive"),
        )
    if kind == "memory":
        return InMemoryStorage()
    if kind in ("gdrive", "onedrive", "sharepoint"):
        return CloudDriveStorage(kind, config.get("folder_id", ""), config.get("credentials"))
    raise ValueError(f"unknown storage backend {kind!r}")
