# Virtuous CRM+ MCP Server

An [MCP](https://modelcontextprotocol.io/) server, built with
[FastMCP](https://github.com/jlowin/fastmcp), that lets an AI assistant work
with [Virtuous CRM+](https://docs.virtuoussoftware.com/): query and read data
freely, and—**only with explicit user confirmation**—create, update, archive,
or delete data.

It provides **complete coverage of the entire Virtuous API** (all 291
endpoints across 39 resource groups) through a small set of convenience tools
plus a generic discovery + call layer, so any endpoint can be reached without
needing a separate tool per endpoint.

## Safety model: reads are free, writes require confirmation

Reading (querying, searching, looking up, listing reference data) runs freely.

Every tool that **changes** data is "mutating" and is guarded in three layers:

1. **Instructions** — the server and each mutating tool tell the model it must
   describe the exact change and get explicit user approval *before* acting.
2. **`confirm` flag** — every mutating tool takes `confirm` (default `false`).
   With `confirm=false` the tool makes **no** API call and returns a *preview*
   of what it would do, so the model can show the user and ask.
3. **Client backstop** — the HTTP client raises `ConfirmationRequired` if a
   write is ever attempted without explicit confirmation, so an accidental
   `confirm=true` is the only way a write can happen.

A request is classified as a **read** if it's a `GET`, or a `POST` to a
`/Query`, `/QueryOptions`, `/Search`, `/Find`, or `/Proximity` path. Everything
else is a write.

## Operational protocols

The HTTP layer is aligned with Virtuous's documented operational behavior:

- **Connection pooling** — a single `httpx.AsyncClient` is reused for the life
  of the process (per base URL), so TLS/keep-alive connections are reused
  instead of re-established on every call.
- **Rate limits** — Virtuous enforces an **org-wide** budget (documented at
  **5,000 requests/hour**) shared by every API key/integration in the org, and
  returns `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset`
  on every response. The client records the latest values; call
  `get_rate_limit_status` to inspect remaining budget.
- **Retries + backoff** — transient `429` and `5xx` responses are retried (up to
  3 times). `429` waits honor `Retry-After` / `X-RateLimit-Reset`; otherwise an
  exponential backoff with jitter is used.
- **Pagination** — query endpoints cap at **1000 records/call**; `query_all`
  auto-pages (with a hard ceiling) so you don't manually loop `skip`/`take`.
- **Bulk writes** — `create_batch` posts many contacts/gifts in one request via
  the recommended batch endpoints, conserving the shared rate budget.

## Tools

### Full-API discovery + generic call

The entire API is reachable through these. Use discovery to find the exact
method + path, then `call_endpoint` to invoke it.

| Tool | Purpose |
| --- | --- |
| `list_resources()` | List all 39 resource groups and their read/write endpoint counts. |
| `list_endpoints(resource, search, only)` | Discover any endpoint (filter by resource, text search, or `reads`/`writes`). |
| `describe_endpoint(method, path)` | Full metadata + parameters for one endpoint. |
| `call_endpoint(method, path, path_params, query_params, body, confirm)` | Invoke **any** endpoint. Reads run freely; writes obey the confirmation gate. |

`call_endpoint` resolves `:placeholders` in the path from `path_params` (e.g.
`/api/Contact/:contactId` + `{"contactId": 123}`), and works even for endpoints
not in the bundled registry.

### Read tools (no confirmation)

| Tool | Purpose |
| --- | --- |
| `list_query_object_types` | List queryable object types + reference-data keys. |
| `get_query_options(object_type)` | Discover queryable fields, data types, and allowed operators for an object. |
| `query_records(object_type, groups, sort_by, descending, skip, take, full_detail)` | Run a filtered bulk query (single page). |
| `query_all(object_type, groups, sort_by, descending, max_records, page_size, full_detail)` | Auto-paginate a query up to `max_records` (caps pages; reports rate-limit budget). |
| `get_record(object_type, record_id)` | Fetch a single record by id. |
| `find_contact(email \| reference_source+reference_id)` | Look up one contact. |
| `search_contacts(search, skip, take)` | Fuzzy free-text contact search. |
| `get_gifts_by_contact(contact_id)` | All gifts for a contact. |
| `get_contact_notes(contact_id, important_only)` | Notes for a contact. |
| `get_individuals_by_contact(contact_id)` | Individuals that make up a contact. |
| `get_reference_data(key)` | Lookup lists: contact/gift/project/task types, tags, custom fields, org groups, etc. |
| `get_current_context()` | Current organization + the API key's permissions. |
| `get_rate_limit_status()` | Latest observed rate-limit headers (remaining org-wide budget + reset time). |
| `read_request(path, params)` | Escape hatch for arbitrary read-only `GET` calls. |

### Write tools (MUTATING — require `confirm=true` after explicit user approval)

| Tool | Purpose |
| --- | --- |
| `create_transaction(kind, body, confirm)` | Recommended way to import a single Contact or Gift (matched/validated). |
| `create_batch(kind, body, confirm)` | Bulk-import many Contacts or Gifts in one request (rate-limit-friendly). |
| `create_record(object_type, body, confirm)` | Create a record (e.g. ContactNote, ContactTag, Task, Relationship). |
| `update_record(object_type, record_id, body, confirm)` | Update a record (PUT). |
| `archive_record(object_type, record_id, unarchive, confirm)` | Archive/unarchive a record. |
| `delete_record(object_type, record_id, confirm)` | **Destructive** delete. |
| `write_request(method, path, body, confirm)` | Escape hatch for any other write (cancel recurring gift, write off pledge, send email, toggle webhook, etc.). |

With `confirm` omitted/`false`, write tools (and `call_endpoint` on a write
endpoint) return a `confirmation_required` preview and change nothing.

> Note: `call_endpoint` is the universal way to reach any write endpoint and is
> subject to the same confirmation gate. The dedicated write tools above are
> just ergonomic shortcuts for the most common operations.

### How queries work

A query body is made of `groups`. Conditions **within** a group are AND-ed;
separate groups are OR-ed. Each condition is:

```json
{ "parameter": "<field name>", "operator": "<operator>", "value": "<value>" }
```

Use `get_query_options` to get the exact `parameter` and `operator` strings for
an object. Example: contacts created on/after 2024-01-01, sorted by id desc:

```json
{
  "object_type": "Contact",
  "groups": [
    { "conditions": [
      { "parameter": "Create Date", "operator": "GreaterThanOrEqual", "value": "01/01/2024" }
    ] }
  ],
  "sort_by": "Id",
  "descending": true,
  "take": 100
}
```

Query endpoints return at most **1000** records per call; use `skip`/`take` to
page manually, or `query_all` to auto-paginate up to a `max_records` ceiling.

## Setup

1. Get a Virtuous API key: in Virtuous, **Settings → All Settings →
   Connectivity → Application Keys → Create an Application Key**.
2. Copy `.env.example` to `.env` and set `VIRTUOUS_API_KEY`.

This project uses [`uv`](https://docs.astral.sh/uv/). Install dependencies:

```bash
uv sync
```

## Run

```bash
VIRTUOUS_API_KEY=your_key uv run virtuous-mcp
```

The server speaks MCP over stdio.

## Use with an MCP client (e.g. Cursor / Claude Desktop)

Add to your MCP client config:

```json
{
  "mcpServers": {
    "virtuous-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/cole.j.cantu/Programs/custom-mcp/virtuous-mcp", "virtuous-mcp"],
      "env": { "VIRTUOUS_API_KEY": "your_api_key_here" }
    }
  }
}
```

## Configuration

| Env var | Required | Default | Description |
| --- | --- | --- | --- |
| `VIRTUOUS_API_KEY` | yes | — | Bearer API key / Application Key. |
| `VIRTUOUS_BASE_URL` | no | `https://api.virtuoussoftware.com` | API base URL. |
