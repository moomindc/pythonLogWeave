# LogWeave — Product Requirements Document

> **Working name.** `LogWeave` is a placeholder; rename freely. The package name
> used throughout the spec is `logweave`.

| | |
|---|---|
| **Status** | Draft 1 — for review |
| **Date** | 2026-11-07 |
| **Owner** | Dan |
| **Lineage** | Inspired by [ptmcg/logmerger](https://github.com/ptmcg/logmerger) (MIT), BareTail / BareTailPro, Notepad++ DocMonitor |

---

## 1. Problem

Diagnosing a fault in a multi-component application means reading several log
files at once — an app log, a service log, a web server access log, maybe a
database log — written by different components, in different formats, possibly on
different machines. The information needed to understand a failure is the
*ordering* of events across those files, and that ordering is exactly what no
existing tool shows well:

- **Tailing N windows side by side** forces the human to do the interleaving by
  eye, across independently scrolling panes. This fails as soon as events are
  milliseconds apart.
- **logmerger** solves the interleaving properly, and its timestamp-format
  auto-detection is genuinely good — but it is a terminal UI, it loads whole
  files into memory, and it has no live-follow mode.
- **BareTail** has an excellent follow-tail engine, highlighting, and handles
  files over 2GB — but shows files in *tabs*, with no merging.
- **Notepad++ / DocMonitor** monitors a file for changes but is an editor first:
  no merging, no timeline, no multi-file view.

Nothing combines *correct timestamp interleaving*, *live follow*, and a *GUI that
stays responsive on multi-GB files*.

## 2. Product vision

A Windows-first desktop application that takes any number of log files, works out
each one's timestamp format automatically, and presents them as a **single
scrollable stream in true chronological order** — syntax-highlighted by severity,
annotated so you can always see which file each line came from, with a
Sublime-style minimap and a wall-clock timeline for navigation, updating live as
the files grow.

**The one-sentence test:** drop five log files on the window and immediately see
what happened, in order, as it happens.

## 3. Goals

| # | Goal |
|---|---|
| G1 | Merge an arbitrary number of log files into one correctly time-ordered view |
| G2 | Detect timestamp formats automatically, with no configuration for common formats |
| G3 | Stay responsive — smooth scrolling, instant seek — on files from KB to multiple GB |
| G4 | Follow files live (`tail -f` semantics) including rotation and truncation |
| G5 | Make severity visible at a glance: highlighted lines, and severity visible in the minimap without scrolling |
| G6 | Always show which file a line came from, in minimal horizontal space |
| G7 | Let a user navigate backwards in time quickly and precisely |
| G8 | Save a reusable **profile** per application, which re-opens that application's logs as they stand on a later day |
| G9 | Drag-and-drop to open: drop files on the window, they merge |
| G10 | Keep the door open to non-file sources (Windows Event Log, syslog) without rework |

## 4. Non-goals

| # | Non-goal | Rationale |
|---|---|---|
| N1 | **Text editing.** The view is read-only. | Users copy data out; they do not edit logs. Removes a large surface: undo, save, encoding round-trips, dirty-state tracking. |
| N2 | A TUI or CLI as the primary interface | Explicitly a GUI product. A small CLI *launcher* (`logweave a.log b.log`) is in scope; a CLI *viewer* is not. |
| N3 | Log aggregation server or storage backend | Not competing with Splunk / ELK / Loki. Single-user desktop tool, no daemon. |
| N4 | Parsing log *fields* into a structured schema | Timestamp and severity are extracted. Beyond that, a line is text — including for CSV and JSONL, whose records are shown verbatim (D17). |
| N5 | Alerting, dashboards, metrics extraction | Out of scope. |
| N6 | Cross-file transaction / request correlation | Interesting, deferred. Search covers the common case. |

## 5. Decisions taken

These were open questions; they are now settled and the spec is built on them.

| # | Decision | Consequence |
|---|---|---|
| D1 | **Naive local time only.** Zone-aware timestamps convert to machine-local and then drop the zone; naive timestamps are used as written. No timezone machinery in the merge core. | Simple and fast. **Accepted risk:** logs from machines in different timezones interleave wrongly, and ordering within a DST fall-back hour is undefined. Mitigated by D2. |
| D2 | **Per-file ± skew offset** is a first-class, phase-1 feature. | A constant added to one file's timestamps at merge time; the view re-orders live as it is adjusted. Covers clock drift *and* is the escape hatch for the D1 risk — apply a whole-hour offset to a file from another zone. |
| D3 | **Windows Event Log is phase 2**, but v1 ships the `LogSource` abstraction it will plug into. | No rework later. v1 implements only file-backed sources. |
| D4 | **Both minimap modes, toggleable.** A row-proportional minimap (Sublime-style, drives the scrollbar) *and* a separate wall-clock timeline strip. Either can be collapsed; the choice persists per profile. | Two render paths to keep fast. Gives both gap-visibility and linear scrolling. |
| D5 | **v1 sources:** local files, UNC / network shares, and remote files over SSH/SFTP — all with follow. | SSH adds a credential surface and a poll-based follow path. SMB needs poll-based watching, because change notifications are unreliable over the wire. |
| D6 | **syslog network listener is out of v1 scope.** | Designed for via `LogSource`; needs a spill-to-disk ring buffer. Phase 3. |
| D7 | **Python + Qt via PySide6** (LGPLv3), not PyQt6 (GPL / commercial). | PySide6 can be shipped inside a closed-source binary without a commercial licence. Verified: PySide6 6.12.0 installs on the target Python 3.14.5. |
| D8 | **No C / native code until a benchmark demands it.** | A fast-path byte-slicing parser and NumPy vectorisation are tried first; an optional accelerator module is a later, isolated change. See SPEC §14. |
| D9 | **Copied and exported rows are attributed to source file + original line number** (`app.log:1`, `SOA.log:7`). | The index must carry each entry's line position within its own file. Costs 4 bytes per entry and no extra compute (SPEC §2.3, §4.4). |
| D10 | **Profiles store absolute paths only.** No `${VAR}` expansion, no portable root. | Simpler and unambiguous. A profile is therefore machine-specific; sharing one between machines is not supported in v1. |
| D11 | **Built-in severity rulesets ship as defaults and are fully user-extensible.** | Users add, edit, reorder and disable rules, and retarget them at severity classes. The eight severity *classes* stay fixed (three bits in the index, §10.2); the patterns and labels mapping onto them are open. |
| D12 | **`.gz` is decompressed once into the cache directory**, then indexed as a plain file. | Simple and gives full random access. Costs disk equal to the uncompressed size; checked up front and refused with a clear message if short. |
| D13 | **Time is the only ordering.** No alternative sort modes. | Removes a whole axis of UI and index work. |
| D14 | **SSH auth by key file, agent, and interactive password.** | Password is prompted per session, or stored in the OS credential vault at the user's choice — never in the profile (NFR-26). |
| D15 | **MIT licence, with attribution.** | `NOTICE` credits logmerger (MIT) for the timestamp-format catalogue. PySide6 stays dynamically linked and unmodified to satisfy LGPLv3 (NFR-25). |
| D16 | **A rotated predecessor is just another file.** `app.log` and `app.log.1` are two independent sources with their own lanes. | No "source group" concept at any phase. Keeps identity, indexing and lane assignment simple. |
| D17 | **CSV and JSONL show their record as raw text** in the message column, verbatim. No field extraction, no per-field columns. | Zero new UI and the fastest path to shipping; search and highlighting work unchanged because they match against the raw text. **Accepted cost:** a JSON record is mostly punctuation and key names, so the readable part is buried and the row is wider than the window. This makes horizontal scrolling (FR-36a) a must, not a nice-to-have. A readable-message-plus-detail-pane treatment is the obvious post-v1 enhancement. |

## 6. Users and scenarios

**Primary user:** an engineer debugging a multi-component application on Windows —
developer, support engineer, or SRE. Comfortable with logs, not interested in
configuring a parser.

### S1 — Post-mortem on a log bundle *(primary)*

A customer sends a zip of eight logs totalling 3GB. The user extracts it, selects
all eight, drags them onto LogWeave. Within a second the first screen is readable;
indexing continues in the background with progress shown. They see a red band in
the timeline at 14:22, click it, and read the interleaved sequence across all
eight components around the failure.

### S2 — Watching a live reproduction *(primary)*

The user opens their saved "Acme Gateway" profile. It reopens the six logs that
application writes, in follow mode, pinned to the tail. They trigger the bug in
the app and watch the error appear, in order, alongside what the other five
components were doing in the same millisecond.

### S3 — Scrolling back in time

While following, the user scrolls up. Follow auto-pauses. They jump back two hours
via the timeline, read, then press `End` to snap back to live tailing.

### S4 — Narrowing down

Too much noise. The user filters to ERROR and WARN only, finds a correlation ID,
searches for it, then switches the filter off to read full context around the hits
— which stay marked in the minimap.

### S5 — Building a profile

After assembling the right set of files, highlight rules, aliases and column
layout for an application, the user saves it as a profile. Six weeks later they
open it and it resolves `C:\Acme\logs\gateway-*.log` to whatever files exist
*that day*.

### S6 — Copying evidence out

The user selects 40 rows spanning four files and copies them into a ticket. Each
pasted row is **attributed back to the file and the line it came from**, so a
reader of the ticket can go and find it:

```
app.log:1      2026-10-04 14:22:08.113  Connection pool exhausted
SOA.log:7      2026-10-04 14:22:08.117  WARN  retry 1/3 scheduled
app.log:2      2026-10-04 14:22:08.120  ERROR upstream timeout after 30s
                   at Gateway.forward(Gw.java:88)
iis.log:88412  2026-10-04 14:22:08.204  GET /api/v2/order 502 30011
```

The line number is the line's **original position in its own source file**, not
its position in the merged view — that is the number that lets someone open
`SOA.log` in any editor and land on the right line.

## 7. Functional requirements

Priority: **M** = must (v1), **S** = should (v1 if it fits), **C** = could (post-v1).

### 7.1 Opening and sources

| ID | Pri | Requirement |
|---|---|---|
| FR-1 | M | Open any number of files via drag-and-drop onto the window, a file dialog, or command-line arguments. |
| FR-2 | M | Dropping a **folder** offers its log-like files for selection rather than failing. |
| FR-3 | M | Support plain text of any extension, gzip (`.gz`), CSV (timestamp in a selectable column), and JSONL (timestamp in a selectable key). |
| FR-4 | M | Support local paths and UNC paths such as `\\server\share\app.log`. |
| FR-4a | M | **Best-effort reading of exclusively-locked files.** Where a writer has opened a log without sharing, escalate through a documented ladder of techniques (SPEC §7.1.1) and read as much as possible. Report plainly which technique succeeded, or why none did, and keep the rest of the session working. |
| FR-5 | M | Handle UTF-8, UTF-8-with-BOM, UTF-16LE/BE, and legacy single-byte codepages; detect per file, allow manual override, never crash or hang on malformed bytes. |
| FR-6 | M | Handle CRLF, LF and lone-CR line endings, including mixed within one file. |
| FR-7 | S | Open and follow files on a remote host over SSH/SFTP. |
| FR-7a | S | SSH authentication by **key file, SSH agent, and interactive password**. A password is never written into a profile or any plaintext file; it is either prompted per session or, at the user's choice, stored in the OS credential vault (Windows Credential Manager / DPAPI). |
| FR-8 | M | Adding a file to an already-open view merges it in without re-reading the others. |
| FR-9 | M | Remove, or temporarily hide, a file from the view without closing the session. |
| FR-10 | C | Open `.evtx` files and live Windows Event Log channels *(phase 2)*. |
| FR-11 | C | Receive syslog over UDP/TCP *(phase 3)*. |

### 7.2 Timestamp detection and merging

| ID | Pri | Requirement |
|---|---|---|
| FR-12 | M | Auto-detect each file's timestamp format from a sample of its lines — **not from the first line alone** — covering at minimum every format logmerger supports (SPEC §4). |
| FR-13 | M | Report the detected format per file in the UI, and allow the user to override it with a chosen or custom format. |
| FR-14 | M | Formats lacking a year, or a date entirely (syslog `Mon DD HH:MM:SS`, strace `HH:MM:SS.ffffff`), infer the missing part and handle **rollover** across midnight and New Year within a file. |
| FR-15 | M | Lines with no timestamp attach to the preceding timestamped entry as a continuation (stack traces, multi-line messages). Toggleable. |
| FR-16 | M | Correctly handle a file whose timestamps are **not monotonic**, rather than assuming sorted input. |
| FR-17 | M | Entries with identical timestamps have a stable, documented order: by file-open order, then by line number. |
| FR-18 | M | Apply a user-set per-file ± offset (D2), with the view re-ordering live. |
| FR-19 | M | Gracefully handle a file in which no timestamp format is found: offer to show it as timestamp-less, assign a format manually, or skip it. |
| FR-20 | S | Clip the view to a time window, including "clip to the span of file X". |

### 7.3 Live follow

| ID | Pri | Requirement |
|---|---|---|
| FR-21 | M | Follow mode: appended lines appear in the merged view automatically, in correct position, within 200ms. |
| FR-22 | M | Auto-scroll to tail while following; **scrolling up pauses auto-scroll** without stopping ingestion; `End` resumes. |
| FR-23 | M | Detect and handle log **rotation** (file renamed or replaced) and **truncation** (file shrank), re-opening or re-indexing as needed, without losing already-read history. |
| FR-24 | M | Follow is per file, independently toggleable, and toggleable globally. |
| FR-25 | M | A file that is deleted or becomes unreachable (share disconnect) is marked stale in the UI; already-read content stays viewable. |
| FR-26 | S | A visible indicator of newly arrived lines while auto-scroll is paused ("N new lines below"). |

### 7.4 The view

| ID | Pri | Requirement |
|---|---|---|
| FR-27 | M | Read-only, virtualised, monospace merged view. Column order, left to right: marks gutter, timestamp, message, **source lanes (slim, right)**, row-minimap, timeline. |
| FR-28 | M | **Source lanes:** one narrow (configurable 4–12px) vertical lane per file on the right, each with a stable distinct colour; a row's lane is filled to show its origin. Scales to ~20 files in ~120px. |
| FR-29 | M | An optional short **source alias** text column. The alias defaults to the shortest unique distinguishing part of the filename and is user-editable. |
| FR-29a | S | An optional **original line number** column, showing each entry's line position within its own source file (not its merged-view position). Off by default; the underlying value is always available for copy and export (FR-46a). |
| FR-30 | M | Keyword and regex **syntax highlighting** of lines and spans. Built-in default rules for ERROR/FATAL/CRITICAL/SEVERE, WARN/WARNING, INFO, DEBUG/TRACE, plus exception and stack-trace shapes. |
| FR-31 | M | Highlight rules are user-editable: pattern (literal or regex), case sensitivity, whole-line vs matched-span styling, foreground / background / bold, priority, enable / disable. Saved per profile and globally. |
| FR-32 | M | **Row-proportional minimap** (Sublime-style): compressed overview of the whole merged stream showing severity colours, search-hit marks, bookmarks, and the current viewport box. Click and drag to navigate. Drives the scrollbar. |
| FR-33 | M | **Wall-clock timeline strip:** vertical axis is time across the merged span, showing idle gaps as blank space and activity bursts as dense bands, coloured by severity density per time bucket. Click to seek to that time. |
| FR-34 | M | Either strip can be collapsed; the choice persists in the profile (D4). |
| FR-35 | M | Timestamp column display format is configurable, including a relative mode: ± from a chosen zero point, or delta from the previous row. |
| FR-36 | S | Line wrapping, toggleable, with correct virtualised scrolling when enabled. |
| FR-36a | M | **Horizontal scrolling** of the message column, by scrollbar, keyboard, and shift+wheel, with the timestamp, marks gutter, source lanes and both strips staying frozen in place. Promoted to a must by D17: a raw JSONL record is routinely wider than the window. |
| FR-37 | S | Bookmarks on rows, navigable, shown in both strips, saved in the profile. |
| FR-38 | S | Font family and size selection; zoom by keyboard and Ctrl+wheel. |
| FR-39 | S | A light and a dark theme. |
| FR-40 | C | Split the view into two independently scrolled panes over the same merged stream. |

### 7.5 Navigation, search, selection

| ID | Pri | Requirement |
|---|---|---|
| FR-41 | M | Keyboard navigation: arrows, PgUp/PgDn, Home/End, and **Go to timestamp**. |
| FR-42 | M | Jump to next / previous entry of a given severity, and to the next / previous entry from a given file. |
| FR-43 | M | **Seek to an arbitrary time is near-instant** regardless of file size — no linear scan. |
| FR-44 | M | Incremental find (literal and regex, case toggle) across the merged stream, with hit count, next / previous, and all hits marked in both strips. |
| FR-45 | M | **Filter** mode: show only rows matching a predicate — severity set, source-file set, text / regex include and exclude, time window — composable, and reversible without re-indexing. |
| FR-46 | M | Row selection: click, shift-click range, Ctrl+A. |
| FR-46a | M | **Copied rows are attributed to their origin: source file and original line number within that file** (S6), e.g. `app.log:1`, `SOA.log:7`. Attribution is on by default. |
| FR-46b | M | Copy output format is selectable — annotated (default), as-displayed/aligned, message-only, Markdown table, CSV — plus a user-defined template over `{source}`, `{alias}`, `{path}`, `{line}`, `{timestamp}`, `{severity}`, `{message}`. The source name may render as basename (default), alias, or full path. |
| FR-47 | S | Export the current view (filtered or whole) to text, CSV, or Markdown, using the same attribution and format options as FR-46a/FR-46b. |
| FR-48 | S | Select a time range directly on the timeline strip to set the time filter. |

### 7.6 Profiles

| ID | Pri | Requirement |
|---|---|---|
| FR-49 | M | Save the session as a named profile: sources, per-source format override, encoding, alias, colour, ± offset, follow state; plus highlight rules, column layout, strip visibility, font, theme, saved filters, bookmarks. |
| FR-50 | M | Profile sources may be **glob patterns** (`C:\Acme\logs\gateway-*.log`), re-resolved at open time against what exists that day (G8, S5). Paths and globs are stored as **absolute** paths, with no variable or relative-root expansion (D10). |
| FR-51 | M | Glob sources support a selection policy: all matches, the newest N, or only those modified within a time window. |
| FR-52 | M | Opening a profile reports missing or unmatched sources clearly without aborting the rest of the open. |
| FR-53 | M | A profile is a single human-readable, diffable, version-controllable file, openable by double-click via a file association. |
| FR-54 | S | Recent-files and recent-profiles lists; optionally reopen the last session on start. |
| FR-55 | C | Profile-level "derived sources" — e.g. an SSH source whose host is a profile parameter prompted at open time. |

## 8. Non-functional requirements

### 8.1 Performance budgets

Acceptance criteria, measured on a mid-range 2024-era laptop (NVMe SSD, 8 cores,
16GB RAM) against a generated corpus (SPEC §15).

| ID | Metric | Budget |
|---|---|---|
| NFR-1 | Time to first readable screen, 1GB file | **< 1.0s** |
| NFR-2 | Time to first readable screen, 10 × 100MB files | **< 1.5s** |
| NFR-3 | Full first-time index throughput | **≥ 100 MB/s** aggregate, parallel across files, UI responsive throughout. *Aggregate*, not per core — chunks and files index concurrently (SPEC §5.3). **Measured at M0: 98 MB/s single-core, ~820 MB/s aggregate.** |
| NFR-4 | Re-open an already-indexed 10GB set (warm sidecar cache) | **< 2.0s** to fully navigable |
| NFR-5 | Scroll frame time while dragging the scrollbar | **< 16ms** (60fps), no tearing, no blank rows |
| NFR-6 | Seek to an arbitrary timestamp in a 10GB merged set | **< 50ms** |
| NFR-7 | Resident memory, 10GB merged set | **< 500MB** steady state |
| NFR-8 | New appended line visible in follow mode | **< 200ms** |
| NFR-9 | Toggle a filter on a 100M-row merged set | **< 500ms** to repaint |
| NFR-10 | Literal search across a 10GB merged set | **< 5s**, cancellable, incremental results |
| NFR-11 | UI must never block | No operation blocks the event loop for more than **50ms**; anything longer is cancellable and shows progress |
| NFR-12 | Cold application start to empty window | **< 2.0s** |

### 8.2 Scale limits

| ID | Requirement |
|---|---|
| NFR-13 | Single file up to **64GB**. |
| NFR-14 | At least **50** concurrent sources (source lanes degrade gracefully beyond ~20). |
| NFR-15 | At least **2 × 10⁹** merged rows addressable. |
| NFR-16 | Individual lines up to **1MB**; longer lines are truncated for display with an indicator, never crash. |

### 8.3 Platform, quality, packaging

| ID | Requirement |
|---|---|
| NFR-17 | **Windows 10/11 x64 is the primary and only *tested* target for v1.** |
| NFR-18 | **No design decision may structurally block Linux or macOS.** All OS-specific behaviour (file watching, path handling, Event Log, SSH) sits behind a platform abstraction with a documented portable fallback. CI runs the non-GUI test suite on Linux from day one. |
| NFR-19 | Ship as a **single self-contained signed executable** requiring no Python installation, *and* as `pip install logweave`. |
| NFR-20 | Never modify, lock, or prevent rotation of a log file. Open read-only, with Windows share modes permitting concurrent write and delete. |
| NFR-21 | Degrade, never crash. An unparseable file, a vanished share, a corrupt index, or a bad encoding produces a clear in-UI message and a usable session. |
| NFR-22 | Index caches are reconstructible. Deleting the cache directory loses nothing but time. |
| NFR-23 | Keyboard-operable for all primary navigation; honour OS font scaling and high-DPI. |
| NFR-24 | No telemetry. No network access except to sources the user explicitly opened. |
| NFR-25 | Licensed **MIT**. Ship a `NOTICE` crediting logmerger (MIT) for the timestamp-format catalogue, and satisfy PySide6's LGPLv3 terms: Qt dynamically linked, unmodified, its licence text included, and its libraries replaceable in the distributed bundle (D15). |
| NFR-26 | Credentials are never written to a profile or any plaintext file. Secrets live only in the OS credential vault or in memory for the session (FR-7a). |

## 9. Proposed layout

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ File  View  Sources  Filter  Profile  Help                                   │
├──────────────────────────────────────────────────────────────────────────────┤
│ [Follow ●] [Filter: ERROR,WARN ×] [Find: timeout      ] 14/312  ‹ ›   [≡][◫] │
├─┬────────────────────────┬──────────────────────────────────┬────┬──┬────────┤
│ │ 14:22:08.113           │ Connection pool exhausted        │█   │  │ ▏      │
│●│ 14:22:08.117           │ WARN  retry 1/3 scheduled        │ █  │▐█│ ▏      │
│ │ 14:22:08.120           │ ERROR upstream timeout after 30s │█   │▐█│ ▏      │
│ │ 14:22:08.120           │   at Gateway.forward(Gw.java:88) │█   │▐█│ ▏      │
│ │ 14:22:08.204           │ GET /api/v2/order 502 30011      │   █│▐█│▐█  ←now│
│ │ 14:22:09.001           │ INFO  pool resized 32 → 64       │  █ │▐█│ ▏      │
│ │                        │                                  │    │▐█│ ▏      │
│ │                        │                                  │    │  │ ▏      │
├─┴────────────────────────┴──────────────────────────────────┴────┴──┴────────┤
│ gateway.log ▸ YMDHMSdotF · 2.1GB · following   │  6 sources · 41.8M rows     │
└──────────────────────────────────────────────────────────────────────────────┘
  ▲       ▲                          ▲                          ▲    ▲     ▲
  marks   timestamp                  message                 source  mini  time
  gutter  (format configurable)      (highlighted)            lanes   map   line
```

## 10. Release phases

**Phase 1 — v1.0, "it merges and it follows"**
All **M** requirements. Local and UNC files; text, `.gz`, CSV, JSONL. Full
indexing, merging, follow, both navigation strips, highlighting, search, filter,
profiles, drag-and-drop, single signed exe. SSH sources (FR-7) if they fit,
otherwise first in 1.1.

**Phase 2 — Windows Event Log**
Live channels via the Event Log query API, saved `.evtx` files, a channel picker,
rendering of structured event payloads, follow via channel subscription. Plus the
remaining **S** requirements.

**Phase 3 — live network sources**
syslog UDP/TCP listener with a spill-to-disk ring buffer. Possibly Windows Event
Forwarding. **C** requirements as justified.

## 11. Questions and answers

### 11.1 Resolved

| # | Question | Answer | Lands in |
|---|---|---|---|
| Q1 | For `.gz`, decompress to a cache file, or build a seekable gzip block index? | **Decompress to cache.** Simple, full random access, costs disk equal to the uncompressed size. | D12, SPEC §3.2 |
| Q2 | Is the default severity vocabulary enough, or are in-house conventions needed? | **Defaults are good, but severity rules must be user-extensible.** Ship the built-ins; let users add their own. | D11, FR-31, SPEC §10 |
| Q3 | Absolute paths in profiles, or a portable root variable? | **Absolute paths.** No variable expansion. | D10, FR-50 |
| Q4 | Any sort order other than time? | **Just time.** | D13 |
| Q5 | For CSV and JSONL, should non-timestamp fields become real view columns, or stay as text in the message column? | **Raw text in the message column.** No field extraction in v1; horizontal scrolling carries the readability cost. | D17, FR-36a |
| Q6 | SSH auth — keys and agent only, or also password? | **Password and key file**, plus agent. Secrets never stored in the profile. | D14, FR-7a, NFR-26 |
| Q7 | Is `app.log.1` a rotated predecessor of `app.log`, or an independent source? | **Separate files**, independent sources, no grouping concept. | D16, SPEC §7.2 |
| Q8 | Is a sidecar index cache in `%LOCALAPPDATA%` acceptable, with its disk cost? | **Yes.** ~21 bytes per entry, LRU-capped at 10GB by default, clearable from the UI. | NFR-22, SPEC §5.4 |
| Q9 | What licence? | **MIT, with attribution** to logmerger in a `NOTICE`; PySide6 kept LGPL-compliant. | D15, NFR-25 |
| Q10 | Must v1 read an exclusively-locked file? | **Try as hard as possible.** Escalation ladder, honest reporting when it fails. | FR-4a, R9, SPEC §7.1.1 |

### 11.2 Still open

**None.** All ten questions are answered and folded into D9–D17 and the
requirements above. New questions arising during the build are recorded as ADRs
(SPEC Appendix A).

## 12. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | ~~Python cannot hit the 100 MB/s index throughput budget (NFR-3)~~ | **RETIRED at M0** | **Measured: 98 MB/s single-core, ~820 MB/s projected aggregate — NFR-3 met with ~8× margin, no native code needed** (SPEC §14.1). Getting there took three fixes M0 found by measurement: a fixed-offset vectorised severity classifier in place of a regex over every byte (8 → 320 MB/s, and the single change that took the pipeline from 7.4 to 98 MB/s), per-column gathers instead of a 208MB 2D index, and a constant-date fast path. One residual breach remains, tracked as R11. |
| R11 | The severity **regex fallback** — for formats whose level word is not at a predictable offset — measures 7.4 MB/s single-core, ~62 MB/s aggregate, which is **below NFR-3** | Medium | Affects only sources on the fallback, and detection reports which those are. Fix is a vectorised multi-pattern matcher built from NumPy primitives (SPEC §14.2 item 1), plus letting a custom format declare the level offset to move that source onto the fast path. Still no native code required. |
| R2 | The naive-local time model (D1) produces visibly wrong order on a real mixed-zone case | Medium | D2 per-file offset; surface detected-zone information in the UI; revisit if it bites. |
| R3 | Two minimap render paths (D4) become the frame-budget bottleneck | Medium | Both are pre-aggregated into fixed-size bucket arrays at index time and cached; a repaint is a blit, not a recompute. |
| R4 | ~~Timestamp auto-detection silently picks a wrong format and mis-orders a file~~ | **RETIRED at M1** | Mitigated four ways, all implemented and tested. (1) **The engine cross-checks itself**: detection synthesises a vectorised layout and then proves it against an independently written per-line regex oracle on the same sample, discarding it on any disagreement (SPEC §4.3 step 6b) — so a silently-wrong fast path cannot ship even for a format nobody wrote a test for. (2) **Ground truth**: all 15 catalogue formats index with `wall_us`, `line_no` and `byte_off` equal to the generator's independently computed manifest. (3) **Rollover inference anchors at the tail** and guards against small backward jumps, fixing logmerger's New Year bug and refusing to read out-of-order logging as a rollover (SPEC §4.5). (4) Detection samples ~300 spread lines, ranks by explicit specificity, enforces a 25% match-rate floor, and reports confidence for FR-19 override. One irreducible ambiguity is documented rather than hidden: an opaque 10-digit id inside the epoch window is indistinguishable from an epoch timestamp; any format carrying a real date outranks it, and the user can override. |
| R5 | Follow-mode correctness on Windows rotation is fiddly — handles, rename-while-open, antivirus interference | Medium | A dedicated rotation test matrix with a rotation-simulator harness; file identity by file-id + size + head checksum, never path alone. |
| R6 | A single-exe bundle of PySide6 is large (~150MB+) and slow to start, breaking NFR-12 and NFR-19 | Medium | Prune unused Qt modules aggressively; measure start time in CI; consider Nuitka if PyInstaller start time misses the budget. |
| R7 | Scope creep from Event Log / syslog pulls Phase 1 out of shape | Medium | D3 and D6 hold them behind the `LogSource` interface; no Phase 1 work beyond that interface. |
| R8 | Out-of-order and rollover edge cases in timestamp inference cause subtle wrong ordering | Medium | Port and extend logmerger's out-of-order test corpus; property-based tests on generated logs with known ground-truth order. |
| R9 | The locked-file ladder (FR-4a) cannot always win — backup semantics needs `SeBackupPrivilege` and a VSS snapshot needs administrator rights, so a non-admin user may still be blocked | Medium | Attempt every rung and report exactly which failed and why, with a one-line explanation of what elevation would buy. Never present a partial read as complete. Set expectations in the docs: this is best-effort by nature. |
| R10 | Line-number attribution (FR-46a) is wrong if an index is stale or a file was rewritten rather than appended | Medium | Line numbers come from the same index generation as the entry; a truncation or identity change starts a new generation (SPEC §7.2) and copy output marks rows from a superseded generation. |

---

*Companion document: `docs/SPEC.md` — technical specification.*
