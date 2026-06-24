"""FastMCP server exposing read and (confirmation-gated) write access to Virtuous CRM+.

POLICY — READ FREELY, WRITE ONLY WITH EXPLICIT CONFIRMATION
==========================================================
Read tools (querying, looking up, listing) may be used freely.

Any tool that creates, updates, archives, deletes, sends, or otherwise CHANGES
data in Virtuous is a "mutating" tool. Mutating tools MUST NOT be executed until
the human user has been shown exactly what will change and has explicitly
approved that specific action in their most recent turn. This is enforced in
three layers:

1. Server + tool instructions (this file) tell the model to always ask first.
2. Every mutating tool takes a ``confirm`` argument that defaults to False. When
   False, the tool performs NO network call and instead returns a preview of
   what it would do, telling the model to ask the user.
3. The underlying client raises ConfirmationRequired if a write is somehow
   attempted without confirmation.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from dotenv import load_dotenv
from fastmcp import FastMCP
from pydantic import Field

from .client import (
    MAX_TAKE,
    ConfirmationRequired,
    VirtuousClient,
    VirtuousError,
    is_read_request,
)
from .endpoints import ENDPOINTS, RESOURCES, find_endpoint

# Load variables from a local .env file (e.g. VIRTUOUS_API_KEY) if present.
load_dotenv()


CONFIRMATION_POLICY = (
    "CRITICAL SAFETY POLICY — DATA-MODIFYING ACTIONS REQUIRE EXPLICIT USER CONFIRMATION.\n"
    "Reading data (querying, searching, looking up, listing reference data) is always "
    "allowed without asking.\n"
    "However, you MUST NOT run ANY tool that creates, updates, archives, deletes, "
    "cancels, sends, writes off, or otherwise CHANGES data in Virtuous until the human "
    "user has explicitly approved that exact action in their latest message. No "
    "exceptions — not even if it seems obviously intended, harmless, or already implied "
    "earlier in the conversation.\n"
    "Before any mutating action you MUST:\n"
    "  1. Clearly describe in plain language exactly what will change (object type, the "
    "specific record/id, and the exact fields and values).\n"
    "  2. Ask the user to confirm, and STOP and wait for their reply.\n"
    "  3. Only after the user explicitly says yes, call the tool with confirm=true.\n"
    "Mutating tools accept a `confirm` flag that defaults to false; calling them without "
    "confirm=true performs no change and just returns a preview — use that preview to "
    "show the user what would happen. Never set confirm=true on your own initiative."
)


mcp = FastMCP(
    name="virtuous-mcp",
    instructions=(
        "Access to Virtuous CRM+ for donor/contact, gift, project, event, and related "
        "data.\n\n"
        + CONFIRMATION_POLICY
        + "\n\nFULL API COVERAGE:\n"
        "This server exposes the ENTIRE Virtuous API. Convenience tools cover the most "
        "common reads/writes, and a generic layer covers everything else:\n"
        "  - `list_resources` -> all resource groups (Contact, Gift, Project, Event, "
        "Webhook, ...).\n"
        "  - `list_endpoints(resource, search, only)` -> discover any endpoint.\n"
        "  - `describe_endpoint(method, path)` -> details + parameters for one endpoint.\n"
        "  - `call_endpoint(method, path, path_params, query_params, body, confirm)` -> "
        "invoke ANY endpoint. Reads run freely; writes obey the confirmation policy "
        "above (confirm defaults to false and returns a preview).\n"
        "Before calling an unfamiliar endpoint, use list_endpoints/describe_endpoint to "
        "find the exact path and parameters.\n\n"
        "QUERY WORKFLOW:\n"
        "1. `list_query_object_types` shows what can be queried.\n"
        "2. `get_query_options(object_type)` returns the exact field names, data types, "
        "and allowed operators for that object.\n"
        "3. Build `groups` and call `query_records` (one page) or `query_all` (auto-paged).\n"
        "Query structure: conditions WITHIN a group are AND-ed; separate groups are "
        "OR-ed. Each condition is {parameter, operator, value} using values from "
        "get_query_options verbatim.\n\n"
        "OPERATIONAL PROTOCOLS (be a good API citizen):\n"
        "  - RATE LIMITS: Virtuous enforces an ORG-WIDE budget (documented at 5,000 "
        "requests/hour) shared across every key/integration in the org. Connections "
        "are pooled and transient 429/5xx responses are retried with backoff "
        "automatically. Call `get_rate_limit_status` to see remaining budget; if it is "
        "low, slow down and avoid large sweeps.\n"
        "  - PAGINATION: Query endpoints return at most 1000 records per call. Use "
        "`query_all` to auto-paginate (it caps total records and reports remaining "
        "rate-limit budget) instead of manually looping `skip`/`take`. Always filter "
        "with `groups` so you fetch only what you need.\n"
        "  - BULK WRITES: To load MANY contacts/gifts, prefer `create_batch` (one batch "
        "request) over many `create_transaction` calls — it uses the matching/"
        "validation pipeline and conserves the shared rate budget. (Still confirmation-"
        "gated like every write.)"
    ),
)


QUERY_OBJECT_TYPES: dict[str, str] = {
    "Contact": "Donor/constituent records (organizations or households).",
    "ContactIndividual": "Individual people that make up a Contact.",
    "Gift": "Donations/gifts.",
    "GiftDesignation": "How gifts are split across projects/funds.",
    "RecurringGift": "Recurring gift commitments.",
    "PlannedGift": "Planned gifts.",
    "GiftAsk": "Gift asks/solicitations.",
    "Pledge": "Pledges (uses /api/v2/Pledge).",
    "Project": "Projects (funds/campaigns/designations targets).",
    "Campaign": "Campaigns.",
    "Communication": "Campaign communications.",
    "ContactNote": "Notes attached to contacts.",
    "Event": "Events.",
    "EventAttendee": "Event attendees.",
    "Grant": "Grants.",
    "Premium": "Gift premiums.",
    "Task": "Tasks.",
    "Volunteer": "Volunteers.",
    "VolunteerOpportunity": "Volunteer opportunities.",
}

# Reference/lookup GET endpoints that return option lists (no record id needed).
REFERENCE_ENDPOINTS: dict[str, str] = {
    "contact_types": "/api/Contact/Types",
    "contact_prefixes": "/api/Contact/Prefixes",
    "contact_custom_fields": "/api/Contact/CustomFields",
    "individual_custom_fields": "/api/ContactIndividual/CustomFields",
    "contact_method_types": "/api/ContactMethod/Types",
    "contact_note_types": "/api/ContactNote/Types",
    "relationship_types": "/api/Relationship/Types",
    "gift_custom_fields": "/api/Gift/CustomFields",
    "non_cash_gift_types": "/api/Gift/NonCashGiftTypes",
    "project_types": "/api/Project/Types",
    "project_custom_fields": "/api/Project/CustomFields",
    "event_types": "/api/Event/Types",
    "task_types": "/api/Task/Types",
    "communication_types": "/api/Communication/CommunicationTypes",
    "communication_channel_types": "/api/Communication/ChannelTypes",
    "tags": "/api/Tag",
    "organization_groups": "/api/OrganizationGroup",
}


def _client() -> VirtuousClient:
    return VirtuousClient()


def _err(e: Exception) -> dict[str, Any]:
    return {"error": str(e)}


def _needs_confirmation_preview(method: str, path: str, body: Any) -> dict[str, Any]:
    return {
        "status": "confirmation_required",
        "message": (
            "This action would MODIFY data in Virtuous and was NOT executed. "
            "Show the user exactly what will change, get their explicit approval, "
            "then call again with confirm=true."
        ),
        "would_call": {"method": method, "path": path, "body": body},
    }


async def _do_write(method: str, path: str, body: Any, confirm: bool) -> Any:
    if not confirm:
        return _needs_confirmation_preview(method, path, body)
    try:
        return await _client().request(method, path, json=body, confirmed=True)
    except (VirtuousError, ConfirmationRequired) as e:
        return _err(e)


# =============================================================================
# DISCOVERY TOOLS (safe — describe the full API surface)
# =============================================================================


@mcp.tool
def list_resources() -> dict[str, Any]:
    """List every Virtuous API resource group (Contact, Gift, Project, Event,
    Webhook, etc.) and how many endpoints each has. Read-only.

    Use with list_endpoints to drill into a resource.
    """
    counts: dict[str, dict[str, int]] = {}
    for e in ENDPOINTS:
        c = counts.setdefault(e["resource"], {"read": 0, "write": 0})
        c["read" if e["read"] else "write"] += 1
    return {
        "total_endpoints": len(ENDPOINTS),
        "resources": {r: counts[r] for r in RESOURCES},
    }


@mcp.tool
def list_endpoints(
    resource: Annotated[
        str | None,
        Field(default=None, description="Filter to one resource group, e.g. 'Contact'. See list_resources."),
    ] = None,
    search: Annotated[
        str | None,
        Field(default=None, description="Case-insensitive substring to match in the title or path."),
    ] = None,
    only: Annotated[
        str | None,
        Field(default=None, description="Filter by kind: 'reads' or 'writes'. Omit for both."),
    ] = None,
) -> dict[str, Any]:
    """Discover Virtuous API endpoints across the ENTIRE API. Read-only.

    Returns matching endpoints with their method, path template (with
    :placeholders), title, whether they read or write, and parameter names. Use
    the returned method+path with `call_endpoint` (or `describe_endpoint` first).
    """
    res = resource.lower() if resource else None
    q = search.lower() if search else None
    kind = only.lower() if only else None
    out = []
    for e in ENDPOINTS:
        if res and e["resource"].lower() != res:
            continue
        if kind == "reads" and not e["read"]:
            continue
        if kind == "writes" and e["read"]:
            continue
        if q and q not in e["title"].lower() and q not in e["path"].lower():
            continue
        out.append(
            {
                "method": e["method"],
                "path": e["path"],
                "title": e["title"],
                "kind": "read" if e["read"] else "WRITE",
                "path_params": e["path_params"],
                "query_params": e["query_params"],
            }
        )
    return {"count": len(out), "endpoints": out}


@mcp.tool
def describe_endpoint(
    method: Annotated[str, Field(description="HTTP method, e.g. 'GET', 'POST', 'PUT'.")],
    path: Annotated[str, Field(description="Exact path template with :placeholders, e.g. '/api/Contact/:contactId'.")],
) -> Any:
    """Get full metadata for a single endpoint (method, path, title, resource,
    read/write, path + query parameters). Read-only.
    """
    e = find_endpoint(method, path)
    if not e:
        return _err(
            VirtuousError(
                f"No endpoint {method.upper()} {path}. Use list_endpoints to find the exact path template."
            )
        )
    return {**e, "kind": "read" if e["read"] else "WRITE"}


# =============================================================================
# READ TOOLS (safe — no confirmation needed)
# =============================================================================


@mcp.tool
def list_query_object_types() -> dict[str, Any]:
    """List Virtuous object types that can be queried/read, with descriptions.

    Read-only. Use the returned keys with get_query_options, query_records, and
    get_record.
    """
    return {
        "object_types": QUERY_OBJECT_TYPES,
        "reference_data_keys": sorted(REFERENCE_ENDPOINTS.keys()),
        "max_records_per_query": MAX_TAKE,
    }


@mcp.tool
async def get_query_options(
    object_type: Annotated[str, Field(description="e.g. 'Contact', 'Gift', 'Project'.")],
) -> Any:
    """Get the queryable fields, data types, and allowed operators for an object
    type so you can construct a valid query. Read-only.
    """
    try:
        return await _client().query_options(object_type)
    except VirtuousError as e:
        return _err(e)


@mcp.tool
async def query_records(
    object_type: Annotated[str, Field(description="Object type to query, e.g. 'Contact' or 'Gift'.")],
    groups: Annotated[
        list[dict[str, Any]] | None,
        Field(
            default=None,
            description=(
                "Filter groups: [{\"conditions\": [{\"parameter\", \"operator\", "
                "\"value\"}, ...]}]. Conditions within a group are AND-ed; groups are "
                "OR-ed. Omit/empty for all records."
            ),
        ),
    ] = None,
    sort_by: Annotated[str | None, Field(default=None, description="Field to sort by.")] = None,
    descending: Annotated[bool, Field(default=False, description="Sort descending.")] = False,
    skip: Annotated[int, Field(default=0, ge=0, description="Records to skip (pagination).")] = 0,
    take: Annotated[int, Field(default=100, ge=1, le=MAX_TAKE, description=f"Records to return (max {MAX_TAKE}).")] = 100,
    full_detail: Annotated[bool, Field(default=False, description="Use full-detail variant (Contact/Gift only).")] = False,
) -> Any:
    """Run a read-only bulk query against a Virtuous object type. Read-only."""
    body: dict[str, Any] = {"groups": groups or []}
    if sort_by:
        body["sortBy"] = sort_by
    body["descending"] = descending
    try:
        return await _client().query(object_type, body, skip=skip, take=take, full=full_detail)
    except VirtuousError as e:
        return _err(e)


# Absolute ceiling for auto-pagination, to protect the shared org-wide rate
# budget from a runaway "fetch everything" loop.
QUERY_ALL_HARD_CAP = 10_000


def _extract_query_items(result: Any) -> tuple[list[Any], int | None]:
    """Pull the record list (and total, if present) out of a query response.

    Virtuous query endpoints return ``{"list": [...], "total": N}``; this also
    tolerates a bare list just in case.
    """
    if isinstance(result, dict):
        items = result.get("list")
        if isinstance(items, list):
            return items, result.get("total")
        return [], result.get("total")
    if isinstance(result, list):
        return result, None
    return [], None


@mcp.tool
async def query_all(
    object_type: Annotated[str, Field(description="Object type to query, e.g. 'Contact' or 'Gift'.")],
    groups: Annotated[
        list[dict[str, Any]] | None,
        Field(
            default=None,
            description=(
                "Filter groups (same structure as query_records): "
                "[{\"conditions\": [{\"parameter\", \"operator\", \"value\"}, ...]}]. "
                "Conditions within a group are AND-ed; groups are OR-ed."
            ),
        ),
    ] = None,
    sort_by: Annotated[str | None, Field(default=None, description="Field to sort by.")] = None,
    descending: Annotated[bool, Field(default=False, description="Sort descending.")] = False,
    max_records: Annotated[
        int,
        Field(default=1000, ge=1, le=QUERY_ALL_HARD_CAP, description=f"Stop after collecting this many records (hard cap {QUERY_ALL_HARD_CAP})."),
    ] = 1000,
    page_size: Annotated[int, Field(default=MAX_TAKE, ge=1, le=MAX_TAKE, description=f"Records per page request (max {MAX_TAKE}).")] = MAX_TAKE,
    full_detail: Annotated[bool, Field(default=False, description="Use full-detail variant (Contact/Gift only).")] = False,
) -> Any:
    """Auto-paginate a query and return up to ``max_records`` records. Read-only.

    Convenience over `query_records`: instead of you managing `skip`/`take`,
    this loops through pages (each capped at 1000 by the API) until it has
    `max_records`, the result set is exhausted, or the `QUERY_ALL_HARD_CAP`
    safety ceiling is hit. Because every page is a request against the
    ORG-WIDE rate budget, keep `max_records` as small as the task needs and
    prefer a precise `groups` filter. The response includes a `rate_limit`
    snapshot so you can see remaining budget after the sweep.
    """
    body: dict[str, Any] = {"groups": groups or []}
    if sort_by:
        body["sortBy"] = sort_by
    body["descending"] = descending

    client = _client()
    collected: list[Any] = []
    skip = 0
    total: int | None = None
    pages = 0
    try:
        while len(collected) < max_records:
            take = min(page_size, max_records - len(collected))
            result = await client.query(
                object_type, body, skip=skip, take=take, full=full_detail
            )
            pages += 1
            items, total = _extract_query_items(result)
            collected.extend(items)
            # Stop when the API returns fewer than we asked for (last page).
            if len(items) < take:
                break
            skip += take
    except VirtuousError as e:
        return _err(e)

    return {
        "object_type": object_type,
        "returned": len(collected),
        "total_available": total,
        "pages_fetched": pages,
        "truncated": total is not None and len(collected) < total,
        "records": collected,
        "rate_limit": VirtuousClient.last_rate_limit(),
    }


@mcp.tool
async def get_record(
    object_type: Annotated[str, Field(description="e.g. 'Contact', 'Gift', 'Project'.")],
    record_id: Annotated[str, Field(description="The id of the record to fetch.")],
) -> Any:
    """Fetch a single record by id via GET /api/{object_type}/{record_id}. Read-only."""
    try:
        return await _client().get_record(object_type, record_id)
    except VirtuousError as e:
        return _err(e)


@mcp.tool
async def find_contact(
    email: Annotated[str | None, Field(default=None, description="Email address to look up.")] = None,
    reference_source: Annotated[str | None, Field(default=None, description="Reference source, e.g. 'Stripe'.")] = None,
    reference_id: Annotated[str | None, Field(default=None, description="Reference id within the source.")] = None,
) -> Any:
    """Find a single contact by email, or by reference source + id. Read-only.

    Provide either an email or a reference_source + reference_id.
    """
    params: dict[str, Any] = {}
    if email:
        params["email"] = email
    if reference_source:
        params["referenceSource"] = reference_source
    if reference_id:
        params["referenceId"] = reference_id
    if not params:
        return _err(VirtuousError("Provide email, or reference_source and reference_id."))
    try:
        return await _client().get("/api/Contact/Find", params=params)
    except VirtuousError as e:
        return _err(e)


@mcp.tool
async def search_contacts(
    search: Annotated[str, Field(description="Free-text search (name, etc.).")],
    skip: Annotated[int, Field(default=0, ge=0)] = 0,
    take: Annotated[int, Field(default=10, ge=1, le=MAX_TAKE)] = 10,
) -> Any:
    """Fuzzy-search contacts by a free-text string. Read-only."""
    try:
        return await _client().request(
            "POST", "/api/Contact/Search", params={"skip": skip, "take": take}, json={"search": search}
        )
    except VirtuousError as e:
        return _err(e)


@mcp.tool
async def get_gifts_by_contact(
    contact_id: Annotated[str, Field(description="The contact id.")],
) -> Any:
    """Get all gifts for a contact via GET /api/Gift/ByContact/{id}. Read-only."""
    try:
        return await _client().get(f"/api/Gift/ByContact/{contact_id}")
    except VirtuousError as e:
        return _err(e)


@mcp.tool
async def get_contact_notes(
    contact_id: Annotated[str, Field(description="The contact id.")],
    important_only: Annotated[bool, Field(default=False, description="Only return important notes.")] = False,
) -> Any:
    """Get notes for a contact. Read-only."""
    path = (
        f"/api/ContactNote/Important/ByContact/{contact_id}"
        if important_only
        else f"/api/ContactNote/ByContact/{contact_id}"
    )
    try:
        return await _client().get(path)
    except VirtuousError as e:
        return _err(e)


@mcp.tool
async def get_individuals_by_contact(
    contact_id: Annotated[str, Field(description="The contact id.")],
) -> Any:
    """Get the individuals that make up a contact. Read-only."""
    try:
        return await _client().get(f"/api/ContactIndividual/ByContact/{contact_id}")
    except VirtuousError as e:
        return _err(e)


@mcp.tool
async def get_reference_data(
    key: Annotated[
        str,
        Field(description="Reference list key (see list_query_object_types -> reference_data_keys)."),
    ],
) -> Any:
    """Fetch a reference/lookup list (contact types, tags, custom fields, task types,
    project types, etc.) used to build queries or understand allowed values. Read-only.
    """
    path = REFERENCE_ENDPOINTS.get(key)
    if not path:
        return _err(VirtuousError(f"Unknown reference key '{key}'. Valid: {sorted(REFERENCE_ENDPOINTS)}"))
    try:
        return await _client().get(path)
    except VirtuousError as e:
        return _err(e)


@mcp.tool
async def get_current_context() -> Any:
    """Get the current organization and the API key's permissions. Read-only.

    Useful to understand what the key is allowed to read/write before attempting
    any action.
    """
    client = _client()
    out: dict[str, Any] = {}
    try:
        out["organization"] = await client.get("/api/Organization/Current")
    except VirtuousError as e:
        out["organization_error"] = str(e)
    try:
        out["permissions"] = await client.get("/api/Permission")
    except VirtuousError as e:
        out["permissions_error"] = str(e)
    return out


@mcp.tool
def get_rate_limit_status() -> dict[str, Any]:
    """Report the most recently observed Virtuous rate-limit headers. Read-only.

    Virtuous enforces an ORG-WIDE request budget (documented at 5,000
    requests/hour) shared by every API key/integration in the organization.
    This returns the latest `X-RateLimit-Limit`, `X-RateLimit-Remaining`,
    `reset_at`, and `seconds_until_reset` seen on a response — useful before
    kicking off a large batch or many queries. It is empty until at least one
    request has been made in this session, so a low `remaining` here reflects
    other integrations' usage too.
    """
    snapshot = VirtuousClient.last_rate_limit()
    if not snapshot:
        return {
            "status": "no_data_yet",
            "message": (
                "No request has been made in this session yet, so no rate-limit "
                "headers have been seen. Make any read call first to populate this."
            ),
        }
    return {"status": "ok", "rate_limit": snapshot}


@mcp.tool
async def read_request(
    path: Annotated[str, Field(description="API path beginning with /api/, e.g. '/api/Contact/123'.")],
    params: Annotated[dict[str, Any] | None, Field(default=None, description="Optional query-string params.")] = None,
) -> Any:
    """Escape hatch for arbitrary READ-ONLY GET requests to the Virtuous API.

    Only GET is allowed here. Use when no dedicated read tool fits. Read-only.
    """
    if not path.startswith("/api/"):
        return _err(VirtuousError("Path must start with /api/."))
    try:
        return await _client().get(path, params=params)
    except VirtuousError as e:
        return _err(e)


# =============================================================================
# WRITE TOOLS (MUTATING — require explicit user confirmation; confirm defaults False)
# =============================================================================


@mcp.tool
async def create_record(
    object_type: Annotated[str, Field(description="Object to create, e.g. 'ContactNote', 'ContactTag', 'Task', 'Relationship'.")],
    body: Annotated[dict[str, Any], Field(description="JSON body for the new record.")],
    confirm: Annotated[bool, Field(default=False, description="Must be true to actually create. Set ONLY after explicit user approval.")] = False,
) -> Any:
    """MUTATING: create a new record via POST /api/{object_type}.

    DO NOT call with confirm=true unless the user has explicitly approved creating
    this exact record. With confirm=false this performs no change and returns a
    preview to show the user. NOTE: to create Contacts/Gifts safely, prefer
    `create_transaction` (it runs through Virtuous matching/validation).
    """
    return await _do_write("POST", f"/api/{object_type}", body, confirm)


@mcp.tool
async def update_record(
    object_type: Annotated[str, Field(description="Object to update, e.g. 'Contact', 'Gift', 'ContactNote'.")],
    record_id: Annotated[str, Field(description="The id of the record to update.")],
    body: Annotated[dict[str, Any], Field(description="Full JSON body with updated fields.")],
    confirm: Annotated[bool, Field(default=False, description="Must be true to actually update. Set ONLY after explicit user approval.")] = False,
) -> Any:
    """MUTATING: update a record via PUT /api/{object_type}/{record_id}.

    DO NOT call with confirm=true unless the user has explicitly approved this
    exact change. With confirm=false this performs no change and returns a preview.
    """
    return await _do_write("PUT", f"/api/{object_type}/{record_id}", body, confirm)


@mcp.tool
async def archive_record(
    object_type: Annotated[str, Field(description="Object that supports archiving, e.g. 'Contact', 'ContactAddress'.")],
    record_id: Annotated[str, Field(description="The id of the record.")],
    unarchive: Annotated[bool, Field(default=False, description="Unarchive instead of archive.")] = False,
    confirm: Annotated[bool, Field(default=False, description="Must be true to actually run. Set ONLY after explicit user approval.")] = False,
) -> Any:
    """MUTATING: archive (or unarchive) a record via PUT /api/{object_type}/Archive/{id}.

    DO NOT call with confirm=true unless the user explicitly approved it. With
    confirm=false this performs no change and returns a preview.
    """
    action = "Unarchive" if unarchive else "Archive"
    return await _do_write("PUT", f"/api/{object_type}/{action}/{record_id}", None, confirm)


@mcp.tool
async def delete_record(
    object_type: Annotated[str, Field(description="Object to delete, e.g. 'ContactNote', 'ContactTag'.")],
    record_id: Annotated[str, Field(description="The id of the record to delete.")],
    confirm: Annotated[bool, Field(default=False, description="Must be true to actually delete. Set ONLY after explicit user approval.")] = False,
) -> Any:
    """MUTATING + DESTRUCTIVE: delete a record via DELETE /api/{object_type}/{id}.

    This permanently removes data. DO NOT call with confirm=true unless the user
    has explicitly approved deleting this exact record. With confirm=false this
    performs no change and returns a preview.
    """
    return await _do_write("DELETE", f"/api/{object_type}/{record_id}", None, confirm)


@mcp.tool
async def create_transaction(
    kind: Annotated[str, Field(description="'contact' or 'gift' — which transaction import to create.")],
    body: Annotated[dict[str, Any], Field(description="Transaction JSON body.")],
    confirm: Annotated[bool, Field(default=False, description="Must be true to actually submit. Set ONLY after explicit user approval.")] = False,
) -> Any:
    """MUTATING: submit a Contact or Gift transaction (the recommended, matched/validated
    way to import contacts and gifts).

    'contact' -> POST /api/Contact/Transaction
    'gift'    -> POST /api/v2/Gift/Transaction
    DO NOT call with confirm=true unless the user explicitly approved it. With
    confirm=false this performs no change and returns a preview.
    """
    kind = kind.lower().strip()
    if kind == "contact":
        path = "/api/Contact/Transaction"
    elif kind == "gift":
        path = "/api/v2/Gift/Transaction"
    else:
        return _err(VirtuousError("kind must be 'contact' or 'gift'."))
    return await _do_write("POST", path, body, confirm)


@mcp.tool
async def create_batch(
    kind: Annotated[str, Field(description="'contact' or 'gift' — which bulk import to submit.")],
    body: Annotated[
        dict[str, Any] | list[Any],
        Field(
            description=(
                "Bulk payload. For 'gift' this is typically a list of gift "
                "transaction objects (same shape as a single gift transaction). "
                "For 'contact' this is the contact-import batch payload. Use "
                "describe_endpoint on the target path for the exact schema."
            )
        ),
    ],
    confirm: Annotated[bool, Field(default=False, description="Must be true to actually submit. Set ONLY after explicit user approval.")] = False,
) -> Any:
    """MUTATING: submit a BULK import of contacts or gifts (the rate-limit-friendly,
    Virtuous-recommended way to load many records at once).

    Virtuous best practices say: to load many gifts/contacts, post them through
    the BATCH endpoints rather than one transaction per call. This both runs the
    records through Virtuous's matching/validation/dedupe pipeline AND makes far
    fewer requests against the ORG-WIDE rate budget (one batch call instead of N).

    'contact' -> POST /api/Contact/Batch
    'gift'    -> POST /api/v2/Gift/Transactions
    DO NOT call with confirm=true unless the user explicitly approved it. With
    confirm=false this performs no change and returns a preview (including how
    many records are in the batch).
    """
    kind = kind.lower().strip()
    if kind == "contact":
        path = "/api/Contact/Batch"
    elif kind == "gift":
        path = "/api/v2/Gift/Transactions"
    else:
        return _err(VirtuousError("kind must be 'contact' or 'gift'."))
    if not confirm:
        preview = _needs_confirmation_preview("POST", path, body)
        try:
            preview["batch_size"] = len(body)  # type: ignore[arg-type]
        except TypeError:
            pass
        return preview
    try:
        return await _client().request("POST", path, json=body, confirmed=True)
    except (VirtuousError, ConfirmationRequired) as e:
        return _err(e)


@mcp.tool
async def write_request(
    method: Annotated[str, Field(description="HTTP method: POST, PUT, PATCH, or DELETE.")],
    path: Annotated[str, Field(description="API path beginning with /api/, e.g. '/api/RecurringGift/Cancel/123'.")],
    body: Annotated[dict[str, Any] | None, Field(default=None, description="Optional JSON body.")] = None,
    confirm: Annotated[bool, Field(default=False, description="Must be true to actually run. Set ONLY after explicit user approval.")] = False,
) -> Any:
    """MUTATING escape hatch for any write endpoint not covered by a dedicated tool
    (e.g. cancel a recurring gift, write off a pledge, send an email, toggle a webhook).

    DO NOT call with confirm=true unless the user has explicitly approved this exact
    request. With confirm=false this performs no change and returns a preview. If the
    request is actually read-only it will be rejected — use `read_request` for reads.
    """
    method = method.upper()
    if method not in ("POST", "PUT", "PATCH", "DELETE"):
        return _err(VirtuousError("write_request only supports POST, PUT, PATCH, DELETE."))
    if not path.startswith("/api/"):
        return _err(VirtuousError("Path must start with /api/."))
    if is_read_request(method, path):
        return _err(VirtuousError("This looks like a read request; use read_request instead."))
    return await _do_write(method, path, body, confirm)


# =============================================================================
# GENERIC CALL (covers the ENTIRE API; writes still gated by confirm)
# =============================================================================


def _resolve_path(path_template: str, path_params: dict[str, Any] | None) -> tuple[str, list[str]]:
    """Replace :placeholders in a path template with provided values.

    Returns the resolved path and a list of any placeholders left unfilled.
    """
    path_params = path_params or {}
    missing: list[str] = []

    def repl(match: Any) -> str:
        name = match.group(1)
        if name in path_params and path_params[name] is not None:
            return str(path_params[name])
        missing.append(name)
        return match.group(0)

    resolved = re.sub(r":(\w+)", repl, path_template)
    return resolved, missing


@mcp.tool
async def call_endpoint(
    method: Annotated[str, Field(description="HTTP method: GET, POST, PUT, PATCH, or DELETE.")],
    path: Annotated[str, Field(description="Path template from list_endpoints, e.g. '/api/Contact/:contactId'.")],
    path_params: Annotated[
        dict[str, Any] | None,
        Field(default=None, description="Values for :placeholders in the path, e.g. {\"contactId\": 123}."),
    ] = None,
    query_params: Annotated[
        dict[str, Any] | None,
        Field(default=None, description="Query-string parameters, e.g. {\"skip\": 0, \"take\": 50}."),
    ] = None,
    body: Annotated[
        dict[str, Any] | None,
        Field(default=None, description="JSON request body (for POST/PUT/PATCH)."),
    ] = None,
    confirm: Annotated[
        bool,
        Field(default=False, description="Required (true) for any WRITE endpoint. Set ONLY after explicit user approval."),
    ] = False,
) -> Any:
    """Invoke ANY Virtuous API endpoint. This gives full coverage of the entire API.

    Reads (GET, and POST to Query/QueryOptions/Search/Find/Proximity) run freely.
    WRITE endpoints (any other POST/PUT/PATCH/DELETE) MODIFY data and obey the
    confirmation policy: with confirm=false NO call is made and a preview is
    returned so you can show the user and ask. Only pass confirm=true after the
    user has explicitly approved the exact action.

    Use list_endpoints / describe_endpoint to find the right method + path first.
    """
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return _err(VirtuousError(f"Unsupported HTTP method: {method}"))
    if not path.startswith("/api/"):
        return _err(VirtuousError("Path must start with /api/."))

    resolved, missing = _resolve_path(path, path_params)
    if missing:
        return _err(
            VirtuousError(
                f"Missing path parameter(s): {missing}. Provide them in path_params."
            )
        )

    # Warn (but do not hard-fail) if the path isn't in the known registry, so new
    # endpoints still work; surface it for transparency.
    known = find_endpoint(method, path) is not None

    if is_read_request(method, resolved):
        try:
            result = await _client().request(method, resolved, params=query_params, json=body)
            return result if known else {"_note": "endpoint not in registry", "result": result}
        except VirtuousError as e:
            return _err(e)

    # Write path.
    if not confirm:
        preview = _needs_confirmation_preview(method, resolved, body)
        if query_params:
            preview["would_call"]["query_params"] = query_params
        return preview
    try:
        return await _client().request(
            method, resolved, params=query_params, json=body, confirmed=True
        )
    except (VirtuousError, ConfirmationRequired) as e:
        return _err(e)


def main() -> None:
    """Entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
