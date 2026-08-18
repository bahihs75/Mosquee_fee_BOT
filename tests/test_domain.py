from decimal import Decimal

import pytest

from domain import DomainError, Status, parse_amount, transition, validate_cumulative_payment, validate_payment


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
