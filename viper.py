#!/usr/bin/env python3
"""Viper v2 Stop Hook: Codex reviews Claude's work before it can finish."""

import sys
import os
import json
import subprocess
import shutil

# Force UTF-8 output on Windows to avoid cp1252 encoding errors
if os.name == 'nt':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


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


def get_changed_files(cwd):
    """Get list of changed files (staged, unstaged, and untracked)."""
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

    # Exclude viper's own state files
    changed = {f for f in changed if not f.startswith('.viper/')}
    return sorted(changed)


def read_changed_files(cwd, changed_files, max_chars=20000):
    """Read the contents of changed files (only used for API fallback)."""
    parts = []
    total = 0

    for fname in changed_files[:15]:
        fpath = os.path.join(cwd, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, 'r', encoding='utf-8', errors='strict') as f:
                content = f.read()
        except (UnicodeDecodeError, PermissionError, OSError):
            continue

        if len(content) > 4000:
            content = content[:4000] + "\n... [truncated]"
        if total + len(content) > max_chars:
            break
        parts.append(f"--- {fname} ---\n{content}")
        total += len(content)

    return "\n\n".join(parts) if parts else ""


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
    """Remove .viper directory."""
    viper_dir = os.path.join(cwd, '.viper')
    if os.path.exists(viper_dir):
        shutil.rmtree(viper_dir, ignore_errors=True)


def is_approved(text):
    """Check if review text indicates approval."""
    if not text:
        return True
    upper = text.upper()
    if "ISSUES FOUND" in upper or "NOT APPROVED" in upper:
        return False
    if "APPROVED" in upper:
        return True
    return False


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


def run_codex_cli(cwd, changed_files, config):
    """Run Codex CLI to review changes. Codex reads the files itself."""
    timeout = config.get("codex_timeout", 180)

    file_list = "\n".join(f"- {f}" for f in changed_files)
    brief = load_brief(cwd)

    brief_section = ""
    if brief:
        brief_section = (
            "## Context from Claude\n"
            "Claude provided the following brief about what it did and why. "
            "Use this to verify the implementation matches the intent:\n\n"
            f"{brief}\n\n"
        )

    prompt = (
        "You are a senior engineer doing a thorough code review. "
        "The following files were modified or created:\n\n"
        f"{file_list}\n\n"
        f"{brief_section}"
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
        "If everything looks correct, respond with exactly: APPROVED\n"
        "If there are problems, respond with: ISSUES FOUND\n"
        "Then list each issue with file path, line number, and description."
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


def run_api_fallback(cwd, changed_files, config):
    """OpenRouter API fallback — must send file contents since no filesystem access."""
    import urllib.request

    api_key = config.get("openrouter_api_key", "")
    if not api_key:
        return None

    max_context = config.get("max_context_chars", 20000)
    file_contents = read_changed_files(cwd, changed_files, max_context)
    if not file_contents:
        return None

    prompt = (
        "Review these code changes for bugs, logic errors, security issues, "
        "and missing edge cases.\n"
        "Do NOT nitpick style, formatting, or naming. Only flag real functional problems.\n\n"
        f"Changed files:\n{file_contents}\n\n"
        "If everything looks correct, respond with exactly: APPROVED\n"
        "If there are problems, respond with: ISSUES FOUND\n"
        "Then list each issue with file path, line number, and description."
    )

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/viper-relay",
        "X-Title": "Viper Code Review"
    }
    payload = {
        "model": config.get("fallback_model", "openai/gpt-4o"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 2000
    }

    try:
        req = urllib.request.Request(url, json.dumps(payload).encode('utf-8'), headers)
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]
    except Exception:
        return None


def run_review(cwd, changed_files, config):
    """Run review via Codex CLI, falling back to API if unavailable."""
    codex_path = shutil.which('codex')
    if codex_path:
        result = run_codex_cli(cwd, changed_files, config)
        if result:
            return result

    return run_api_fallback(cwd, changed_files, config)


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
    max_cycles = config.get("max_review_cycles", 3)

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

    # 5. Run review — Codex reads files itself, API fallback gets contents
    review = run_review(cwd, changed, config)

    if not review:
        sys.exit(0)  # Review failed, fail open

    # 5. Parse verdict
    if is_approved(review):
        state["approved"] = True
        save_state(cwd, state)
        sys.exit(0)

    # 6. ISSUES FOUND — block Claude from stopping
    state["cycle"] += 1
    save_state(cwd, state)

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
