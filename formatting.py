from datetime import date, datetime
from decimal import Decimal
from html import escape
from typing import Any

from domain import Status, display_profile_html

STATUS_LABELS = {
    Status.DRAFT.value: "مسودة",
    Status.SUBMITTED.value: "بانتظار مراجعة الإدارة",
    Status.CHANGES_REQUESTED.value: "مطلوب تعديل من المستخدم",
    Status.RESUBMITTED.value: "أُعيد للإدارة بعد التعديل",
    Status.APPROVED_BY_ADMIN.value: "اعتمدتها الإدارة – بانتظار اعتماد الجيرون/الأجوان والصندوق",
    Status.CASHIER_REVIEW.value: "قيد مراجعة الصندوق",
    Status.PAID_CONFIRMED.value: "تم تأكيد الدفع",
    Status.PAYMENT_REJECTED.value: "علّقها الصندوق / رفض الدفع",
    Status.PARTIALLY_PAID.value: "دفع جزئي",
    Status.CANCELLED_BY_ADMIN.value: "ألغتها الإدارة",
    Status.CANCELLED_BY_USER.value: "ألغاه المستخدم",
    Status.EXPIRED.value: "متأخر عن المعالجة",
    Status.REOPENED.value: "أعيد فتحه",
}


def money(value: Any, currency: str = "DZD") -> str:
    return f"{Decimal(value or 0):,.2f} {escape(currency)}"


def date_text(value: Any) -> str:
    if not value:
        return "غير محدد"
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return escape(str(value))


def status_text(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def request_summary(row: dict[str, Any], include_actions: bool = False, internal: bool = False) -> str:
    applicant = display_profile_html(row.get("full_name", "غير معروف"), int(row["user_id"]), row.get("username"))
    text = (
        f"<b>طلب استرجاع مصاريف {escape(str(row.get('public_id', '')))}</b>\n\n"
        f"<b>المستخدم:</b> {applicant}\n"
        f"<b>المسجد / المهمة:</b> {escape(str(row.get('mosque_name', '')))}\n"
        f"<b>الولاية:</b> {escape(str(row.get('wilaya', '')))}\n"
        f"<b>البلدية:</b> {escape(str(row.get('baladiya', '')) or 'غير محددة')}\n"
        f"<b>مدة المهمة:</b> {escape(str(row.get('duration_text', '')))}\n"
        f"<b>المسؤول:</b> {escape(str(row.get('responsable', '')) or 'غير محدد')}\n"
        f"<b>نوع السجاد:</b> {escape(str(row.get('carpet_type', '')) or 'غير محدد')}\n"
        f"<b>المساحة المفرشة:</b> {escape(str(row.get('carpet_area', '')) or 'غير محددة')} م²\n"
        f"<b>Feutre:</b> {'نعم' if row.get('has_feutre') else 'لا'}\n"
        f"<b>المرحلة:</b> {int(row.get('approval_stage') or 0)}/3\n"
        f"<b>المبلغ الإجمالي:</b> {money(row.get('amount_requested', 0), row.get('currency', 'DZD'))}\n"
        f"<b>الحالة:</b> {escape(status_text(str(row.get('status', ''))))}\n"
        f"<b>النسخة:</b> {row.get('version_no', 1)}\n"
    )
    if internal:
        text += f"<b>سعر المتر:</b> {money(row.get('carpet_rate', 0), row.get('currency', 'DZD'))}\n"
        text += f"<b>مبلغ السجاد:</b> {money(row.get('carpet_amount', 0), row.get('currency', 'DZD'))}\n"
    items = row.get("mission_expenses") or []
    if items:
        text += "<b>مصاريف المهمة التفصيلية:</b>\n"
        for index, item in enumerate(items, 1):
            text += f"{index}. {escape(str(item.get('description', '')))} — {money(item.get('amount', 0), item.get('currency', 'DZD'))}\n"
    if row.get("mission_start_date") or row.get("mission_end_date"):
        text += f"<b>الفترة:</b> {date_text(row.get('mission_start_date'))} → {date_text(row.get('mission_end_date'))}\n"
    if row.get("additional_details"):
        text += f"<b>ملاحظات:</b> {escape(str(row['additional_details']))}\n"
    if row.get("cancel_reason"):
        text += f"<b>سبب الإلغاء:</b> {escape(str(row['cancel_reason']))}\n"
    if row.get("rejection_reason"):
        text += f"<b>ملاحظة الصندوق:</b> {escape(str(row['rejection_reason']))}\n"
    if row.get("paid_amount") is not None:
        text += f"<b>المبلغ المدفوع:</b> {money(row['paid_amount'], row.get('currency', 'DZD'))}\n"
    if row.get("payment_method"):
        text += f"<b>طريقة الدفع:</b> {escape(str(row['payment_method']))}\n"
    if row.get("payment_note"):
        text += f"<b>ملاحظة الدفع:</b> {escape(str(row['payment_note']))}\n"
    if include_actions:
        text += "\nاختر الإجراء من الأزرار أسفل الرسالة. كل إجراء يُسجّل باسم منفذه."
    return text


def user_summary(row: dict[str, Any]) -> str:
    profile = display_profile_html(row["full_name"], int(row["telegram_user_id"]), row.get("username"))
    return (
        f"{profile}\n"
        f"المعرف الرقمي: <code>{row['telegram_user_id']}</code>\n"
        f"الحالة: {escape(str(row.get('status', 'active')))}\n"
        f"تاريخ التسجيل: {date_text(row.get('registered_at'))}"
    )
