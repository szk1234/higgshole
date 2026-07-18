# HiggsHole

A self-hosted AI image and video generation console with a browsable media
library, backed by the [OpenRouter](https://openrouter.ai) API and exposed to
local AI agents over MCP.

> **Status:** design complete, implementation not yet started.
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
startup on Linux, but nothing in the architecture depends on it.

## Configuration

Everything is configured through environment variables, readable from a `.env`
file — see [`.env.example`](.env.example) for the full list with defaults. By
default the app writes to `~/.local/share/higgshole` and `~/.local/state/higgshole`
and binds to `127.0.0.1`, so a fresh clone runs unprivileged with no setup.

Exposing it to your local network is a deliberate act: change
`HIGGSHOLE_BIND_HOST`. There is no authentication, by design.

If you set a daily spend cap, also set a **credit limit on the OpenRouter key
itself**. That limit is enforced provider-side and is the only guard that
cannot be defeated by a bug in this application.

## Licence

MIT
