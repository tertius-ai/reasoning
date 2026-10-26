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
access to its source topic. Completed original bytes cannot be replaced. Writers
may explicitly retry PDF text extraction without changing that original identity.
Attachments are included in database backups; there is no external public bucket.

## Agent context

Only same-project attachments explicitly referenced by selected source messages are considered
for context. Up to eight references fit the existing context allowance. PDF
uploads extract page-marked text in a separate Python process using pypdf,
outside database transactions. At most two extractions run per API process.
Each has a 12-second wall deadline, up to 200 pages and 2 MiB of UTF-8 text;
POSIX workers additionally enforce 512 MiB address-space and eight CPU-second
limits. NUL characters and invalid Unicode are replaced. Coverage, truncation,
password requirements, empty text, and parser/resource failures are reported
explicitly. Successful upload does not promise readable text. Writers can retry
extraction from the original attachment through its PDF controls. Scanned pages
need OCR or a TeX/text source. Compatible uploader text is retained as an explicitly
partial fallback when server extraction fails.

Extracted text and its mathematical content remain unverified. Native models
receive text rather than original PDF bytes; authorized CLI bots can download
the original bytes through their existing confined media tools. For a PDF,
`media_read` reports `text_status` (`server_extracted`, `uploader_extracted`, or
`unavailable`) and extraction coverage, plus a notice naming the limitations and
source alternatives. `media_read` retrieves bounded text
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

## Project text files

**Project files** above the beta message box, or `/files` in beta/gamma topics,
opens saved project text and the current topic's attachments. TeX and code display
as source. Previews contain up to 16,000 characters; **Open source**, **Copy complete source**, and **Download complete file**
retrieve the full SHA-256-verified revision, including chunked files. The content
URL names both the path and revision and rechecks current topic/project access;
editing the file does not change an earlier link. Downloads preserve UTF-8 bytes
and the original filename, with private no-store responses.

`artifact_write`, `artifact_list`, and `research_file_read` return exact revision
links. Model replies presenting a saved file should include that link. Whether or
not they do, each model or bot reply shows one chip per project file its own run
saved, read from that run's completed `artifact_write` receipts: the file name,
the last revision that run saved, its length, a download action and an action
that opens that revision in **Project files**. Chips are confirmed against the
retained revision in the topic's project. Local CLI
workspace files remain private until explicitly published; `workspace_publish_text`
publishes up to 30 KiB of UTF-8 source through the bot's live `artifact_write` grant.
Overwrites require the current SHA-256. Publication does not compile TeX or verify
the mathematics. File output in chat can therefore link the saved source and a
separately generated PDF without asking the user to recover text from a session.

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
- `POST /{id}/{sha256}/extract` retries bounded extraction from the immutable PDF for a current topic writer.

Chunk transport uses the standard 64 KiB JSON request limit. Exact chunk and finish
retries are idempotent; a changed retry is refused. Browsers retain the original
file on the user's device if an interrupted upload must be restarted.
Bot upload/download chunk transfer has a shared 512-chunk-per-minute allowance; beginning and
finishing a file still count toward the ordinary bot write quota. Chunk admission
requires a current authorized lease, and each immutable 32 KiB write remains
subject to the declared file and organization storage limits.
