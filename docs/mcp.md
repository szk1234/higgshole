# Using HiggsHole from an AI agent (MCP)

HiggsHole ships an MCP server that speaks stdio and exposes the same eleven
operations the web UI uses. It contains no logic of its own: every tool is one
HTTP call against the running HiggsHole API, so the two interfaces cannot drift
apart.

## Prerequisites

The HiggsHole service must be **running**, because the MCP server is a client
of it. Start it however you like — `systemctl start higgshole.service`, or
directly from a checkout — and confirm:

```bash
curl -s http://127.0.0.1:8077/api/budget
```

If the service is down, every tool returns
`{"error": "api_unreachable", "message": "..."}` rather than hanging.

## Registering the server

Most MCP clients read a JSON configuration file with an `mcpServers` object.
Add an entry that launches the console script:

```json
{
  "mcpServers": {
    "higgshole": {
      "command": "higgshole-mcp",
      "args": [],
      "env": {
        "HIGGSHOLE_API_BASE": "http://127.0.0.1:8077"
      }
    }
  }
}
```

If `higgshole-mcp` is not on the client's `PATH` — which is common, since the
client may not inherit your shell environment — give an absolute command
instead:

```json
{
  "mcpServers": {
    "higgshole": {
      "command": "/opt/higgshole/.venv/bin/higgshole-mcp",
      "args": [],
      "env": {
        "HIGGSHOLE_API_BASE": "http://127.0.0.1:8077"
      }
    }
  }
}
```

Or run it through `uv` from a checkout:

```json
{
  "mcpServers": {
    "higgshole": {
      "command": "uv",
      "args": ["run", "--directory", "/opt/higgshole", "higgshole-mcp"]
    }
  }
}
```

## `HIGGSHOLE_API_BASE`

`HIGGSHOLE_API_BASE` defaults to `http://127.0.0.1:8077` and only needs setting
when the service listens elsewhere.

Despite sharing the `HIGGSHOLE_` prefix with the server's settings, it is a
**client-side** variable: you set it in the MCP client's `env` block (or the
shell that launches `higgshole-mcp`), and it only tells the stdio server where
the already-running REST API listens. It is read by the MCP client process, not
by the web service, so it is **not a `Settings` field**. It is still listed in
the configuration table in the spec and in [`.env.example`](../.env.example) —
as a commented, clearly-labelled MCP-client entry — so that it is discoverable
alongside the other `HIGGSHOLE_` variables. Changing where the server listens is
done with the server's own settings, and this value is then pointed at the new
address.

## Verifying it

```bash
HIGGSHOLE_API_BASE=http://127.0.0.1:8077 higgshole-mcp
```

The process will sit waiting on stdin — that is correct; it speaks JSON-RPC
over the stream, not to a terminal. Press Ctrl-D to exit. A crash on startup
means the SDK is missing (`uv sync`) or the console script was not installed.

## The tools

| Tool | Behaviour |
|---|---|
| `list_models` | Image and video models with their capability constraints. Read this first: supported resolutions, durations and reference slots differ per model. |
| `generate_image` | Synchronous. Blocks until the image exists, then returns it. |
| `generate_video` | Submits the job and returns its ID. **Does not block** — a multi-minute render inside one tool call invites client timeouts. |
| `get_job` | Current state, with an optional `wait_seconds` long-poll bounded by the caller. |
| `upload_asset` | Ingests a local file and returns an asset ID usable as a reference or a video frame. |
| `list_media` | Browse the library with project, kind, model and date filters. |
| `get_media` | Full metadata for one generation, including its input lineage. |
| `delete_media` | Removes a generation, its files and its thumbnails. |
| `list_projects` | Enumerate projects. `unsorted` always exists. |
| `create_project` | Create a project. |
| `get_budget` | Provider-authoritative remaining credit plus local cap status. |

## A typical video flow

1. `list_models(kind="video")` — pick a model and read its supported durations
   and resolutions.
2. `upload_asset(path="/path/to/first-frame.png")` — optional, for
   image-to-video.
3. `generate_video(model=..., prompt=..., duration=..., first_frame_asset_id=...)`
   — returns immediately with a generation ID.
4. `get_job(generation_id=..., wait_seconds=60)` — repeat until `state` is
   `COMPLETE` or `FAILED`.

## Conventions worth knowing

- **Both locations are always returned.** Every asset carries `local_path` (a
  filesystem path on this host) and `url` (an absolute HTTP URL). Agents run on
  the same host, so either is usable.
- **Money is a string or `null`, never a number.** `cost_usd: null` with
  `cost_known: false` means the provider reported no cost — it does *not* mean
  the generation was free. Zero is never used to represent an unknown cost.
- **Batch generation is refused.** `n` is fixed at 1; `n > 1` fails locally with
  `batch_not_supported` before any request is sent.
- **Errors are structured.** A failure returns
  `{"error": "<code>", "message": "..."}` with a stable machine-readable code.
  The ones worth branching on: `validation_failed`, `local_daily_cap`,
  `in_flight_limit`, `provider_credit_limit`, `moderation_refused`,
  `indeterminate`, `api_unreachable`.
- **`indeterminate` means a charge may have occurred.** The connection was lost
  after the request was sent. Do not retry blindly.
