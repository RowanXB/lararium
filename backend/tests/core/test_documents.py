import os
import re
import stat
import uuid
from pathlib import Path

import pytest
from sqlalchemy import exc, func, select, text

from lararium.core import documents, models
from lararium.core.documents import (
    LocalDocumentStore,
    get_document,
    read_document,
    store_document,
)
from lararium.core.errors import DocumentIntegrityError, DocumentNotFoundError

CONTENT = b"date,amount\n"


@pytest.fixture
def store(tmp_path: Path) -> LocalDocumentStore:
    return LocalDocumentStore(tmp_path)


def _path(root: Path, sha256: str) -> Path:
    return root / sha256[:2] / sha256[2:4] / sha256


async def _store(run, store: LocalDocumentStore, filename: str = "a.csv"):
    return await run(
        store_document,
        store,
        CONTENT,
        filename=filename,
        media_type="text/csv",
        source="finance.import",
    )


async def test_store_and_read_round_trip(session, run, store) -> None:
    record = await _store(run, store)

    assert record.size_bytes == 12
    assert re.fullmatch(r"[0-9a-f]{64}", record.sha256)
    assert (record.media_type, record.original_filename, record.source) == (
        "text/csv",
        "a.csv",
        "finance.import",
    )
    assert await get_document(session, record.id) == record
    assert await read_document(session, store, record.id) == CONTENT


async def test_same_content_is_stored_once(session, run, store) -> None:
    first = await _store(run, store, "a.csv")
    second = await _store(run, store, "b.csv")

    assert second.id == first.id
    assert second.original_filename == "a.csv"
    events = await session.scalar(
        select(func.count())
        .select_from(models.Event)
        .where(models.Event.type == "core.document.stored", models.Event.subject_id == first.id)
    )
    assert events == 1


async def test_tampered_file_is_an_integrity_error(session, run, store, tmp_path) -> None:
    record = await _store(run, store)
    path = _path(tmp_path, record.sha256)

    path.write_bytes(b"date,amount\n1,2\n")
    with pytest.raises(DocumentIntegrityError):
        await read_document(session, store, record.id)

    path.unlink()
    with pytest.raises(DocumentIntegrityError):
        await read_document(session, store, record.id)


async def test_unknown_document_is_not_found(session) -> None:
    with pytest.raises(DocumentNotFoundError):
        await get_document(session, uuid.uuid4())


async def test_backend_role_cannot_update_or_delete_documents(session, run, store) -> None:
    await _store(run, store)

    for statement in ("update core.documents set source = 'x'", "delete from core.documents"):
        with pytest.raises(exc.ProgrammingError, match="permission denied"):
            async with session.begin_nested():
                await session.execute(text(statement))


def test_stored_files_and_their_directories_are_owner_only(tmp_path: Path) -> None:
    """Documents are bank statements: the shard directories are 0700, the files 0600,
    whatever the process umask."""
    root = tmp_path / "documents"
    store = LocalDocumentStore(root)
    sha = "ab" + "0" * 62
    previous = os.umask(0o022)
    try:
        store.put(sha, CONTENT)
    finally:
        os.umask(previous)

    path = _path(root, sha)
    assert path.read_bytes() == CONTENT
    for directory in (root, path.parent.parent, path.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_put_syncs_the_rename_and_new_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Third review: the rename wasn't made durable, so after a power cut a committed row
    could point at a file that never reached the disk."""
    synced: list[Path] = []
    monkeypatch.setattr(documents, "_fsync_directory", synced.append)
    store = LocalDocumentStore(tmp_path / "docs")
    sha = "ab" * 32
    store.put(sha, b"statement")
    shard = tmp_path / "docs" / "ab" / "ab"
    assert synced == [shard, shard.parent, tmp_path / "docs"]
