# Pictures, PDFs and TeX

Attach a PNG, JPEG, WebP or PDF in an alpha, beta or gamma topic. The attachment
control uploads the file privately and inserts a Markdown reference in the
composer. Sending that message makes the attachment part of the conversation.
The `/attach` command opens the same file chooser in every topic kind.
Pictures display inline; PDFs expand into the local PDF.js canvas reader. Raw
HTML, SVG, remote images, tracking pixels and remote PDF embedding are disabled.
Research files includes the current topic's attachments alongside project text
files, including editable `.tex` sources.

Every attachment reference contains its organization, source topic, immutable
file ID and SHA-256. Upload, list, preview, download and tool reads check current
topic permissions. A reference copied to another conversation does not grant
access to its source topic. Completed bytes and previews cannot be replaced.
Attachments are included in database backups; there is no external public bucket.

## Agent context

Only same-project attachments explicitly referenced by selected source messages are considered
for context. Up to eight references fit the existing context allowance. A PDF
upload extracts page-marked text from successive pages, up to 60,000 UTF-8 bytes,
in a browser worker with a 15-second extraction deadline; pages read before the
deadline or a damaged page are kept. NUL characters and unpaired surrogates are
replaced. Scanned PDFs need OCR elsewhere. Extracted text is untrusted
uploader-supplied reference data, not a server-verified transcription; the
original PDF hash remains its identity. The server does not parse PDFs, and no
model receives PDF bytes, whatever file input it advertises. For a PDF,
`media_read` reports `text_status` (`uploader_extracted` or `unavailable`) and a
notice naming what may be missing and the alternatives, such as arXiv source
tools or a member pasting the passage. `media_read` retrieves bounded text
slices and exact metadata, with optional `source_topic_id` for an explicitly
referenced same-project topic. Bots must additionally be granted that source
topic, and both requester and owner must still have access. It does not pretend
to inspect pictures or PDF diagrams.

Browser image uploads also produce a reduced JPEG projection (maximum 512 pixels
on either side, 32 KiB). Up to two projections enter context only when every
selected OpenRouter model advertises image input and the conservative serialized
context allowance can accommodate them. Both original and projection hashes are
captured. Text-only models, large omitted previews and insufficient context retain
explicit metadata-only inspection descriptions. Provider calls recheck captured
attachment access for references or captured pixels actually supplied in the call;
a queued snapshot does not bypass later permission revocation. Delegated agents
receive only attachments explicitly named in their assignment, including matching
captured image projections. Parent conversation and unrelated attachment receipts
do not enter the delegated context.
Successful native `media_read`, `media_download_chunk` and `media_finish` calls capture their source
receipts atomically with tool completion, so newly read text also undergoes the
next provider call's access check.
Reservations include advertised per-image fees where supplied, alongside the
existing conservative context estimate; provider usage remains authoritative.
Gamma model comparisons and AI workflow steps capture only attachment references
in their explicit prompt and dependency inputs, not surrounding topic history.
The same vision and current-access checks apply without enabling research tools
or a persistent agent thread for a comparison. An idempotent retry keeps its
original captured attachment context.

## Bot PDF generation

An owner-connected bot with knowledge read access can use `media_list`,
`media_read` and `media_download_chunk` in its active leased topic; publishing
also needs knowledge write access for `media_begin`, `media_chunk` and
`media_finish`. These tools recheck the lease and owner/requester access inside each storage
transaction. `research_file_read` retrieves saved project text, including TeX.
`workspace_publish_media` publishes an owned regular file from the confined topic
workspace; host paths, symlinks and hardlinks are refused.
`workspace_fetch_media` verifies a complete 8 MiB maximum download against its
exact SHA-256 before publishing a private file atomically inside the topic
workspace. It does not overwrite a different file; exact repeated copies are
reused after current access is rechecked. No remote URLs are accepted.
`workspace_view_image` rechecks live source access and the fetched file's hash,
then delivers bounded PNG/JPEG/WebP bytes as MCP image content rather than
pretending a text description inspects pixels. Actual image support depends on
the connected CLI and selected model. SVG/PDF are not image inputs. Read-only
independent reviewers have `media_read`, not binary download or publishing tools.

Enable local execution on the bot and install system `pdflatex` and Linux
`bubblewrap`. The `tex_compile` tool compiles a saved workspace `.tex` source after
the bot owner approves the exact command in Reasoning. It runs with
`-no-shell-escape` in the existing networkless sandbox, exposing the topic
workspace and read-only system toolchains, not the owner's home or credentials.
The primary source is staged as a read-only copy in a reserved, hash-named
directory and mounted read-only into the sandbox. The approved command includes
that hash-bound path. A fresh output directory ensures successful zero-output
commands cannot publish a stale PDF. Successful compilation publishes the new
regular PDF and records the primary source and PDF hashes. Included workspace
files remain live dependencies, not a fully pinned TeX source bundle.
Changed staged sources, failed compilation, owner denial and missing dependencies
do not publish. The server never runs TeX. One compilation pass is performed;
inspect warnings and the PDF, particularly citations and cross-references.
Compilation is not mathematical verification. Modal compute profiles do not
implicitly include TeX.

For example, ask the connected bot: “Write `note.tex` in this topic's workspace,
compile it with `tex_compile`, and post the returned PDF attachment.” Approve the
execution from the bot owner's account. Rebuild/restart the bot against the same
release to expose the local compilation and publication tools.
In beta and gamma topics, `/tex` opens a source editor with connected locally-enabled
bots. Preparing inserts an exact named-bot request into the composer; sending
starts the request using that bot's advertised default CLI model. Preparation
alone does not execute, authorize, or enqueue anything.

Topic JSON includes completed attachment metadata and authenticated references,
not attachment bytes. Compact diagnostic receipts replace encoded binary payloads
with their length and SHA-256; the full trace retains recorded tool payloads.

## Limits and API

Files are at most 8 MiB; the sum of declared original file bytes is at most 256 MiB per
organization and at most 10,000 completed/pending files. Each uploader can hold
32 pending uploads and begin at most 30 files per minute. Byte, row-count and
pending-count admission share an atomic organization lock. Base64 storage,
indexes and previews add database overhead beyond declared original bytes.
Image headers are checked without decoding: maximum 8,192 pixels
per side and 32 million pixels. Unfinished uploads owned by an uploader expire
after an hour and are reclaimed on that uploader's next upload. Pending uploads
consume their full declared size until reclaimed. PDF display accepts at most
200 pages and bounds canvas dimensions; larger documents remain downloadable.
Bot text workspace reads retain their 128 KiB limit; binary publication supports
8 MiB. There is no automatic quota expansion or remote fetch.

Under `/api/organizations/{org}/topics/{topic}/media`:

- `POST` declares name, MIME, size and SHA-256; it returns an upload ID.
- `PUT /{id}/chunks/{index}` accepts an immutable base64 chunk of at most 32 KiB.
- `POST /{id}/finish` verifies bytes, format and digest and returns the private reference.
- `GET` lists completed files in this topic only.
- `GET /{id}/{sha256}/content` serves authenticated bytes, with no public caching.
- `GET /{id}/{sha256}/read?offset=0&length=12000` returns a bounded text preview and metadata.

Chunk transport uses the standard 64 KiB JSON request limit. Exact chunk and finish
retries are idempotent; a changed retry is refused. Browsers retain the original
file on the user's device if an interrupted upload must be restarted.
Bot upload/download chunk transfer has a shared 512-chunk-per-minute allowance; beginning and
finishing a file still count toward the ordinary bot write quota. Chunk admission
requires a current authorized lease, and each immutable 32 KiB write remains
subject to the declared file and organization storage limits.
