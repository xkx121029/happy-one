"""映射表与续传表的 SQL 行为。"""

from __future__ import annotations

from happyone.storage import repository

SHA = "d" * 64


def _insert(conn, *, entity_id="ent_1", object_id="obj_1", key="relay/a.txt", namespace="relay"):
    return repository.upsert_object(
        conn,
        namespace=namespace,
        bucket="relay" if namespace == "relay" else "license-artifacts",
        key=key,
        object_id=object_id,
        sha256=SHA,
        size_bytes=5,
        content_type="text/plain",
        entity_type="message",
        entity_id=entity_id,
        filename="a.txt",
    )


def test_upsert_is_idempotent_per_entity(conn):
    first = _insert(conn)
    second = _insert(conn, object_id="obj_2")

    assert first.id == second.id
    assert second.object_id == "obj_2"
    rows = repository.list_by_entity(conn, "message", "ent_1")
    assert len(rows) == 1


def test_same_key_can_be_shared_by_entities(conn):
    _insert(conn, entity_id="ent_1")
    _insert(conn, entity_id="ent_2")

    assert len(repository.list_by_entity(conn, "message", "ent_1")) == 1
    assert len(repository.list_by_entity(conn, "message", "ent_2")) == 1
    assert repository.count_shares(conn, "obj_1") == 2


def test_count_shares_excludes_given_record(conn):
    first = _insert(conn, entity_id="ent_1")
    _insert(conn, entity_id="ent_2")
    assert repository.count_shares(conn, "obj_1", exclude_record_id=first.id) == 1


def test_list_by_entity_filters_namespace(conn):
    _insert(conn, entity_id="ent_1")
    repository.upsert_object(
        conn,
        namespace="license",
        bucket="license-artifacts",
        key="license/license/ent_1/a.txt",
        object_id="obj_2",
        sha256=SHA,
        size_bytes=5,
        content_type=None,
        entity_type="message",
        entity_id="ent_1",
        filename="a.txt",
    )

    assert len(repository.list_by_entity(conn, "message", "ent_1")) == 2
    assert len(repository.list_by_entity(conn, "message", "ent_1", namespace="license")) == 1


def test_delete_record(conn):
    record = _insert(conn)
    assert repository.delete_record(conn, record.id) is True
    assert repository.get_by_id(conn, record.id) is None


def test_upload_session_roundtrip(conn):
    repository.upsert_upload_session(
        conn,
        upload_id="up_1",
        bucket="relay",
        key="relay/a.txt",
        sha256=SHA,
        size_bytes=10,
        chunk_size=4,
    )
    session = repository.find_upload_session(
        conn, bucket="relay", key="relay/a.txt", sha256=SHA, size_bytes=10
    )
    assert session is not None
    assert session.uploaded_chunks == ()

    repository.mark_chunk_uploaded(conn, "up_1", 1)
    repository.mark_chunk_uploaded(conn, "up_1", 0)
    repository.mark_chunk_uploaded(conn, "up_1", 1)

    session = repository.get_upload_session(conn, "up_1")
    assert session.uploaded_chunks == (0, 1)

    repository.delete_upload_session(conn, "up_1")
    assert repository.get_upload_session(conn, "up_1") is None


def test_find_upload_session_requires_matching_fingerprint(conn):
    repository.upsert_upload_session(
        conn,
        upload_id="up_1",
        bucket="relay",
        key="relay/a.txt",
        sha256=SHA,
        size_bytes=10,
        chunk_size=4,
    )
    assert (
        repository.find_upload_session(
            conn, bucket="relay", key="relay/a.txt", sha256=SHA, size_bytes=11
        )
        is None
    )
    assert (
        repository.find_upload_session(
            conn, bucket="relay", key="relay/b.txt", sha256=SHA, size_bytes=10
        )
        is None
    )
