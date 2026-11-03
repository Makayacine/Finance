"""The read side: GraphQL, for the finance role, with exactly three queries.

    voucher(code)       one voucher and every event the service wrote for it   service database
    liability(asOf)     outstanding cents and vouchers at the end of that day  warehouse
    cohort(month)       issued, redeemed, expired, open, breakageRate,         warehouse
                        redeemedBy(days)

Writes stay REST: each one moves money, needs its own status code and, for issuing, an
idempotency key, and none of that is what GraphQL is for. Reads are the opposite case -- finance
asks for a voucher with its history, or a cohort with three points on its curve, and gets exactly
those fields in one round trip -- so they are GraphQL, and only reads are. There is no mutation
type.

``voucher`` reads the service's own database, because it is the question "what does the system
of record say about this voucher right now", and the outbox already holds its history with
``published_at`` beside each event. ``liability`` and ``cohort`` read the warehouse, because they
are about every voucher, over time, and the warehouse is where duplicates have been dropped and
late events placed on their own days.

Money is ``BigInt`` cents: GraphQL's ``Int`` is 32-bit and runs out at 21,474,836.47. The
recipient is not exposed; finance has no use for it.
"""

import re
from datetime import date

import graphene
from flask import current_app
from graphql import GraphQLError

from . import config

MONTH = re.compile(r"(\d{4})-(\d{2})")


class Event(graphene.ObjectType):
    event_id = graphene.String(required=True)
    event_type = graphene.String(required=True)
    amount_cents = graphene.BigInt(required=True)
    occurred_at = graphene.String(required=True)
    published_at = graphene.String(
        description="When the stream accepted it; null until the relay has published it")


class Voucher(graphene.ObjectType):
    code = graphene.String(required=True)
    status = graphene.String(required=True)
    amount_cents = graphene.BigInt(required=True)
    issued_at = graphene.String(required=True)
    expires_at = graphene.String(required=True)
    redeemed_at = graphene.String()
    events = graphene.List(graphene.NonNull(Event), required=True)

    def resolve_events(parent, info):
        rows = info.context["db"].execute(
            "select event_id, event_type, amount_cents, occurred_at, published_at from outbox"
            " where voucher_code = ? order by outbox_id", (parent.code,)).fetchall()
        return [Event(**dict(row)) for row in rows]


class Liability(graphene.ObjectType):
    as_of = graphene.Date(required=True)
    day = graphene.Date(description="The last day with activity at or before asOf")
    outstanding_cents = graphene.BigInt(required=True)
    outstanding_vouchers = graphene.Int(required=True)


class Cohort(graphene.ObjectType):
    month = graphene.String(required=True)
    issued = graphene.BigInt(required=True, description="Cents issued in the month")
    redeemed = graphene.BigInt(required=True, description="Cents of it redeemed so far")
    expired = graphene.BigInt(required=True, description="Cents of it lapsed so far")
    open = graphene.BigInt(required=True, description="Cents of it still redeemable")
    breakage_rate = graphene.Float(
        required=True, description="Lapsed share of issued value: realised once nothing is "
        "open, projected from the pooled life table until then")
    redeemed_by = graphene.Float(
        required=True, days=graphene.Int(required=True),
        description="Share of issued value redeemed within this many days of issue, from the "
        "cohort's own life table; while the cohort is open, the later days rest on its oldest "
        "vouchers alone")

    def resolve_redeemed_by(parent, info, days):
        if not 0 <= days <= 3660:
            raise GraphQLError("invalid request")
        return warehouse(info).redeemed_by(parent.first_day, days)


def warehouse(info):
    store = info.context["warehouse"]
    if store is None:
        raise GraphQLError("warehouse unavailable")
    return store


class Query(graphene.ObjectType):
    voucher = graphene.Field(Voucher, code=graphene.String(required=True))
    liability = graphene.Field(Liability, as_of=graphene.Date(required=True))
    cohort = graphene.Field(Cohort, month=graphene.String(required=True,
                                                          description="As 2026-10"))

    def resolve_voucher(root, info, code):
        if not config.CODE_PATTERN.fullmatch(code):
            return None
        row = info.context["db"].execute(
            "select code, status, amount_cents, issued_at, expires_at, redeemed_at"
            " from vouchers where code = ?", (code,)).fetchone()
        return Voucher(**dict(row)) if row else None

    def resolve_liability(root, info, as_of):
        return Liability(as_of=as_of, **warehouse(info).liability(as_of))

    def resolve_cohort(root, info, month):
        match = MONTH.fullmatch(month)
        if not match or not 1 <= int(match.group(2)) <= 12:
            raise GraphQLError("invalid request")
        first_day = date(int(match.group(1)), int(match.group(2)), 1)
        found = warehouse(info).cohort(first_day)
        if found is None:
            return None
        cohort = Cohort(month=month, **found)
        cohort.first_day = first_day
        return cohort


schema = graphene.Schema(query=Query, auto_camelcase=True)


def execute(payload, db, store):
    """Run one GraphQL request; returns (status, body). Errors the resolvers did not raise on
    purpose are reported as "internal error" and nothing else."""
    if not isinstance(payload, dict) or not isinstance(payload.get("query"), str) \
            or not set(payload) <= {"query", "variables", "operationName"} \
            or not isinstance(payload.get("variables") or {}, dict) \
            or not isinstance(payload.get("operationName") or "", str):
        return 400, {"error": "invalid request"}
    result = schema.execute(payload["query"], variable_values=payload.get("variables"),
                            operation_name=payload.get("operationName"),
                            context_value={"db": db, "warehouse": store})
    body = {"data": result.data}
    if result.errors:
        errors = []
        for err in result.errors:
            planned = err.original_error is None or isinstance(err.original_error, GraphQLError)
            if not planned:
                current_app.logger.error("graphql resolver failed", exc_info=err.original_error)
            errors.append({"message": err.message if planned else "internal error",
                           **({"path": err.path} if err.path else {})})
        body["errors"] = errors
    return (400 if result.data is None and result.errors else 200), body
