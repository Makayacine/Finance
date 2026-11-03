"""Every error the service returns, as one fixed phrase per status.

A client gets the status and a phrase from this table -- or "invalid credentials" for a failed
login -- and nothing else: no field names, no echo of its input, no stack trace, no hint whether
a username exists. The status already tells an honest client what to fix, and the phrase is the
same for everyone.
"""

from flask import jsonify

MESSAGES = {
    400: "invalid request",
    401: "authentication required",
    403: "forbidden",
    404: "not found",
    405: "method not allowed",
    409: "voucher cannot be redeemed",
    413: "request too large",
    415: "unsupported media type",
    422: "idempotency key already used with a different request",
    500: "internal error",
}


def error(status, message=None):
    response = jsonify(error=message or MESSAGES.get(status, "error"))
    response.status_code = status
    return response
