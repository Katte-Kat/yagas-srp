"""Which approved requests have been paid?

The wallet booking that paid a request names the request's inbox number in its reason ("SRP #123", the inbox
hands out that text to copy). yagas_finance reads the number back out of its wallet journal; this turns what it
finds into one small state per request for the inbox pages. Everything here is optional and must never get in
the way of the inbox: without yagas_finance, or when it fails, there is simply nothing to show.
"""
import logging

logger = logging.getLogger(__name__)


def payment_states(requests) -> dict:
    """{request pk: {"state", "paid", "expected", "date", "others"}} for the requests that have something to show.

    state: "paid" (booked exactly the payout), "differs" (booked a different amount), "shared" (one booking
    paid several requests, "others" lists the rest), "open" (approved, no booking found yet). Requests that
    are not approved and have no booking get no entry at all."""
    requests = list(requests)
    if not requests:
        return {}
    try:
        from yagas_finance.srp_link import payments_for_requests, starts
    except ImportError:
        return {}
    try:
        found = payments_for_requests(requests)
        begins = starts()
    except Exception:
        logger.exception("yagas_srp: could not read the SRP payments from the finance wallet journal")
        return {}
    if found is None or not begins:   # the SRP mark is not set up in finance: nothing can be said about payments
        return {}
    start = min(begins.values())

    states = {}
    for request in requests:
        payments = found.get(request.pk, [])
        if not payments:
            # "not paid yet" only for requests approved once the mark was on: older approvals were paid before
            # anybody typed "SRP #number", so an empty result says nothing about them
            if request.status == request.Status.APPROVED and request.reviewed_at and request.reviewed_at >= start:
                states[request.pk] = {"state": "open"}
            continue
        others = sorted({number for payment in payments for number in payment["together_with"]})
        paid = sum(payment["amount"] for payment in payments)
        if others:
            state = "shared"
        elif paid == request.payout_value:
            state = "paid"
        else:
            state = "differs"
        states[request.pk] = {
            "state": state, "paid": paid, "expected": request.payout_value,
            "date": max(payment["date"] for payment in payments), "others": others,
        }
    return states
