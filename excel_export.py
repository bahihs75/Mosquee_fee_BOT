from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from domain import calculate_day_fee
from formatting import status_text


HEADERS = [
    "رقم الطلب",
    "اسم المستخدم المسجل",
    "رابط ملف تيليغرام",
    "المعرف الرقمي",
    "اسم المسجد / المهمة",
    "الولاية",
    "بداية المهمة",
    "نهاية المهمة",
    "مدة المهمة",
    "المبلغ المطلوب",
    "المبلغ المدفوع",
    "العملة",
    "طريقة الدفع",
    "ملاحظات الطلب",
    "ملاحظات الدفع",
    "من أكد الدفع",
    "تاريخ التأكيد",
    "الإصدار المعتمد",
]

HISTORY_HEADERS = [
    "حالة الطلب",
    "رقم الطلب",
    "اسم المستخدم وقت الطلب",
    "اسم تيليغرام",
    "المعرف الرقمي",
    "رابط ملف تيليغرام",
    "المسجد / المهمة",
    "الولاية",
    "البلدية",
    "بداية المهمة",
    "نهاية المهمة",
    "المدة المسجلة",
    "الأيام المحسوبة",
    "سعر اليوم (دج)",
    "رسم الأيام الإجمالي (دج)",
    "حساب رسم الأيام",
    "المسؤول",
    "نوع السجاد / الجودة",
    "المساحة المفرشة (م²)",
    "مع Feutre؟",
    "سعر المتر (دج)",
    "مبلغ السجاد (دج)",
    "تفاصيل مصاريف المهمة",
    "إجمالي مصاريف المهمة (دج)",
    "المبلغ المطلوب (دج)",
    "المبلغ المدفوع (دج)",
    "العملة",
    "طريقة الدفع",
    "ملاحظات الدفع",
    "المعرف الرقمي للصندوق",
    "تاريخ تأكيد الدفع",
    "مرحلة الاعتماد",
    "سبب الإلغاء",
    "سبب رفض / تعليق الدفع",
    "ملاحظات الطلب",
    "المرفقات",
    "الإصدار الحالي",
    "الإصدار المعتمد",
    "تاريخ الإنشاء",
    "آخر تحديث",
]


def _date(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else value.isoformat()
    return str(value)


def _number(value: Any) -> float | str:
    if value is None or value == "":
        return ""
    return float(Decimal(str(value)))


def _style_sheet(ws, headers: list[str]) -> None:
    ws.sheet_view.rightToLeft = True
    ws.freeze_panes = "A2"
    header_fill = PatternFill("solid", fgColor="1F4E78")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for index, header in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(index)].width = min(max(len(header) + 2, 14), 34)
    ws.auto_filter.ref = ws.dimensions
    ws.row_dimensions[1].height = 38


def _build_workbook(rows: list[dict[str, Any]], sheet_title: str, include_status: bool) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.sheet_view.rightToLeft = True
    ws.freeze_panes = "A2"
    headers = (["حالة الطلب"] if include_status else []) + HEADERS
    ws.append(headers)

    header_fill = PatternFill("solid", fgColor="1F4E78")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for row in rows:
        values = [
            row.get("public_id", ""),
            row.get("full_name", ""),
            row.get("profile_link", ""),
            row.get("user_id", ""),
            row.get("mosque_name", ""),
            row.get("wilaya", ""),
            _date(row.get("mission_start_date")),
            _date(row.get("mission_end_date")),
            row.get("duration_text", ""),
            _number(row.get("amount_requested", 0)),
            _number(row.get("paid_amount") or 0),
            row.get("currency", "DZD"),
            row.get("payment_method", ""),
            row.get("additional_details", ""),
            row.get("payment_note", ""),
            row.get("cashier_user_id", ""),
            _date(row.get("paid_at")),
            row.get("approved_version_no", row.get("version_no", 1)),
        ]
        if include_status:
            values.insert(0, status_text(str(row.get("status", ""))))
        ws.append(values)

    _style_sheet(ws, headers)

    meta = wb.create_sheet("تعريف الحقول")
    meta.append(["الحقل", "الشرح"])
    definitions = [
        ("رقم الطلب", "المعرف الدائم للطلب في قاعدة البيانات."),
        ("اسم المستخدم المسجل", "الاسم المحفوظ في الطلب وقت إنشائه؛ تغيير الاسم لا يغيّر الطلبات السابقة."),
        ("المبلغ المدفوع", "المبلغ الذي أكد الصندوق دفعه فعلياً."),
        ("الإصدار المعتمد", "نسخة الطلب التي اعتمدتها الإدارة قبل تحويله للصندوق."),
    ]
    if include_status:
        definitions.insert(0, ("حالة الطلب", "الحالة الحالية للطلب في سجل النظام."))
    for item in definitions:
        meta.append(item)
    _style_sheet(meta, ["الحقل", "الشرح"])
    meta.column_dimensions["A"].width = 28
    meta.column_dimensions["B"].width = 80

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def _history_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    exported: list[list[Any]] = []
    for row in rows:
        items = row.get("mission_expenses") or []
        items_total = sum((Decimal(str(item.get("amount") or 0)) for item in items), Decimal("0"))
        item_details = "\n".join(
            f"{item.get('description', '')} — {_number(item.get('amount', 0))} {item.get('currency', 'DZD')}"
            for item in items
        )
        duration_days = row.get("duration_days")
        saved_rate = row.get("day_fee_rate")
        day_rate = Decimal(str(saved_rate)) if saved_rate is not None else None
        if duration_days is None or day_rate is None:
            day_fee_value: float | str = ""
            day_fee_calculation = "غير محسوب (طلب سابق)"
        else:
            day_fee = calculate_day_fee(Decimal(str(duration_days)), day_rate)
            day_fee_value = float(day_fee)
            day_fee_calculation = f"{Decimal(str(duration_days)).normalize():f} × {day_rate.normalize():f} = {day_fee.normalize():f} دج"
        attachments = row.get("attachments") or []
        attachment_text = "\n".join(
            str(item.get("original_name") or item.get("media_type") or "مرفق")
            for item in attachments
        )
        has_feutre = "نعم" if row.get("has_feutre") else "لا"
        values = [
            status_text(str(row.get("status", ""))),
            row.get("public_id", ""),
            row.get("full_name", row.get("requester_name", "")),
            row.get("username", ""),
            row.get("user_id", ""),
            row.get("profile_link", ""),
            row.get("mosque_name", ""),
            row.get("wilaya", ""),
            row.get("baladiya", ""),
            _date(row.get("mission_start_date")),
            _date(row.get("mission_end_date")),
            row.get("duration_text", ""),
            _number(duration_days),
            _number(day_rate),
            day_fee_value,
            day_fee_calculation,
            row.get("responsable", ""),
            row.get("carpet_type", ""),
            _number(row.get("carpet_area")),
            has_feutre,
            _number(row.get("carpet_rate")),
            _number(row.get("carpet_amount")),
            item_details,
            float(items_total),
            _number(row.get("amount_requested")),
            _number(row.get("paid_amount")),
            row.get("currency", "DZD"),
            row.get("payment_method", ""),
            row.get("payment_note", ""),
            row.get("cashier_user_id", ""),
            _date(row.get("paid_at")),
            f"{int(row.get('approval_stage') or 0)}/3",
            row.get("cancel_reason", ""),
            row.get("rejection_reason", ""),
            row.get("additional_details", ""),
            attachment_text,
            row.get("version_no", 1),
            row.get("approved_version_no", ""),
            _date(row.get("created_at")),
            _date(row.get("updated_at")),
        ]
        if len(values) != len(HISTORY_HEADERS):
            raise ValueError("History export headers and values do not match")
        exported.append(values)
    return exported


def build_confirmed_workbook(rows: list[dict[str, Any]]) -> BytesIO:
    return _build_workbook(rows, "الطلبات المؤكدة", include_status=False)


def build_history_workbook(rows: list[dict[str, Any]]) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "سجل الطلبات"
    ws.append(HISTORY_HEADERS)
    for values in _history_rows(rows):
        ws.append(values)
    _style_sheet(ws, HISTORY_HEADERS)

    details = wb.create_sheet("تفاصيل مصاريف المهمة")
    detail_headers = ["رقم الطلب", "الإصدار", "نوع المصروف", "الوصف", "المبلغ", "العملة"]
    details.append(detail_headers)
    for row in rows:
        for item in row.get("mission_expenses") or []:
            details.append([
                row.get("public_id", ""),
                row.get("version_no", 1),
                item.get("item_type", "mission"),
                item.get("description", ""),
                _number(item.get("amount")),
                item.get("currency", row.get("currency", "DZD")),
            ])
    _style_sheet(details, detail_headers)

    meta = wb.create_sheet("تعريف الحقول")
    meta.append(["الحقل", "الشرح"])
    definitions = [
        ("الأيام المحسوبة", "عدد الأيام الذي أدخله المستخدم بمضاعفات نصف يوم."),
        ("سعر اليوم", "700 دج لكل يوم؛ نصف يوم = 350 دج."),
        ("حساب رسم الأيام", "عدد الأيام × سعر اليوم. الطلبات السابقة لا يُعاد احتسابها بأثر رجعي."),
        ("مبلغ السجاد", "المساحة المفرشة × سعر المتر المحفوظ في الطلب."),
        ("إجمالي مصاريف المهمة", "مجموع البنود المفصلة في ورقة «تفاصيل مصاريف المهمة»."),
        ("المبلغ المطلوب", "إجمالي الطلب المحفوظ؛ الطلبات السابقة تبقى بمبالغها الأصلية."),
        ("اسم المستخدم وقت الطلب", "الاسم المسجل لهذا الطلب عند إنشائه، ولا يتغير بتغيير اسم الحساب لاحقاً."),
    ]
    for item in definitions:
        meta.append(item)
    _style_sheet(meta, ["الحقل", "الشرح"])
    meta.column_dimensions["A"].width = 30
    meta.column_dimensions["B"].width = 80

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output
