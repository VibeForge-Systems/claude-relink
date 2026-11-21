#!/usr/bin/env python3
"""
claude_relink.py — find Claude Code conversations that were orphaned when a
project directory moved, and move or merge them to the directory's new home.

Python 3.8+, standard library only. Linux/macOS (reads Windows-style paths in
archived stores, e.g. a rescued ~/.claude from a Windows machine).

How Claude Code stores a project (verified against Claude Code 2.1.x):

  <claude-dir>/projects/<slug>/                slug = re.sub(r'[^a-zA-Z0-9]', '-', cwd)
      <sessionId>.jsonl                        transcript; records carry a "cwd" field
      <sessionId>/subagents/agent-*.jsonl      subagent transcripts
      <sessionId>/tool-results/, workflows/    sidecars (session-id keyed)
      memory/MEMORY.md, memory/*.md            auto-memory for the project
      sessions-index.json                      optional index: fullPath, projectPath
  <claude-dir>/history.jsonl                   prompt history; "project" = cwd
  ~/.claude.json  (or $CLAUDE_CONFIG_DIR/.claude.json)
      projects[<cwd>] = { allowedTools, mcpServers, hasTrustDialogAccepted, ... }
  <claude-dir>/file-history/<sessionId>/       rewind snapshots (session-id keyed, no paths)
  <claude-dir>/todos/, session-env/            session-id keyed

Because the slug is derived from the absolute path, moving a project directory
strands every session under the old slug: `claude --resume` in the new
directory looks only under the new slug. This tool rebuilds the link.

Commands
  scan     list project stores whose directory no longer exists, with ranked
           candidates for where it went (default command)
  relink   move or merge one orphaned store to a new directory (dry-run unless
           --apply); also handles importing from another store (--src-claude-dir)
  undo     restore the state captured in a relink backup
  dedupe   merge duplicate ~/.claude.json project entries that differ only by
           path spelling (D:\\x vs D:/x vs d:/x)

Nothing is modified without --apply. Every --apply writes a full backup of the
touched items first and prints the undo command.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

__version__ = "1.0.0"

SLUG_MAX = 200  # Claude Code truncates longer slugs and appends a hash we can't reproduce
SKIP_DIR_NAMES = {
    "node_modules", ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "dist",
    "build", ".next", ".cache", "target", ".idea", ".vscode", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", ".terraform", "vendor",
}
PATH_INPUT_KEYS = ("file_path", "path", "notebook_path", "filePath")
FINGERPRINT_CAP = 400
HEAD_LINES_FOR_CWD = 400


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def slugify(path: str) -> str:
    """Claude Code's project-directory key. Longer than 200 chars gets a hash suffix we cannot reproduce."""
    s = re.sub(r"[^a-zA-Z0-9]", "-", path)
    if len(s) > SLUG_MAX:
        raise ValueError(f"path too long for a reproducible slug ({len(s)} > {SLUG_MAX}): {path}")
    return s


def is_windows_path(p: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", p)) or p.startswith("\\\\")


def norm_path_key(p: str) -> str:
    """Canonical form for *comparing* paths: Windows paths are case-folded with '/' separators; POSIX untouched."""
    p = p.strip()
    if is_windows_path(p):
        p = p.replace("\\", "/")
        p = p[0].lower() + p[1:]
        return p.rstrip("/").lower()
    return p.rstrip("/") if p != "/" else p


def path_basename(p: str) -> str:
    return re.split(r"[\\/]", p.rstrip("\\/"))[-1]


def path_components(p: str) -> List[str]:
    return [c for c in re.split(r"[\\/]", p) if c and not re.match(r"^[A-Za-z]:$", c)]


def now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def iso_from_mtime(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if path.exists():
            shutil.copymode(str(path), tmp)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def dumps_line(obj) -> str:
    """Serialize like JSON.stringify: compact, UTF-8 passthrough; fall back to ASCII escapes if a lone surrogate sneaks in."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        s = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    return s


# --------------------------------------------------------------------------- #
# Locating the Claude Code store
# --------------------------------------------------------------------------- #

@dataclass
class Store:
    """A Claude Code config/data location: the projects dir, history.jsonl and .claude.json."""
    claude_dir: Path
    config_file: Path

    @property
    def projects_dir(self) -> Path:
        return self.claude_dir / "projects"

    @property
    def history_file(self) -> Path:
        return self.claude_dir / "history.jsonl"

    @property
    def sessions_dir(self) -> Path:
        return self.claude_dir / "sessions"

    def load_config(self) -> dict:
        if not self.config_file.exists():
            return {}
        with open(self.config_file, encoding="utf-8") as f:
            return json.load(f)


def default_store(claude_dir: Optional[str], config_file: Optional[str]) -> Store:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if claude_dir:
        cd = Path(claude_dir).expanduser()
    elif env:
        cd = Path(env).expanduser()
    else:
        cd = Path.home() / ".claude"
    if config_file:
        cf = Path(config_file).expanduser()
    elif claude_dir:
        # explicit dir: prefer a .claude.json inside it, else the home one
        inner = cd / ".claude.json"
        cf = inner if inner.exists() else Path.home() / ".claude.json"
    elif env:
        cf = cd / ".claude.json"
    else:
        cf = Path.home() / ".claude.json"
    return Store(cd.resolve() if cd.exists() else cd, cf)


# --------------------------------------------------------------------------- #
# Project stores (slug directories)
# --------------------------------------------------------------------------- #

@dataclass
class SessionInfo:
    session_id: str
    file: Path
    size: int
    mtime: float
    first_cwd: Optional[str] = None
    first_prompt: str = ""
    title: str = ""


@dataclass
class ProjectStore:
    slug: str
    dir: Path
    sessions: List[SessionInfo] = field(default_factory=list)
    cwd_counts: Counter = field(default_factory=Counter)
    index_project_paths: Counter = field(default_factory=Counter)
    extras: List[str] = field(default_factory=list)  # non-session entries: memory/, sessions-index.json, ...
    total_bytes: int = 0
    last_activity: float = 0.0
    pr_repos: Counter = field(default_factory=Counter)  # "owner/repo" from pr-link records

    @property
    def cwd(self) -> Optional[str]:
        """Dominant project root recorded in transcripts (falls back to sessions-index projectPath)."""
        if self.cwd_counts:
            return self.cwd_counts.most_common(1)[0][0]
        if self.index_project_paths:
            return self.index_project_paths.most_common(1)[0][0]
        return None

    @property
    def cwd_variants(self) -> List[str]:
        return [c for c, _ in self.cwd_counts.most_common()]

    def exists_on_disk(self) -> bool:
        c = self.cwd
        return bool(c) and os.path.isdir(c)

    def is_orphan(self) -> bool:
        return self.cwd is not None and not self.exists_on_disk()

    @property
    def foreign_os(self) -> bool:
        c = self.cwd
        return bool(c) and is_windows_path(c) and os.name != "nt"


def _dominant_root(cwds: Counter) -> Optional[str]:
    """Pick the project root among cwd variants: the shortest path that is a prefix of the most-common ones."""
    if not cwds:
        return None
    best = cwds.most_common(1)[0][0]
    bk = norm_path_key(best)
    for c in sorted(cwds, key=len):
        ck = norm_path_key(c)
        if bk == ck or bk.startswith(ck + "/"):
            # prefer the most common spelling of that root (D:\x vs d:\x)
            spellings = [s for s in cwds if norm_path_key(s) == ck]
            return max(spellings, key=lambda s: cwds[s])
    return best


def scan_session_head(file: Path) -> Tuple[Counter, str, str, Counter]:
    """Read the head of a transcript: cwd counts, first prompt, ai title, pr repos."""
    cwds: Counter = Counter()
    pr: Counter = Counter()
    first_prompt = ""
    title = ""
    try:
        with open(file, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= HEAD_LINES_FOR_CWD:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                c = d.get("cwd")
                if isinstance(c, str) and c:
                    cwds[c] += 1
                t = d.get("type")
                if t == "ai-title" and not title and isinstance(d.get("aiTitle"), str):
                    title = d["aiTitle"]
                elif t == "pr-link" and isinstance(d.get("prRepository"), str):
                    pr[d["prRepository"]] += 1
                elif t == "user" and not first_prompt and not d.get("isMeta"):
                    m = d.get("message")
                    if isinstance(m, dict):
                        content = m.get("content")
                        if isinstance(content, str):
                            first_prompt = content
                        elif isinstance(content, list):
                            for b in content:
                                if isinstance(b, dict) and b.get("type") == "text":
                                    first_prompt = b.get("text", "")
                                    break
    except OSError:
        pass
    first_prompt = re.sub(r"\s+", " ", first_prompt).strip()
    if first_prompt.startswith("<"):  # system-ish wrappers (ide_opened_file, task-notification…)
        first_prompt = re.sub(r"<[^>]+>", " ", first_prompt)
        first_prompt = re.sub(r"\s+", " ", first_prompt).strip()
    return cwds, first_prompt[:120], title[:80], pr


def load_project_store(slug_dir: Path) -> ProjectStore:
    ps = ProjectStore(slug=slug_dir.name, dir=slug_dir)
    for entry in sorted(slug_dir.iterdir()):
        if entry.is_file() and entry.suffix == ".jsonl":
            st = entry.stat()
            cwds, first_prompt, title, pr = scan_session_head(entry)
            si = SessionInfo(entry.stem, entry, st.st_size, st.st_mtime, _dominant_root(cwds), first_prompt, title)
            ps.sessions.append(si)
            ps.cwd_counts.update(cwds)
            ps.pr_repos.update(pr)
            ps.total_bytes += st.st_size
            ps.last_activity = max(ps.last_activity, st.st_mtime)
        elif entry.is_dir() and re.match(r"^[0-9a-f-]{36}$", entry.name):
            # session sidecar dir (subagents/, tool-results/ …); counted in size/mtime only
            for p in entry.rglob("*"):
                if p.is_file():
                    st = p.stat()
                    ps.total_bytes += st.st_size
                    ps.last_activity = max(ps.last_activity, st.st_mtime)
        else:
            ps.extras.append(entry.name)
            if entry.name == "sessions-index.json":
                try:
                    with open(entry, encoding="utf-8") as f:
                        idx = json.load(f)
                    for e in idx.get("entries", []):
                        pp = e.get("projectPath")
                        if isinstance(pp, str):
                            ps.index_project_paths[pp] += 1
                except (OSError, json.JSONDecodeError, AttributeError):
                    pass
            if entry.is_dir():
                for p in entry.rglob("*"):
                    if p.is_file():
                        ps.last_activity = max(ps.last_activity, p.stat().st_mtime)
    # collapse the cwd variants to the project root (sessions that `cd` into subdirs record the subdir)
    root = _dominant_root(ps.cwd_counts)
    if root:
        ps.cwd_counts = Counter({root: sum(ps.cwd_counts.values())})
        # keep spelling variants that normalise the same (D:\x, d:/x) for config/history matching
    return ps


def load_all_project_stores(store: Store) -> List[ProjectStore]:
    out = []
    if not store.projects_dir.is_dir():
        return out
    for d in sorted(store.projects_dir.iterdir()):
        if d.is_dir():
            out.append(load_project_store(d))
    return out


# --------------------------------------------------------------------------- #
# Path rewriting
# --------------------------------------------------------------------------- #

class PathMapper:
    """
    Rewrites occurrences of old_root (and paths beneath it) to new_root inside
    strings, JSON structures and text. Windows-style old roots match
    case-insensitively with either separator; when converting Windows -> POSIX
    the separators of the sub-path are converted too.
    """

    _SUFFIX = r"(?P<suffix>(?:[\\/][^\s\"'<>|:*?`()\[\]{},;]*)?)"

    def __init__(self, old_root: str, new_root: str):
        self.old_root = old_root.rstrip("\\/") if len(old_root) > 1 else old_root
        self.new_root = new_root.rstrip("/") if len(new_root) > 1 else new_root
        self.old_is_win = is_windows_path(self.old_root)
        self.new_is_win = is_windows_path(self.new_root)
        self.convert_seps = self.old_is_win != self.new_is_win
        comps = re.split(r"[\\/]+", self.old_root)
        if self.old_is_win:
            drive = re.escape(comps[0][0])
            body = r"[\\/]+".join(re.escape(c) for c in comps[1:] if c)
            root_re = rf"[{drive.lower()}{drive.upper()}]:[\\/]+{body}" if body else rf"[{drive.lower()}{drive.upper()}]:"
            flags = re.IGNORECASE
        else:
            root_re = re.escape(self.old_root)
            flags = 0
        # boundary: end of string, a separator, something that cannot continue a path component,
        # or a sentence-ending period ("…is D:\dev\proj.")  — but not "proj.bak" / "proj-v2"
        boundary = r"(?=$|[\\/]|[^A-Za-z0-9_.\-]|\.(?:\s|$|[\"'\)\]]))"
        self.root_re = re.compile(root_re + boundary, flags)
        self.full_re = re.compile(root_re + self._SUFFIX + boundary, flags) if self.convert_seps else None
        # cheap pre-check on raw JSON lines (backslashes appear escaped there)
        if self.old_is_win:
            raw_body = r"(?:\\\\|\\|/)+".join(re.escape(c) for c in comps[1:] if c)
            self.raw_re = re.compile(rf"[{drive.lower()}{drive.upper()}]:(?:\\\\|\\|/)+{raw_body}" if raw_body else rf"[{drive.lower()}{drive.upper()}]:", re.IGNORECASE)
        else:
            self.raw_re = re.compile(re.escape(self.old_root))

    def matches_root(self, s: str) -> bool:
        return bool(self.root_re.match(s)) and norm_path_key(s) == norm_path_key(self.old_root)

    def is_under(self, s: str) -> bool:
        return bool(self.root_re.match(s))

    def sub(self, s: str) -> str:
        if not isinstance(s, str) or not s:
            return s
        if self.full_re is not None:
            def repl(m):
                suf = m.group("suffix") or ""
                suf = suf.replace("\\", "/") if not self.new_is_win else suf.replace("/", "\\")
                return self.new_root + suf
            return self.full_re.sub(repl, s)
        return self.root_re.sub(lambda m: self.new_root, s)

    def rewrite_obj(self, obj):
        """Recursively rewrite string values AND dict keys. Returns (new_obj, changed)."""
        changed = False
        if isinstance(obj, str):
            n = self.sub(obj)
            return n, n != obj
        if isinstance(obj, list):
            out = []
            for v in obj:
                nv, c = self.rewrite_obj(v)
                out.append(nv)
                changed = changed or c
            return (out if changed else obj), changed
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                nk = self.sub(k) if isinstance(k, str) else k
                nv, c = self.rewrite_obj(v)
                if nk != k:
                    c = True
                out[nk] = nv
                changed = changed or c
            return (out if changed else obj), changed
        return obj, False

    def rewrite_jsonl_text(self, text: str) -> Tuple[str, int, int]:
        """Rewrite a JSONL document line by line. Untouched lines are preserved byte-for-byte."""
        out_lines = []
        changed_lines = 0
        unparsable = 0
        for line in text.split("\n"):
            if line and self.raw_re.search(line):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    unparsable += 1
                    out_lines.append(line)
                    continue
                new_obj, changed = self.rewrite_obj(obj)
                if changed:
                    out_lines.append(dumps_line(new_obj))
                    changed_lines += 1
                    continue
            out_lines.append(line)
        return "\n".join(out_lines), changed_lines, unparsable

    def rewrite_text(self, text: str) -> Tuple[str, int]:
        new = self.sub(text)
        return new, (0 if new == text else len(self.raw_re.findall(text)))


# --------------------------------------------------------------------------- #
# Fingerprinting and candidate matching
# --------------------------------------------------------------------------- #

@dataclass
class Fingerprint:
    rel_paths: Set[str] = field(default_factory=set)   # files referenced under the old root
    repos: Counter = field(default_factory=Counter)    # github owner/repo mentions
    repo_names: Counter = field(default_factory=Counter)


def collect_fingerprint(ps: ProjectStore, cap: int = FINGERPRINT_CAP) -> Fingerprint:
    fp = Fingerprint()
    root = ps.cwd
    if not root:
        return fp
    mapper = PathMapper(root, "/__ROOT__")
    gh = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?=[/\s\"'`)>,;]|$)")
    fp.repos.update(ps.pr_repos)
    files: List[Path] = []
    for s in sorted(ps.sessions, key=lambda x: -x.mtime):
        files.append(s.file)
        sub = ps.dir / s.session_id / "subagents"
        if sub.is_dir():
            files.extend(sorted(sub.glob("agent-*.jsonl")))
    for file in files:
        if len(fp.rel_paths) >= cap:
            break
        try:
            with open(file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if len(fp.rel_paths) >= cap:
                        break
                    if not mapper.raw_re.search(line) and "github.com" not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(d, dict):
                        continue
                    msg = d.get("message")
                    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                        for b in msg["content"]:
                            if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
                                for k in PATH_INPUT_KEYS:
                                    v = b["input"].get(k)
                                    if isinstance(v, str) and mapper.is_under(v):
                                        rel = mapper.sub(v)[len("/__ROOT__"):].lstrip("/")
                                        if rel and "*" not in rel:
                                            fp.rel_paths.add(rel)
                    tur = d.get("toolUseResult")
                    if isinstance(tur, dict):
                        for v in (tur.get("filePath"), (tur.get("file") or {}).get("filePath") if isinstance(tur.get("file"), dict) else None):
                            if isinstance(v, str) and mapper.is_under(v):
                                rel = mapper.sub(v)[len("/__ROOT__"):].lstrip("/")
                                if rel:
                                    fp.rel_paths.add(rel)
                    for m in gh.finditer(line):
                        fp.repos[f"{m.group(1)}/{m.group(2)}"] += 1
        except OSError:
            continue
    for r in fp.repos:
        fp.repo_names[r.split("/", 1)[1].lower()] += fp.repos[r]
    return fp


def git_remotes(dir_: Path) -> Set[str]:
    """owner/repo strings from .git/config remotes (github-style URLs only)."""
    out: Set[str] = set()
    cfg = dir_ / ".git" / "config"
    if not cfg.is_file():
        gitfile = dir_ / ".git"
        if gitfile.is_file():  # worktree / submodule pointer
            try:
                line = gitfile.read_text(encoding="utf-8").strip()
                if line.startswith("gitdir:"):
                    cfg = Path(line[7:].strip()) / "config"
                    if not cfg.is_absolute():
                        cfg = (dir_ / cfg).resolve()
            except OSError:
                return out
        if not cfg.is_file():
            return out
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for m in re.finditer(r"url\s*=\s*(\S+)", text):
        mm = re.search(r"[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", m.group(1))
        if mm:
            out.add(f"{mm.group(1)}/{mm.group(2)}")
    return out


@dataclass
class Candidate:
    path: str
    score: float
    reasons: List[str]
    coverage: Optional[float] = None


def discover_candidate_dirs(search_roots: List[str], depth: int) -> List[str]:
    found: List[str] = []
    seen: Set[str] = set()

    def walk(d: Path, level: int):
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for e in entries:
            if not e.is_dir() or e.is_symlink():
                continue
            if e.name.startswith(".") or e.name in SKIP_DIR_NAMES:
                continue
            rp = str(e.resolve())
            if rp not in seen:
                seen.add(rp)
                found.append(rp)
            if level < depth:
                walk(e, level + 1)

    for r in search_roots:
        rp = Path(r).expanduser()
        if rp.is_dir():
            walk(rp, 1)
    return found


def score_candidates(ps: ProjectStore, fp: Fingerprint, candidates: Iterable[str], known_cwds: Set[str]) -> List[Candidate]:
    old = ps.cwd or ""
    old_base = path_basename(old).lower()
    old_comps = [c.lower() for c in path_components(old)]
    results: List[Candidate] = []
    for cand in candidates:
        if norm_path_key(cand) == norm_path_key(old):
            continue
        reasons: List[str] = []
        score = 0.0
        base = path_basename(cand).lower()
        comps = [c.lower() for c in path_components(cand)]
        if base == old_base:
            score += 0.45
            reasons.append("same directory name")
            # trailing-component overlap beyond the basename (e.g. acme/ledger-api matching src/acme/ledger-api)
            k = 1
            while k < min(len(comps), len(old_comps)) and comps[-1 - k] == old_comps[-1 - k]:
                k += 1
            if k > 1:
                score += min(0.15, 0.05 * (k - 1))
                reasons.append(f"{k} trailing path components match")
        elif base.replace("_", "-") == old_base.replace("_", "-"):
            score += 0.35
            reasons.append("directory name matches (-/_ normalised)")
        coverage = None
        if fp.rel_paths:
            hits = sum(1 for r in fp.rel_paths if os.path.exists(os.path.join(cand, r)))
            coverage = hits / len(fp.rel_paths)
            score += 0.5 * coverage
            if coverage > 0:
                reasons.append(f"{hits}/{len(fp.rel_paths)} files referenced in transcripts exist here ({coverage:.0%})")
        if fp.repos or fp.repo_names:
            remotes = git_remotes(Path(cand))
            if remotes:
                full = [r for r in remotes if r in fp.repos]
                if full:
                    score += 0.3
                    reasons.append(f"git remote matches transcripts ({full[0]})")
                else:
                    names = [r for r in remotes if r.split('/', 1)[1].lower() in fp.repo_names]
                    if names:
                        score += 0.15
                        reasons.append(f"git remote repo name matches ({names[0]}; owner differs)")
        if cand in known_cwds:
            reasons.append("already a Claude project (merge target)")
        if score > 0:
            results.append(Candidate(cand, min(score, 1.0), reasons, coverage))
    results.sort(key=lambda c: -c.score)
    return results


# --------------------------------------------------------------------------- #
# Config (.claude.json) and history helpers
# --------------------------------------------------------------------------- #

def config_entries_for(config: dict, root: str) -> List[str]:
    """All projects[] keys whose normalised spelling equals root (D:\\x, D:/x, d:/x ...)."""
    key = norm_path_key(root)
    return [k for k in (config.get("projects") or {}) if norm_path_key(k) == key]


def _union_list(a, b):
    out = []
    seen = set()
    for item in list(a or []) + list(b or []):
        marker = json.dumps(item, sort_keys=True) if not isinstance(item, str) else item
        if marker not in seen:
            seen.add(marker)
            out.append(item)
    return out


def merge_project_config(target: Optional[dict], sources: List[dict]) -> dict:
    """Merge project config entries. Target (the live/new entry) wins on conflicts; lists are unioned; trust is OR'd."""
    result: dict = dict(target or {})
    list_union = {"allowedTools", "mcpContextUris", "enabledMcpjsonServers", "disabledMcpjsonServers",
                  "disabledMcpServers", "history", "exampleFiles", "loggedAuthoredArtifactPaths"}
    bool_or = {"hasTrustDialogAccepted", "hasCompletedProjectOnboarding", "hasClaudeMdExternalIncludesApproved",
               "hasClaudeMdExternalIncludesWarningShown"}
    num_max = {"projectOnboardingSeenCount"}
    for src in sources:
        for k, v in src.items():
            if k in list_union:
                result[k] = _union_list(result.get(k), v)
            elif k == "mcpServers":
                merged = dict(v or {})
                merged.update(result.get(k) or {})
                result[k] = merged
            elif k in bool_or:
                result[k] = bool(result.get(k)) or bool(v)
            elif k in num_max:
                result[k] = max(result.get(k) or 0, v or 0)
            elif k not in result:
                result[k] = v
    return result


def read_history(path: Path) -> List[str]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().split("\n")


# --------------------------------------------------------------------------- #
# Liveness guard
# --------------------------------------------------------------------------- #

def running_claude_sessions(store: Store) -> List[dict]:
    live = []
    if not store.sessions_dir.is_dir():
        return live
    for p in store.sessions_dir.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pid = d.get("pid")
        if not isinstance(pid, int):
            continue
        alive = False
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        if alive and sys.platform.startswith("linux"):
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                alive = "claude" in cmd.lower()
            except OSError:
                alive = False
        if alive:
            live.append({"pid": pid, "cwd": d.get("cwd"), "sessionId": d.get("sessionId")})
    return live


# --------------------------------------------------------------------------- #
# Backups
# --------------------------------------------------------------------------- #

@dataclass
class Backup:
    dir: Path
    manifest: dict

    @staticmethod
    def create(store: Store, label: str) -> "Backup":
        bdir = store.claude_dir / "backups" / "claude-relink" / f"{now_stamp()}-{label}"
        bdir.mkdir(parents=True, exist_ok=False)
        return Backup(bdir, {"version": 1, "tool": f"claude_relink {__version__}", "created": _dt.datetime.now().isoformat(),
                             "claude_dir": str(store.claude_dir), "config_file": str(store.config_file), "items": []})

    def archive_dir(self, path: Path, key: str) -> None:
        name = f"{key}.tar.gz"
        with tarfile.open(self.dir / name, "w:gz") as tf:
            tf.add(str(path), arcname=path.name)
        self.manifest["items"].append({"kind": "dir", "key": key, "path": str(path), "archive": name})

    def copy_file(self, path: Path, key: str) -> None:
        if path.exists():
            shutil.copy2(str(path), str(self.dir / f"{key}.bak"))
            self.manifest["items"].append({"kind": "file", "key": key, "path": str(path), "copy": f"{key}.bak"})
        else:
            self.manifest["items"].append({"kind": "file-absent", "key": key, "path": str(path)})

    def note_created_dir(self, path: Path) -> None:
        self.manifest["items"].append({"kind": "created-dir", "path": str(path)})

    def write_manifest(self, **extra) -> None:
        self.manifest.update(extra)
        (self.dir / "manifest.json").write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")


def restore_backup(bdir: Path, dry_run: bool) -> None:
    manifest = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
    print(f"Restoring backup {bdir}")
    print(f"  operation: {manifest.get('operation')}  ({manifest.get('created')})")
    # created dirs are removed first, then archives restored, then files copied back
    for item in manifest["items"]:
        if item["kind"] == "created-dir":
            p = Path(item["path"])
            print(f"  remove   {p}")
            if not dry_run and p.exists():
                shutil.rmtree(p)
    for item in manifest["items"]:
        if item["kind"] == "dir":
            p = Path(item["path"])
            print(f"  restore  {p}  <- {item['archive']}")
            if not dry_run:
                if p.exists():
                    shutil.rmtree(p)
                with tarfile.open(bdir / item["archive"], "r:gz") as tf:
                    _safe_extract(tf, p.parent)
    for item in manifest["items"]:
        if item["kind"] == "file":
            p = Path(item["path"])
            print(f"  restore  {p}  <- {item['copy']}")
            if not dry_run:
                shutil.copy2(str(bdir / item["copy"]), str(p))
        elif item["kind"] == "file-absent":
            p = Path(item["path"])
            print(f"  remove   {p} (did not exist before)")
            if not dry_run and p.exists():
                p.unlink()
    if dry_run:
        print("Dry run — nothing restored. Add --apply.")
    else:
        print("Restored.")


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    dest_r = dest.resolve()
    for m in tf.getmembers():
        target = (dest_r / m.name).resolve()
        if dest_r != target and dest_r not in target.parents:
            raise RuntimeError(f"refusing to extract outside destination: {m.name}")
    if hasattr(tarfile, "data_filter"):  # Python 3.12+
        tf.extractall(str(dest_r), filter="data")
    else:
        tf.extractall(str(dest_r))


# --------------------------------------------------------------------------- #
# Relink plan and execution
# --------------------------------------------------------------------------- #

@dataclass
class Action:
    kind: str       # move-session, merge-session, skip-duplicate, conflict, memory, index, config, history, project-file, remove-dir
    detail: str
    src: Optional[Path] = None
    dst: Optional[Path] = None


class RelinkPlan:
    def __init__(self, target: Store, source: Store, ps: ProjectStore, new_cwd: str, paths_mode: str,
                 rewrite_project_files: bool, keep_source: bool):
        self.target = target
        self.source = source
        self.ps = ps
        self.old_cwd = ps.cwd or ""
        self.new_cwd = new_cwd
        self.new_slug = slugify(new_cwd)
        self.new_dir = target.projects_dir / self.new_slug
        self.cross_store = source.claude_dir.resolve() != target.claude_dir.resolve()
        self.mode = "merge" if self.new_dir.exists() else "move"
        self.paths_mode = paths_mode
        self.rewrite_project_files = rewrite_project_files
        self.keep_source = keep_source
        self.mapper = PathMapper(self.old_cwd, self.new_cwd)
        self.actions: List[Action] = []
        self.warnings: List[str] = []
        self.session_moves: List[Tuple[Path, Path]] = []      # (src jsonl, dst jsonl)
        self.sidecar_moves: List[Tuple[Path, Path]] = []      # (src dir, dst dir)
        self.memory_actions: List[Tuple[str, Path, Path]] = []  # (how, src, dst)
        self.skipped: List[Path] = []
        self.conflicts: List[Path] = []

    # ---- planning -------------------------------------------------------- #
    def build(self) -> None:
        ps, nd = self.ps, self.new_dir
        if norm_path_key(self.old_cwd) == norm_path_key(self.new_cwd) and not self.cross_store:
            raise SystemExit("old and new path are the same; nothing to do")
        if ps.dir.resolve() == nd.resolve():
            raise SystemExit(f"source and target slug directories are the same ({nd}); nothing to do")
        existing_ids = {p.stem for p in nd.glob("*.jsonl")} if nd.exists() else set()
        for s in ps.sessions:
            dst = nd / s.file.name
            if s.session_id in existing_ids:
                if dst.stat().st_size == s.size and sha256_file(dst) == sha256_file(s.file):
                    self.skipped.append(s.file)
                    self.actions.append(Action("skip-duplicate", f"{s.session_id} already present (identical)", s.file, dst))
                else:
                    self.conflicts.append(s.file)
                    self.actions.append(Action("conflict", f"{s.session_id} exists at target with different content — kept target's, source copy left in backup", s.file, dst))
                continue
            self.session_moves.append((s.file, dst))
            self.actions.append(Action("move-session" if self.mode == "move" else "merge-session",
                                       f"{s.session_id}  ({human_size(s.size)}, {iso_from_mtime(s.mtime)}){'  ' + s.title if s.title else ''}", s.file, dst))
        moved_ids = {src.stem for src, _ in self.session_moves}
        for entry in sorted(ps.dir.iterdir()):
            if entry.is_dir() and re.match(r"^[0-9a-f-]{36}$", entry.name):
                dst = nd / entry.name
                if entry.name in moved_ids or not dst.exists():
                    self.sidecar_moves.append((entry, dst))
                    self.actions.append(Action("sidecar", f"{entry.name}/ (subagents, tool-results …)", entry, dst))
                else:
                    self.actions.append(Action("sidecar-merge", f"{entry.name}/ merged into existing sidecar dir (no overwrite)", entry, dst))
                    self.sidecar_moves.append((entry, dst))
            elif entry.is_dir() and entry.name == "memory":
                self._plan_memory(entry, nd / "memory")
            elif entry.is_file() and entry.name == "sessions-index.json":
                self.actions.append(Action("index", "sessions-index.json entries rewritten and merged into target index", entry, nd / entry.name))
            elif entry.is_file() and entry.suffix == ".jsonl":
                continue
            elif entry.name not in ("memory", "sessions-index.json"):
                dst = nd / entry.name
                if dst.exists():
                    self.actions.append(Action("extra-skip", f"{entry.name} exists at target; source copy stays in backup", entry, dst))
                else:
                    self.actions.append(Action("extra", f"{entry.name} copied", entry, dst))
        # config
        src_cfg = self.source.load_config()
        old_keys = config_entries_for(src_cfg, self.old_cwd)
        tgt_keys = config_entries_for(self.target.load_config(), self.new_cwd)
        if old_keys or tgt_keys:
            self.actions.append(Action("config", f".claude.json: merge {old_keys or '[]'} -> projects[{self.new_cwd!r}]"
                                                 + (f" (existing entry kept as base)" if tgt_keys else " (new entry)")))
        # history
        n_hist = 0
        for line in read_history(self.source.history_file):
            if line and self.mapper.raw_re.search(line):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict) and isinstance(d.get("project"), str) and self.mapper.is_under(d["project"]):
                    n_hist += 1
        if n_hist:
            self.actions.append(Action("history", f"history.jsonl: {n_hist} prompt entries re-pointed to {self.new_cwd}"
                                                  + (" (appended to target history)" if self.cross_store else "")))
        # project-local files that may embed the old absolute path
        for rel in (".claude/settings.local.json", ".claude/settings.json", ".mcp.json"):
            p = Path(self.new_cwd) / rel
            if p.is_file():
                try:
                    txt = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                n = len(self.mapper.raw_re.findall(txt))
                if n:
                    if self.rewrite_project_files:
                        self.actions.append(Action("project-file", f"{p}: {n} old-path reference(s) rewritten"))
                    else:
                        self.warnings.append(f"{p} still references the old path ({n}x). Re-run with --rewrite-project-files or fix by hand.")
        if not self.keep_source:
            self.actions.append(Action("remove-dir", f"remove emptied source store {ps.dir}"))
        if self.paths_mode == "cwd-only":
            self.warnings.append("--paths cwd-only: only top-level `cwd` fields are rewritten; tool inputs/results keep the old absolute paths.")
        try:
            slugify(self.old_cwd)
        except ValueError as e:
            self.warnings.append(str(e))
        if self.ps.foreign_os:
            self.warnings.append("Source store records Windows paths; sub-path separators will be converted to '/'.")

    def _plan_memory(self, src: Path, dst: Path) -> None:
        if not dst.exists():
            self.memory_actions.append(("copy-dir", src, dst))
            self.actions.append(Action("memory", "memory/ copied (paths rewritten)", src, dst))
            return
        for f in sorted(src.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(src)
            target = dst / rel
            if not target.exists():
                self.memory_actions.append(("copy", f, target))
                self.actions.append(Action("memory", f"memory/{rel} copied", f, target))
            elif sha256_file(f) == sha256_file(target):
                self.actions.append(Action("memory", f"memory/{rel} identical at target; skipped", f, target))
            elif rel.name == "MEMORY.md":
                imported = dst / f"MEMORY.imported-from-{self.ps.slug}.md"
                self.memory_actions.append(("import-index", f, imported))
                self.actions.append(Action("memory", f"memory/MEMORY.md differs: old index saved as {imported.name} and linked from MEMORY.md", f, imported))
            else:
                imported = target.with_name(f"{target.stem}.imported-from-{self.ps.slug}{target.suffix}")
                self.memory_actions.append(("copy", f, imported))
                self.actions.append(Action("memory", f"memory/{rel} differs: old copy saved as {imported.name}", f, imported))

    # ---- presentation ---------------------------------------------------- #
    def describe(self) -> str:
        lines = []
        lines.append(f"Relink plan  [{self.mode.upper()}{', cross-store import' if self.cross_store else ''}]")
        lines.append(f"  from : {self.old_cwd}")
        lines.append(f"         {self.ps.dir}")
        lines.append(f"  to   : {self.new_cwd}")
        lines.append(f"         {self.new_dir}")
        lines.append(f"  sessions: {len(self.session_moves)} to {self.mode}, {len(self.skipped)} duplicates skipped, {len(self.conflicts)} conflicts")
        lines.append(f"  path rewrite: {self.paths_mode}   (old root appears in transcripts as e.g. {self.ps.cwd_variants[0] if self.ps.cwd_variants else self.old_cwd!r})")
        lines.append("")
        for a in self.actions:
            lines.append(f"  [{a.kind:>14}] {a.detail}")
        if self.warnings:
            lines.append("")
            for w in self.warnings:
                lines.append(f"  ! {w}")
        return "\n".join(lines)

    # ---- execution ------------------------------------------------------- #
    def apply(self) -> Backup:
        backup = Backup.create(self.target, f"{self.ps.slug[:40]}")
        if not self.keep_source:  # the source store is only modified (removed) when not kept
            backup.archive_dir(self.ps.dir, "source-store")
        if self.new_dir.exists():
            backup.archive_dir(self.new_dir, "target-store")
        else:
            backup.note_created_dir(self.new_dir)
        backup.copy_file(self.target.config_file, "config")
        backup.copy_file(self.target.history_file, "history")
        backup.write_manifest(operation=f"relink {self.mode}", old_cwd=self.old_cwd, new_cwd=self.new_cwd,
                              old_slug=self.ps.slug, new_slug=self.new_slug, source_claude_dir=str(self.source.claude_dir))
        stats = Counter()
        self.new_dir.mkdir(parents=True, exist_ok=True)
        # 1. transcripts
        for src, dst in self.session_moves:
            stats["lines_changed"] += self._rewrite_jsonl_file(src, dst)
            stats["sessions"] += 1
        # 2. sidecar dirs
        for src, dst in self.sidecar_moves:
            for f in src.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(src)
                target = dst / rel
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if f.suffix == ".jsonl" and "subagents" in rel.parts:
                    stats["lines_changed"] += self._rewrite_jsonl_file(f, target)
                else:
                    shutil.copy2(str(f), str(target))
                stats["sidecar_files"] += 1
        # 3. memory
        for how, src, dst in self.memory_actions:
            if how == "copy-dir":
                for f in src.rglob("*"):
                    if f.is_file():
                        t = dst / f.relative_to(src)
                        t.parent.mkdir(parents=True, exist_ok=True)
                        self._copy_text_rewritten(f, t)
                        stats["memory_files"] += 1
            elif how == "copy":
                dst.parent.mkdir(parents=True, exist_ok=True)
                self._copy_text_rewritten(src, dst)
                stats["memory_files"] += 1
            elif how == "import-index":
                self._copy_text_rewritten(src, dst)
                index = dst.parent / "MEMORY.md"
                txt = index.read_text(encoding="utf-8", errors="replace")
                note = f"- [Notes imported from previous location {self.old_cwd}]({dst.name}) — merged by claude_relink on {_dt.date.today().isoformat()}\n"
                if dst.name not in txt:
                    atomic_write_text(index, txt.rstrip("\n") + "\n" + note)
                stats["memory_files"] += 1
        # 4. extras
        for a in self.actions:
            if a.kind == "extra" and a.src and a.dst:
                if a.src.is_dir():
                    shutil.copytree(str(a.src), str(a.dst), dirs_exist_ok=True)
                else:
                    shutil.copy2(str(a.src), str(a.dst))
        # 5. sessions-index.json
        self._merge_sessions_index()
        # 6. config
        self._apply_config()
        # 7. history
        stats["history"] = self._apply_history()
        # 8. project-local files
        if self.rewrite_project_files:
            for rel in (".claude/settings.local.json", ".claude/settings.json", ".mcp.json"):
                p = Path(self.new_cwd) / rel
                if p.is_file():
                    txt = p.read_text(encoding="utf-8", errors="replace")
                    new, n = self.mapper.rewrite_text(txt)
                    if n:
                        shutil.copy2(str(p), str(backup.dir / (rel.replace('/', '__') + ".bak")))
                        atomic_write_text(p, new)
                        stats["project_files"] += 1
        # 9. remove source store
        if not self.keep_source:
            shutil.rmtree(self.ps.dir)
        backup.write_manifest(stats=dict(stats), completed=_dt.datetime.now().isoformat())
        print(f"Applied: {stats['sessions']} sessions, {stats['lines_changed']} transcript lines rewritten, "
              f"{stats['sidecar_files']} sidecar files, {stats['memory_files']} memory files, {stats['history']} history entries.")
        print(f"Backup: {backup.dir}")
        print(f"Undo  : {sys.argv[0]} undo --backup {backup.dir.name} --claude-dir {self.target.claude_dir} --apply")
        return backup

    def _rewrite_jsonl_file(self, src: Path, dst: Path) -> int:
        raw = src.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        if self.paths_mode == "cwd-only":
            new_text, changed = self._rewrite_cwd_only(text)
        else:
            new_text, changed, unparsable = self.mapper.rewrite_jsonl_text(text)
            if unparsable:
                self.warnings.append(f"{src.name}: {unparsable} unparsable line(s) left untouched")
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
        shutil.copystat(str(src), str(dst))
        return changed

    def _rewrite_cwd_only(self, text: str) -> Tuple[str, int]:
        out = []
        changed = 0
        for line in text.split("\n"):
            if line and '"cwd"' in line and self.mapper.raw_re.search(line):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    out.append(line)
                    continue
                if isinstance(d, dict) and isinstance(d.get("cwd"), str) and self.mapper.is_under(d["cwd"]):
                    d["cwd"] = self.mapper.sub(d["cwd"])
                    out.append(dumps_line(d))
                    changed += 1
                    continue
            out.append(line)
        return "\n".join(out), changed

    def _copy_text_rewritten(self, src: Path, dst: Path) -> None:
        try:
            txt = src.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copy2(str(src), str(dst))
            return
        new, _ = self.mapper.rewrite_text(txt)
        with open(dst, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        shutil.copystat(str(src), str(dst))

    def _merge_sessions_index(self) -> None:
        src_idx = self.ps.dir / "sessions-index.json"
        if not src_idx.is_file():
            return
        try:
            src = json.loads(src_idx.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.warnings.append("source sessions-index.json unreadable; skipped")
            return
        dst_idx = self.new_dir / "sessions-index.json"
        dst = {"version": src.get("version", 1), "entries": []}
        if dst_idx.is_file():
            try:
                dst = json.loads(dst_idx.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                dst = {"version": src.get("version", 1), "entries": []}
        have = {e.get("sessionId") for e in dst.get("entries", [])}
        moved = {s.stem for s, _ in self.session_moves}
        for e in src.get("entries", []):
            sid = e.get("sessionId")
            if sid in have or sid not in moved:
                continue
            e = dict(e)
            e["fullPath"] = str(self.new_dir / f"{sid}.jsonl")
            if isinstance(e.get("projectPath"), str):
                e["projectPath"] = self.mapper.sub(e["projectPath"])
            e, _ = self.mapper.rewrite_obj(e)
            dst.setdefault("entries", []).append(e)
        atomic_write_text(dst_idx, json.dumps(dst, indent=2, ensure_ascii=False) + "\n")

    def _apply_config(self) -> None:
        tgt_cfg = self.target.load_config()
        src_cfg = tgt_cfg if self.source.config_file.resolve() == self.target.config_file.resolve() else self.source.load_config()
        projects = tgt_cfg.setdefault("projects", {})
        old_keys = config_entries_for(src_cfg, self.old_cwd)
        sources = [src_cfg["projects"][k] for k in old_keys]
        tgt_keys = config_entries_for(tgt_cfg, self.new_cwd)
        base = None
        for k in tgt_keys:  # fold spelling variants of the new path too
            base = merge_project_config(base, [projects[k]])
        if not sources and base is None:
            return
        merged = merge_project_config(base, sources)
        merged, _ = self.mapper.rewrite_obj(merged)
        for k in set(tgt_keys) | (set(old_keys) if src_cfg is tgt_cfg else set()):
            projects.pop(k, None)
        projects[self.new_cwd] = merged
        atomic_write_text(self.target.config_file, json.dumps(tgt_cfg, indent=2, ensure_ascii=False) + "\n")

    def _apply_history(self) -> int:
        """Re-point the target history's entries for the old path; on a cross-store import also append the
        source history's entries for that project (deduplicated). Untouched lines stay byte-identical."""
        def key_of(d: dict):
            return (d.get("timestamp"), d.get("sessionId"), d.get("display"))

        existing = set()
        out: List[str] = []
        n = 0
        tgt_lines = read_history(self.target.history_file)
        for line in tgt_lines:
            if not line:
                out.append(line)
                continue
            d = None
            if self.mapper.raw_re.search(line) or self.cross_store:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    d = None
            if isinstance(d, dict):
                existing.add(key_of(d))
                if isinstance(d.get("project"), str) and self.mapper.is_under(d["project"]):
                    d["project"] = self.mapper.sub(d["project"])
                    out.append(dumps_line(d))
                    n += 1
                    continue
            out.append(line)
        added = 0
        if self.cross_store:
            for line in read_history(self.source.history_file):
                if not line or not self.mapper.raw_re.search(line):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict) and isinstance(d.get("project"), str) and self.mapper.is_under(d["project"]):
                    d["project"] = self.mapper.sub(d["project"])
                    if key_of(d) not in existing:
                        existing.add(key_of(d))
                        if out and out[-1] == "":
                            out.pop()
                        out.append(dumps_line(d))
                        out.append("")
                        added += 1
        if n or added:
            atomic_write_text(self.target.history_file, "\n".join(out))
        return n + added


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def resolve_store_selector(stores: List[ProjectStore], selector: str) -> ProjectStore:
    """--from accepts an absolute path (any spelling), a slug, or a unique substring of either."""
    key = norm_path_key(selector)
    exact = [s for s in stores if s.cwd and norm_path_key(s.cwd) == key]
    if len(exact) == 1:
        return exact[0]
    by_slug = [s for s in stores if s.slug == selector]
    if len(by_slug) == 1:
        return by_slug[0]
    try:
        sl = slugify(selector)
        by_slug = [s for s in stores if s.slug == sl]
        if len(by_slug) == 1:
            return by_slug[0]
    except ValueError:
        pass
    fuzzy = [s for s in stores if selector.lower() in s.slug.lower() or (s.cwd and selector.lower() in s.cwd.lower())]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if not fuzzy:
        raise SystemExit(f"no project store matches {selector!r}")
    raise SystemExit("ambiguous --from; matches:\n  " + "\n  ".join(f"{s.slug}  ({s.cwd})" for s in fuzzy))


def cmd_scan(args) -> int:
    store = default_store(args.claude_dir, args.config)
    if not store.projects_dir.is_dir():
        print(f"no projects directory at {store.projects_dir}", file=sys.stderr)
        return 2
    stores = load_all_project_stores(store)
    config = store.load_config()
    known_cwds = {k for k in (config.get("projects") or {}) if os.path.isdir(k)}
    known_cwds |= {s.cwd for s in stores if s.exists_on_disk()}
    candidates = discover_candidate_dirs(args.search_root or [], args.depth)
    cand_pool = sorted(set(candidates) | known_cwds)

    orphans = [s for s in stores if s.is_orphan()]
    unknown = [s for s in stores if s.cwd is None]
    connected = [s for s in stores if s.exists_on_disk()]

    report = []
    for ps in sorted(orphans, key=lambda s: -s.last_activity):
        fp = collect_fingerprint(ps) if cand_pool else Fingerprint()
        cands = score_candidates(ps, fp, cand_pool, known_cwds)[: args.top]
        report.append((ps, fp, cands))

    if args.json:
        out = {
            "claude_dir": str(store.claude_dir),
            "config_file": str(store.config_file),
            "orphaned": [
                {
                    "slug": ps.slug, "cwd": ps.cwd, "cwd_variants": ps.cwd_variants, "foreign_os": ps.foreign_os,
                    "sessions": len(ps.sessions), "bytes": ps.total_bytes, "last_activity": iso_from_mtime(ps.last_activity) if ps.last_activity else None,
                    "extras": ps.extras, "fingerprint_files": len(fp.rel_paths), "repos": dict(fp.repos),
                    "candidates": [{"path": c.path, "score": round(c.score, 2), "reasons": c.reasons} for c in cands],
                }
                for ps, fp, cands in report
            ],
            "unknown": [{"slug": s.slug, "extras": s.extras} for s in unknown],
            "connected": [{"slug": s.slug, "cwd": s.cwd, "sessions": len(s.sessions)} for s in connected],
        }
        print(json.dumps(out, indent=2))
        return 0

    print(f"Claude Code store : {store.claude_dir}")
    print(f"Config            : {store.config_file}{'' if store.config_file.exists() else '  (missing)'}")
    print(f"Project stores    : {len(stores)}  ({len(connected)} connected, {len(orphans)} orphaned, {len(unknown)} undeterminable)")
    if args.search_root:
        print(f"Candidate dirs    : {len(candidates)} under {', '.join(args.search_root)} (depth {args.depth})")
    print()
    if not orphans:
        print("No orphaned project stores found.")
    for ps, fp, cands in report:
        print(f"ORPHANED  {ps.cwd}{'   [windows paths]' if ps.foreign_os else ''}")
        print(f"  store    : {ps.dir}")
        print(f"  sessions : {len(ps.sessions)}  ({human_size(ps.total_bytes)}, last activity {iso_from_mtime(ps.last_activity) if ps.last_activity else 'n/a'})"
              + (f"   extras: {', '.join(ps.extras)}" if ps.extras else ""))
        if len(ps.cwd_variants) > 1:
            print(f"  spellings: {', '.join(ps.cwd_variants)}")
        if fp.repos:
            print(f"  repos    : {', '.join(f'{k} ({v})' for k, v in fp.repos.most_common(3))}")
        for s in sorted(ps.sessions, key=lambda x: -x.mtime)[: args.sessions]:
            label = s.title or s.first_prompt or "(no prompt)"
            print(f"    - {s.session_id[:8]}  {iso_from_mtime(s.mtime)}  {human_size(s.size):>8}  {label[:70]}")
        if len(ps.sessions) > args.sessions:
            print(f"    … {len(ps.sessions) - args.sessions} more")
        if cands:
            print("  candidates:")
            for c in cands:
                tag = "HIGH" if c.score >= 0.8 else "med " if c.score >= 0.5 else "low "
                print(f"    [{tag} {c.score:.2f}] {c.path}")
                for r in c.reasons:
                    print(f"               - {r}")
            best = cands[0]
            if best.score >= 0.5:
                print(f"  relink   : {_self_cmd()} relink --from {_sh(ps.cwd)} --to {_sh(best.path)} --apply")
        elif cand_pool:
            print("  candidates: none scored — try --search-root pointing at where projects live now, or pass --to explicitly")
        else:
            print("  candidates: (pass --search-root DIR to look for the new location)")
        print()
    if unknown:
        print("Stores with no transcripts (cannot determine original path):")
        for s in unknown:
            print(f"  {s.dir}   contents: {', '.join(s.extras) or 'empty'}")
        print()
    if args.all and connected:
        print("Connected stores:")
        for s in sorted(connected, key=lambda x: -x.last_activity):
            print(f"  {len(s.sessions):3d} sessions  {iso_from_mtime(s.last_activity) if s.last_activity else '          '}  {s.cwd}")
    return 0


def _self_cmd() -> str:
    return f"python3 {Path(sys.argv[0]).name}" if sys.argv[0].endswith(".py") else Path(sys.argv[0]).name


def _sh(s: Optional[str]) -> str:
    if s is None:
        return "''"
    if re.match(r"^[A-Za-z0-9_./:-]+$", s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def cmd_relink(args) -> int:
    target = default_store(args.claude_dir, args.config)
    if args.src_claude_dir:
        source = default_store(args.src_claude_dir, args.src_config)
        if not args.src_config:
            inner = source.claude_dir / ".claude.json"
            sibling = source.claude_dir.parent / ".claude.json"
            source = Store(source.claude_dir, inner if inner.exists() else sibling if sibling.exists() else inner)
    else:
        source = target
    if not source.projects_dir.is_dir():
        print(f"no projects directory at {source.projects_dir}", file=sys.stderr)
        return 2
    stores = load_all_project_stores(source)

    if args.auto:
        return _relink_auto(args, target, source, stores)

    if not args.from_ or not args.to:
        print("relink needs --from and --to (or --auto with --search-root)", file=sys.stderr)
        return 2
    ps = resolve_store_selector(stores, args.from_)
    new_cwd = _resolve_new_cwd(args.to)
    plan = RelinkPlan(target, source, ps, new_cwd, args.paths, args.rewrite_project_files,
                      keep_source=args.keep_source or (source.claude_dir.resolve() != target.claude_dir.resolve()))
    plan.build()
    print(plan.describe())
    print()
    return _maybe_apply(args, target, [plan])


def _resolve_new_cwd(to: str) -> str:
    p = Path(to).expanduser()
    if not p.is_dir():
        raise SystemExit(f"--to directory does not exist: {to}")
    real = str(p.resolve())
    if real != str(p.absolute()).rstrip("/"):
        print(f"note: {to} resolves to {real}; Claude Code records the resolved path, using that.")
    return real


def _relink_auto(args, target: Store, source: Store, stores: List[ProjectStore]) -> int:
    if not args.search_root:
        print("--auto needs at least one --search-root", file=sys.stderr)
        return 2
    config = target.load_config()
    known_cwds = {k for k in (config.get("projects") or {}) if os.path.isdir(k)}
    known_cwds |= {s.cwd for s in load_all_project_stores(target) if s.exists_on_disk()}
    pool = sorted(set(discover_candidate_dirs(args.search_root, args.depth)) | known_cwds)
    plans = []
    for ps in sorted((s for s in stores if s.is_orphan()), key=lambda s: -s.last_activity):
        fp = collect_fingerprint(ps)
        cands = score_candidates(ps, fp, pool, known_cwds)
        if not cands:
            print(f"skip  {ps.cwd}: no candidates")
            continue
        best = cands[0]
        runner_up = cands[1].score if len(cands) > 1 else 0.0
        if best.score < args.min_confidence:
            print(f"skip  {ps.cwd}: best candidate {best.path} scores {best.score:.2f} < {args.min_confidence}")
            continue
        if runner_up >= best.score - 0.1:
            print(f"skip  {ps.cwd}: ambiguous ({best.path} {best.score:.2f} vs {cands[1].path} {runner_up:.2f})")
            continue
        plan = RelinkPlan(target, source, ps, best.path, args.paths, args.rewrite_project_files,
                          keep_source=args.keep_source or (source.claude_dir.resolve() != target.claude_dir.resolve()))
        plan.build()
        print(plan.describe())
        print()
        plans.append(plan)
    if not plans:
        print("Nothing to relink.")
        return 0
    return _maybe_apply(args, target, plans)


def _maybe_apply(args, target: Store, plans: List[RelinkPlan]) -> int:
    if not args.apply:
        print(f"Dry run — nothing changed. Re-run with --apply to execute ({len(plans)} plan(s)).")
        return 0
    live = running_claude_sessions(target)
    if live and not args.force:
        print("Refusing to modify the store while Claude Code sessions are running:", file=sys.stderr)
        for l in live:
            print(f"  pid {l['pid']}  {l.get('cwd')}", file=sys.stderr)
        print("Exit them (or pass --force if you are sure these are stale).", file=sys.stderr)
        return 3
    for plan in plans:
        print(f"== applying: {plan.old_cwd} -> {plan.new_cwd}")
        plan.apply()
        for w in plan.warnings:
            print(f"  ! {w}")
        print()
    return 0


def cmd_undo(args) -> int:
    store = default_store(args.claude_dir, args.config)
    root = store.claude_dir / "backups" / "claude-relink"
    if not args.backup:
        if not root.is_dir():
            print("no relink backups found")
            return 1
        print(f"Backups in {root}:")
        for d in sorted(root.iterdir()):
            m = d / "manifest.json"
            if m.is_file():
                try:
                    man = json.loads(m.read_text(encoding="utf-8"))
                    print(f"  {d.name}   {man.get('operation')}   {man.get('old_cwd')} -> {man.get('new_cwd')}")
                except (OSError, json.JSONDecodeError):
                    print(f"  {d.name}   (unreadable manifest)")
        print("Pass --backup <name> [--apply] to restore one.")
        return 0
    bdir = Path(args.backup)
    if not bdir.is_absolute():
        bdir = root / args.backup
    if not (bdir / "manifest.json").is_file():
        print(f"no manifest in {bdir}", file=sys.stderr)
        return 2
    if args.apply:
        live = running_claude_sessions(store)
        if live and not args.force:
            print("Refusing to restore while Claude Code sessions are running (use --force to override).", file=sys.stderr)
            return 3
    restore_backup(bdir, dry_run=not args.apply)
    return 0


def cmd_dedupe(args) -> int:
    store = default_store(args.claude_dir, args.config)
    cfg = store.load_config()
    projects = cfg.get("projects") or {}
    groups: Dict[str, List[str]] = defaultdict(list)
    for k in projects:
        groups[norm_path_key(k)].append(k)
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    if not dupes:
        print("No duplicate project entries in", store.config_file)
        return 0
    for key, keys in dupes.items():
        existing = [k for k in keys if os.path.isdir(k)]
        canonical = existing[0] if existing else (max(keys, key=lambda k: len(json.dumps(projects[k]))))
        print(f"{len(keys)} spellings -> {canonical!r}")
        for k in keys:
            print(f"    {k!r}{'  (keep as canonical)' if k == canonical else '  (fold in)'}")
        if args.apply:
            base = projects[canonical]
            merged = merge_project_config(base, [projects[k] for k in keys if k != canonical])
            for k in keys:
                projects.pop(k, None)
            projects[canonical] = merged
    if args.apply:
        live = running_claude_sessions(store)
        if live and not args.force:
            print("Refusing to modify config while Claude Code sessions are running (use --force to override).", file=sys.stderr)
            return 3
        backup = Backup.create(store, "dedupe")
        backup.copy_file(store.config_file, "config")
        backup.write_manifest(operation="dedupe")
        atomic_write_text(store.config_file, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
        print(f"Config rewritten. Backup: {backup.dir}")
    else:
        print("Dry run — add --apply to merge these entries.")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude_relink", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"claude_relink {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--claude-dir", help="Claude Code data dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    common.add_argument("--config", help="path to .claude.json (default: next to the data dir per Claude Code's rules)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("scan", parents=[common], help="find orphaned project stores and candidate new locations")
    s.add_argument("--search-root", action="append", metavar="DIR", help="directory tree to search for moved projects (repeatable)")
    s.add_argument("--depth", type=int, default=2, help="how deep to look under each search root (default 2)")
    s.add_argument("--top", type=int, default=3, help="candidates to show per orphan (default 3)")
    s.add_argument("--sessions", type=int, default=5, help="sessions to list per orphan (default 5)")
    s.add_argument("--all", action="store_true", help="also list connected (healthy) stores")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("relink", parents=[common], help="move/merge an orphaned store to the project's new directory")
    r.add_argument("--from", dest="from_", metavar="OLD", help="old project path (any spelling), slug, or unique substring")
    r.add_argument("--to", metavar="NEWDIR", help="the directory the project lives in now")
    r.add_argument("--auto", action="store_true", help="relink every orphan with a single high-confidence candidate")
    r.add_argument("--search-root", action="append", metavar="DIR", help="(with --auto) where to look for moved projects")
    r.add_argument("--depth", type=int, default=2)
    r.add_argument("--min-confidence", type=float, default=0.8, help="(with --auto) minimum candidate score (default 0.8)")
    r.add_argument("--paths", choices=["all", "cwd-only"], default="all",
                   help="rewrite every old-path reference in transcripts (default) or only the cwd fields")
    r.add_argument("--rewrite-project-files", action="store_true",
                   help="also rewrite old paths inside <new>/.claude/settings*.json and .mcp.json")
    r.add_argument("--src-claude-dir", metavar="DIR", help="import from another Claude data dir (e.g. a rescued ~/.claude copy)")
    r.add_argument("--src-config", metavar="FILE", help="the .claude.json that goes with --src-claude-dir")
    r.add_argument("--keep-source", action="store_true", help="leave the old store directory in place (default when importing)")
    r.add_argument("--apply", action="store_true", help="execute (default is a dry run)")
    r.add_argument("--force", action="store_true", help="proceed even if Claude Code sessions appear to be running")
    r.set_defaults(func=cmd_relink)

    u = sub.add_parser("undo", parents=[common], help="list relink backups or restore one")
    u.add_argument("--backup", metavar="NAME", help="backup directory name (from relink output) or absolute path")
    u.add_argument("--apply", action="store_true")
    u.add_argument("--force", action="store_true")
    u.set_defaults(func=cmd_undo)

    d = sub.add_parser("dedupe", parents=[common], help="merge .claude.json project entries that differ only by path spelling")
    d.add_argument("--apply", action="store_true")
    d.add_argument("--force", action="store_true")
    d.set_defaults(func=cmd_dedupe)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = ("scan", "relink", "undo", "dedupe")
    pos = next((i for i, a in enumerate(argv) if a in commands), None)
    if pos is None:
        if not argv or argv[0] not in ("-h", "--help", "--version"):
            argv = ["scan"] + argv
    elif pos > 0:  # allow `--claude-dir X relink ...` as well as `relink --claude-dir X ...`
        argv = [argv[pos]] + argv[:pos] + argv[pos + 1:]
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
