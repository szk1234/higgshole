# HiggsHole — Design Specification

**Date:** 2026-07-18
**Status:** Approved for planning
**Revision:** 3 (post cross-plan audit)

A self-hosted AI image and video generation console with a browsable media
library, backed by the OpenRouter API, exposed to local AI agents over MCP.

---

## 1. Purpose and constraints

### What this is

A single-user web application that runs on a host you control. It submits
text-to-image, text-to-video, image-to-image and image-to-video generation
requests to OpenRouter, stores results on local disk with their metadata, and
presents them in a browsable, playable library. The same functionality is
exposed to locally running AI agents through an MCP server.

### Explicit non-goals

- **No prompt rewriting.** Prompts are passed to the provider verbatim.
- **No authentication.** Intended for a trusted local network.
- **No multi-user support.** One operator, one library.
- **No storyboarding, character libraries, or multi-shot sequencing.**
- **No preset/motion catalogue.**
- **No batch generation.** `n` is fixed at 1 per request (see §5.5).
- **No operator-initiated job cancellation.** OpenRouter exposes no cancel
  endpoint; the `cancelled` status is handled as an upstream-terminal state
  (§4.3).
- **No webhook receiver.** Job completion is detected by polling only (§2.6).

### Constraints

| Constraint | Value |
|---|---|
| Backend language | Python 3.12+ |
| Provider | OpenRouter (single upstream) |
| Auth | None |
| Agent interface | MCP over stdio |
| External binaries | `ffmpeg`, `ffprobe` |
| Media storage | Configurable local directory |

The application itself requires only Python and ffmpeg. A systemd unit is
**provided** for boot-time startup on Linux, but nothing in the architecture
depends on systemd.

---

## 2. Provider capabilities (verified 2026-07-18)

Facts in this section were verified against the live OpenRouter API and its
OpenAPI specification (`https://openrouter.ai/openapi.json`). They are recorded
because implementation depends on them, and because OpenRouter's prose
documentation contradicts its own specification in several places.

Claims are labelled **[verified]** where checked against the live API or
specification, and **[unverified]** where they could not be established. No
claim in this document should be read as documented unless labelled.

### 2.1 Endpoints

| Purpose | Method and path |
|---|---|
| List image models | `GET /api/v1/images/models` |
| Image model pricing | `GET /api/v1/images/models/{author}/{slug}/endpoints` |
| Generate image | `POST /api/v1/images` |
| List video models | `GET /api/v1/videos/models` |
| Submit video job | `POST /api/v1/videos` |
| Poll video job | `GET /api/v1/videos/{jobId}` |
| Download video | `GET /api/v1/videos/{jobId}/content?index=0` |
| Key status and limits | `GET /api/v1/key` |

There is **no image edit, inpaint, or mask endpoint** [verified]. Image-to-image
is performed via `input_references` on the standard generation endpoint.

There is **no job cancellation endpoint** [verified] — `/videos/{jobId}`
exposes only `get`.

### 2.2 Image generation

`POST /api/v1/images` is **synchronous**. Response data is **always base64**;
there is no hosted-URL response mode [verified].

Request fields: `model`, `prompt` (required); `n` (1–10), `resolution`
(`512` | `1K` | `2K` | `4K`), `aspect_ratio` (22 enum values), `size`,
`quality` (`auto` | `low` | `medium` | `high`), `output_format`
(`png` | `jpeg` | `webp` | `svg`), `background`, `output_compression`, `seed`,
`stream`, `input_references`, `provider`.

`size` given as explicit pixels is authoritative; a conflicting `resolution` or
`aspect_ratio` alongside it is rejected with HTTP 400 [verified].

`usage` is **not a required response field**, and `usage.cost` is typed
`["number", "null"]` [verified]. Cost may therefore be absent — BYOK routing is
one realistic cause. See §3.4.

### 2.3 Video generation

`POST /api/v1/videos` is **asynchronous**, returning HTTP 202 with
`{id, polling_url, status}` [verified].

Request fields: `model`, `prompt` (required); `duration`, `resolution`,
`aspect_ratio`, `size`, `frame_images`, `input_references`, `generate_audio`,
`seed`, `callback_url`, `provider`.

`negative_prompt` is **not** a top-level field [verified]. It exists only as a
provider passthrough with provider-specific spelling (`negativePrompt` for
Google Vertex, `negative_prompt` for Kling).

`frame_images` and `input_references` are **distinct fields**. If both are
supplied, `frame_images` takes precedence and the request is treated as
image-to-video [verified].

`VideoGenerationResponse` has **no `model` field** [verified].

### 2.4 Job status

From `components.schemas.VideoGenerationResponse.status.enum` [verified]:

```
pending | in_progress | completed | failed | cancelled | expired
```

The prose documentation lists only the first four. **All six must be handled**,
with explicit mappings in §4.3.

The schema carries `x-speakeasy-unknown-values: "allow"`. This annotation
appears on **every enum in the specification** (180 occurrences), including
MIME-type and TTL enums, and is a blanket Speakeasy code-generation directive
rather than a signal that OpenRouter anticipates new status values. **It must
not be read as evidence about status evolution.**

An unknown status is nonetheless treated as **non-terminal (continue polling)**,
justified on its own merits: over-polling is bounded by the wall-clock ceiling
(§4.3) and self-corrects, whereas incorrectly treating a live job as terminal
loses a paid generation irrecoverably. The asymmetry, not the annotation, is the
reason.

Errors appear as a top-level `error` **string** on the job object, not the
`{"error": {"code", "message"}}` envelope used for HTTP-level errors [verified].

### 2.5 Result durability — critical

- **No result TTL is published anywhere** [verified absent]. Not in the guide,
  cookbooks, SDK references, or the OpenAPI specification.
- `unsigned_urls` may contain a **foreign provider storage URL**. The OpenAPI
  example shows `https://storage.example.com/...` [verified].
- The `/content` endpoint is documented as streaming *"from the upstream
  provider"* and can return HTTP 502 [verified]. **OpenRouter is a proxy at
  download time, not a durable store.**
- An `expired` status exists, described as "Job exceeded maximum time to live"
  [verified].

**Consequence:** both URL forms are ephemeral and provider-bound. The worker
observing `completed` must download immediately, within the same task. A result
URL must never be persisted as a durable reference.

### 2.6 Webhooks — out of scope

OpenRouter supports a `callback_url` with HMAC-signed delivery. **This design
does not use it**, for two reasons: a service on a private network is not
reachable by OpenRouter without a tunnel, and the retry schedule is
undocumented, so polling would be required as a fallback regardless.

Completion is detected by polling only. `callback_url` is never sent. No webhook
secret is stored and no receiving route exists.

### 2.7 Per-model capability discovery

Model capabilities are machine-readable and **must not be hardcoded**.

`GET /api/v1/videos/models` returns per model: `supported_resolutions`,
`supported_aspect_ratios`, `supported_sizes`, `supported_durations`,
`supported_frame_images`, `generate_audio`, `seed`, `pricing_skus`,
`allowed_passthrough_parameters`.

`GET /api/v1/images/models` returns `supported_parameters` with each parameter's
type and permitted values, including
`input_references: {type: "range", min: 0, max: N}`.

Known asymmetries the UI must respect [verified]:

- `openai/sora-2-pro` accepts **no** frame images (text-to-video only).
- `minimax/hailuo-2.3`, `alibaba/wan-2.6`, `alibaba/happyhorse-*` and
  `x-ai/grok-imagine-video` accept **`first_frame` only**.
- `input_references` limits range from 1 (all Recraft models) to 16
  (`openai/gpt-image-2`).

**`supported_resolutions` is not authoritative.** On **4 of 16** video models it
contradicts `pricing_skus` [verified]:

| Model | Declares | SKUs also cover |
|---|---|---|
| `kwaivgi/kling-v3.0-pro` | `["720p"]` | 480p, 1080p |
| `kwaivgi/kling-v3.0-std` | `["720p"]` | 480p, 1080p |
| `openai/sora-2-pro` | `["720p","1080p"]` | 1024p |
| `alibaba/wan-2.6` | `["720p","1080p"]` | 480p |

**Precedence rule (resolves the validate-vs-warn conflict):** a parameter value
absent from a model's declared capability list but **present in its
`pricing_skus`** is treated as *advisory* — the UI warns and permits dispatch.
A value absent from **both** is treated as *hard invalid* and rejected before
dispatch.

### 2.8 Reference image transport — open question

`FrameImage` extends `ContentPartImage`, whose `url` field is an unconstrained
string [verified]. The image API documents this shared type as accepting
*"base64 data URLs or HTTP(S) URLs"*, and image references via data URI are
confirmed working.

**Whether video providers accept data URIs is unknown.** The video guide is
silent on the matter: it says only *"ensure they are high quality and
relevant"* and *"check that any reference images are accessible and in
supported formats"*. It does **not** state that providers fetch server-side, and
the word "public" does not appear in it. Earlier drafts of this document
asserted otherwise; that assertion was unsupported and has been removed.

So: schema-level acceptance is near-certain; runtime acceptance is untested.
This matters because a service on a private network cannot serve a URL an
upstream provider could reach.

**Design response.** Reference transport is a single configurable strategy,
`HIGGSHOLE_REFERENCE_TRANSPORT`, defaulting to `data_uri`. Resolution of open
item §12.1 determines the outcome:

- If data URIs work → image-to-video works with no further design.
- If they do not → **image-to-video is deferred**, and the UI disables video
  reference slots with an explanatory message. Image-to-image is unaffected.

A public-URL transport mode is **explicitly not designed here.** Making local
files provider-reachable requires a tunnel or object store, contradicts the
trusted-network premise, and would need its own lifetime and revocation design.
It is out of scope unless open item §12.1 forces the question.

---

## 3. Cost control

### 3.1 Why client-side estimation is insufficient

Pre-flight estimation was investigated and found **unreliable for roughly
40–50% of the video catalogue** [verified against live pricing data]:

1. **Token-priced models cannot be estimated.** Three Seedance models price by
   `video_tokens` (e.g. `"0.000007"`) with no published tokens-per-second table.
2. **Unit ambiguity within one field.** `x-ai/grok-imagine-video` uses a
   `cents_per_*` prefix. Reading `"7"` as dollars is a 100× overestimate.
3. **Non-orthogonal pricing axes.** `kwaivgi/kling-v3.0-pro` carries mode- and
   resolution-qualified SKUs at `0.112` with **no audio variants**, alongside
   `duration_seconds_with_audio` at `0.168`. Image-to-video at 720p with audio
   has no correct key; most-specific-match drops a **50%** surcharge.
4. **Missing axes.** `alibaba/wan-2.7` and `kwaivgi/kling-video-o1` are
   audio-capable but carry only a bare `duration_seconds` SKU.
5. **Documented SKU grammar does not match reality.** The guide's example uses
   hyphenated keys (`per-video-second`); **no live model uses that form.**
6. **A second pricing surface reports zero.**
   `GET /api/v1/models/{author}/{slug}/endpoints` resolves all 16 video models
   and reports `pricing: {prompt: "0", completion: "0"}`. Reusing the generic
   endpoint quotes $0.00 for a $3.20 job.

OpenRouter disclaims the schema: *"Pricing SKU units can differ by provider …
inspect the matching model's `pricing_skus` before routing production
traffic."*

**A spend cap must not rest on client-side estimation.**

### 3.2 Three layers

**Layer 1 — provider-enforced key limit (authoritative).** OpenRouter supports
per-key credit limits enforced server-side, returning HTTP 402 when exhausted.
This is the primary protection: it cannot be defeated by a local calculation
error.

Critically, `GET /api/v1/key` is free and returns the authoritative figures
[verified]:

```json
{"limit": 100, "limit_remaining": 74.5, "limit_reset": "monthly",
 "usage": 25.5, "usage_daily": 25.5}
```

**All budget displayed to the operator or returned by `get_budget` is sourced
from this call**, cached for 60 seconds, not from the local ledger. The local
ledger governs only the *local daily cap*. Where the call is unavailable, the
UI shows the ledger figure explicitly labelled as local-only.

**Layer 2 — local daily cap over the ledger.** Enforced pre-dispatch through a
reservation protocol (§3.3), reconciled to actual cost on completion.

**Layer 3 — advisory estimate.** Shown where exactly computable. Where not, the
API returns `estimate: null` with a machine-readable `estimate_unavailable`
reason and **never a fabricated number**.

Estimability by billing unit [verified]:

| Unit | Models | Estimate |
|---|---|---|
| `image` | Recraft (11), Riverflow, Grok, Seedream | Exact |
| `megapixel` | `flux.2-{pro,max,flex,klein-4b}` | Exact |
| `token` | OpenAI GPT-Image, Gemini, MAI | Not computable |
| `duration_seconds*` | Veo, Kling, Wan, Hailuo, HappyHorse | Exact where axes resolve |
| `video_tokens` | Seedance ×3 | Not computable |
| `cents_per_*` | `x-ai/grok-imagine-video` | Exact, after ÷100 |

Image pricing requires one request per model
(`/images/models/{author}/{slug}/endpoints`) and returns an **array of line
items** (`{billable, unit, cost_usd, variant?}`), not a map. Input-side items
are material: `input_reference` on `riverflow-v2-pro` costs $0.20 each, so five
references add $1.00 before generation.

### 3.3 The reservation protocol

This resolves cap enforcement for non-estimable models and the concurrent-
submission race.

**Cap window.** "Daily" means a **UTC calendar day**, computed over
`spend_ledger.recorded_at`. The cap does not reset on service restart. When
`HIGGSHOLE_DAILY_CAP_USD` is unset there is no cap and `get_budget` returns
`cap: null`.

**Reservation.** Before dispatch, a reservation row is appended:

- If the cost is exactly estimable → reserve the estimate.
- If it is not → reserve `HIGGSHOLE_MAX_JOB_COST_USD` (default `2.00`), a
  configurable pessimistic ceiling.

**The gate is serialized.** Estimate, cap check, and reservation write occur
inside a single process-wide async lock, so concurrent submissions cannot each
observe the same remaining balance. Additionally, in-flight jobs are limited to
`HIGGSHOLE_MAX_IN_FLIGHT` (default `3`).

Without both mechanisms, ten videos submitted within one second all pass a gate
that sees no recorded spend.

**Reconciliation.** `spend_ledger` is append-only with **signed amounts** and a
`kind` discriminator (`reservation` | `reversal` | `actual`). On terminal state,
a `reversal` negating the reservation is appended, followed by an `actual` where
one applies. Spend for a window is the plain sum of `amount`, which is therefore
never double-counted. Failed jobs net to zero.

### 3.4 When actual cost is unknown

`usage.cost` is optional and nullable [verified]. If a job completes without a
cost:

- The reservation is **not reversed**; it stands as the recorded charge.
- The `actual` row is written with `cost_known = false`.
- The UI marks the day's total as **"≥"** rather than exact.

The cap therefore over-counts rather than under-counts. Recording zero — which
would let the cap silently never trip — is prohibited.

### 3.5 Unbounded quality

`quality: "auto"` on token-billed models is unbounded by definition. It is
rejected **whenever `HIGGSHOLE_DAILY_CAP_USD` is set to any value**, regardless
of remaining balance. With no cap configured it is permitted.

---

## 4. Architecture

### 4.1 Component boundaries

```
        Browser (HTMX)          Local AI agents (stdio MCP)
              |                            |
              +----------> HTTP <----------+
                            |
                   web/  FastAPI: REST API + pages + media serving
                            |
                    jobs/  generation state machines
                        +---+---+
              orclient/  |       |  store/
              OpenRouter |       |  SQLite + files + metadata
                         +---+---+
                        budget/  ledger + cap
                        catalog/ capability + pricing cache
```

| Package | Responsibility | May depend on |
|---|---|---|
| `orclient/` | OpenRouter HTTP client: catalogue fetch, image generation, video submit/poll/download, key status, typed errors | HTTP only. **No filesystem, no database.** |
| `store/` | Path allocation, atomic writes, sidecar JSON, SQLite access, metadata extraction, thumbnails | Disk and database only. **Never calls OpenRouter.** |
| `catalog/` | Fetches capability and pricing data via `orclient`, persists it via `store`, serves it to `budget`, `jobs` and `web` | `orclient`, `store` |
| `budget/` | Estimation, ledger, reservation protocol, cap | `catalog`, `store` |
| `jobs/` | Generation state machines; boot-time poller reattachment | `orclient`, `store`, `budget`, `catalog` |
| `web/` | REST API, HTMX pages, range-serving media | `jobs`, `store`, `catalog`, `budget` |
| `mcp_server.py` | stdio MCP entrypoint | The REST API URL only |

Invariants worth defending in review:

- **`orclient/` never touches disk.** The provider integration is fully testable
  against recorded fixtures, with no filesystem and no spend.
- **`mcp_server.py` contains no business logic.** It translates MCP tool calls
  into HTTP requests, so the two interfaces cannot diverge.
- **`catalog/` exists to give the cache an owner.** Without it, neither
  `orclient` (no persistence) nor `store` (no network) could hold it.

### 4.2 Catalogue caching

Model capabilities and pricing are cached in SQLite (`model_catalog`,
`model_pricing`).

- Refreshed at startup and every `HIGGSHOLE_CATALOG_TTL_HOURS` (default `24`).
- Image pricing requires one request per model, so it is fetched **lazily on
  first use of a model** and then cached, rather than eagerly for all ~38 at
  boot.
- If a refresh fails, the stale cache continues to serve and a warning is
  surfaced. The application never blocks startup on catalogue availability.
- A manual refresh is available from Settings.

### 4.3 State machines

Image and video generation have **different shapes** and do not share a machine.

**Image (synchronous):**

```
PENDING -> GENERATING -> WRITING -> COMPLETE
   |
   +-> REJECTED (validation / cap)      -> FAILED
```

**Video (asynchronous):**

```
PENDING -> SUBMITTED -> RUNNING -> DOWNLOADING -> COMPLETE
   |       (job id                  (result
   |       persisted)                bytes)
   +-> REJECTED (validation / cap)   -> FAILED
```

Only **video** rows in `SUBMITTED` or `RUNNING` are reattached to pollers at
startup. Image rows can never occupy those states.

**Provider status mapping:**

| Provider status | Internal state | Notes |
|---|---|---|
| `pending`, `in_progress` | `RUNNING` | Continue polling |
| `completed` | `DOWNLOADING` → `COMPLETE` | Download immediately |
| `failed` | `FAILED` | Error string surfaced |
| `cancelled` | `FAILED`, reason `provider_cancelled` | Terminal; not operator-initiated |
| `expired` | `FAILED`, reason `provider_expired` | Retention window elapsed |
| *unrecognised* | `RUNNING` | Continue polling (§2.4) |

**Rules:**

- **Durability:** the provider job ID is committed to SQLite **before** polling
  begins.
- **Ordering:** local validation → budget gate → dispatch.
- **Timeout:** `HIGGSHOLE_JOB_TIMEOUT_MINUTES` (default `30`) bounds any
  non-terminal video job, after which it is `FAILED`.
- **Atomicity:** downloads write to `.part` and are atomically renamed.
- **Reconciliation:** reservations are reversed on every terminal state (§3.3).

### 4.4 Submission retry policy

- **Submission is never blindly retried.** `POST /api/v1/images` is synchronous
  and non-idempotent; a retry risks double-charging. On a connection error
  after the request is sent, the generation is marked `FAILED` with reason
  `indeterminate` and the operator is told the charge may have occurred.
- **HTTP 429** is retried with exponential backoff **before** dispatch is
  considered to have occurred, up to `HIGGSHOLE_MAX_RETRIES` (default `3`).
- **HTTP 5xx on submit** follows the same rule as connection errors.
- **Polling and download** are freely retryable, being idempotent GETs.

### 4.5 Lineage

An edit or improvement pass is an ordinary generation whose references point at
existing library assets. Because a generation may carry up to 16 references,
lineage is recorded in a **join table**, not a single parent column (§5.4).

---

## 5. Data model

### 5.1 On-disk layout

The media root is configurable (`HIGGSHOLE_MEDIA_ROOT`), defaulting to
`${XDG_DATA_HOME:-~/.local/share}/higgshole/media` so that an unprivileged
clone works with no setup. The provided systemd unit overrides it explicitly.

```
$HIGGSHOLE_MEDIA_ROOT/
├── projects/
│   ├── unsorted/                 <- default project
│   └── <project-slug>/
│       ├── images/
│       │   ├── 20260718-143022_a3f21c9d4e07_neon-city-street.png
│       │   └── 20260718-143022_a3f21c9d4e07_neon-city-street.json
│       ├── videos/
│       │   ├── 20260718-150411_b7e004aa1c32_drone-over-coast.mp4
│       │   └── 20260718-150411_b7e004aa1c32_drone-over-coast.json
│       └── uploads/
└── thumbs/
    └── <project-slug>/
        ├── a3f21c9d4e07.webp
        └── b7e004aa1c32.webp
```

**Identifiers** are **12 hex characters** (48 bits), with a `UNIQUE` constraint
on `generations.id` and generation-with-retry on collision. The earlier 6-char
form reached 50% collision probability at roughly 4,800 items — inside a year of
routine use.

**Thumbnails are sharded by project**, mirroring the media tree, so that a
project rename or delete has a single corresponding thumbnail directory and
cannot orphan or overwrite another project's files.

**Filename rule:** `{timestamp}_{id}_{slug}.{ext}` where `slug` is the prompt
NFKD-normalised, lowercased, non-`[a-z0-9]` runs collapsed to `-`, trimmed, and
**truncated to 60 characters**. An empty slug is omitted along with its
separator. This bounds the name well inside the 255-byte ext4 limit.

A `unsorted` project always exists, so agent-triggered generation never fails
for want of a project name.

### 5.2 State location

SQLite lives **outside the media root**, defaulting to
`${XDG_STATE_HOME:-~/.local/state}/higgshole/higgshole.db`
(`HIGGSHOLE_DB_PATH`).

Rationale: the media root is expected to be exported over a file-sharing
protocol. A database reachable by remote clients risks corruption through
incompatible locking, and stored API keys should not sit in a shared tree.

### 5.3 Source of truth, and its limits

The database is a **partially rebuildable index**.

1. Every generation writes a `.json` sidecar beside its media file.
2. Parameters are embedded **into the media file** — PNG `tEXt`, EXIF
   `UserComment` for JPEG/WebP, MP4 metadata tags — carrying prompt, model,
   seed and settings.
3. `rescan` reconstructs `generations`, `assets`, `generation_inputs` and
   project rows from sidecars.

**What `rescan` cannot recover:** `spend_ledger` and `settings` exist nowhere on
disk. Losing the database therefore loses the authoritative local spend record
and all stored credentials. These are **not** covered by the "rebuildable"
claim, and the state directory should be included in any backup. Layer 1
(§3.2) is unaffected, being provider-side.

### 5.4 Tables

| Table | Columns |
|---|---|
| `projects` | `id`, `slug` (unique), `name`, `created_at` |
| `generations` | `id` (12-hex, unique), `project_id`, `kind` (`image`\|`video`), `model`, `prompt`, `params` (JSON), `state`, `provider_job_id`, `file_path`, `error_reason`, `error_detail`, `created_at`, `updated_at`, `completed_at` |
| `generation_inputs` | `generation_id`, `asset_id`, `role` (`input_reference`\|`first_frame`\|`last_frame`), `position` |
| `assets` | `id`, `generation_id` (nullable — uploads have none), `kind` (`upload`\|`output`\|`thumbnail`\|`poster`), `file_path`, `mime_type`, `bytes`, `width`, `height`, `duration_s` (nullable), `created_at` |
| `spend_ledger` | `id`, `generation_id`, `kind` (`reservation`\|`reversal`\|`actual`), `amount` (signed USD), `cost_known` (bool), `recorded_at` |
| `model_catalog` | `model_id`, `kind`, `capabilities` (JSON), `fetched_at` |
| `model_pricing` | `model_id`, `pricing` (JSON), `fetched_at` |
| `settings` | `key`, `value` |

`generation_inputs` replaces a single `parent_id`, since a generation may carry
up to 16 references plus first/last frames. Lineage is walked through it.

### 5.5 Batch generation

`n` is **fixed at 1**. The UI does not expose it and the MCP layer rejects
`n > 1`.

Rationale: `n > 1` returns multiple images under a single `usage.cost`, which
would require a cost-attribution rule and a one-row-many-files representation.
This is deferred rather than half-specified.

---

## 6. Interfaces

### 6.1 Web UI

| Screen | Contents |
|---|---|
| **Create** | Prompt, image/video toggle, model picker (favourites pinned, catalogue behind an expander), capability-derived controls, reference slots, file upload, cost estimate, remaining allowance |
| **Library** | Thumbnail grid; filter by project, type, model, date; poster frames and duration badges for video; delete |
| **Detail** | Full view or playback; metadata panel; lineage; "Use as input", "Regenerate", "Delete" |
| **Jobs** | In-flight generations with live status over SSE |
| **Settings** | API keys, daily cap, favourites, catalogue refresh, rescan |

Generation controls are **rendered from discovered capabilities**. Unsupported
options are not offered, including reference slots, which appear only in the
quantity a model accepts.

### 6.2 REST API and MCP tools

Both interfaces expose the same operations; MCP tools are a thin translation.

| Tool | Behaviour |
|---|---|
| `list_models` | Image and video models with capability constraints |
| `generate_image` | Synchronous; returns the finished asset |
| `generate_video` | Returns a job ID immediately — **does not block** |
| `get_job` | Status; optional long-poll bounded by a caller-supplied timeout |
| `upload_asset` | Ingests a local file into a project's `uploads/`, returning an asset ID usable as a reference |
| `list_media` | Browse with filters |
| `get_media` | Full metadata for one item |
| `delete_media` | Removes a generation, its files and its thumbnails |
| `list_projects` | Enumerate projects |
| `create_project` | Create a project |
| `get_budget` | Provider-authoritative remaining credit plus local cap status |

`generate_video` is non-blocking by design: a multi-minute render inside a
single tool call invites client timeouts.

`upload_asset` closes the ingress gap — without it an agent holding a local
image has no way to use it as a reference.

Every asset-returning tool provides **both the local filesystem path and the
HTTP URL**, since agents run on the same host.

### 6.3 Media serving

`FileResponse` supports HTTP Range natively as of **Starlette 0.39.0**
[verified empirically across 0.38.6, 0.52.1 and 1.3.1]. Browser seeking works
with no custom code. `StaticFiles` delegates to `FileResponse` and behaves
identically.

Two verified hazards:

1. **`GZipMiddleware` corrupts 206 responses.** It compresses partial content
   while `Content-Range` describes the uncompressed entity, and does not exempt
   `video/mp4`.
2. **`BaseHTTPMiddleware` that buffers or rewrites `Content-Length` breaks
   ranges.**

**Mechanism (not merely a requirement):** the application subclass **overrides
`__call__`** and dispatches requests whose path begins with a media prefix
directly to the media handler **before** delegating to `super().__call__()`.
Media responses therefore never enter the middleware stack at all.

Mounting a sub-application was tested and found **insufficient**. On Starlette
1.3.1 with FastAPI, `Starlette.__call__` delegates to `self.middleware_stack`,
which wraps the entire router *including* mounts, so a mounted media app is not
exempt. Measured against `GZipMiddleware` with a 206 partial-content response:

```
app.mount("/media", sub_app)  -> 206 | content-encoding: gzip   <- corrupted
override __call__             -> 206 | content-encoding: None   <- clean
```

§11 requires a regression test asserting a 206 response carries no
`Content-Encoding`, so a future middleware addition cannot silently reintroduce
the fault. That test is only meaningful because the negative control — a plain
mount — demonstrably fails it.

---

## 7. Security

The service runs without authentication, as specified. The following measures
are included because they are inexpensive and address real exposure.

**Path traversal is prevented unconditionally.** Every media-serving endpoint
resolves the requested path and asserts containment within the media root
before opening a file. Without it, a crafted path exposes the filesystem to any
host on the network. This is not optional.

**API key handling:**

- Keys are stored in the database outside the shared media tree, mode `0600`.
- Keys are **write-only through the UI**. After saving, only a masked suffix is
  returned to a client.
- Keys are validated on save via `GET /api/v1/key`, which costs nothing.
- A client-side `sk-or-v1-` prefix check runs before submission. The server does
  distinguish absent from malformed keys, but its messages are misleading
  (a malformed key yields *"Missing Authentication header"*), so pasting a key
  from another provider would otherwise produce a confusing error.
- Separate image and video keys are supported, each falling back to a shared
  default, permitting video spend to be capped independently at the provider.

**Encryption at rest is deliberately not implemented.** A service that must
decrypt keys unattended gains no meaningful protection from it.

**Network:** binds to `127.0.0.1` by default. Exposure to a local network is a
deliberate act, requiring `HIGGSHOLE_BIND_HOST` to be changed.

---

## 8. Configuration

All settings are environment variables, readable from a `.env` file. Keys may
alternatively be set through the UI, which persists them to the database. The
database overlays the environment rather than being seeded from it: each key and
the daily cap are resolved per request, taking the stored value when one is set
and falling back to the environment otherwise. A value saved in the UI therefore
takes effect immediately without a restart, and clearing it restores the
environment value. An environment value remains required for headless or MCP-only
operation, where there is no UI to set one.

| Variable | Default | Purpose |
|---|---|---|
| `HIGGSHOLE_OPENROUTER_API_KEY` | — | Default key for both media types |
| `HIGGSHOLE_OPENROUTER_API_KEY_IMAGE` | falls back to default | Image-only key |
| `HIGGSHOLE_OPENROUTER_API_KEY_VIDEO` | falls back to default | Video-only key |
| `HIGGSHOLE_MEDIA_ROOT` | `${XDG_DATA_HOME:-~/.local/share}/higgshole/media` | Media tree |
| `HIGGSHOLE_DB_PATH` | `${XDG_STATE_HOME:-~/.local/state}/higgshole/higgshole.db` | Database |
| `HIGGSHOLE_BIND_HOST` | `127.0.0.1` | Listen address |
| `HIGGSHOLE_BIND_PORT` | `8077` | Listen port |
| `HIGGSHOLE_DAILY_CAP_USD` | unset (no cap) | Local daily spend cap |
| `HIGGSHOLE_MAX_JOB_COST_USD` | `2.00` | Pessimistic reservation for non-estimable jobs |
| `HIGGSHOLE_MAX_IN_FLIGHT` | `3` | Concurrent job ceiling |
| `HIGGSHOLE_JOB_TIMEOUT_MINUTES` | `30` | Video wall-clock ceiling |
| `HIGGSHOLE_POLL_INTERVAL_SECONDS` | `5` | Video poll cadence |
| `HIGGSHOLE_MAX_RETRIES` | `3` | Retry budget for 429 and download failures |
| `HIGGSHOLE_CATALOG_TTL_HOURS` | `24` | Catalogue refresh interval |
| `HIGGSHOLE_REFERENCE_TRANSPORT` | `data_uri` | Reference image transport (§2.8) |
| `HIGGSHOLE_LIVE_TESTS` | unset | Enables the paid smoke test (§11) |
| `HIGGSHOLE_API_BASE` | `http://127.0.0.1:8077` | REST API base URL used by the MCP server |

`HIGGSHOLE_API_BASE` is read by the MCP client process to locate a *running*
REST API, not by the server itself, and is therefore deliberately not part of
`Settings`.

A `.env.example` documenting all of the above ships with the repository.

---

## 9. Deployment

A systemd unit is **provided but not required**; the application runs anywhere
Python 3.12 and ffmpeg run.

`deploy/higgshole.service.example` ships with `@USER@` and `@INSTALL_DIR@`
placeholders and `EnvironmentFile=-/etc/higgshole/higgshole.env`, so no
machine-specific values are committed. Deployment documentation covers creating
the service user and state directories.

Unit properties:

- `Restart=always`, ordered `After=network-online.target`
- Hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`,
  `ProtectKernelTunables`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`,
  with `ReadWritePaths` limited to the media root and state directory

**uvicorn runs with exactly one worker.** Video pollers are in-process asyncio
tasks; multiple workers would each reattach a poller to the same job at boot,
causing duplicate downloads and double-counted spend. The reservation lock
(§3.3) is likewise process-local and assumes a single worker.

---

## 10. Error handling

| Condition | Behaviour |
|---|---|
| Hard-invalid parameter | Rejected locally, citing the constraint (§2.7) |
| Advisory parameter mismatch | Warned, dispatched (§2.7) |
| Local daily cap reached | `REJECTED`, never dispatched; remaining reported |
| Provider 402 | Surfaced distinctly from the local cap, naming the provider key limit |
| Moderation refusal | Distinct error type carrying the provider's reason |
| ZDR enabled on account | Detected on video failure; explains that video is ineligible for Zero Data Retention and cannot route |
| HTTP 429 | Backoff and retry before dispatch (§4.4) |
| Connection error after submit | `FAILED`, reason `indeterminate`; operator warned a charge may have occurred |
| Unknown job status | Non-terminal; polling continues |
| `cancelled` | `FAILED`, reason `provider_cancelled` |
| `expired` | `FAILED`, reason `provider_expired`; explains retention lapse |
| Download 502 | Retried with backoff, then terminal — retention may have lapsed |
| Wall-clock timeout | `FAILED`; reservation reversed |
| Missing `usage.cost` | Reservation stands as charge; day marked "≥" (§3.4) |
| Interrupted download | `.part` discarded; never renamed into place |

---

## 11. Testing

| Layer | Approach |
|---|---|
| `orclient/` | Recorded fixtures — no network, no spend. Covers all six job statuses, an unknown status, and documented error shapes |
| `store/` | `tmp_path` and in-memory SQLite; round-trips embedded metadata; slug truncation and ID collision retry |
| `budget/` | Real `pricing_skus` payloads including the `cents_per_*` unit trap, token-priced models yielding `null`, the Kling audio-axis collision, ledger reversal arithmetic, and null-cost handling |
| `jobs/` | State machines against a fake client; restart-mid-job reattachment; concurrent submission cannot exceed the cap |
| `web/` | Range requests (206, suffix ranges, 416); **regression test asserting 206 carries no `Content-Encoding`**; path-traversal rejection; key masking |
| `mcp_server.py` | Tool schemas and HTTP translation against a stubbed API |
| Live smoke | One opt-in test behind `HIGGSHOLE_LIVE_TESTS`, naming the model used and its approximate cost |

---

## 12. Open items

1. **Verify whether video `frame_images` accepts base64 data URIs** (§2.8).
   Determines whether image-to-video is available on a private-network
   deployment. Resolve with one cheap live generation before §6.1's video
   reference slots are built.
2. **Confirm the OpenRouter per-key credit-limit workflow** and document the
   operator setup steps. Layer 1 (§3.2) is the authoritative guard, and its
   configuration procedure is currently unverified.
3. **Measure result-URL retention empirically** on the first successful video
   job and record the observed window (§2.5).
