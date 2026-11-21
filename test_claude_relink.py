#!/usr/bin/env python3
"""
Tests for claude_relink.py. Builds a synthetic Claude Code store that mirrors
the real on-disk layout (transcript records with cwd, subagents, tool-results,
memory/, sessions-index.json, .claude.json projects, history.jsonl) and checks
scan / relink (merge + move + Windows import) / undo / dedupe end to end.

Run:  python3 test_claude_relink.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_relink as cr  # noqa: E402


def rec(kind, cwd, sid, **extra):
    d = {"parentUuid": None, "isSidechain": False, "type": kind, "uuid": hashlib.md5(json.dumps(extra, sort_keys=True).encode()).hexdigest(),
         "timestamp": "2026-10-01T12:00:00.000Z", "sessionId": sid, "version": "2.1.300", "gitBranch": "main", "cwd": cwd,
         "userType": "external", "entrypoint": "cli"}
    d.update(extra)
    return d


def user_text(cwd, sid, text):
    return rec("user", cwd, sid, message={"role": "user", "content": text})


def tool_use(cwd, sid, name, inp):
    return rec("assistant", cwd, sid, message={"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": name, "input": inp}]})


def tool_result(cwd, sid, text, file_path=None):
    d = rec("user", cwd, sid, message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": text}]})
    if file_path:
        d["toolUseResult"] = {"type": "text", "file": {"filePath": file_path, "content": "x"}, "filePath": file_path}
    return d


def write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


class Fixture:
    """A fake ~/.claude plus fake project directories."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="claude-relink-test-"))
        self.home = self.root / "home"
        self.claude_dir = self.home / ".claude"
        self.config = self.home / ".claude.json"
        self.projects = self.claude_dir / "projects"
        self.old_root = str(self.root / "dev" / "personal" / "projA")   # gone
        self.new_root = str(self.root / "dev" / "acme" / "projA")  # exists
        self.decoy = str(self.root / "dev" / "acme" / "projA-fork")
        self.other_old = str(self.root / "dev" / "personal" / "projB")  # gone, no target yet
        self.other_new = str(self.root / "dev" / "acme" / "projB")
        self._build()

    def _build(self):
        # real project dirs (the moved projects)
        for root in (self.new_root, self.decoy, self.other_new):
            for rel in ("src/main.py", "README.md", "docs/arch.md"):
                p = Path(root) / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("# content\n")
        (Path(self.new_root) / "src" / "only_in_real.py").write_text("real\n")
        (Path(self.new_root) / ".git").mkdir()
        (Path(self.new_root) / ".git" / "config").write_text('[remote "origin"]\n\turl = git@github.com:acme/projA.git\n')
        (Path(self.decoy) / ".git").mkdir()
        (Path(self.decoy) / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/someone/projA-fork.git\n')
        (Path(self.new_root) / ".claude").mkdir()
        (Path(self.new_root) / ".claude" / "settings.local.json").write_text(json.dumps(
            {"permissions": {"allow": [f"Read({self.old_root}/**)", "Bash(npm test)"]}}, indent=2))

        # --- old store for projA: 2 sessions, subagent, tool-results, memory, index
        old_slug = cr.slugify(self.old_root)
        sd = self.projects / old_slug
        s1, s2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
        write_jsonl(sd / f"{s1}.jsonl", [
            {"type": "queue-operation", "operation": "enqueue", "sessionId": s1, "timestamp": "2026-09-01T00:00:00Z", "content": "hi"},
            user_text(self.old_root, s1, "Please refactor the main module"),
            tool_use(self.old_root, s1, "Read", {"file_path": f"{self.old_root}/src/main.py"}),
            tool_result(self.old_root, s1, "print('x')", file_path=f"{self.old_root}/src/main.py"),
            tool_use(self.old_root, s1, "Bash", {"command": f"cd {self.old_root} && git status", "description": "status"}),
            tool_use(self.old_root, s1, "Read", {"file_path": f"{self.old_root}/src/only_in_real.py"}),
            tool_use(self.old_root, s1, "Read", {"file_path": f"{self.old_root}/docs/arch.md"}),
            {"type": "file-history-snapshot", "messageId": "m1", "snapshot": {"messageId": "m1", "trackedFileBackups": {f"{self.old_root}/src/main.py": {"backupFileName": "abc@v1", "version": 1}}}, "isSnapshotUpdate": True},
            {"type": "ai-title", "aiTitle": "Refactor main module", "sessionId": s1},
            {"type": "pr-link", "sessionId": s1, "prNumber": 3, "prUrl": "https://github.com/acme/projA/pull/3", "prRepository": "acme/projA", "timestamp": "2026-09-01T00:00:00Z"},
            user_text(self.old_root, s1, f"note: also look at {self.old_root}2 (a different project) and {self.old_root}.bak"),
        ])
        write_jsonl(sd / f"{s2}.jsonl", [
            user_text(self.old_root, s2, "Second session, unicode ✓ ünïcödé"),
            tool_use(self.old_root, s2, "Edit", {"file_path": f"{self.old_root}/README.md", "old_string": "a", "new_string": "b"}),
        ])
        # a corrupt trailing line must survive untouched
        with open(sd / f"{s2}.jsonl", "a", encoding="utf-8") as f:
            f.write('{"type":"user","cwd":"' + self.old_root.replace("\\", "\\\\") + '","truncated')
        write_jsonl(sd / s1 / "subagents" / "agent-abc.jsonl", [
            tool_use(self.old_root, s1, "Grep", {"pattern": "foo", "path": f"{self.old_root}/src"}),
        ])
        (sd / s1 / "tool-results").mkdir(parents=True)
        (sd / s1 / "tool-results" / "big.txt").write_text(f"output mentioning {self.old_root}/src/main.py\n")
        (sd / "memory").mkdir()
        (sd / "memory" / "MEMORY.md").write_text(f"# Memory Index\n\n- [Build notes](build.md) — in {self.old_root}\n")
        (sd / "memory" / "build.md").write_text(f"Build from {self.old_root}/src with make.\n")
        (sd / "memory" / "shared.md").write_text("identical content\n")
        (sd / "sessions-index.json").write_text(json.dumps({"version": 1, "entries": [
            {"sessionId": s1, "fullPath": str(sd / f"{s1}.jsonl"), "fileMtime": 1, "firstPrompt": "Please refactor", "messageCount": 5,
             "created": "2026-09-01T00:00:00Z", "modified": "2026-09-01T00:00:00Z", "gitBranch": "main", "projectPath": self.old_root, "isSidechain": False},
        ]}, indent=2))
        os.utime(sd / f"{s1}.jsonl", (1_790_000_000, 1_790_000_000))

        # --- new store for projA already exists (user already ran claude there): 1 session + memory
        new_slug = cr.slugify(self.new_root)
        nd = self.projects / new_slug
        s3 = "33333333-3333-4333-8333-333333333333"
        write_jsonl(nd / f"{s3}.jsonl", [user_text(self.new_root, s3, "New-location session")])
        (nd / "memory").mkdir()
        (nd / "memory" / "MEMORY.md").write_text("# Memory Index\n\n- [New notes](new.md)\n")
        (nd / "memory" / "new.md").write_text("new\n")
        (nd / "memory" / "shared.md").write_text("identical content\n")

        # --- old store for projB: no target store yet (pure move)
        sdb = self.projects / cr.slugify(self.other_old)
        s4 = "44444444-4444-4444-8444-444444444444"
        write_jsonl(sdb / f"{s4}.jsonl", [user_text(self.other_old, s4, "projB work"),
                                           tool_use(self.other_old, s4, "Read", {"file_path": f"{self.other_old}/README.md"})])

        # --- a connected store (healthy)
        cd = self.projects / cr.slugify(str(self.root / "dev" / "acme" / "projA-fork"))
        s5 = "55555555-5555-4555-8555-555555555555"
        write_jsonl(cd / f"{s5}.jsonl", [user_text(self.decoy, s5, "fork work")])

        # --- a Windows-origin store (e.g. restored from a laptop) with duplicate-spelling config entries
        wroot = "D:\\development\\projA"
        wd = self.projects / cr.slugify(wroot)
        s6 = "66666666-6666-4666-8666-666666666666"
        write_jsonl(wd / f"{s6}.jsonl", [
            user_text(wroot, s6, "windows session"),
            user_text("d:\\development\\projA", s6, "lower-case drive"),
            tool_use(wroot, s6, "Read", {"file_path": "D:\\development\\projA\\src\\main.py"}),
            tool_use(wroot, s6, "Bash", {"command": "cd D:\\development\\projA\\docs && dir"}),
            tool_use("D:\\development\\projA\\docs", s6, "Read", {"file_path": "d:/development/projA/docs/arch.md"}),
        ])

        # config + history
        cfg = {"userID": "u", "projects": {
            self.old_root: {"allowedTools": ["Bash(npm test)", "Read"], "mcpServers": {"old-only": {"command": "x"}, "both": {"command": "old"}},
                            "hasTrustDialogAccepted": True, "projectOnboardingSeenCount": 3, "enabledMcpjsonServers": ["a"]},
            self.new_root: {"allowedTools": ["Read", "Write"], "mcpServers": {"both": {"command": "new"}}, "hasTrustDialogAccepted": False,
                            "projectOnboardingSeenCount": 1, "lastSessionId": "33333333-3333-4333-8333-333333333333"},
            self.decoy: {"allowedTools": [], "hasTrustDialogAccepted": True},
            self.other_old: {"allowedTools": ["Grep"], "hasTrustDialogAccepted": True},
            "D:\\development\\projA": {"allowedTools": ["Read"], "hasTrustDialogAccepted": True},
            "D:/development/projA": {"allowedTools": ["Edit"], "mcpServers": {"w": {"command": "w"}}},
            "d:/development/projA": {"allowedTools": ["Bash(dir)"], "hasTrustDialogAccepted": False},
        }}
        self.config.write_text(json.dumps(cfg, indent=2))
        hist = [
            {"display": "Please refactor the main module", "pastedContents": {}, "timestamp": 1_790_000_000_000, "project": self.old_root, "sessionId": s1},
            {"display": "Second session", "pastedContents": {}, "timestamp": 1_790_000_001_000, "project": self.old_root, "sessionId": s2},
            {"display": "New-location session", "pastedContents": {}, "timestamp": 1_790_000_002_000, "project": self.new_root, "sessionId": s3},
            {"display": "fork work", "pastedContents": {}, "timestamp": 1_790_000_003_000, "project": self.decoy, "sessionId": s5},
            {"display": "projB work", "pastedContents": {}, "timestamp": 1_790_000_004_000, "project": self.other_old, "sessionId": s4},
        ]
        write_jsonl(self.claude_dir / "history.jsonl", hist)
        (self.claude_dir / "sessions").mkdir()
        (self.claude_dir / "sessions" / "999999.json").write_text(json.dumps({"pid": 999999, "sessionId": "dead", "cwd": self.new_root}))

    def store(self):
        return cr.Store(self.claude_dir, self.config)

    def snapshot(self):
        """Hash of every file under the claude dir except backups — for undo round-trip checks."""
        out = {}
        for p in sorted(self.claude_dir.rglob("*")):
            if p.is_file() and "backups" not in p.parts:
                out[str(p.relative_to(self.claude_dir))] = hashlib.sha256(p.read_bytes()).hexdigest()
        out[".claude.json"] = hashlib.sha256(self.config.read_bytes()).hexdigest()
        return out

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class RelinkTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.cleanup()

    def args(self, *argv):
        # common options are accepted before or after the subcommand
        return [argv[0], "--claude-dir", str(self.fx.claude_dir), "--config", str(self.fx.config), *argv[1:]]

    # ---- unit-level ------------------------------------------------------ #
    def test_slugify_matches_claude_code(self):
        self.assertEqual(cr.slugify("/home/claude"), "-home-claude")
        self.assertEqual(cr.slugify("/srv/dev/acme/ledger-api"), "-srv-dev-acme-ledger-api")
        self.assertEqual(cr.slugify("D:\\development\\ledger_api"), "D--development-ledger-api")
        self.assertEqual(cr.slugify("d:/development/ledger-api"), "d--development-ledger-api")

    def test_mapper_posix_boundaries(self):
        m = cr.PathMapper("/dev/personal/projA", "/dev/acme/projA")
        self.assertEqual(m.sub("/dev/personal/projA"), "/dev/acme/projA")
        self.assertEqual(m.sub("/dev/personal/projA/src/x.py"), "/dev/acme/projA/src/x.py")
        self.assertEqual(m.sub("cd /dev/personal/projA && ls"), "cd /dev/acme/projA && ls")
        self.assertEqual(m.sub("/dev/personal/projA2/x"), "/dev/personal/projA2/x")      # different project
        self.assertEqual(m.sub("/dev/personal/projA.bak"), "/dev/personal/projA.bak")     # different project
        self.assertEqual(m.sub("Read(/dev/personal/projA/**)"), "Read(/dev/acme/projA/**)")
        self.assertEqual(m.sub("your cwd is /dev/personal/projA."), "your cwd is /dev/acme/projA.")
        self.assertEqual(m.sub("see /dev/personal/projA. Then"), "see /dev/acme/projA. Then")
        self.assertEqual(m.sub("/dev/personal/projA-v2"), "/dev/personal/projA-v2")

    def test_mapper_windows_to_posix(self):
        m = cr.PathMapper("D:\\development\\projA", "/srv/dev/acme/projA")
        self.assertEqual(m.sub("D:\\development\\projA"), "/srv/dev/acme/projA")
        self.assertEqual(m.sub("d:\\development\\projA\\src\\main.py"), "/srv/dev/acme/projA/src/main.py")
        self.assertEqual(m.sub("D:/development/projA/docs/arch.md"), "/srv/dev/acme/projA/docs/arch.md")
        self.assertEqual(m.sub("cd D:\\development\\projA\\docs && dir"), "cd /srv/dev/acme/projA/docs && dir")
        self.assertEqual(m.sub("see (D:\\development\\projA\\x) now"), "see (/srv/dev/acme/projA/x) now")
        self.assertEqual(m.sub("D:\\development\\projAB\\x"), "D:\\development\\projAB\\x")
        # raw-line precheck sees the JSON-escaped form
        self.assertTrue(m.raw_re.search('{"cwd":"D:\\\\development\\\\projA"}'))
        self.assertTrue(m.raw_re.search('{"cwd":"d:/development/projA"}'))

    def test_rewrite_obj_keys_and_values(self):
        m = cr.PathMapper("/old/p", "/new/p")
        obj = {"/old/p/a.py": {"x": ["/old/p/b", 1, None]}, "cwd": "/old/p", "other": "/old/q"}
        new, changed = m.rewrite_obj(obj)
        self.assertTrue(changed)
        self.assertEqual(new, {"/new/p/a.py": {"x": ["/new/p/b", 1, None]}, "cwd": "/new/p", "other": "/old/q"})

    def test_merge_project_config(self):
        t = {"allowedTools": ["Read"], "mcpServers": {"both": {"command": "new"}}, "hasTrustDialogAccepted": False, "projectOnboardingSeenCount": 1}
        s = {"allowedTools": ["Bash", "Read"], "mcpServers": {"both": {"command": "old"}, "old": {}}, "hasTrustDialogAccepted": True, "projectOnboardingSeenCount": 3, "lastCost": 1.5}
        m = cr.merge_project_config(t, [s])
        self.assertEqual(m["allowedTools"], ["Read", "Bash"])
        self.assertEqual(m["mcpServers"], {"both": {"command": "new"}, "old": {}})
        self.assertTrue(m["hasTrustDialogAccepted"])
        self.assertEqual(m["projectOnboardingSeenCount"], 3)
        self.assertEqual(m["lastCost"], 1.5)

    # ---- scan ------------------------------------------------------------ #
    def test_scan_finds_orphans_and_ranks_candidates(self):
        stores = cr.load_all_project_stores(self.fx.store())
        orphans = {s.cwd for s in stores if s.is_orphan()}
        self.assertEqual(orphans, {self.fx.old_root, self.fx.other_old, "D:\\development\\projA"})
        ps = next(s for s in stores if s.cwd == self.fx.old_root)
        self.assertEqual(len(ps.sessions), 2)
        self.assertIn("memory", ps.extras)
        fp = cr.collect_fingerprint(ps)
        self.assertEqual(fp.rel_paths, {"src/main.py", "src/only_in_real.py", "docs/arch.md", "README.md", "src"})
        self.assertIn("acme/projA", fp.repos)
        pool = cr.discover_candidate_dirs([str(self.fx.root / "dev")], 2)
        cands = cr.score_candidates(ps, fp, pool, set())
        self.assertEqual(cands[0].path, self.fx.new_root)
        self.assertGreaterEqual(cands[0].score, 0.8)
        self.assertTrue(any(c.path == self.fx.decoy for c in cands))
        self.assertLess([c for c in cands if c.path == self.fx.decoy][0].score, cands[0].score - 0.1)
        # windows-origin store still matches the POSIX candidate by name + fingerprint
        wps = next(s for s in stores if s.cwd and s.cwd.lower().startswith("d:"))
        wfp = cr.collect_fingerprint(wps)
        self.assertEqual(wfp.rel_paths, {"src/main.py", "docs/arch.md"})
        wc = cr.score_candidates(wps, wfp, pool, set())
        self.assertEqual(wc[0].path, self.fx.new_root)

    def test_scan_cli_runs(self):
        rc = cr.main(self.args("scan", "--search-root", str(self.fx.root / "dev"), "--all"))
        self.assertEqual(rc, 0)
        rc = cr.main(self.args("scan", "--json"))
        self.assertEqual(rc, 0)

    # ---- relink: merge into existing store -------------------------------- #
    def test_relink_merge_dry_run_changes_nothing(self):
        before = self.fx.snapshot()
        rc = cr.main(self.args("relink", "--from", self.fx.old_root, "--to", self.fx.new_root))
        self.assertEqual(rc, 0)
        self.assertEqual(before, self.fx.snapshot())

    def test_relink_merge_apply_and_undo(self):
        before = self.fx.snapshot()
        rc = cr.main(self.args("relink", "--from", self.fx.old_root, "--to", self.fx.new_root, "--rewrite-project-files", "--apply"))
        self.assertEqual(rc, 0)
        old_dir = self.fx.projects / cr.slugify(self.fx.old_root)
        new_dir = self.fx.projects / cr.slugify(self.fx.new_root)
        self.assertFalse(old_dir.exists())
        s1 = "11111111-1111-4111-8111-111111111111"
        s2 = "22222222-2222-4222-8222-222222222222"
        # transcripts moved and rewritten
        for sid in (s1, s2, "33333333-3333-4333-8333-333333333333"):
            self.assertTrue((new_dir / f"{sid}.jsonl").exists())
        text = (new_dir / f"{s1}.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(self.fx.old_root + "/", text)
        self.assertNotIn(f'"cwd":"{self.fx.old_root}"', text)
        self.assertIn(f'"cwd":"{self.fx.new_root}"', text)
        self.assertIn(f"{self.fx.new_root}/src/main.py", text)                       # tool input + toolUseResult + snapshot key
        self.assertIn(f"cd {self.fx.new_root} && git status", text)
        self.assertIn(f"{self.fx.old_root}2 (a different project) and {self.fx.old_root}.bak", text)  # boundaries respected
        self.assertIn("acme/projA/pull/3", text)                        # unrelated content untouched
        recs = [json.loads(l) for l in text.splitlines() if l]
        snap = next(r for r in recs if r["type"] == "file-history-snapshot")
        self.assertEqual(list(snap["snapshot"]["trackedFileBackups"]), [f"{self.fx.new_root}/src/main.py"])
        # mtime preserved (resume picker sorts by it)
        self.assertEqual(int((new_dir / f"{s1}.jsonl").stat().st_mtime), 1_790_000_000)
        # corrupt trailing line preserved verbatim, unicode intact
        t2 = (new_dir / f"{s2}.jsonl").read_text(encoding="utf-8")
        self.assertTrue(t2.endswith('","truncated'))
        self.assertIn("ünïcödé", t2)
        # subagent rewritten, tool-results copied as-is
        sub = (new_dir / s1 / "subagents" / "agent-abc.jsonl").read_text()
        self.assertIn(f'"path":"{self.fx.new_root}/src"', sub)
        self.assertIn(self.fx.old_root, (new_dir / s1 / "tool-results" / "big.txt").read_text())
        # memory merged: new index kept, old index imported and linked, topic copied, identical skipped
        idx = (new_dir / "memory" / "MEMORY.md").read_text()
        self.assertIn("[New notes](new.md)", idx)
        imported = new_dir / "memory" / f"MEMORY.imported-from-{cr.slugify(self.fx.old_root)}.md"
        self.assertTrue(imported.exists())
        self.assertIn(imported.name, idx)
        self.assertIn(self.fx.new_root, imported.read_text())
        self.assertIn(self.fx.new_root + "/src", (new_dir / "memory" / "build.md").read_text())
        self.assertEqual((new_dir / "memory" / "shared.md").read_text(), "identical content\n")
        self.assertFalse(list((new_dir / "memory").glob("shared.imported-*")))
        # sessions-index merged and rewritten
        index = json.loads((new_dir / "sessions-index.json").read_text())
        e = index["entries"][0]
        self.assertEqual(e["projectPath"], self.fx.new_root)
        self.assertEqual(e["fullPath"], str(new_dir / f"{s1}.jsonl"))
        # config merged
        cfg = json.loads(self.fx.config.read_text())
        self.assertNotIn(self.fx.old_root, cfg["projects"])
        p = cfg["projects"][self.fx.new_root]
        self.assertEqual(p["allowedTools"], ["Read", "Write", "Bash(npm test)"])
        self.assertEqual(p["mcpServers"], {"both": {"command": "new"}, "old-only": {"command": "x"}})
        self.assertTrue(p["hasTrustDialogAccepted"])
        self.assertEqual(p["projectOnboardingSeenCount"], 3)
        self.assertEqual(p["lastSessionId"], "33333333-3333-4333-8333-333333333333")
        self.assertIn(self.fx.decoy, cfg["projects"])  # untouched
        self.assertIn("D:\\development\\projA", cfg["projects"])  # untouched
        # history re-pointed
        hist = [json.loads(l) for l in (self.fx.claude_dir / "history.jsonl").read_text().splitlines() if l]
        self.assertEqual([h["project"] for h in hist], [self.fx.new_root, self.fx.new_root, self.fx.new_root, self.fx.decoy, self.fx.other_old])
        # project-local settings rewritten
        local = json.loads((Path(self.fx.new_root) / ".claude" / "settings.local.json").read_text())
        self.assertEqual(local["permissions"]["allow"][0], f"Read({self.fx.new_root}/**)")
        # backup exists with manifest
        backups = list((self.fx.claude_dir / "backups" / "claude-relink").iterdir())
        self.assertEqual(len(backups), 1)
        man = json.loads((backups[0] / "manifest.json").read_text())
        self.assertEqual(man["operation"], "relink merge")
        self.assertEqual(man["stats"]["sessions"], 2)
        # undo restores the exact prior state
        (Path(self.fx.new_root) / ".claude" / "settings.local.json").write_text(
            json.dumps({"permissions": {"allow": [f"Read({self.fx.old_root}/**)", "Bash(npm test)"]}}, indent=2))  # project files are outside undo's scope
        rc = cr.main(self.args("undo", "--backup", backups[0].name, "--apply"))
        self.assertEqual(rc, 0)
        self.assertEqual(before, self.fx.snapshot())

    def test_relink_duplicate_and_conflict_sessions(self):
        old_dir = self.fx.projects / cr.slugify(self.fx.old_root)
        new_dir = self.fx.projects / cr.slugify(self.fx.new_root)
        s1 = "11111111-1111-4111-8111-111111111111"
        s2 = "22222222-2222-4222-8222-222222222222"
        shutil.copy2(old_dir / f"{s1}.jsonl", new_dir / f"{s1}.jsonl")          # identical duplicate
        (new_dir / f"{s2}.jsonl").write_text('{"type":"user","cwd":"x"}\n')      # conflicting content
        rc = cr.main(self.args("relink", "--from", self.fx.old_root, "--to", self.fx.new_root, "--apply"))
        self.assertEqual(rc, 0)
        self.assertEqual((new_dir / f"{s2}.jsonl").read_text(), '{"type":"user","cwd":"x"}\n')  # target kept
        self.assertFalse(old_dir.exists())
        # the conflicting source copy is recoverable from the backup archive
        backups = list((self.fx.claude_dir / "backups" / "claude-relink").iterdir())
        self.assertTrue((backups[0] / "source-store.tar.gz").exists())

    # ---- relink: pure move (no target store) ------------------------------ #
    def test_relink_move(self):
        rc = cr.main(self.args("relink", "--from", "projB", "--to", self.fx.other_new, "--apply"))
        self.assertEqual(rc, 0)
        new_dir = self.fx.projects / cr.slugify(self.fx.other_new)
        self.assertTrue(new_dir.exists())
        self.assertFalse((self.fx.projects / cr.slugify(self.fx.other_old)).exists())
        text = (new_dir / "44444444-4444-4444-8444-444444444444.jsonl").read_text()
        self.assertIn(f'"cwd":"{self.fx.other_new}"', text)
        cfg = json.loads(self.fx.config.read_text())
        self.assertEqual(cfg["projects"][self.fx.other_new]["allowedTools"], ["Grep"])
        self.assertNotIn(self.fx.other_old, cfg["projects"])
        man = json.loads(next((self.fx.claude_dir / "backups" / "claude-relink").iterdir()).joinpath("manifest.json").read_text())
        self.assertEqual(man["operation"], "relink move")
        self.assertTrue(any(i["kind"] == "created-dir" for i in man["items"]))
        # undo removes the created dir and restores the old one
        rc = cr.main(self.args("undo", "--backup", Path(man["items"][0]["path"]).name if False else next((self.fx.claude_dir / "backups" / "claude-relink").iterdir()).name, "--apply"))
        self.assertEqual(rc, 0)
        self.assertFalse(new_dir.exists())
        self.assertTrue((self.fx.projects / cr.slugify(self.fx.other_old)).exists())

    def test_cwd_only_mode(self):
        rc = cr.main(self.args("relink", "--from", "projB", "--to", self.fx.other_new, "--paths", "cwd-only", "--apply"))
        self.assertEqual(rc, 0)
        text = (self.fx.projects / cr.slugify(self.fx.other_new) / "44444444-4444-4444-8444-444444444444.jsonl").read_text()
        self.assertIn(f'"cwd":"{self.fx.other_new}"', text)
        self.assertIn(f'"file_path":"{self.fx.other_old}/README.md"', text)

    # ---- relink: Windows store import -> POSIX target ---------------------- #
    def test_relink_windows_store_into_posix_target(self):
        rc = cr.main(self.args("relink", "--from", "D:/development/projA", "--to", self.fx.new_root, "--apply"))
        self.assertEqual(rc, 0)
        new_dir = self.fx.projects / cr.slugify(self.fx.new_root)
        text = (new_dir / "66666666-6666-4666-8666-666666666666.jsonl").read_text()
        self.assertNotIn("D:\\\\development", text)
        self.assertNotIn("d:\\\\development", text)
        self.assertIn(f'"cwd":"{self.fx.new_root}"', text)
        self.assertIn(f'"cwd":"{self.fx.new_root}/docs"', text)
        self.assertIn(f'"file_path":"{self.fx.new_root}/src/main.py"', text)
        self.assertIn(f'"file_path":"{self.fx.new_root}/docs/arch.md"', text)
        self.assertIn(f'"command":"cd {self.fx.new_root}/docs && dir"', text)
        cfg = json.loads(self.fx.config.read_text())
        for k in ("D:\\development\\projA", "D:/development/projA", "d:/development/projA"):
            self.assertNotIn(k, cfg["projects"])
        p = cfg["projects"][self.fx.new_root]
        self.assertEqual(p["allowedTools"], ["Read", "Write", "Edit", "Bash(dir)"])
        self.assertIn("w", p["mcpServers"])

    # ---- cross-store import (rescued ~/.claude from another machine) ------- #
    def test_import_from_other_store(self):
        src_root = self.fx.root / "rescue"
        src_claude = src_root / "claude"
        shutil.copytree(self.fx.claude_dir, src_claude, ignore=shutil.ignore_patterns("backups"))
        shutil.copy2(self.fx.config, src_root / ".claude.json")
        # wipe the orphan from the live store so the only copy is in the rescue
        shutil.rmtree(self.fx.projects / cr.slugify(self.fx.old_root))
        before_src = {str(p): p.stat().st_mtime for p in src_claude.rglob("*") if p.is_file()}
        rc = cr.main(self.args("relink", "--src-claude-dir", str(src_claude), "--from", self.fx.old_root, "--to", self.fx.new_root, "--apply"))
        self.assertEqual(rc, 0)
        new_dir = self.fx.projects / cr.slugify(self.fx.new_root)
        self.assertTrue((new_dir / "11111111-1111-4111-8111-111111111111.jsonl").exists())
        # source untouched
        after_src = {str(p): p.stat().st_mtime for p in src_claude.rglob("*") if p.is_file()}
        self.assertEqual(before_src, after_src)
        self.assertTrue((src_claude / "projects" / cr.slugify(self.fx.old_root)).exists())
        # history appended (deduped against existing), config merged from the source config
        hist = [json.loads(l) for l in (self.fx.claude_dir / "history.jsonl").read_text().splitlines() if l]
        self.assertEqual(sum(1 for h in hist if h["project"] == self.fx.new_root), 3)
        self.assertEqual(len(hist), 5)
        cfg = json.loads(self.fx.config.read_text())
        self.assertIn("Bash(npm test)", cfg["projects"][self.fx.new_root]["allowedTools"])
        self.assertIn(self.fx.old_root, cfg["projects"])  # live config's own old entry is not touched on import (source is the rescue config)

    # ---- guards and dedupe ------------------------------------------------ #
    def test_refuses_when_claude_running(self):
        (self.fx.claude_dir / "sessions" / "self.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "x", "cwd": "/"}))
        real = cr.running_claude_sessions
        cr.running_claude_sessions = lambda store: [{"pid": os.getpid(), "cwd": "/", "sessionId": "x"}]
        try:
            rc = cr.main(self.args("relink", "--from", "projB", "--to", self.fx.other_new, "--apply"))
            self.assertEqual(rc, 3)
            self.assertTrue((self.fx.projects / cr.slugify(self.fx.other_old)).exists())
            rc = cr.main(self.args("relink", "--from", "projB", "--to", self.fx.other_new, "--apply", "--force"))
            self.assertEqual(rc, 0)
        finally:
            cr.running_claude_sessions = real

    def test_dedupe_config(self):
        rc = cr.main(self.args("dedupe"))
        self.assertEqual(rc, 0)
        cfg = json.loads(self.fx.config.read_text())
        self.assertEqual(len([k for k in cfg["projects"] if k.lower().startswith("d:")]), 3)
        rc = cr.main(self.args("dedupe", "--apply"))
        self.assertEqual(rc, 0)
        cfg = json.loads(self.fx.config.read_text())
        wins = [k for k in cfg["projects"] if k.lower().startswith("d:")]
        self.assertEqual(len(wins), 1)
        self.assertEqual(set(cfg["projects"][wins[0]]["allowedTools"]), {"Read", "Edit", "Bash(dir)"})
        self.assertTrue(cfg["projects"][wins[0]]["hasTrustDialogAccepted"])

    def test_ambiguous_from_is_rejected(self):
        with self.assertRaises(SystemExit):
            cr.main(self.args("relink", "--from", "proj", "--to", self.fx.new_root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
