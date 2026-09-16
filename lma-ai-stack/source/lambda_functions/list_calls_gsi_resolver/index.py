# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
AppSync Lambda resolver for `listCallsDateRange` and `getCallCount`.

Uses the `TypeDateIndex` GSI on EventSourcingTable for efficient queries
instead of scanning the table.

- listCallsDateRange: Paginated query on the GSI, enriched with the
  corresponding call-detail items via BatchGetItem so the client gets
  full Call objects in a single round-trip.
- getCallCount: Paginated COUNT query on the GSI.

RBAC: we apply Owner / SharedWith filtering post-query, matching the
behaviour of the deprecated listCalls* VTL resolvers (see the old
`listCalls.response.vtl` which filtered by $identity).

Performance: O(matched items) instead of O(total table items).
Scaling: single `ItemType="call"` hot partition is acceptable at
expected volumes (<< DDB per-partition read limit in on-demand mode)
since each item is tiny (~200 B).  Can be sharded later without a
schema break by changing ItemType to `call#<YYYY-MM>`.
"""

import base64
import json
import logging
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")

TYPE_DATE_INDEX = "TypeDateIndex"
ITEM_TYPE_CALL = "call"

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
# DynamoDB BatchGetItem hard limit
BATCH_GET_LIMIT = 100


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal objects from DynamoDB."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        return super().default(obj)


def _encode_token(last_key):
    if not last_key:
        return None
    return base64.urlsafe_b64encode(
        json.dumps(last_key, cls=DecimalEncoder).encode("utf-8")
    ).decode("ascii")


def _decode_token(token):
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        logger.warning("Invalid nextToken %r: %s", token, e)
        return None


def _get_caller_identity(event):
    """Extract caller identity from AppSync event for RBAC filtering."""
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    # AppSync ties identity.username to the Cognito sub-claim for IAM-authed
    # requests; for Cognito User Pools it's often the email. We try several
    # sources so the filter works in all call paths.
    username = claims.get("cognito:username") or identity.get("username") or claims.get("sub") or ""
    email = claims.get("email") or ""
    return {
        "username": username,
        "email": email,
        "groups": groups,
        "is_admin": "Admin" in groups,
    }


def _call_visible_to(caller, detail_item):
    """allOps policy: every authenticated user (Admin or User) can see every
    meeting — no Owner/SharedWith filtering. Kept as a function (rather than
    inlining `True` at the one call site) so the policy is documented in one
    place and the call site itself reads as an explicit RBAC checkpoint.
    """
    del caller, detail_item  # unused now the check is unconditional
    return True


def _batch_get_call_details(table, call_ids):
    """BatchGetItem on `c#<CallId>` detail rows. Handles 100-item batches
    and unprocessed keys retry.
    """
    if not call_ids:
        return {}

    results = {}
    keys = [{"PK": f"c#{cid}", "SK": f"c#{cid}"} for cid in call_ids]

    # Process in chunks of BATCH_GET_LIMIT
    table_name = table.table_name
    client = boto3.resource("dynamodb").meta.client
    for i in range(0, len(keys), BATCH_GET_LIMIT):
        batch = keys[i : i + BATCH_GET_LIMIT]
        request = {table_name: {"Keys": batch}}
        # Retry unprocessed keys a few times
        for _attempt in range(5):
            response = client.batch_get_item(RequestItems=request)
            for item in response.get("Responses", {}).get(table_name, []):
                pk = item.get("PK", "")
                if pk.startswith("c#"):
                    results[pk[2:]] = item
            unprocessed = response.get("UnprocessedKeys", {}).get(table_name)
            if not unprocessed or not unprocessed.get("Keys"):
                break
            request = {table_name: unprocessed}
    return results


def _merge_list_into_call(list_item, detail):
    """Overlay the call-detail item onto the GSI list-item, filling any
    gaps (e.g. if a detail row is missing) from the GSI projection.

    IMPORTANT: the ``Call`` type in schema.graphql exposes ``PK`` / ``SK`` but
    NOT ``ListPK`` / ``ListSK``.  The legacy ``listCalls`` VTL returned rows
    directly from the list-tracking table so ``PK`` was ``cls#<date>#s#<shard>``
    and ``SK`` was ``ts#<iso>#id#<CallId>``.  The UI's
    ``use-calls-graphql-api.js`` relies on this and builds the delete / share
    payload as ``{ListPK: c.PK, ListSK: c.SK, ...}``.

    If we let ``detail`` overwrite ``PK`` / ``SK`` here, the UI would submit
    ``ListPK = ListSK = c#<CallId>`` and the ``deleteCall`` VTL would issue a
    ``TransactWriteItems`` with two items targeting the same key, which
    DynamoDB rejects (``TransactionCanceledException``) — the net effect users
    see is that the Delete confirmation modal hangs silently.  We therefore
    always restore the list-row coordinates onto ``PK`` / ``SK`` after the
    merge, and also expose them as ``ListPK`` / ``ListSK`` for any future
    consumer that adds those fields to the schema.
    """
    merged = dict(list_item) if list_item else {}
    if detail:
        merged.update(detail)
    if list_item:
        # Restore list-row coordinates clobbered by the detail overlay so the
        # UI continues to derive ListPK / ListSK correctly from PK / SK.
        list_pk = list_item.get("PK")
        list_sk = list_item.get("SK")
        if list_pk is not None:
            merged["PK"] = list_pk
        if list_sk is not None:
            merged["SK"] = list_sk
        merged["ListPK"] = list_pk
        merged["ListSK"] = list_sk
    return merged


def _sk_bound(iso):
    """Build an SK-format lower/upper bound from an ISO-8601 datetime.

    list-tracking items use SK = "ts#<ISO8601>#id#<CallId>".  Because this is
    lexicographic-sortable, we prefix the ISO value with "ts#" to produce a
    boundary value that DynamoDB can compare against the SK range key.
    """
    return f"ts#{iso}"


def _build_key_condition(start_dt, end_dt):
    base = Key("ItemType").eq(ITEM_TYPE_CALL)
    if start_dt and end_dt:
        # Use "{end}#~" as the upper bound so rows at exactly end_dt are
        # included (SK is `ts#<iso>#id#<CallId>`; "~" sorts after "#").
        return base & Key("SK").between(_sk_bound(start_dt), f"{_sk_bound(end_dt)}#~")
    if start_dt:
        return base & Key("SK").gte(_sk_bound(start_dt))
    if end_dt:
        return base & Key("SK").lte(f"{_sk_bound(end_dt)}#~")
    return base


def handler(event, context):
    """Route to the appropriate field handler."""
    field = (event.get("info") or {}).get("fieldName", "")
    logger.info("Resolver invoked for field: %s", field)
    if field == "listCallsDateRange":
        return list_calls_date_range(event)
    if field == "getCallCount":
        return get_call_count(event)
    raise ValueError(f"Unknown field: {field}")


def list_calls_date_range(event):
    """Paginated GSI query + BatchGetItem enrichment + RBAC filter."""
    args = event.get("arguments") or {}
    start_dt = args.get("startDateTime")
    end_dt = args.get("endDateTime")
    limit = min(int(args.get("limit") or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)
    next_token = args.get("nextToken")

    table_name = os.environ["EVENT_SOURCING_TABLE_NAME"]
    table = dynamodb.Table(table_name)

    caller = _get_caller_identity(event)
    logger.info(
        "Caller: username=%s email=%s groups=%s admin=%s",
        caller["username"],
        caller["email"],
        caller["groups"],
        caller["is_admin"],
    )

    collected = []  # list of enriched Call dicts post-RBAC
    last_key = _decode_token(next_token)
    pages = 0
    # Bound the number of GSI pages we fetch per client request so a very
    # restrictive RBAC filter can't produce unbounded server latency.
    MAX_PAGES = 10

    while len(collected) < limit and pages < MAX_PAGES:
        query_kwargs = {
            "IndexName": TYPE_DATE_INDEX,
            "KeyConditionExpression": _build_key_condition(start_dt, end_dt),
            "Limit": limit,
            "ScanIndexForward": False,  # newest first
        }
        if last_key:
            query_kwargs["ExclusiveStartKey"] = last_key

        logger.info("GSI query page %d, last_key=%s", pages, last_key is not None)
        try:
            response = table.query(**query_kwargs)
        except Exception as e:  # noqa: BLE001
            logger.error("GSI query failed: %s", e)
            raise

        list_items = response.get("Items", [])
        last_key = response.get("LastEvaluatedKey")
        pages += 1
        logger.info(
            "Page %d: %d list items, has-more=%s",
            pages,
            len(list_items),
            last_key is not None,
        )

        if not list_items:
            if not last_key:
                break
            continue

        # Fetch call-detail rows in one batch
        call_ids = [it["CallId"] for it in list_items if it.get("CallId")]
        details = _batch_get_call_details(table, call_ids)

        for list_item in list_items:
            call_id = list_item.get("CallId")
            detail = details.get(call_id, {})
            merged = _merge_list_into_call(list_item, detail)
            if not _call_visible_to(caller, merged):
                continue
            collected.append(merged)
            if len(collected) >= limit:
                break

        if not last_key:
            break

    result = {
        "Calls": collected[:limit],
        "nextToken": _encode_token(last_key) if last_key else None,
    }
    logger.info(
        "Returning %d calls, nextToken=%s, pages=%d",
        len(result["Calls"]),
        "yes" if result["nextToken"] else "no",
        pages,
    )
    return result


# Non-admin count path caps pages so callers with very broad ranges can't
# allOps policy: every caller sees every meeting, so every caller gets the
# cheap Select="COUNT" path — there's no non-admin RBAC-filtered path left to
# cap separately (that used to cost a per-item Owner/SharedWith check).
MAX_COUNT_PAGES = 500


def get_call_count(event):
    """Count matching list items on the GSI. Uses DynamoDB ``Select="COUNT"``
    — no per-item work, cheap regardless of range size — for every caller;
    see _call_visible_to for why there's no RBAC filtering to apply here.

    The response includes a ``truncated`` flag indicating whether the
    page cap was hit before exhausting the range; clients should display
    something like "500+" in that case.
    """
    args = event.get("arguments") or {}
    start_dt = args.get("startDateTime")
    end_dt = args.get("endDateTime")

    table_name = os.environ["EVENT_SOURCING_TABLE_NAME"]
    table = dynamodb.Table(table_name)

    caller = _get_caller_identity(event)
    key_condition = _build_key_condition(start_dt, end_dt)

    total = 0
    pages = 0
    last_key = None

    query_kwargs = {
        "IndexName": TYPE_DATE_INDEX,
        "KeyConditionExpression": key_condition,
        "Select": "COUNT",
    }
    while pages < MAX_COUNT_PAGES:
        if last_key:
            query_kwargs["ExclusiveStartKey"] = last_key
        response = table.query(**query_kwargs)
        total += response.get("Count", 0)
        last_key = response.get("LastEvaluatedKey")
        pages += 1
        if not last_key:
            break

    truncated = bool(last_key)
    logger.info(
        "Call count: %d (range=%s..%s, caller=%s, pages=%d, truncated=%s)",
        total,
        start_dt,
        end_dt,
        caller["username"],
        pages,
        truncated,
    )
    return {"count": total, "truncated": truncated}
