#!/usr/bin/env python3
"""Viper Plan Review Hook: Codex reviews Claude's plan before plan mode exits.

Registered as a Claude Code PreToolUse hook with matcher "ExitPlanMode".
When Claude finalizes a plan and tries to exit plan mode, this hook fires,
sends the plan to Codex for review, and either lets it through or denies
the tool call with feedback so Claude has to revise the plan first.

Off by default. Enable by setting "plan_review_enabled": true in config.json
and registering the hook in ~/.claude/settings.json (see README).

Single-cycle by design — plan reviews don't iterate. If issues are found,
they're surfaced once. Claude either revises or proceeds. No state file.
"""

import json
import os
import shutil
import subprocess
import sys

# Force UTF-8 output on Windows. Match viper.py.
if os.name == 'nt':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

# Import shared helpers from the sibling viper.py module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from viper import is_approved, load_config, load_rules, _find_cmd, _ensure_npm_path  # noqa: E402

_ensure_npm_path()


def extract_plan(event):
    """Get the plan text from the hook event.

    Fast path: ExitPlanMode tool_input includes the plan directly.
    Fallback: read the transcript JSONL at transcript_path, find the
    assistant message containing a tool_use with the matching tool_use_id,
    and extract input.plan from there.

    Returns "" if neither path yields a plan.
    """
    # Fast path
    tool_input = event.get("tool_input") or {}
    if isinstance(tool_input, dict):
        plan = tool_input.get("plan", "")
        if plan:
            return plan

    # Fallback: walk the transcript
    transcript_path = event.get("transcript_path", "")
    tool_use_id = event.get("tool_use_id", "")
    if not transcript_path or not tool_use_id or not os.path.exists(transcript_path):
        return ""

    try:
        with open(transcript_path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message")
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if (item.get("type") == "tool_use"
                            and item.get("id") == tool_use_id
                            and item.get("name") == "ExitPlanMode"):
                        plan = (item.get("input") or {}).get("plan", "")
                        if plan:
                            return plan
    except (OSError, UnicodeError):
        return ""
    return ""


def review_plan(cwd, plan, config):
    """Send the plan to Codex CLI for review. Returns the review text or None.

    Falls back silently when Codex isn't installed or the call fails — plan
    review must never block Claude on infrastructure failures, only on real
    plan problems.
    """
    timeout = config.get("codex_timeout", 180)
    rules = load_rules(cwd)

    rules_section = ""
    if rules:
        rules_section = (
            "## Project-specific review rules (authoritative — honor these)\n"
            f"{rules}\n\n"
        )

    prompt = (
        "You are reviewing a PLAN, not finished code. The plan below describes "
        "what Claude is about to implement. Your job is to catch architectural "
        "mistakes, missing requirements, wrong abstractions, and ignored edge "
        "cases BEFORE any code is written.\n\n"
        f"{rules_section}"
        "## The plan\n\n"
        f"{plan}\n\n"
        "## Review criteria\n\n"
        "Flag only things that would cause the implementation to fail or solve "
        "the wrong problem:\n"
        "- Missing requirements the plan doesn't address\n"
        "- Wrong abstractions or design choices that will cause problems later\n"
        "- Edge cases the plan should handle but doesn't mention\n"
        "- Solutions that don't actually solve the stated problem\n"
        "- Scope creep — features being added that weren't requested\n"
        "- Internal contradictions in the plan\n\n"
        "DO NOT critique writing style, naming, formatting, or word choice.\n"
        "DO NOT suggest unrelated improvements.\n"
        "DO NOT nitpick. Plans are intentionally less detailed than code; do "
        "not flag things just because the plan doesn't enumerate every step.\n"
        "If the plan looks reasonable, approve it — better to let Claude "
        "implement and catch issues at the code-review stage than to "
        "second-guess every plan.\n\n"
        "## Output format (strict)\n\n"
        "- If there are problems, list each one with a short description.\n"
        "- End your response with a single verdict line by itself, exactly one of:\n"
        "    VERDICT: APPROVED\n"
        "    VERDICT: ISSUES FOUND\n"
        "- The verdict line must be the last non-empty line."
    )

    codex = shutil.which('codex')
    if not codex:
        return None

    cmd = [codex, 'exec', '-s', 'read-only', '-C', cwd, '-']
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            encoding='utf-8',
            errors='replace',
        )
        if result.returncode != 0:
            return None
        output = result.stdout.strip()
        return output or None
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return None


def _log_plan_review(cwd, verdict, review_text, plan_excerpt):
    """Append a plan-review entry to .viper/plan_review.log. Never crashes."""
    try:
        from datetime import datetime
        viper_dir = os.path.join(cwd, '.viper')
        os.makedirs(viper_dir, exist_ok=True)
        log_path = os.path.join(viper_dir, 'plan_review.log')
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[{timestamp}] Plan verdict: {verdict}\n")
            f.write(f"{'='*60}\n")
            f.write(f"Plan (first 500 chars):\n{plan_excerpt[:500]}\n\n")
            f.write(f"Review:\n{review_text or '(no response from reviewer)'}\n")
    except Exception:
        pass


def save_approved_plan(cwd, session_id, plan_text):
    """Save an APPROVED plan so the Stop hook can compare the implementation
    against it later in the same session.

    File format (.viper/last_approved_plan.md):
        session_id: <uuid>
        ---
        <plan text as-is>

    Stop hook (viper.py) reads this and discards it if the session_id doesn't
    match the current session, so stale plans never bleed across sessions.
    Cleared on APPROVED at stop time and on cleanup_state.
    """
    if not plan_text or not plan_text.strip():
        return
    viper_dir = os.path.join(cwd, '.viper')
    try:
        os.makedirs(viper_dir, exist_ok=True)
        path = os.path.join(viper_dir, 'last_approved_plan.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"session_id: {session_id}\n")
            f.write("---\n")
            f.write(plan_text)
    except OSError:
        pass


def main():
    # 1. Read PreToolUse event from stdin
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # malformed event — fail open

    # 2. Verify this is the right tool. Defensive: if hook is misconfigured
    # and fires for other tools, do nothing rather than crashing.
    if event.get("tool_name") != "ExitPlanMode":
        sys.exit(0)

    # 3. Check config — plan review must be explicitly enabled
    config = load_config()
    if not config.get("plan_review_enabled", False):
        sys.exit(0)

    cwd = event.get("cwd", os.getcwd())
    session_id = event.get("session_id", "unknown")

    # 4. Get the plan text. If we can't find it, there's nothing to review.
    plan = extract_plan(event)
    if not plan or not plan.strip():
        sys.exit(0)

    # 5. Run the review. Failure to call Codex => fail open (let Claude proceed).
    review = review_plan(cwd, plan, config)
    if not review:
        _log_plan_review(cwd, "NO RESPONSE", review, plan)
        sys.exit(0)

    # 6. Parse verdict
    if is_approved(review):
        _log_plan_review(cwd, "APPROVED", review, plan)
        # Save the approved plan so the Stop hook can compare the actual
        # implementation against it later in the same session. This is the
        # plan→code drift detection link: every promise made at planning time
        # gets verified at stop time.
        save_approved_plan(cwd, session_id, plan)
        sys.exit(0)

    # 7. ISSUES FOUND — deny the ExitPlanMode tool call with feedback.
    # Use the PreToolUse hookSpecificOutput format. Claude will receive
    # the reason as feedback and revise the plan.
    _log_plan_review(cwd, "ISSUES FOUND", review, plan)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "[Viper Plan Review]\n\n"
                f"{review}\n\n"
                "Revise the plan to address the above, then try to exit plan mode again. "
                "If you disagree with a finding, address it explicitly in the revised plan."
            ),
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == '__main__':
    main()
