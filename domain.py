from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Iterable
from urllib.parse import quote


class Status(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    CHANGES_REQUESTED = "changes_requested"
    RESUBMITTED = "resubmitted"
    APPROVED_BY_ADMIN = "approved_by_admin"
    CASHIER_REVIEW = "cashier_review"
    PAID_CONFIRMED = "paid_confirmed"
    PAYMENT_REJECTED = "payment_rejected"
    PARTIALLY_PAID = "partially_paid"
    CANCELLED_BY_ADMIN = "cancelled_by_admin"
    CANCELLED_BY_USER = "cancelled_by_user"
    EXPIRED = "expired"
    REOPENED = "reopened"


@dataclass(frozen=True)
class ExpenseInput:
    mosque_name: str
    wilaya: str
    duration_text: str
    amount_requested: Decimal
    currency: str
    additional_details: str = ""
    mission_start_date: date | None = None
    mission_end_date: date | None = None
    baladiya: str = ""
    responsable: str = ""
    carpet_type: str = ""
    carpet_area: Decimal | None = None
    has_feutre: bool = False
    carpet_rate: Decimal | None = None
    carpet_amount: Decimal | None = None
    mission_expenses: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Transition:
    from_status: Status
    to_status: Status
    event_type: str


class DomainError(ValueError):
    """خطأ قابل للعرض للمستخدم أو لتسجيله في سجل التدقيق."""


ADMIN_GROUP_STATUSES = {Status.SUBMITTED, Status.RESUBMITTED, Status.PAYMENT_REJECTED, Status.EXPIRED, Status.REOPENED}
USER_EDITABLE_STATUSES = {Status.CHANGES_REQUESTED}
USER_CANCELABLE_STATUSES = {Status.DRAFT, Status.SUBMITTED, Status.CHANGES_REQUESTED}
CLOSED_STATUSES = {Status.PAID_CONFIRMED, Status.CANCELLED_BY_ADMIN, Status.CANCELLED_BY_USER}


def normalize_text(value: str, field_name: str, min_length: int = 2, max_length: int = 1000) -> str:
    value = " ".join((value or "").strip().split())
    if len(value) < min_length:
        raise DomainError(f"حقل {field_name} مطلوب ويجب ألا يكون قصيراً جداً.")
    if len(value) > max_length:
        raise DomainError(f"حقل {field_name} يتجاوز الحد المسموح.")
    return value


def parse_amount(raw: str | Decimal | int | float, currency: str = "DZD") -> Decimal:
    try:
        amount = Decimal(str(raw).replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DomainError("المبلغ يجب أن يكون رقماً صحيحاً.") from exc
    if amount <= 0:
        raise DomainError("المبلغ يجب أن يكون أكبر من الصفر.")
    if len(currency.strip()) not in range(3, 8):
        raise DomainError("العملة غير صالحة.")
    return amount


def parse_positive_number(raw: str | Decimal | int | float, field_name: str) -> Decimal:
    try:
        value = Decimal(str(raw).replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DomainError(f"{field_name} يجب أن يكون رقماً.") from exc
    if value <= 0:
        raise DomainError(f"{field_name} يجب أن يكون أكبر من الصفر.")
    return value


def calculate_carpet_amount(area: Decimal, has_feutre: bool, rate_without: Decimal = Decimal("15"), rate_with: Decimal = Decimal("20")) -> Decimal:
    rate = rate_with if has_feutre else rate_without
    return (Decimal(area) * Decimal(rate)).quantize(Decimal("0.01"))


def normalize_mission_expenses(items: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    for item in items:
        description = normalize_text(str(item.get("description", "")), "وصف المصروف", 2, 300)
        amount = parse_amount(item.get("amount", 0))
        normalized.append({"description": description, "amount": amount, "currency": str(item.get("currency", "DZD")).upper()})
    return tuple(normalized)


def mission_expenses_total(items: Iterable[dict[str, Any]]) -> Decimal:
    return sum((Decimal(item["amount"]) for item in items), Decimal("0")).quantize(Decimal("0.01"))


def validate_expense(data: ExpenseInput) -> ExpenseInput:
    mosque_name = normalize_text(data.mosque_name, "اسم المسجد أو المهمة", 2, 200)
    wilaya = normalize_text(data.wilaya, "الولاية", 2, 100)
    duration_text = normalize_text(data.duration_text, "مدة المهمة", 1, 100)
    currency = normalize_text(data.currency.upper(), "العملة", 3, 7).upper()
    details = (data.additional_details or "").strip()
    if len(details) > 4000:
        raise DomainError("الملاحظات طويلة جداً.")
    amount = parse_amount(data.amount_requested, currency)
    if data.mission_start_date and data.mission_end_date:
        if data.mission_end_date < data.mission_start_date:
            raise DomainError("تاريخ نهاية المهمة لا يمكن أن يسبق تاريخ البداية.")
        if (data.mission_end_date - data.mission_start_date).days > 366:
            raise DomainError("مدة المهمة تتجاوز سنة، يرجى مراجعة التواريخ.")

    baladiya = normalize_text(data.baladiya, "البلدية", 2, 100) if data.baladiya else ""
    responsable = normalize_text(data.responsable, "المسؤول", 2, 200) if data.responsable else ""
    carpet_type = normalize_text(data.carpet_type, "نوع السجاد", 2, 200) if data.carpet_type else ""
    items = normalize_mission_expenses(data.mission_expenses)
    carpet_area = carpet_rate = carpet_amount = None
    if data.carpet_area is not None:
        carpet_area = parse_positive_number(data.carpet_area, "المساحة المفرشة")
        if not carpet_type:
            raise DomainError("نوع السجاد مطلوب.")
        carpet_rate = parse_positive_number(data.carpet_rate or (Decimal("20") if data.has_feutre else Decimal("15")), "سعر المتر")
        carpet_amount = parse_positive_number(data.carpet_amount or carpet_area * carpet_rate, "مبلغ السجاد")
        expected_total = (carpet_amount + mission_expenses_total(items)).quantize(Decimal("0.01"))
        if amount != expected_total:
            raise DomainError("المبلغ الإجمالي يجب أن يساوي مبلغ السجاد مضافاً إليه مجموع مصاريف المهمة.")

    return ExpenseInput(
        mosque_name=mosque_name,
        wilaya=wilaya,
        duration_text=duration_text,
        amount_requested=amount,
        currency=currency,
        additional_details=details,
        mission_start_date=data.mission_start_date,
        mission_end_date=data.mission_end_date,
        baladiya=baladiya,
        responsable=responsable,
        carpet_type=carpet_type,
        carpet_area=carpet_area,
        has_feutre=bool(data.has_feutre),
        carpet_rate=carpet_rate,
        carpet_amount=carpet_amount,
        mission_expenses=items,
    )


def profile_link(user_id: int, username: str | None) -> str:
    if username:
        return f"https://t.me/{quote(username.lstrip('@'))}"
    return f"tg://user?id={user_id}"


def display_profile_html(full_name: str, user_id: int, username: str | None) -> str:
    from html import escape
    return f'<a href="{escape(profile_link(user_id, username), quote=True)}">{escape(full_name)}</a>'


def transition(from_status: str | Status, action: str) -> Transition:
    current = Status(from_status)
    transitions: dict[tuple[Status, str], Transition] = {
        (Status.SUBMITTED, "request_changes"): Transition(current, Status.CHANGES_REQUESTED, "changes_requested"),
        (Status.RESUBMITTED, "request_changes"): Transition(current, Status.CHANGES_REQUESTED, "changes_requested"),
        (Status.SUBMITTED, "admin_edit"): Transition(current, Status.SUBMITTED, "admin_edited"),
        (Status.RESUBMITTED, "admin_edit"): Transition(current, Status.RESUBMITTED, "admin_edited"),
        (Status.SUBMITTED, "approve_admin"): Transition(current, Status.APPROVED_BY_ADMIN, "admin_approved"),
        (Status.RESUBMITTED, "approve_admin"): Transition(current, Status.APPROVED_BY_ADMIN, "admin_approved"),
        (Status.PAYMENT_REJECTED, "reopen"): Transition(current, Status.REOPENED, "reopened"),
        (Status.EXPIRED, "reopen"): Transition(current, Status.REOPENED, "reopened"),
        (Status.REOPENED, "approve_admin"): Transition(current, Status.APPROVED_BY_ADMIN, "admin_approved"),
        (Status.APPROVED_BY_ADMIN, "cashier_review"): Transition(current, Status.CASHIER_REVIEW, "cashier_review_started"),
        (Status.APPROVED_BY_ADMIN, "confirm_payment"): Transition(current, Status.PAID_CONFIRMED, "payment_confirmed"),
        (Status.CASHIER_REVIEW, "confirm_payment"): Transition(current, Status.PAID_CONFIRMED, "payment_confirmed"),
        (Status.APPROVED_BY_ADMIN, "partial_payment"): Transition(current, Status.PARTIALLY_PAID, "partial_payment_confirmed"),
        (Status.CASHIER_REVIEW, "partial_payment"): Transition(current, Status.PARTIALLY_PAID, "partial_payment_confirmed"),
        (Status.PARTIALLY_PAID, "partial_payment"): Transition(current, Status.PARTIALLY_PAID, "partial_payment_confirmed"),
        (Status.PARTIALLY_PAID, "confirm_payment"): Transition(current, Status.PAID_CONFIRMED, "payment_confirmed"),
        (Status.APPROVED_BY_ADMIN, "reject_payment"): Transition(current, Status.PAYMENT_REJECTED, "payment_rejected"),
        (Status.CASHIER_REVIEW, "reject_payment"): Transition(current, Status.PAYMENT_REJECTED, "payment_rejected"),
        (Status.DRAFT, "cancel_user"): Transition(current, Status.CANCELLED_BY_USER, "user_cancelled"),
        (Status.SUBMITTED, "cancel_user"): Transition(current, Status.CANCELLED_BY_USER, "user_cancelled"),
        (Status.CHANGES_REQUESTED, "cancel_user"): Transition(current, Status.CANCELLED_BY_USER, "user_cancelled"),
    }
    if action == "cancel_admin" and current in {Status.SUBMITTED, Status.RESUBMITTED, Status.PAYMENT_REJECTED, Status.EXPIRED, Status.REOPENED}:
        return Transition(current, Status.CANCELLED_BY_ADMIN, "admin_cancelled")
    try:
        return transitions[(current, action)]
    except KeyError as exc:
        raise DomainError(f"لا يمكن تنفيذ الإجراء {action} عندما تكون الحالة {current}.") from exc


def validate_payment(requested: Decimal, paid: Decimal, allow_partial: bool) -> str:
    paid = Decimal(paid).quantize(Decimal("0.01"))
    requested = Decimal(requested).quantize(Decimal("0.01"))
    if paid <= 0:
        raise DomainError("المبلغ المدفوع يجب أن يكون أكبر من الصفر.")
    if paid == requested:
        return "full"
    if paid < requested and allow_partial:
        return "partial"
    if paid < requested:
        raise DomainError("المبلغ المدفوع أقل من المبلغ المعتمد. يجب تفعيل الدفع الجزئي أو إدخال المبلغ الصحيح.")
    raise DomainError("لا يمكن تأكيد مبلغ مدفوع أكبر من المبلغ المعتمد.")


def validate_cumulative_payment(requested: Decimal, already_paid: Decimal, new_payment: Decimal, allow_partial: bool) -> tuple[str, Decimal]:
    new_payment = Decimal(new_payment).quantize(Decimal("0.01"))
    already_paid = Decimal(already_paid or 0).quantize(Decimal("0.01"))
    if new_payment <= 0:
        raise DomainError("المبلغ المدفوع يجب أن يكون أكبر من الصفر.")
    total = already_paid + new_payment
    return validate_payment(requested, total, allow_partial), total


def snapshot_from_input(data: ExpenseInput) -> dict[str, Any]:
    validated = validate_expense(data)
    return {
        "mosque_name": validated.mosque_name,
        "wilaya": validated.wilaya,
        "baladiya": validated.baladiya,
        "mission_start_date": validated.mission_start_date.isoformat() if validated.mission_start_date else None,
        "mission_end_date": validated.mission_end_date.isoformat() if validated.mission_end_date else None,
        "duration_text": validated.duration_text,
        "responsable": validated.responsable,
        "carpet_type": validated.carpet_type,
        "carpet_area": str(validated.carpet_area) if validated.carpet_area is not None else None,
        "has_feutre": validated.has_feutre,
        "carpet_rate": str(validated.carpet_rate) if validated.carpet_rate is not None else None,
        "carpet_amount": str(validated.carpet_amount) if validated.carpet_amount is not None else None,
        "mission_expenses": [{"description": item["description"], "amount": str(item["amount"]), "currency": item.get("currency", "DZD")} for item in validated.mission_expenses],
        "amount_requested": str(validated.amount_requested),
        "currency": validated.currency,
        "additional_details": validated.additional_details,
    }
