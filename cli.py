#!/usr/bin/env python3
"""Viper CLI — the user-facing front door.

Subcommands:
    init                  Bootstrap a project: create .viper/ artifacts,
                          detect the test framework, gitignore the directory,
                          and print the settings.json snippet you need to
                          register the hook.

    status                Health check + recent activity for the current
                          project. Reports which hooks are registered, which
                          .viper/ artifacts exist, and what's been happening
                          in review.jsonl recently.

    review                Dry-run a Codex code review against the current
                          uncommitted git diff WITHOUT going through a Claude
                          session. Honors .viper/rules.md, brief.md, and
                          test_command. Read-only — does not modify any
                          session state. Exit codes: 0 APPROVED, 1 ISSUES
                          FOUND, 2 Codex unavailable / failed.

    enable-plan-review    Flip plan_review_enabled to true in config.json
                          and print the PreToolUse:ExitPlanMode snippet to
                          paste into ~/.claude/settings.json. Idempotent.

    stats                 Alias for `python stats.py` (the existing review-
                          stats command).

Standalone — no third-party dependencies. Designed to be invocable directly:

    python ~/.claude/hooks/viper/cli.py init
    python ~/.claude/hooks/viper/cli.py status
    python ~/.claude/hooks/viper/cli.py review
    python ~/.claude/hooks/viper/cli.py enable-plan-review

The hooks themselves (viper.py, plan_review.py) are still invoked by Claude
Code — this CLI is for the human.
"""

import json
import os
import sys
from pathlib import Path

# Force UTF-8 output on Windows. Use reconfigure() to avoid the destructor
# bug that closes the underlying buffer when the original wrapper is replaced.
if os.name == 'nt':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

# Make sibling viper.py importable so cmd_review can reuse the same review
# pipeline the Stop hook uses.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

OK = "[OK]"
SKIP = "[--]"
WARN = "[!!]"
FAIL = "[XX]"


def _hook_dir():
    """Path to the directory containing this script (the viper hook dir)."""
    return Path(__file__).resolve().parent


def _settings_path():
    """Path to the user's Claude Code settings.json (best-effort)."""
    home = Path.home()
    return home / '.claude' / 'settings.json'


# ---------------------------------------------------------------------------
# init — bootstrap a project
# ---------------------------------------------------------------------------

# Test framework detection. First match wins. Each entry is:
#   (display_name, marker_check_fn, command_string)
# marker_check_fn takes the project Path and returns True if this framework applies.

def _has_pytest(project: Path) -> bool:
    if (project / 'pytest.ini').exists():
        return True
    if (project / 'pyproject.toml').exists():
        try:
            content = (project / 'pyproject.toml').read_text(encoding='utf-8', errors='replace')
            if '[tool.pytest' in content or '"pytest"' in content or "'pytest'" in content:
                return True
        except OSError:
            pass
    if (project / 'setup.cfg').exists():
        try:
            content = (project / 'setup.cfg').read_text(encoding='utf-8', errors='replace')
            if '[tool:pytest]' in content:
                return True
        except OSError:
            pass
    if (project / 'tests').is_dir() or (project / 'test').is_dir():
        # Heuristic: any test_*.py file in tests/?
        for d in ('tests', 'test'):
            test_dir = project / d
            if test_dir.is_dir():
                for f in test_dir.iterdir():
                    if f.name.startswith('test_') and f.name.endswith('.py'):
                        return True
    return False


def _has_npm_test(project: Path) -> bool:
    pkg = project / 'package.json'
    if not pkg.exists():
        return False
    try:
        data = json.loads(pkg.read_text(encoding='utf-8', errors='replace'))
        return isinstance(data.get('scripts'), dict) and 'test' in data['scripts']
    except (OSError, json.JSONDecodeError):
        return False


def _has_cargo(project: Path) -> bool:
    return (project / 'Cargo.toml').exists()


def _has_go(project: Path) -> bool:
    return (project / 'go.mod').exists()


def _has_unittest_layout(project: Path) -> bool:
    """Last-resort: a Python project with test_*.py files but no pytest config."""
    for f in project.rglob('test_*.py'):
        # Don't recurse forever — bail after first match
        return True
    return False


def detect_test_command(project: Path):
    """Best-effort test command detection. Returns (label, command) or (None, None)."""
    if _has_pytest(project):
        return ('pytest', 'python -m pytest -x')
    if _has_npm_test(project):
        return ('npm', 'npm test')
    if _has_cargo(project):
        return ('cargo', 'cargo test')
    if _has_go(project):
        return ('go', 'go test ./...')
    if _has_unittest_layout(project):
        return ('unittest', 'python -m unittest discover')
    return (None, None)


RULES_TEMPLATE = """\
# Viper review rules — edit this to match your codebase

# Rules are prepended to every Codex review prompt. They're authoritative —
# the reviewer honors them even when they contradict its defaults. Use this
# file to tell the reviewer what THIS codebase cares about.

## Examples (delete and replace with your own)

# - This project uses loose typing on purpose. Don't flag `any` or untyped
#   function parameters.
# - We handle money. Be paranoid about rounding, precision, and currency
#   conversion.
# - Templates in `views/` are trusted. Don't flag XSS findings there.
# - Ignore findings about missing docstrings or comments.
# - This is an internal CLI tool, not a public API. Don't flag missing input
#   validation on commands the user runs themselves.

## Your rules:

# (write your project-specific guidance here)
"""

TEST_COMMAND_TEMPLATE_NONE = """\
# Viper test command — one shell command on the first non-empty line.
# Lines starting with # are comments and ignored.
#
# Viper runs this before every code review and feeds the output to the
# reviewer as additional context. Test failures DO NOT auto-block — the
# reviewer weighs them against the code and decides. If you don't want a
# test command, just delete this file.
#
# Examples:
#   pytest -x
#   npm test
#   cargo test
#   go test ./...
#
# (no test framework detected — add your test command below)
"""


def _atomic_write(path: Path, content: str):
    """Write a file only if it doesn't already exist (idempotent init)."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return True


def _ensure_gitignore_entry(project: Path, entry: str):
    """Append `entry` to .gitignore if not already present. Creates the file
    if needed. Returns one of: 'created', 'added', 'already_present'.
    """
    gi = project / '.gitignore'
    if not gi.exists():
        gi.write_text(entry + '\n', encoding='utf-8')
        return 'created'
    try:
        existing = gi.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return 'already_present'  # don't crash; assume it's there
    # Match exact line (with optional trailing slash) — don't match substrings
    lines = [l.strip() for l in existing.splitlines()]
    if entry in lines or entry.rstrip('/') in lines or (entry + '/') in lines:
        return 'already_present'
    # Append on a new line, ensuring previous content ends with newline
    sep = '' if existing.endswith('\n') else '\n'
    with gi.open('a', encoding='utf-8') as f:
        f.write(f"{sep}{entry}\n")
    return 'added'


def _is_hook_registered(settings: dict, event: str, command_substring: str) -> bool:
    """Check if a hook for the given event is registered with a command
    containing the given substring."""
    hooks = settings.get('hooks') or {}
    event_hooks = hooks.get(event) or []
    for entry in event_hooks:
        for h in (entry.get('hooks') or []):
            if command_substring in (h.get('command') or ''):
                return True
    return False


def cmd_init(project_arg=None):
    """Bootstrap a project with Viper artifacts."""
    project = Path(project_arg).resolve() if project_arg else Path.cwd().resolve()
    if not project.is_dir():
        print(f"{FAIL} Not a directory: {project}", file=sys.stderr)
        return 1

    print(f"[viper init] in {project}")
    print()

    viper_dir = project / '.viper'

    # 1. Create .viper/ directory
    if viper_dir.exists():
        print(f"{SKIP} .viper/ already exists")
    else:
        viper_dir.mkdir(parents=True)
        print(f"{OK} Created .viper/")

    # 2. .viper/rules.md (template)
    rules_path = viper_dir / 'rules.md'
    if _atomic_write(rules_path, RULES_TEMPLATE):
        print(f"{OK} Created .viper/rules.md (edit to match your codebase)")
    else:
        print(f"{SKIP} .viper/rules.md already exists")

    # 3. .viper/test_command (auto-detected or empty template)
    test_path = viper_dir / 'test_command'
    if test_path.exists():
        print(f"{SKIP} .viper/test_command already exists")
    else:
        label, cmd = detect_test_command(project)
        if cmd:
            content = f"# Auto-detected: {label}\n{cmd}\n"
            test_path.write_text(content, encoding='utf-8')
            print(f"{OK} Detected test framework: {label}")
            print(f"{OK} Created .viper/test_command ({cmd})")
        else:
            test_path.write_text(TEST_COMMAND_TEMPLATE_NONE, encoding='utf-8')
            print(f"{WARN} No test framework detected")
            print(f"{OK} Created .viper/test_command (empty — add your command)")

    # 4. .gitignore
    gi_status = _ensure_gitignore_entry(project, '.viper/')
    if gi_status == 'created':
        print(f"{OK} Created .gitignore with .viper/")
    elif gi_status == 'added':
        print(f"{OK} Added .viper/ to .gitignore")
    else:
        print(f"{SKIP} .gitignore already excludes .viper/")

    # 5. Tell the user how to register the hook (do NOT auto-edit settings.json)
    print()
    settings = _settings_path()
    hook_path = (_hook_dir() / 'viper.py').as_posix()
    plan_hook_path = (_hook_dir() / 'plan_review.py').as_posix()

    already_stop = False
    already_plan = False
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding='utf-8', errors='replace'))
            already_stop = _is_hook_registered(data, 'Stop', 'viper.py')
            already_plan = _is_hook_registered(data, 'PreToolUse', 'plan_review.py')
        except (OSError, json.JSONDecodeError):
            pass

    if already_stop:
        print(f"{OK} Stop hook already registered in {settings}")
    else:
        print(f"{WARN} Stop hook NOT registered. Add this to {settings}:")
        print()
        print('  {')
        print('    "hooks": {')
        print('      "Stop": [')
        print('        {')
        print('          "hooks": [')
        print(f'            {{ "type": "command", "command": "python {hook_path}" }}')
        print('          ]')
        print('        }')
        print('      ]')
        print('    }')
        print('  }')
        print()

    if already_plan:
        print(f"{OK} Plan-review hook already registered")
    else:
        print(f"{SKIP} Plan-review hook NOT registered (optional)")
        print(f"     To enable: set plan_review_enabled=true in config.json AND add a")
        print(f"     PreToolUse hook with matcher 'ExitPlanMode' pointing at:")
        print(f"       python {plan_hook_path}")

    print()
    print(f"Done. Run `python {(_hook_dir() / 'cli.py').as_posix()} status` to verify.")
    return 0


# ---------------------------------------------------------------------------
# status — health check + recent activity
# ---------------------------------------------------------------------------

def _format_relative_time(timestamp_iso):
    """Convert an ISO timestamp to a short human-readable relative time."""
    from datetime import datetime
    try:
        ts = datetime.fromisoformat(timestamp_iso)
    except (ValueError, TypeError):
        return "?"
    delta = datetime.now() - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return "in the future"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _load_jsonl_entries(path: Path):
    """Best-effort JSONL loader. Same shape as stats.py's loader."""
    entries = []
    if not path.exists():
        return entries
    try:
        with path.open(encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries


def cmd_status(project_arg=None):
    """Health check + recent activity for the current project."""
    from collections import Counter, defaultdict
    from datetime import datetime, timedelta

    project = Path(project_arg).resolve() if project_arg else Path.cwd().resolve()
    if not project.is_dir():
        print(f"{FAIL} Not a directory: {project}", file=sys.stderr)
        return 1

    print(f"Viper Status — {project}")
    print("=" * 60)
    print()

    # --- Hook registration ---
    print("Hooks (in ~/.claude/settings.json):")
    settings = _settings_path()
    if not settings.exists():
        print(f"  {WARN} {settings} does not exist — no hooks registered anywhere")
    else:
        try:
            data = json.loads(settings.read_text(encoding='utf-8', errors='replace'))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  {FAIL} Could not parse {settings}: {e}")
            data = {}
        stop = _is_hook_registered(data, 'Stop', 'viper.py')
        plan = _is_hook_registered(data, 'PreToolUse', 'plan_review.py')
        print(f"  {OK if stop else WARN} Stop hook (viper.py)")
        print(f"  {OK if plan else SKIP} PreToolUse:ExitPlanMode hook (plan_review.py) [optional]")
    print()

    # --- Project files ---
    print(f"Project artifacts (.viper/ in {project.name}):")
    viper_dir = project / '.viper'
    artifacts = [
        ('rules.md',          'human guidance for the reviewer'),
        ('test_command',      'shell command to run before each review'),
        ('brief.md',          "Claude's per-session brief (auto-managed)"),
        ('state.json',        'cycle tracker (auto-managed)'),
        ('last_findings.md',  'previous-cycle findings (auto-managed)'),
        ('last_approved_plan.md', 'approved plan for drift check (auto-managed)'),
        ('review.log',        'human-readable review history'),
        ('review.jsonl',      'structured review log (for stats)'),
        ('plan_review.log',   'plan-review history (only with plan review enabled)'),
    ]
    if not viper_dir.exists():
        print(f"  {WARN} .viper/ does not exist. Run `viper init` to bootstrap.")
    else:
        for name, desc in artifacts:
            p = viper_dir / name
            if p.exists():
                try:
                    mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec='seconds')
                    age = _format_relative_time(mtime)
                except OSError:
                    age = "?"
                print(f"  {OK} {name:24} {age:>10}  ({desc})")
            else:
                print(f"  {SKIP} {name:24} {'—':>10}  ({desc})")
    print()

    # --- .gitignore ---
    gi = project / '.gitignore'
    if not gi.exists():
        print(f"{WARN} No .gitignore — Viper artifacts will be tracked by git!")
    else:
        try:
            content = gi.read_text(encoding='utf-8', errors='replace')
            ignored = '.viper/' in content or '.viper' in content.split()
            if ignored:
                print(f"{OK} .viper/ is in .gitignore")
            else:
                print(f"{WARN} .viper/ is NOT in .gitignore — review log may leak into git")
        except OSError:
            pass
    print()

    # --- Recent activity ---
    entries = _load_jsonl_entries(viper_dir / 'review.jsonl')
    if not entries:
        print("Recent activity: no review.jsonl yet (run a Claude session to populate)")
        return 0

    # Filter to last 7 days for the headline numbers
    cutoff = (datetime.now() - timedelta(days=7)).isoformat(timespec='seconds')
    recent_week = [e for e in entries if (e.get('timestamp') or '') >= cutoff]

    print(f"Recent activity (last 7 days, {len(recent_week)} reviews):")
    if recent_week:
        verdict_counts = Counter(e.get('verdict', '?') for e in recent_week)
        sessions_week = defaultdict(list)
        for e in recent_week:
            sid = e.get('session_id')
            if sid:
                sessions_week[sid].append(e)

        approved_sessions = sum(
            1 for entries_for_sess in sessions_week.values()
            if any(e.get('verdict') == 'APPROVED' for e in entries_for_sess)
        )
        approval_pct = (100 * approved_sessions // len(sessions_week)) if sessions_week else 0

        cycles_to_approval = []
        for sess_entries in sessions_week.values():
            approved_cycles = [
                e.get('cycle') for e in sess_entries
                if e.get('verdict') == 'APPROVED' and e.get('cycle') is not None
            ]
            if approved_cycles:
                cycles_to_approval.append(min(approved_cycles))

        print(f"  Sessions:           {len(sessions_week)}")
        print(f"  Reached APPROVED:   {approved_sessions} ({approval_pct}%)")
        if cycles_to_approval:
            avg = sum(cycles_to_approval) / len(cycles_to_approval)
            print(f"  Avg cycles to APPROVED: {avg:.1f}  (max: {max(cycles_to_approval)})")
        for v in ('APPROVED', 'ISSUES FOUND', 'NO RESPONSE'):
            count = verdict_counts.get(v, 0)
            if count:
                print(f"    {v:<14}  {count}")

        # Most-flagged files this week
        file_flags = Counter()
        for e in recent_week:
            if e.get('verdict') == 'ISSUES FOUND':
                for f in e.get('files') or []:
                    file_flags[f] += 1
        if file_flags:
            print()
            print("  Most-flagged files this week:")
            for fname, count in file_flags.most_common(5):
                print(f"    {fname:<50}  {count} flag{'s' if count != 1 else ''}")
    print()

    # --- Most recent review (always shown) ---
    last = entries[-1]
    last_ts = last.get('timestamp', '?')
    last_verdict = last.get('verdict', '?')
    last_cycle = last.get('cycle', '?')
    print(f"Most recent review: {_format_relative_time(last_ts)} — {last_verdict} (cycle {last_cycle})")
    return 0


# ---------------------------------------------------------------------------
# enable-plan-review — flip the config flag and print the hook snippet
# ---------------------------------------------------------------------------

def cmd_enable_plan_review():
    """Enable the plan-review hook end-to-end (well, as end-to-end as we
    can be without auto-editing the user's settings.json).

    1. Sets `plan_review_enabled: true` in our own config.json (we own
       that file, so editing it is safe and idempotent).
    2. Checks whether `~/.claude/settings.json` already has a
       PreToolUse hook with matcher 'ExitPlanMode' pointing at
       plan_review.py. If yes, confirms. If no, prints the exact
       snippet to paste.

    The CLI never modifies settings.json itself — that file is too
    important to risk corrupting.
    """
    config_path = _hook_dir() / 'config.json'
    if not config_path.exists():
        print(f"{FAIL} {config_path} not found", file=sys.stderr)
        return 1

    try:
        config = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        print(f"{FAIL} Could not read {config_path}: {e}", file=sys.stderr)
        return 1

    was_enabled = bool(config.get('plan_review_enabled'))
    if was_enabled:
        print(f"{SKIP} plan_review_enabled is already true in {config_path}")
    else:
        config['plan_review_enabled'] = True
        try:
            config_path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
        except OSError as e:
            print(f"{FAIL} Could not write {config_path}: {e}", file=sys.stderr)
            return 1
        print(f"{OK} Set plan_review_enabled=true in {config_path}")

    # Check whether the PreToolUse hook is already registered
    settings = _settings_path()
    hook_registered = False
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding='utf-8', errors='replace'))
            hook_registered = _is_hook_registered(data, 'PreToolUse', 'plan_review.py')
        except (OSError, json.JSONDecodeError):
            pass

    print()
    if hook_registered:
        print(f"{OK} PreToolUse:ExitPlanMode hook already registered in {settings}")
        print(f"{OK} Plan review is fully enabled.")
        print(f"     Test it: enter plan mode in any project (Claude Code -> Shift+Tab),")
        print(f"     submit a plan, and watch Codex review it before Claude exits plan mode.")
        return 0

    plan_hook_path = (_hook_dir() / 'plan_review.py').as_posix()
    print(f"{WARN} PreToolUse:ExitPlanMode hook is NOT yet registered.")
    print(f"     Add this block to {settings}:")
    print()
    print('  {')
    print('    "hooks": {')
    print('      "PreToolUse": [')
    print('        {')
    print('          "matcher": "ExitPlanMode",')
    print('          "hooks": [')
    print(f'            {{ "type": "command", "command": "python {plan_hook_path}" }}')
    print('          ]')
    print('        }')
    print('      ]')
    print('    }')
    print('  }')
    print()
    print("If you already have a `hooks` block, merge the PreToolUse entry in.")
    print("Then enter plan mode in any project to test it.")
    return 0


# ---------------------------------------------------------------------------
# review — dry-run a Codex review against the current uncommitted changes
# ---------------------------------------------------------------------------

def cmd_review(project_arg=None):
    """Run a Codex code review against the current uncommitted git diff
    WITHOUT going through a Claude session.

    Reuses the same review pipeline the Stop hook uses, with one important
    difference: it uses a synthetic session_id so it does NOT pick up stale
    findings or approved plans from a real Claude session, and it does NOT
    modify state.json, review.log, review.jsonl, last_findings.md, or
    last_approved_plan.md. Pure read-only invocation of the engine.

    Honors existing per-project artifacts (.viper/rules.md, .viper/brief.md,
    .viper/test_command) — those are project state, not session state.

    Exit codes:
        0  APPROVED (or nothing to review)
        1  ISSUES FOUND
        2  Codex unavailable, timed out, or otherwise failed
    """
    import shutil
    from datetime import datetime

    project = Path(project_arg).resolve() if project_arg else Path.cwd().resolve()
    if not project.is_dir():
        print(f"{FAIL} Not a directory: {project}", file=sys.stderr)
        return 2

    # Codex must be installed
    if not shutil.which('codex'):
        print(f"{FAIL} Codex CLI not found on PATH.", file=sys.stderr)
        print("       Install with:  npm install -g @openai/codex", file=sys.stderr)
        print("       Then:          codex login", file=sys.stderr)
        return 2

    # Import viper as a sibling module. The sys.path insert is at the top
    # of cli.py so this is safe.
    try:
        import viper
    except ImportError as e:
        print(f"{FAIL} Could not import viper.py: {e}", file=sys.stderr)
        return 2

    print(f"Viper Review (dry run) — {project}")
    print("=" * 60)

    # Snapshot the .viper/ files we promise NOT to modify, so we can verify
    # at the end. This is a sanity check, not a security boundary.
    viper_dir = project / '.viper'
    snapshot = {}
    for name in ('state.json', 'review.log', 'review.jsonl',
                 'last_findings.md', 'last_approved_plan.md'):
        p = viper_dir / name
        if p.exists():
            try:
                snapshot[name] = (p.stat().st_mtime, p.stat().st_size)
            except OSError:
                pass

    # What's changed?
    changed = viper.get_changed_files(str(project))
    if not changed:
        print()
        print("Nothing to review — no changed files (or not a git repo).")
        print("(If you expect changes, check `git status` and `git diff HEAD`.)")
        return 0

    print(f"Changed files ({len(changed)}):")
    for f in changed:
        print(f"  - {f}")
    print()
    print("Calling Codex... this typically takes 30-180 seconds.")
    print()

    # Use a synthetic session_id so the cycle-aware loaders return "" and
    # we don't pick up findings or plans from a real Claude session.
    synthetic_session = f"cli-review-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    config = viper.load_config()
    review = viper.run_review(str(project), changed, config, session_id=synthetic_session)

    if not review:
        print(f"{FAIL} Codex did not return a review (timeout, rate limit, or crash).",
              file=sys.stderr)
        print("      Try `codex --version` and `codex login` to verify your setup.",
              file=sys.stderr)
        return 2

    print(review)
    print()
    print("=" * 60)

    # Verify we didn't accidentally modify any session state
    for name, (orig_mtime, orig_size) in snapshot.items():
        p = viper_dir / name
        if p.exists():
            try:
                cur = p.stat()
                if cur.st_mtime != orig_mtime or cur.st_size != orig_size:
                    print(f"{WARN} {name} was modified during dry-run review (unexpected)",
                          file=sys.stderr)
            except OSError:
                pass

    if viper.is_approved(review):
        print(f"{OK} APPROVED")
        return 0
    print(f"{FAIL} ISSUES FOUND")
    return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def usage():
    print(__doc__)


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ('-h', '--help', 'help'):
        usage()
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == 'init':
        return cmd_init(rest[0] if rest else None)
    if cmd == 'status':
        return cmd_status(rest[0] if rest else None)
    if cmd == 'review':
        return cmd_review(rest[0] if rest else None)
    if cmd in ('enable-plan-review', 'enable_plan_review'):
        return cmd_enable_plan_review()
    if cmd == 'stats':
        # Delegate to stats.py
        import subprocess
        stats = _hook_dir() / 'stats.py'
        return subprocess.run([sys.executable, str(stats)] + rest).returncode
    print(f"Unknown command: {cmd}", file=sys.stderr)
    print()
    usage()
    return 2


if __name__ == '__main__':
    sys.exit(main())
