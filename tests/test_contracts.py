from pathlib import Path

import pytest

from domain import DomainError, Status, transition


SCHEMA = Path(__file__).parents[1].joinpath("schema.sql").read_text(encoding="utf-8")
APP_SOURCE = Path(__file__).parents[1].joinpath("app.py").read_text(encoding="utf-8")
DB_SOURCE = Path(__file__).parents[1].joinpath("db.py").read_text(encoding="utf-8")
DOMAIN_SOURCE = Path(__file__).parents[1].joinpath("domain.py").read_text(encoding="utf-8")


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


def test_history_export_is_admin_only_registered_and_omits_cancelled_requests():
    assert 'CommandHandler("history", history_command)' in APP_SOURCE
    assert "async def history_command" in APP_SOURCE
    history_handler = APP_SOURCE[APP_SOURCE.index("async def history_command"):]
    assert "if not await is_admin_member(update, context):" in history_handler
    assert "هذا الأمر متاح فقط لأعضاء مجموعة الإدارة المضبوطة" in history_handler
    assert 'logger.exception("فشل تصدير سجل الطلبات")' in history_handler
    assert "WHERE r.status NOT IN ('cancelled_by_admin','cancelled_by_user')" in DB_SOURCE


def test_day_fee_is_half_day_based_and_added_to_existing_components():
    assert "duration_days NUMERIC(7,2)" in SCHEMA
    assert "day_fee_rate NUMERIC(14,2)" in SCHEMA
    assert "expense_requests_day_fee_rate_positive_check" in SCHEMA
    assert "expense_requests_duration_days_half_day_check" in SCHEMA
    assert 'DAILY_MISSION_RATE = Decimal("700")' in DOMAIN_SOURCE
    assert "calculate_day_fee(duration_days, day_fee_rate)" in DOMAIN_SOURCE
    assert "calculate_day_fee(row.get(\"duration_days\")" in DB_SOURCE


def test_user_name_change_is_recorded_per_request_and_applies_to_new_requests():
    assert 'callback_data="u:change_name"' in APP_SOURCE
    assert 'action == "change_name"' in APP_SOURCE
    assert "requester_name" in SCHEMA
    assert "requester_name,mosque_name" in DB_SOURCE
    assert "COALESCE(NULLIF(r.requester_name,''),u.full_name) AS full_name" in DB_SOURCE
    assert "ستبقى الطلبات السابقة باسمها المسجل وقت إنشائها" in APP_SOURCE


def test_history_export_loads_current_expense_items_and_attachments():
    assert "تفاصيل مصاريف المهمة" in (Path(__file__).parents[1] / "excel_export.py").read_text(encoding="utf-8")
    assert "FROM expense_items WHERE request_id=ANY(%s)" in DB_SOURCE
    assert "FROM attachments " in DB_SOURCE
