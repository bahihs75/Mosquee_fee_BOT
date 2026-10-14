from decimal import Decimal

import pytest

from domain import DomainError, ExpenseInput, Status, calculate_day_fee, parse_amount, parse_day_equivalents, transition, validate_cumulative_payment, validate_expense, validate_payment


def test_admin_can_request_changes_from_submitted():
    tx = transition(Status.SUBMITTED, "request_changes")
    assert tx.to_status == Status.CHANGES_REQUESTED


def test_admin_approval_is_not_allowed_after_payment():
    with pytest.raises(DomainError):
        transition(Status.PAID_CONFIRMED, "approve_admin")


def test_partial_payment_requires_explicit_setting():
    with pytest.raises(DomainError):
        validate_payment(Decimal("100.00"), Decimal("80.00"), allow_partial=False)
    assert validate_payment(Decimal("100.00"), Decimal("80.00"), allow_partial=True) == "partial"


def test_overpayment_is_always_rejected():
    with pytest.raises(DomainError):
        validate_payment(Decimal("100.00"), Decimal("100.01"), allow_partial=True)


def test_cumulative_partial_payment_can_finish_cycle():
    kind, total = validate_cumulative_payment(Decimal("100.00"), Decimal("40.00"), Decimal("60.00"), allow_partial=True)
    assert kind == "full"
    assert total == Decimal("100.00")


def test_cumulative_partial_payment_cannot_exceed_cycle():
    with pytest.raises(DomainError):
        validate_cumulative_payment(Decimal("100.00"), Decimal("80.00"), Decimal("30.00"), allow_partial=True)


def test_amount_parser_rejects_zero():
    with pytest.raises(DomainError):
        parse_amount("0")


def test_transition_is_explicit_for_cancellation():
    tx = transition(Status.SUBMITTED, "cancel_admin")
    assert tx.to_status == Status.CANCELLED_BY_ADMIN


def test_day_equivalents_support_full_and_half_days_at_700_dzd_per_day():
    assert parse_day_equivalents("0.5") == Decimal("0.50")
    assert calculate_day_fee(Decimal("0.5")) == Decimal("350.00")
    assert calculate_day_fee(Decimal("1.5")) == Decimal("1050.00")


@pytest.mark.parametrize("value", ["0", "-0.5", "0.25", "0.504", "not a number", "NaN", "Infinity"])
def test_day_equivalents_reject_invalid_or_non_half_day_values(value):
    with pytest.raises(DomainError):
        parse_day_equivalents(value)


def test_validated_request_total_adds_day_fee_to_carpet_and_itemized_expenses():
    expense = ExpenseInput(
        mosque_name="مسجد الاختبار",
        wilaya="الجزائر",
        baladiya="باب الوادي",
        duration_text="1.5 يوم",
        duration_days=Decimal("1.5"),
        responsable="مسؤول المهمة",
        carpet_type="صوف",
        carpet_area=Decimal("10"),
        has_feutre=False,
        carpet_rate=Decimal("15"),
        carpet_amount=Decimal("150"),
        mission_expenses=({"description": "نقل", "amount": Decimal("100"), "currency": "DZD"},),
        amount_requested=Decimal("1300"),
        currency="DZD",
    )
    validated = validate_expense(expense)
    assert validated.amount_requested == Decimal("1300.00")
    assert validated.duration_days == Decimal("1.50")


def test_validated_request_rejects_total_that_omits_daily_fee():
    expense = ExpenseInput(
        mosque_name="مسجد الاختبار",
        wilaya="الجزائر",
        duration_text="0.5 يوم",
        duration_days=Decimal("0.5"),
        carpet_area=Decimal("10"),
        has_feutre=False,
        carpet_rate=Decimal("15"),
        carpet_amount=Decimal("150"),
        amount_requested=Decimal("150"),
        currency="DZD",
    )
    with pytest.raises(DomainError):
        validate_expense(expense)
