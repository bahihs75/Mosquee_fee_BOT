from decimal import Decimal

from openpyxl import load_workbook

from excel_export import build_confirmed_workbook, build_history_workbook


def test_confirmed_workbook_contains_financial_columns():
    stream = build_confirmed_workbook([
        {
            "public_id": "EXP-2026-000001",
            "full_name": "مستخدم تجريبي",
            "profile_link": "tg://user?id=123",
            "user_id": 123,
            "mosque_name": "مسجد الاختبار",
            "wilaya": "الجزائر",
            "duration_text": "3 أيام",
            "amount_requested": Decimal("100.00"),
            "paid_amount": Decimal("100.00"),
            "currency": "DZD",
            "payment_method": "نقداً",
            "additional_details": "",
            "payment_note": "تم التسليم",
            "cashier_user_id": 456,
            "approved_version_no": 1,
        }
    ])
    wb = load_workbook(stream)
    assert "الطلبات المؤكدة" in wb.sheetnames
    headers = [cell.value for cell in wb["الطلبات المؤكدة"][1]]
    assert "رابط ملف تيليغرام" in headers
    assert "المبلغ المدفوع" in headers
    assert wb["الطلبات المؤكدة"]["A2"].value == "EXP-2026-000001"


def test_history_workbook_includes_status_id_and_legacy_requests_without_recalculation():
    stream = build_history_workbook([
        {
            "status": "submitted",
            "public_id": "EXP-2026-000002",
            "full_name": "مستخدم تجريبي",
            "user_id": 123,
            "amount_requested": Decimal("100.00"),
            "paid_amount": None,
            "duration_text": "يومان",
            "duration_days": Decimal("2"),
        }
    ])
    wb = load_workbook(stream)
    ws = wb["سجل الطلبات"]
    headers = {cell.value: cell.column for cell in ws[1]}
    assert ws["A2"].value == "بانتظار مراجعة الإدارة"
    assert ws["B2"].value == "EXP-2026-000002"
    assert ws.cell(2, headers["حساب رسم الأيام"]).value == "غير محسوب (طلب سابق)"
    assert ws.cell(2, headers["سعر اليوم (دج)"]).value is None
    assert ws.cell(2, headers["رسم الأيام الإجمالي (دج)"]).value is None
    assert ws.cell(2, headers["المبلغ المدفوع (دج)"]).value is None


def test_history_workbook_shows_fee_formula_and_separate_expense_lines():
    stream = build_history_workbook([
        {
            "status": "submitted",
            "public_id": "EXP-2026-000003",
            "full_name": "مستخدم تجريبي",
            "username": "user_test",
            "user_id": 123,
            "profile_link": "tg://user?id=123",
            "mosque_name": "مسجد الاختبار",
            "wilaya": "الجزائر",
            "baladiya": "باب الوادي",
            "duration_text": "1.5 يوم",
            "duration_days": Decimal("1.50"),
            "day_fee_rate": Decimal("700.00"),
            "responsable": "مسؤول المهمة",
            "carpet_type": "صوف",
            "carpet_area": Decimal("10.00"),
            "has_feutre": True,
            "carpet_rate": Decimal("20.00"),
            "carpet_amount": Decimal("200.00"),
            "mission_expenses": [
                {"item_type": "mission", "description": "نقل", "amount": Decimal("50.00"), "currency": "DZD"}
            ],
            "amount_requested": Decimal("1300.00"),
            "currency": "DZD",
            "version_no": 2,
            "attachments": [{"original_name": "receipt.jpg", "media_type": "photo"}],
        }
    ])
    wb = load_workbook(stream)
    ws = wb["سجل الطلبات"]
    headers = {cell.value: cell.column for cell in ws[1]}
    assert ws.cell(2, headers["الأيام المحسوبة"]).value == 1.5
    assert ws.cell(2, headers["سعر اليوم (دج)"]).value == 700
    assert ws.cell(2, headers["رسم الأيام الإجمالي (دج)"]).value == 1050
    assert ws.cell(2, headers["حساب رسم الأيام"]).value == "1.5 × 700 = 1050 دج"
    assert ws.cell(2, headers["البلدية"]).value == "باب الوادي"
    assert ws.cell(2, headers["نوع السجاد / الجودة"]).value == "صوف"
    assert ws.cell(2, headers["مبلغ السجاد (دج)"]).value == 200
    assert ws.cell(2, headers["إجمالي مصاريف المهمة (دج)"]).value == 50
    assert ws.cell(2, headers["المرفقات"]).value == "receipt.jpg"

    details = wb["تفاصيل مصاريف المهمة"]
    assert details["A2"].value == "EXP-2026-000003"
    assert details["D2"].value == "نقل"
    assert details["E2"].value == 50
