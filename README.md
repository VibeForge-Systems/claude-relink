<p align="center">
  <img src="docs/assets/crest.png" alt="VibeForge Systems" width="120">
</p>

<h1 align="center">claude-relink</h1>

<p align="center">
  Recover Claude Code conversations orphaned by a moved project directory.<br>
  Single file · Python 3.8+ · standard library only · dry run by default · backup and undo on every apply.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-0a1a2f?labelColor=d4af37&color=0a1a2f"></a>
  <img alt="Python 3.8+" src="https://img.shields.io/badge/python-3.8%2B-0a1a2f?labelColor=d4af37&color=0a1a2f">
  <img alt="Dependencies: none" src="https://img.shields.io/badge/dependencies-none-0a1a2f?labelColor=d4af37&color=0a1a2f">
</p>

---

Claude Code files every project under a slug of its absolute path. Move the
directory and `claude --resume` looks under a slug that has nothing in it: the
transcripts are intact, filed under a path that no longer exists. The same
mechanism strands sessions when a repository is cloned on another machine with
a different mount point, or when a project moves between GitHub organizations.

`claude-relink` finds those stores, works out where the project went, and
moves or merges them — transcripts, subagent transcripts, project memory,
`.claude.json` settings and prompt history — rewriting every absolute path
inside so a resumed session sees the new location everywhere.

**Manual:** [`docs/index.html`](docs/index.html) — mechanism, commands,
scoring, options and guard rails on one page (GitHub Pages ready).

## Contents

- [How Claude Code stores a project](#how-claude-code-stores-a-project)
- [Install](#install)
- [Usage](#usage)
  - [01 Scan](#01-scan)
  - [02 Relink](#02-relink)
  - [03 Batch](#03-batch)
  - [04 Import from another machine](#04-import-from-another-machine)
  - [05 Undo](#05-undo)
  - [06 Dedupe config spellings](#06-dedupe-config-spellings)
- [Candidate scoring](#candidate-scoring)
- [Guard rails](#guard-rails)
- [Limitations](#limitations)
- [Development](#development)
- [License](#license)

## How Claude Code stores a project

```
~/.claude/projects/<slug>/        slug = re.sub(r'[^a-zA-Z0-9]', '-', cwd)
```

`/home/dev/src/personal/ledger-api` becomes
`-home-dev-src-personal-ledger-api`. Verified against Claude Code 2.1.x:

| Item | Carries paths? | Handling |
|---|---|---|
| `<sessionId>.jsonl` transcripts | yes — `cwd` on every record, tool inputs (`file_path`, `command` …), `toolUseResult.filePath`, file-history snapshot keys | moved and rewritten line by line; untouched lines stay byte-identical; mtimes preserved so `--resume` ordering is unchanged |
| `<sessionId>/subagents/agent-*.jsonl` | yes | moved and rewritten |
| `<sessionId>/tool-results/`, `workflows/` … | opaque tool output | moved as-is |
| `memory/MEMORY.md`, `memory/*.md` | often | moved and rewritten; on merge the target's index is kept and the old one is saved as `MEMORY.imported-from-<slug>.md` and linked from it |
| `sessions-index.json` | yes — `fullPath`, `projectPath` | entries rewritten and merged into the target index |
| `~/.claude.json → projects[<cwd>]` | the key | all spellings of the old path merged into `projects[<new cwd>]`: allow-lists unioned, MCP servers merged (target wins), trust OR'd |
| `~/.claude/history.jsonl` | yes — `project` | entries re-pointed |
| `~/.claude/file-history/`, `todos/`, `session-env/` | session-id keyed | untouched |
| `<project>/.claude/settings.local.json`, `.mcp.json` | sometimes | reported; rewritten with `--rewrite-project-files` |

## Install

```bash
git clone https://github.com/VibeForge-Systems/claude-relink.git
cd claude-relink
python3 test_claude_relink.py        # 17 tests, ~0.2 s

# optional: put it on PATH
install -m 755 claude_relink.py ~/.local/bin/claude-relink
```

No dependencies. `CLAUDE_CONFIG_DIR` is honoured exactly as Claude Code
honours it; `--claude-dir` and `--config` override both.

## Usage

Nothing is modified without `--apply`. Every apply writes a full backup first
and prints the undo command.

### 01 Scan

What is orphaned, and where did it go. Read-only.

```bash
claude-relink scan --search-root ~/src
```

```
Claude Code store : /home/dev/.claude
Project stores    : 23  (18 connected, 5 orphaned, 0 undeterminable)
Candidate dirs    : 61 under /home/dev/src (depth 2)

ORPHANED  /home/dev/src/personal/ledger-api
  store    : /home/dev/.claude/projects/-home-dev-src-personal-ledger-api
  sessions : 9  (31.4MB, last activity 2026-09-02 14:11)   extras: memory, sessions-index.json
  repos    : acme/ledger-api (27)
    - 4e12ae05  2026-09-02 14:11    12.2MB  Migrate settlement batch to event sourcing
    - e645dc69  2026-08-30 09:40     4.1MB  Idempotency keys for the payouts endpoint
  candidates:
    [HIGH 0.96] /home/dev/src/acme/ledger-api
               - same directory name
               - 212/240 files referenced in transcripts exist here (88%)
               - git remote matches transcripts (acme/ledger-api)
               - already a Claude project (merge target)
  relink   : claude-relink relink --from /home/dev/src/personal/ledger-api --to /home/dev/src/acme/ledger-api --apply
```

Without `--search-root` the candidate pool is the projects Claude already
knows about, which is enough once you have opened Claude in the new location.
`--all` also lists healthy stores; `--json` gives machine-readable output.

### 02 Relink

Dry run first, then apply. `--from` accepts the old path in any spelling, the
slug, or a unique substring.

```bash
claude-relink relink --from personal/ledger-api --to ~/src/acme/ledger-api
claude-relink relink --from personal/ledger-api --to ~/src/acme/ledger-api --apply
```

Mode is chosen automatically:

- **move** — no store exists for the new path yet; the store is recreated under
  the new slug with everything rewritten.
- **merge** — Claude has already been run in the new location; old sessions are
  added alongside the new ones. Identical duplicates are skipped. A session id
  present at both ends with different content keeps the target's copy; the
  source copy stays in the backup.

Then `cd` into the new directory and `claude --resume`. The old conversations
are listed with the new ones, and a resumed session sees the new absolute paths
throughout rather than attempting to read files at the old location.

### 03 Batch

Every orphan with exactly one candidate at or above the threshold and no
runner-up within 0.1 of it. Everything else is skipped with a reason.

```bash
claude-relink relink --auto --search-root ~/src --min-confidence 0.8          # dry run
claude-relink relink --auto --search-root ~/src --min-confidence 0.8 --apply
```

### 04 Import from another machine

Point `--src-claude-dir` at a copied `~/.claude`. The source is never modified.
Windows roots match case-insensitively with either separator, and sub-path
separators are converted: `D:\src\ledger-api\api\routes.ts` becomes
`…/ledger-api/api/routes.ts`. The matching `.claude.json` is found beside the
copied directory or given with `--src-config`; its project entry (all
spellings) is merged into the live config and its history entries for that
project are appended, deduplicated.

```bash
claude-relink relink \
  --src-claude-dir /mnt/backup/laptop/.claude \
  --from 'D:/src/ledger-api' \
  --to   ~/src/acme/ledger-api
```

`scan --claude-dir /mnt/backup/laptop/.claude --config /mnt/backup/laptop/.claude.json --search-root ~/src`
inventories what the copy contains and where each project now lives.

### 05 Undo

Each apply writes `~/.claude/backups/claude-relink/<timestamp>-<slug>/`: a
tarball of every store directory it changed, copies of `.claude.json` and
`history.jsonl`, and a manifest. Undo restores exactly that prior state.

```bash
claude-relink undo                                    # list backups
claude-relink undo --backup 20261031-104210-… --apply
```

### 06 Dedupe config spellings

Claude Code creates separate `.claude.json` entries for `D:\src\ledger-api`,
`D:/src/ledger-api` and `d:/src/ledger-api` depending on how it was launched.
This merges them; the entry whose directory exists is kept as the canonical key.

```bash
claude-relink dedupe          # dry run; --apply to merge
```

### Options

| Flag | Applies to | Effect |
|---|---|---|
| `--claude-dir DIR`, `--config FILE` | all | Override the store location. Accepted before or after the subcommand. |
| `--search-root DIR` (repeatable), `--depth N` | scan, relink --auto | Where to look for moved projects; default depth 2. Hidden directories, `node_modules`, build output and similar are skipped. |
| `--top N`, `--sessions N`, `--all`, `--json` | scan | Candidates and sessions shown per orphan; include healthy stores; machine-readable output. |
| `--from OLD`, `--to NEWDIR` | relink | Old path (any spelling), slug or unique substring; the directory the project lives in now (symlinks resolved, as Claude Code records them). |
| `--paths all \| cwd-only` | relink | Default rewrites every old-path reference. `cwd-only` keeps tool inputs and results as a strict historical record; resume works but Claude sees stale paths in its context. |
| `--rewrite-project-files` | relink | Also fix old absolute paths in `.claude/settings.local.json`, `.claude/settings.json`, `.mcp.json` under the new directory (copies kept in the backup). |
| `--src-claude-dir DIR`, `--src-config FILE` | relink | Import from another store; source is read-only. |
| `--keep-source` | relink | Leave the old store directory in place. Implied when importing. |
| `--auto`, `--min-confidence X` | relink | Batch mode and its threshold (default 0.8). |
| `--apply` | relink, undo, dedupe | Required for every write. |
| `--force` | relink, undo, dedupe | Proceed even if Claude Code sessions appear to be running. |

## Candidate scoring

Three independent signals, capped at 1.0. A name match alone never qualifies
for `--auto`; the fingerprint is what separates a project from a fork or a
backup copy of it.

| Signal | Weight |
|---|---|
| Directory name matches | 0.45 (+ up to 0.15 for deeper trailing components) |
| Files the transcripts touched exist under the candidate | 0.5 × coverage |
| Git remote matches `pr-link` records / GitHub URLs in the transcripts | 0.3 for owner/repo, 0.15 for repo name only |

`HIGH ≥ 0.80` is eligible for `--auto`; `med ≥ 0.50` prints a suggested
command; lower scores are shown for reference. The fingerprint is built from up
to 400 distinct file paths found in `Read`/`Edit`/`Write`/`Grep` inputs and
tool results across the orphan's transcripts and subagents, relative to the old
root. A move between GitHub organizations scores "repo name only" on the remote
until the clone's origin is updated.

## Guard rails

- Dry run unless `--apply`; a backup and an undo command on every apply.
- Refuses to modify the store while a Claude Code process is registered in
  `~/.claude/sessions/` and still alive (`--force` overrides). Exit all Claude
  Code sessions first: Claude Code rewrites `.claude.json` on exit and would
  clobber the merge.
- Boundary-aware rewriting: `/src/proj` never matches `/src/proj2` or
  `/src/proj.bak`, but does match `/src/proj/…`, a sentence-ending
  `/src/proj.` and `Read(/src/proj/**)`.
- Lines that do not mention the old path are copied byte-for-byte; a truncated
  final line from a crash is copied untouched.
- Transcript mtimes are preserved.

## Limitations

- Paths whose slug exceeds 200 characters get a hash suffix in Claude Code
  that cannot be reproduced; those paths are refused rather than guessed.
- Worktree sessions (`<project>/.claude/worktrees/<name>`) appear as their own
  orphaned stores; relink them explicitly to the new worktree path.
- `tool-results/` files are copied verbatim; they are saved tool output and
  are not parsed on resume.
- Linux and macOS hosts. Reading Windows-origin stores is supported; running on
  Windows is untested.
- Claude Code's on-disk format is undocumented and may change. The layout
  above was verified against 2.1.x; `scan` is read-only, so it is a safe first
  check against a newer version.

## Development

```bash
python3 test_claude_relink.py -v
```

The suite builds a synthetic store mirroring the real layout — transcript
records with `cwd`, subagents, tool-results, `memory/`, `sessions-index.json`,
`.claude.json` project entries in several spellings, `history.jsonl` — and
exercises scan, merge, move, Windows-to-POSIX import, cross-store import,
duplicate and conflict handling, the running-session guard, dedupe, and an
undo round-trip that must restore a byte-identical state.

## License

Apache License 2.0. Copyright 2026 VibeForge Systems LLC.

Not affiliated with or endorsed by Anthropic. Claude and Claude Code are
trademarks of Anthropic, PBC.
