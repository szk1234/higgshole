# HiggsHole

A self-hosted AI image and video generation console with a browsable media
library, backed by the [OpenRouter](https://openrouter.ai) API and exposed to
local AI agents over MCP.

> **Status:** implemented. Web UI, REST API, MCP server and systemd unit are in
> place and covered by an offline test suite.
> See [the design specification](docs/specs/2026-07-18-higgshole-design.md).

## What it does

- Text-to-image, text-to-video, image-to-image and image-to-video generation
  through a single OpenRouter API key
- Model picker built from OpenRouter's **live capability catalogue** — no
  hardcoded model list, so new models appear without a code change
- Generated media stored on local disk with metadata embedded in the files
  themselves, browsable and playable in the web UI
- Any result can be fed back in as a reference for an edit or improvement pass,
  with the lineage recorded
- An MCP server (stdio) exposing the same functionality to locally running AI
  agents
- Spend controls: provider-enforced key limits, a local ledger of actual costs,
  and a daily cap

## What it deliberately does not do

- No prompt rewriting — prompts are passed to the provider verbatim
- No authentication — intended for a trusted LAN
- No multi-user support
- No batch generation — `n` is fixed at 1 so every generation has its own cost
  record

## Quick start

```bash
uv sync
uv run pytest -q          # the whole suite, offline and free
uv run higgshole          # serves http://127.0.0.1:8077
```

The suite makes no network requests and costs nothing: a socket-blocking
fixture enforces that rather than trusting convention. The single billable test
is opt-in behind `HIGGSHOLE_LIVE_TESTS` and skipped by default.

## MCP tools

Eleven tools, each a thin translation of one REST call — see
[docs/mcp.md](docs/mcp.md) for client registration:

| Tool | Behaviour |
|---|---|
| `list_models` | Image and video models with capability constraints |
| `generate_image` | Synchronous; returns the finished asset |
| `generate_video` | Returns a job ID immediately — does not block |
| `get_job` | Status, with optional bounded long-polling |
| `upload_asset` | Ingests a local file, returning a reusable asset ID |
| `list_media` | Browse with filters |
| `get_media` | Full metadata for one item, including lineage |
| `delete_media` | Removes a generation, its files and its thumbnails |
| `list_projects` | Enumerate projects |
| `create_project` | Create a project |
| `get_budget` | Provider-authoritative credit plus local cap status |

Every asset-returning tool provides both the local filesystem path and the HTTP
URL, since agents run on the same host. Costs are strings or `null` — never `0`
to represent an unknown cost.

## Design notes

The specification records several **verified corrections** to OpenRouter's
published documentation, including the true job-status enumeration, the
unreliability of client-side video cost estimation, and the ephemerality of
result URLs. These were established against the live API and its OpenAPI
specification rather than the prose docs, which contradict it in places.

## Requirements

- Python 3.12+
- `ffmpeg` / `ffprobe`
- An OpenRouter API key

Runs anywhere Python and ffmpeg run. A systemd unit is provided for boot-time
startup on Linux — see [docs/deployment.md](docs/deployment.md) — but nothing
in the architecture depends on it.

## Configuration

Everything is configured through environment variables, readable from a `.env`
file — see [`.env.example`](.env.example) for the full list with defaults. By
default the app writes under `${XDG_DATA_HOME:-~/.local/share}/higgshole` and
`${XDG_STATE_HOME:-~/.local/state}/higgshole` and binds to `127.0.0.1`, so a
fresh clone runs unprivileged with no setup.

Exposing it to your local network is a deliberate act: change
`HIGGSHOLE_BIND_HOST`. There is no authentication, by design.

If you set a daily spend cap, also set a **credit limit on the OpenRouter key
itself**. That limit is enforced provider-side and is the only guard that
cannot be defeated by a bug in this application.

## Licence

MIT
