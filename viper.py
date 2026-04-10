#!/usr/bin/env python3
"""Viper v2 Stop Hook: Codex reviews Claude's work before it can finish."""

import sys
import os
import json
import subprocess
import shutil

# Force UTF-8 output on Windows to avoid cp1252 encoding errors.
# Use reconfigure() (Python 3.7+) instead of replacing sys.stdout — replacing
# it leaks a TextIOWrapper whose destructor closes the underlying buffer,
# which breaks any caller that imports this module.
if os.name == 'nt':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass  # older Python or stdout already detached — best effort


def _ensure_npm_path():
    """Ensure npm global bin is on PATH for subprocess calls."""
    if os.name == 'nt':
        npm_bin = os.path.join(os.environ.get('APPDATA', ''), 'npm')
        if npm_bin and npm_bin not in os.environ.get('PATH', ''):
            os.environ['PATH'] = npm_bin + os.pathsep + os.environ.get('PATH', '')

_ensure_npm_path()


def _find_cmd(name):
    """Resolve a command to its full path, or return the name as-is."""
    path = shutil.which(name)
    return path if path else name


def load_config():
    """Load config.json from the same directory as this script."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(config_path):
        with open(config_path, encoding='utf-8') as f:
            return json.load(f)
    return {}


# File patterns Viper should NEVER ask the reviewer to read.
# These are binary, generated, or vendor files that aren't useful in a review
# and only burn reviewer attention. Path-prefix and suffix matching only —
# no globs, no regex, kept deliberately simple.
_REVIEW_EXCLUDE_PREFIXES = (
    '.viper/',           # Viper's own state files
    '__pycache__/',      # Python bytecode
    'node_modules/',     # JS deps
    'target/',           # Rust/Java build output
    'build/',            # generic build output
    'dist/',             # JS/Python dist
    '.next/',            # Next.js build
    '.nuxt/',            # Nuxt build
    '.venv/', 'venv/',   # Python virtualenvs
    '.tox/',             # Python tox
    '.pytest_cache/',    # pytest cache
    '.mypy_cache/',      # mypy cache
    '.ruff_cache/',      # ruff cache
    'vendor/',           # Go/PHP vendored deps
    '.git/',             # git internals (defensive — git diff shouldn't return these)
)
_REVIEW_EXCLUDE_SUFFIXES = (
    # Python
    '.pyc', '.pyo', '.pyd',
    # Native binaries / shared libs
    '.so', '.dll', '.dylib', '.exe', '.o', '.a', '.lib',
    # JVM
    '.class', '.jar', '.war',
    # Images
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.svg',
    '.bmp', '.tiff', '.tif',
    # Audio/video
    '.mp3', '.mp4', '.wav', '.ogg', '.webm', '.mov', '.avi',
    # Archives
    '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar',
    # Fonts
    '.ttf', '.otf', '.woff', '.woff2', '.eot',
    # Documents (binary)
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    # OS junk
    '.DS_Store',
    # Lockfiles — high-noise, low-signal for code review
    '.lock',
)


def _should_exclude_from_review(path):
    """Return True if a file path should be filtered out of the review list.

    Filters binary, generated, and vendor files that the reviewer can't
    meaningfully read. Pure pattern matching — no filesystem calls.
    """
    p = path.replace('\\', '/')  # normalize Windows separators
    if any(p.startswith(prefix) or f'/{prefix}' in p for prefix in _REVIEW_EXCLUDE_PREFIXES):
        return True
    lower = p.lower()
    if any(lower.endswith(suffix) for suffix in _REVIEW_EXCLUDE_SUFFIXES):
        return True
    # Common lockfile names that don't have a .lock extension
    base = lower.rsplit('/', 1)[-1]
    if base in ('package-lock.json', 'yarn.lock', 'pnpm-lock.yaml',
                'poetry.lock', 'pipfile.lock', 'cargo.lock',
                'composer.lock', 'gemfile.lock'):
        return True
    return False


def get_changed_files(cwd):
    """Get list of changed files (staged, unstaged, and untracked).

    Filters out binary, generated, and vendor files that aren't useful in a
    code review (see _REVIEW_EXCLUDE_PREFIXES / _SUFFIXES). The filtering is
    intentionally pattern-based and conservative: real source files in
    languages we don't recognize will pass through.
    """
    changed = set()

    try:
        # Check if we're in a git repo
        result = subprocess.run(
            ['git', 'rev-parse', '--is-inside-work-tree'],
            capture_output=True, text=True, timeout=10, cwd=cwd
        )
        if result.returncode != 0:
            return []

        # Staged + unstaged changes vs HEAD
        result = subprocess.run(
            ['git', 'diff', '--name-only', 'HEAD'],
            capture_output=True, text=True, timeout=10, cwd=cwd
        )
        if result.returncode == 0:
            for f in result.stdout.strip().split('\n'):
                if f.strip():
                    changed.add(f.strip())

        # Untracked files
        result = subprocess.run(
            ['git', 'ls-files', '--others', '--exclude-standard'],
            capture_output=True, text=True, timeout=10, cwd=cwd
        )
        if result.returncode == 0:
            for f in result.stdout.strip().split('\n'):
                if f.strip():
                    changed.add(f.strip())

    except Exception:
        return []

    # Filter out generated/binary/vendor files and Viper's own state.
    changed = {f for f in changed if not _should_exclude_from_review(f)}
    return sorted(changed)


def load_state(cwd, session_id):
    """Load cycle state for this session."""
    state_path = os.path.join(cwd, '.viper', 'state.json')
    if os.path.exists(state_path):
        try:
            with open(state_path, encoding='utf-8') as f:
                state = json.load(f)
            if state.get("session_id") == session_id:
                return state
        except Exception:
            pass
    return {"session_id": session_id, "cycle": 0, "approved": False}


def save_state(cwd, state):
    """Save cycle state."""
    viper_dir = os.path.join(cwd, '.viper')
    os.makedirs(viper_dir, exist_ok=True)
    state_path = os.path.join(viper_dir, 'state.json')
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


def cleanup_state(cwd):
    """Remove session state, brief, last-cycle findings, and last approved
    plan, but preserve the review log so it accumulates across sessions."""
    viper_dir = os.path.join(cwd, '.viper')
    for fname in ('state.json', 'brief.md', 'last_findings.md', 'last_approved_plan.md'):
        fpath = os.path.join(viper_dir, fname)
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
            except OSError:
                pass


def load_last_findings(cwd, session_id):
    """Load the previous cycle's findings for the same session, if any.

    Returns the review text from the prior cycle, or "" if there's nothing
    relevant. Stale findings from a previous session are ignored — the
    file's session_id marker must match the current session_id.

    File format (.viper/last_findings.md):
        session_id: <uuid>
        cycle: <n>
        ---
        <review text from that cycle>
    """
    path = os.path.join(cwd, '.viper', 'last_findings.md')
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding='utf-8') as f:
            content = f.read()
    except (OSError, UnicodeError):
        return ""

    # Parse the simple header
    lines = content.split('\n')
    saved_session_id = None
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('session_id:'):
            saved_session_id = stripped.split(':', 1)[1].strip()
        elif stripped == '---':
            body_start = i + 1
            break

    if saved_session_id != session_id:
        return ""  # different session — discard stale findings

    body = '\n'.join(lines[body_start:]).strip()
    return body


def save_last_findings(cwd, session_id, cycle, review_text):
    """Save this cycle's findings so the next cycle can verify they were fixed."""
    if not review_text:
        return
    viper_dir = os.path.join(cwd, '.viper')
    try:
        os.makedirs(viper_dir, exist_ok=True)
        path = os.path.join(viper_dir, 'last_findings.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"session_id: {session_id}\n")
            f.write(f"cycle: {cycle}\n")
            f.write("---\n")
            f.write(review_text)
    except OSError:
        pass  # logging must never block the hook


def clear_last_findings(cwd):
    """Remove the last-findings file (e.g. after an APPROVED verdict)."""
    path = os.path.join(cwd, '.viper', 'last_findings.md')
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def load_last_approved_plan(cwd, session_id):
    """Load the most recently approved plan for the current session, if any.

    Written by plan_review.py when a plan is approved. The Stop hook reads it
    so the code reviewer can verify the implementation actually matches what
    was promised at planning time.

    File format (.viper/last_approved_plan.md):
        session_id: <uuid>
        ---
        <plan text>

    Returns "" if there's no file, the session_id doesn't match, or the file
    is unreadable. Stale plans from a previous session are ignored.
    """
    path = os.path.join(cwd, '.viper', 'last_approved_plan.md')
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding='utf-8') as f:
            content = f.read()
    except (OSError, UnicodeError):
        return ""

    lines = content.split('\n')
    saved_session_id = None
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('session_id:'):
            saved_session_id = stripped.split(':', 1)[1].strip()
        elif stripped == '---':
            body_start = i + 1
            break

    if saved_session_id != session_id:
        return ""  # different session — discard stale plan

    return '\n'.join(lines[body_start:]).strip()


def clear_last_approved_plan(cwd):
    """Remove the last-approved-plan file (e.g. after an APPROVED verdict)."""
    path = os.path.join(cwd, '.viper', 'last_approved_plan.md')
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def is_approved(text):
    """Check if review text indicates approval.

    Prefers an explicit 'VERDICT: APPROVED' / 'VERDICT: ISSUES FOUND' token
    on its own line. Falls back to substring detection for older reviewer
    output: approves only if an APPROVED token is present AND no negative
    token is. Ambiguous or unknown text fails closed.
    """
    if not text:
        return True  # empty/None handled by caller; defensive default

    # Preferred: explicit verdict line
    for line in text.splitlines():
        stripped = line.strip().upper().rstrip('.')
        if stripped in ("VERDICT: APPROVED", "VERDICT:APPROVED"):
            return True
        if stripped in ("VERDICT: ISSUES FOUND", "VERDICT:ISSUES FOUND", "VERDICT: NOT APPROVED"):
            return False

    # Fallback: no explicit verdict line. Only approve if the approval
    # token is present AND no negative token is. Ambiguity => fail closed.
    upper = text.upper()
    has_issues = ("ISSUES FOUND" in upper) or ("NOT APPROVED" in upper)
    # Match "APPROVED" that isn't part of "NOT APPROVED".
    has_approved = False
    idx = upper.find("APPROVED")
    while idx != -1:
        if not upper[max(0, idx - 4):idx].endswith("NOT "):
            has_approved = True
            break
        idx = upper.find("APPROVED", idx + 1)

    if has_issues:
        return False  # any sign of issues => not approved
    return has_approved  # approved only if token present, else fail closed


def _log_review(cwd, review, changed_files, session_id="unknown", cycle=0):
    """Log every review result to .viper/review.log (human-readable) and
    .viper/review.jsonl (structured, for stats tooling). Fail-safe — never crashes.
    """
    try:
        from datetime import datetime
        viper_dir = os.path.join(cwd, '.viper')
        os.makedirs(viper_dir, exist_ok=True)
        now = datetime.now()
        timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
        verdict = "NO RESPONSE" if not review else ("APPROVED" if is_approved(review) else "ISSUES FOUND")

        # Human-readable log (unchanged format — stable for anyone tailing it)
        log_path = os.path.join(viper_dir, 'review.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[{timestamp}] Verdict: {verdict}\n")
            f.write(f"Files reviewed: {', '.join(changed_files)}\n")
            f.write(f"{'='*60}\n")
            f.write(f"{review or '(no response from reviewer)'}\n")

        # Structured log for stats tooling
        jsonl_path = os.path.join(viper_dir, 'review.jsonl')
        entry = {
            "timestamp": now.isoformat(timespec='seconds'),
            "session_id": session_id,
            "cycle": cycle,
            "verdict": verdict,
            "files": list(changed_files),
        }
        with open(jsonl_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception:
        pass  # Logging must never block the hook


def load_brief(cwd):
    """Load the review brief written by Claude, if it exists."""
    brief_path = os.path.join(cwd, '.viper', 'brief.md')
    if os.path.exists(brief_path):
        try:
            with open(brief_path, encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def load_rules(cwd):
    """Load per-project reviewer rules from .viper/rules.md, if present.

    Rules are human-curated guidance about what this specific codebase
    cares about (or doesn't). Prepended to the review prompt so the
    reviewer can tailor its findings. Never auto-generated — manual only,
    to avoid silencing real bugs through inferred patterns.
    """
    rules_path = os.path.join(cwd, '.viper', 'rules.md')
    if os.path.exists(rules_path):
        try:
            with open(rules_path, encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def load_test_command(cwd):
    """Load the per-project test command from .viper/test_command, if present.

    File format: a single shell command on the first non-empty line.
    Lines starting with '#' are treated as comments and ignored.
    Returns the command string, or "" if the file is missing/empty/unreadable.
    """
    path = os.path.join(cwd, '.viper', 'test_command')
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    return line
    except Exception:
        pass
    return ""


def run_tests(cwd, test_cmd, timeout):
    """Run the project's test command and capture its output.

    Returns a dict: {ran, exit_code, output, error} where:
      - ran: True if the command was launched (even if it failed)
      - exit_code: integer exit code, or None if timed out / not launched
      - output: combined stdout+stderr, truncated to the last ~4000 chars
      - error: short description if the command couldn't run at all

    Test failures never block Claude — this is context for the reviewer,
    not a verdict. If anything goes wrong, return a dict with ran=False
    and let the caller skip the test-results section silently.
    """
    if not test_cmd:
        return {"ran": False, "exit_code": None, "output": "", "error": "no command"}

    try:
        result = subprocess.run(
            test_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            encoding='utf-8',
            errors='replace',
        )
        combined = (result.stdout or "") + (result.stderr or "")
        # Keep the tail — that's where failure traces live.
        max_chars = 4000
        if len(combined) > max_chars:
            combined = "... [output truncated — showing last 4000 chars] ...\n" + combined[-max_chars:]
        return {
            "ran": True,
            "exit_code": result.returncode,
            "output": combined.strip(),
            "error": "",
        }
    except subprocess.TimeoutExpired:
        return {
            "ran": True,
            "exit_code": None,
            "output": "",
            "error": f"test command timed out after {timeout}s",
        }
    except (FileNotFoundError, OSError) as e:
        return {"ran": False, "exit_code": None, "output": "", "error": str(e)}
    except Exception as e:
        return {"ran": False, "exit_code": None, "output": "", "error": str(e)}


def format_test_section(test_result):
    """Format a test-result dict as a markdown section for the review prompt.

    Returns "" if tests didn't run, so the prompt stays clean when there's
    no test command configured.
    """
    if not test_result or not test_result.get("ran"):
        return ""

    exit_code = test_result.get("exit_code")
    output = test_result.get("output", "")
    error = test_result.get("error", "")

    if error:
        status = f"ERROR ({error})"
    elif exit_code == 0:
        status = "PASSED (exit 0)"
    else:
        status = f"FAILED (exit {exit_code})"

    body = output if output else "(no output)"
    return (
        "## Test Results\n"
        "Claude's project has a configured test command. Viper ran it just now. "
        "Use this as additional context — a test failure alone is not proof of a bug, "
        "and a passing suite is not proof of correctness. Weigh it alongside the code.\n\n"
        f"Status: {status}\n"
        "Output:\n"
        "```\n"
        f"{body}\n"
        "```\n\n"
    )


def run_codex_cli(cwd, changed_files, config, session_id=""):
    """Run Codex CLI to review changes. Codex reads the files itself."""
    timeout = config.get("codex_timeout", 180)

    file_list = "\n".join(f"- {f}" for f in changed_files)
    brief = load_brief(cwd)
    rules = load_rules(cwd)
    test_cmd = load_test_command(cwd)
    test_timeout = config.get("test_timeout", 60)
    test_result = run_tests(cwd, test_cmd, test_timeout) if test_cmd else None
    test_section = format_test_section(test_result)
    last_findings = load_last_findings(cwd, session_id) if session_id else ""
    approved_plan = load_last_approved_plan(cwd, session_id) if session_id else ""

    brief_section = ""
    if brief:
        brief_section = (
            "## Context from Claude\n"
            "Claude provided the following brief about what it did and why. "
            "Use this to verify the implementation matches the intent:\n\n"
            f"{brief}\n\n"
        )

    rules_section = ""
    if rules:
        rules_section = (
            "## Project-specific review rules\n"
            "This project has defined its own review rules. These are "
            "authoritative — honor them even if they contradict your defaults. "
            "Do not flag anything these rules explicitly tell you to ignore.\n\n"
            f"{rules}\n\n"
        )

    approved_plan_section = ""
    if approved_plan:
        approved_plan_section = (
            "## Approved plan (drift check)\n"
            "Earlier in this session, Claude submitted a plan via plan mode "
            "and you approved it. The text of that approved plan is below. "
            "Your job is to verify the actual implementation matches what was "
            "promised.\n\n"
            "Flag MAJOR deviations only — these are the things to look for:\n"
            "- Functionality the plan promised but the code doesn't implement\n"
            "- Functionality the code implements that the plan never authorized "
            "  (scope creep, undocumented features, surprise refactors)\n"
            "- Files modified that the plan said wouldn't be touched\n"
            "- Architectural choices that contradict what the plan committed to\n\n"
            "DO NOT flag:\n"
            "- Small helper functions or local cleanup adjacent to the main change\n"
            "- File path differences if the functionality matches\n"
            "- Implementation details the plan didn't specify\n"
            "- Style or naming differences\n\n"
            "If the implementation matches the plan in spirit and scope, do not "
            "flag drift — proceed to the rest of the review. If it diverges in "
            "scope or substance, list those deviations under a heading "
            "`### Plan drift` near the top of your findings.\n\n"
            "### The approved plan:\n\n"
            f"{approved_plan}\n\n"
        )

    last_findings_section = ""
    if last_findings:
        last_findings_section = (
            "## Previous cycle findings (CRITICAL — read this first)\n"
            "The PREVIOUS review of this same code (earlier in this session) "
            "flagged the issues below. Claude has since claimed to fix them and "
            "is asking to stop again.\n\n"
            "Your FIRST job, before any fresh review, is to verify each of these "
            "findings was ACTUALLY addressed in the current code. Do not take "
            "Claude's word for it — re-read the relevant files and check.\n\n"
            "For each previous finding:\n"
            "- If it was genuinely fixed, note that briefly and move on.\n"
            "- If it was NOT fixed (still present, or only superficially patched "
            "  like a comment/rename without addressing the underlying bug), "
            "  list it at the TOP of your findings prefixed with "
            "  `[NOT FIXED FROM PREVIOUS CYCLE]`. This is the most important "
            "  signal in your response — do not bury it.\n\n"
            "Then proceed with a fresh review for any NEW issues introduced by "
            "the latest changes.\n\n"
            "### Previous findings to verify:\n\n"
            f"{last_findings}\n\n"
        )

    prompt = (
        "You are a senior engineer doing a thorough code review. "
        "The following files were modified or created:\n\n"
        f"{file_list}\n\n"
        f"{rules_section}"
        f"{brief_section}"
        f"{test_section}"
        f"{approved_plan_section}"
        f"{last_findings_section}"
        "Do a deep review. Do NOT skim — actually trace through the code:\n\n"
        "1. Read every changed file top to bottom\n"
        "2. Run `git diff HEAD` to see exactly what changed in tracked files\n"
        "3. For new/untracked files, read the full file\n"
        "4. Trace the call chain — find every caller of changed functions, "
        "every import, every module that depends on this code. Read them.\n"
        "5. Check if the changes break any existing code that calls into these files\n"
        "6. Look at the data flow — where does input come from, where does output go, "
        "what happens at each boundary\n"
        "7. Read any tests that cover this code. Check if the tests still make sense "
        "after the changes, and whether new test cases are needed\n\n"
        "Review for:\n"
        "- Bugs, logic errors, security issues, race conditions, missing edge cases\n"
        "- Broken callers — did these changes break anything that depends on them\n"
        "- Whether the implementation actually matches the stated requirements\n"
        "- Wrong abstractions or architectural decisions that will cause problems\n"
        "- Missing functionality that was requested but not implemented\n"
        "- Misunderstood requirements (code works but solves the wrong problem)\n"
        "- Data flow issues — unvalidated input, unhandled errors at boundaries\n\n"
        "Do NOT nitpick style, formatting, or naming. Only flag real problems.\n\n"
        "Output format (strict):\n"
        "- If there are problems, list each one with file path, line number, "
        "and a short description.\n"
        "- End your response with a single verdict line by itself, exactly one of:\n"
        "    VERDICT: APPROVED\n"
        "    VERDICT: ISSUES FOUND\n"
        "- The verdict line must be the last non-empty line."
    )

    cmd = [
        _find_cmd('codex'), 'exec',
        '-s', 'read-only',
        '-C', cwd,
        '-'
    ]

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            encoding='utf-8',
            errors='replace'
        )
        # Non-zero exit = codex failed (rate limit, crash, etc.) — not a review
        if result.returncode != 0:
            return None
        output = result.stdout.strip()
        if not output:
            return None
        return output
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return None


def run_review(cwd, changed_files, config, session_id=""):
    """Run a code review via Codex CLI. Returns the review text or None.

    If Codex isn't installed or the call fails for any reason, returns None
    and the caller fails open (lets Claude stop normally).
    """
    if not shutil.which('codex'):
        return None
    return run_codex_cli(cwd, changed_files, config, session_id=session_id)


def main():
    # 1. Read Stop hook event from stdin
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    cwd = event.get("cwd", os.getcwd())
    session_id = event.get("session_id", "unknown")

    # 2. Check git diff — any files changed?
    changed = get_changed_files(cwd)
    if not changed:
        sys.exit(0)

    # 3. Load cycle state (prevent infinite loops)
    state = load_state(cwd, session_id)
    config = load_config()
    max_cycles = config.get("max_review_cycles", 4)

    if state["cycle"] >= max_cycles:
        cleanup_state(cwd)
        sys.exit(0)

    if state.get("approved"):
        sys.exit(0)

    # 4. Check for review brief — if missing on first cycle, ask Claude to write one
    brief = load_brief(cwd)
    if not brief and state["cycle"] == 0:
        state["cycle"] += 1
        save_state(cwd, state)
        output = {
            "decision": "block",
            "reason": (
                "[Viper] Write a review brief before stopping.\n\n"
                "Create `.viper/brief.md` with:\n"
                "- **Task**: What was requested\n"
                "- **Approach**: What you did and why\n"
                "- **Key decisions**: Architectural choices, tradeoffs made\n"
                "- **Changed files**: What each file change does\n"
                "- **Edge cases**: What you considered and what you didn't\n\n"
                "Then try to stop again."
            )
        }
        print(json.dumps(output))
        sys.exit(0)

    # 5. Run review — Codex reads files itself.
    # Pass session_id so the reviewer can be cycle-aware (load_last_findings
    # will inject the previous cycle's findings into the prompt).
    review = run_review(cwd, changed, config, session_id=session_id)

    # Log every review result so you can see Codex ran.
    # state["cycle"] is the pre-increment value at this point; the user-facing
    # cycle number for this review is (state["cycle"] + 1) to match the block
    # message formatting below.
    _log_review(cwd, review, changed, session_id=session_id, cycle=state["cycle"] + 1)

    if not review:
        sys.exit(0)  # Review failed, fail open

    # 6. Parse verdict
    if is_approved(review):
        state["approved"] = True
        save_state(cwd, state)
        # Approved — clear last findings AND the last approved plan so future
        # cycles in this session don't carry stale context from work that's
        # already been signed off on.
        clear_last_findings(cwd)
        clear_last_approved_plan(cwd)
        sys.exit(0)

    # 6. ISSUES FOUND — block Claude from stopping.
    state["cycle"] += 1
    save_state(cwd, state)

    # Save these findings so the NEXT cycle can verify they were actually
    # fixed instead of starting from scratch. This is the cycle-aware
    # reviewer loop: the next review will tag any of these as
    # "[NOT FIXED FROM PREVIOUS CYCLE]" if they're still present.
    save_last_findings(cwd, session_id, state["cycle"], review)

    output = {
        "decision": "block",
        "reason": (
            f"[Viper Code Review - Cycle {state['cycle']}/{max_cycles}]\n\n"
            f"{review}\n\n"
            "Fix the issues above. Do NOT explain what you're doing — just fix them."
        )
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
