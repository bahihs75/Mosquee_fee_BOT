from pathlib import Path

import pytest

from domain import DomainError, Status, transition


SCHEMA = Path(__file__).parents[1].joinpath("schema.sql").read_text(encoding="utf-8")


def test_schema_preserves_registered_users_and_profile_links():
    assert "full_name TEXT NOT NULL" in SCHEMA
    assert "profile_link TEXT NOT NULL" in SCHEMA
    assert "users_full_name_not_blank" in SCHEMA


def test_schema_preserves_request_versions_and_audit_events():
    assert "CREATE TABLE IF NOT EXISTS request_versions" in SCHEMA
    assert "CREATE TABLE IF NOT EXISTS workflow_events" in SCHEMA
    assert "target_user_id" in SCHEMA
    assert "created_by" in SCHEMA


def test_schema_preserves_delivery_failure_records():
    assert "CREATE TABLE IF NOT EXISTS delivery_messages" in SCHEMA
    assert "last_error TEXT" in SCHEMA
    assert "attempts INTEGER" in SCHEMA


@pytest.mark.parametrize("status,action", [
    (Status.PAID_CONFIRMED, "approve_admin"),
    (Status.PAID_CONFIRMED, "cancel_admin"),
    (Status.PAID_CONFIRMED, "confirm_payment"),
    (Status.CANCELLED_BY_ADMIN, "approve_admin"),
    (Status.CANCELLED_BY_USER, "approve_admin"),
])
def test_closed_cycles_cannot_be_reopened_by_an_old_button(status, action):
    with pytest.raises(DomainError):
        transition(status, action)


def test_partial_payment_can_only_finish_after_cashier_confirmation():
    with pytest.raises(DomainError):
        transition(Status.PARTIALLY_PAID, "approve_admin")
    assert transition(Status.PARTIALLY_PAID, "confirm_payment").to_status == Status.PAID_CONFIRMED
