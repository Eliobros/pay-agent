"""Idempotency registry: claim, replay, conflict and stale recovery."""

from payagent import db, idempotency


def test_claim_complete_replay(database):
    assert idempotency.claim("store_demo", "capture", "o1")[0] == idempotency.CLAIMED
    assert idempotency.claim("store_demo", "capture", "o1")[0] == idempotency.BUSY

    idempotency.complete("store_demo", "capture", "o1", {"status": "COMPLETED"})
    state, cached = idempotency.claim("store_demo", "capture", "o1")
    assert state == idempotency.REPLAY
    assert cached == {"status": "COMPLETED"}


def test_same_key_with_a_different_payload_is_a_conflict(database):
    first = idempotency.fingerprint({"product": "p5", "quantity": 1})
    second = idempotency.fingerprint({"product": "p1", "quantity": 5})
    assert idempotency.claim("store_demo", "order_create", "k", first)[0] == idempotency.CLAIMED
    assert idempotency.claim("store_demo", "order_create", "k", second)[0] == idempotency.CONFLICT
    assert idempotency.claim("store_demo", "order_create", "k", first)[0] == idempotency.BUSY


def test_keys_are_scoped_per_store_and_per_scope(database):
    assert idempotency.claim("store_demo", "refund", "k")[0] == idempotency.CLAIMED
    assert idempotency.claim("store_other", "refund", "k")[0] == idempotency.CLAIMED
    assert idempotency.claim("store_demo", "capture", "k")[0] == idempotency.CLAIMED


def test_failed_operation_releases_the_key(database):
    idempotency.claim("store_demo", "capture", "o1")
    idempotency.fail("store_demo", "capture", "o1")
    assert idempotency.claim("store_demo", "capture", "o1")[0] == idempotency.CLAIMED


def test_stale_in_flight_key_can_be_reclaimed(database):
    idempotency.claim("store_demo", "capture", "o1")
    db.execute("UPDATE idempotency_keys SET updated_at = '2000-01-01T00:00:00+00:00'")
    assert idempotency.claim("store_demo", "capture", "o1")[0] == idempotency.CLAIMED
