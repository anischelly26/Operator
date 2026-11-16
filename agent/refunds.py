"""Order-linked refund authorization. All ledger amounts use integer cents."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json


class RefundPolicyError(ValueError):
    pass


def money_to_cents(value):
    if isinstance(value, bool):
        raise ValueError("Money must be a finite amount with at most two decimal places")
    try:
        amount = Decimal(str(value))
        cents = amount * 100
        if not amount.is_finite() or cents != cents.to_integral_value():
            raise ValueError("Money must have at most two decimal places")
        return int(cents)
    except (InvalidOperation, TypeError, OverflowError) as error:
        raise ValueError("Invalid monetary amount") from error


@dataclass(frozen=True)
class RefundAuthorization:
    customer_id: str
    order_id: str
    amount_cents: int
    paid_cents: int
    refunded_cents: int


def authorize_refund(conn, customer, payload, now=None):
    """Called before approval and again while holding the SQLite write lock."""
    amount_cents = money_to_cents(payload["amount"])
    if not 0 < amount_cents <= 15000:
        raise RefundPolicyError("Requested refund exceeds operator limit of $150.00 or is not positive.")
    order = next(
        (o for o in json.loads(customer["orders"]) if o.get("order_id") == payload["order_id"]), None,
    )
    if order is None:
        raise RefundPolicyError("Order does not belong to the ticket customer.")
    if order.get("status") not in {"PENDING", "PROCESSING", "SHIPPED", "DELIVERED"}:
        raise RefundPolicyError("This order state requires manual refund review.")
    try:
        purchased_at = datetime.fromisoformat(order["purchased_at"].replace("Z", "+00:00"))
        if purchased_at.tzinfo is None:
            raise ValueError("Purchase timestamp needs a timezone")
        now = now or datetime.now(timezone.utc)
        age = now - purchased_at
        paid_cents = money_to_cents(order["amount"])
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise RefundPolicyError("Purchase date or paid amount is missing or invalid; manual review required.") from error
    if age < timedelta(0) or age > timedelta(days=30):
        raise RefundPolicyError("Refund is outside the 30-day purchase window or the purchase date is in the future.")
    if paid_cents <= 0:
        raise RefundPolicyError("The recorded order has no refundable paid amount.")

    # Existing databases may contain unlinked financial actions. Do not guess
    # their order or backfill a ledger that could permit a duplicate refund.
    legacy = conn.execute(
        "SELECT 1 FROM executed_actions a JOIN tickets t ON t.ticket_id = a.ticket_id "
        "LEFT JOIN refund_ledger r ON r.idempotency_key = a.idempotency_key "
        "WHERE a.action_type = 'ISSUE_REFUND' AND t.customer_id = ? "
        "AND r.idempotency_key IS NULL LIMIT 1", (customer["customer_id"],),
    ).fetchone()
    if legacy:
        raise RefundPolicyError("Unlinked historical refunds exist for this customer; reconcile them before issuing another refund.")
    refunded_cents = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM refund_ledger WHERE customer_id = ? AND order_id = ?",
        (customer["customer_id"], payload["order_id"]),
    ).fetchone()[0]
    ceiling_cents = min(paid_cents, 15000)
    if refunded_cents + amount_cents > ceiling_cents:
        raise RefundPolicyError(
            f"Refund exceeds remaining operator-authorized amount ${(ceiling_cents - refunded_cents) / 100:.2f} for this order.",
        )
    return RefundAuthorization(customer["customer_id"], payload["order_id"], amount_cents, paid_cents, refunded_cents)
