"""Documents: uploaded files, kept by content hash and never changed.

The bytes live in a DocumentStore; core.documents records what each one is. Callers say
where a document came from in `source` (e.g. "finance.import"); core knows nothing more.
"""

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core import models
from lararium.core.commands import CommandContext
from lararium.core.errors import DocumentIntegrityError, DocumentNotFoundError


class DocumentStore(Protocol):
    def put(self, sha256: str, content: bytes) -> None:
        """Store content under its hash; storing a hash that is already there does nothing."""
        ...

    def get(self, sha256: str) -> bytes:
        """The content stored under this hash; raises FileNotFoundError if there is none."""
        ...


class LocalDocumentStore(DocumentStore):
    """Files in a local directory, at `root / sha[:2] / sha[2:4] / sha`."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256[2:4] / sha256

    def put(self, sha256: str, content: bytes) -> None:
        path = self._path(sha256)
        if path.exists():
            return
        # Owner-only, whatever the umask lets through: the files are bank statements, and the
        # directory names are their hashes. mkstemp already makes the files themselves 0600.
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.parent.mkdir(mode=0o700, exist_ok=True)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        # Written beside the target and renamed into place, so a reader never sees half a file.
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def get(self, sha256: str) -> bytes:
        return self._path(sha256).read_bytes()


@dataclass(frozen=True)
class DocumentRecord:
    id: UUID
    sha256: str
    size_bytes: int
    media_type: str
    original_filename: str
    source: str
    created_at: datetime


def _columns() -> tuple[Any, ...]:
    d = models.Document
    return (
        d.id,
        d.sha256,
        d.size_bytes,
        d.media_type,
        d.original_filename,
        d.source,
        d.created_at,
    )


def _record(row: Any) -> DocumentRecord:
    return DocumentRecord(
        id=row.id,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        media_type=row.media_type,
        original_filename=row.original_filename,
        source=row.source,
        created_at=row.created_at,
    )


async def store_document(
    ctx: CommandContext,
    store: DocumentStore,
    content: bytes,
    *,
    filename: str,
    media_type: str,
    source: str,
) -> DocumentRecord:
    """Store a document; if the same content is already stored, return that one unchanged."""
    d = models.Document
    digest = hashlib.sha256(content).hexdigest()
    # The file first: a row must never point at bytes that were not written.
    store.put(digest, content)
    inserted = await ctx.session.scalar(
        insert(d)
        .values(
            sha256=digest,
            size_bytes=len(content),
            media_type=media_type,
            original_filename=filename,
            source=source,
        )
        .on_conflict_do_nothing(index_elements=[d.sha256])
        .returning(d.id)
    )
    if inserted is None:
        existing = (await ctx.session.execute(select(*_columns()).where(d.sha256 == digest))).one()
        return _record(existing)
    record = await get_document(ctx.session, inserted)
    ctx.emit("core.document.stored", record.id, {"document_id": str(record.id), "sha256": digest})
    ctx.record_change(
        f"document:{record.id}",
        {
            "sha256": digest,
            "size_bytes": record.size_bytes,
            "media_type": media_type,
            "original_filename": filename,
            "source": source,
        },
    )
    return record


async def get_document(session: AsyncSession, document_id: UUID) -> DocumentRecord:
    row = (
        await session.execute(select(*_columns()).where(models.Document.id == document_id))
    ).one_or_none()
    if row is None:
        raise DocumentNotFoundError(document_id)
    return _record(row)


async def read_document(session: AsyncSession, store: DocumentStore, document_id: UUID) -> bytes:
    """The document's content, checked against its hash."""
    record = await get_document(session, document_id)
    try:
        content = store.get(record.sha256)
    except FileNotFoundError:
        raise DocumentIntegrityError(
            f"document {document_id}: the stored file is missing"
        ) from None
    if hashlib.sha256(content).hexdigest() != record.sha256:
        raise DocumentIntegrityError(
            f"document {document_id}: the stored file doesn't match its hash"
        )
    return content
