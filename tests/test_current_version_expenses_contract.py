from pathlib import Path


ROOT = Path(__file__).parents[1]
DB_SOURCE = (ROOT / "db.py").read_text(encoding="utf-8")
FORMATTING_SOURCE = (ROOT / "formatting.py").read_text(encoding="utf-8")


def test_request_reads_only_current_version_expenses():
    assert "WHERE request_id=%s AND version_no=%s ORDER BY id" in DB_SOURCE
    assert "SELECT * FROM expense_items WHERE request_id=%s ORDER BY id" not in DB_SOURCE


def test_admin_approval_status_waits_only_for_cashier_confirmation():
    assert 'Status.APPROVED_BY_ADMIN.value: "اعتمدتها الإدارة – بانتظار تأكيد الصندوق"' in FORMATTING_SOURCE
    assert "اعتماد الجيرون/الأجوان والصندوق" not in FORMATTING_SOURCE
