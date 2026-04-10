#!/usr/bin/env python3
"""Viper stats — summarize review history from .viper/review.jsonl.

Standalone CLI. Reads the structured review log written by viper.py and
prints a summary. Falls back to parsing the human-readable review.log if
no JSONL is present (legacy projects), in which case session-level stats
are unavailable.

Usage:
    python stats.py [project_path]

If no path is given, uses the current working directory.
"""

import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime

# Force UTF-8 output on Windows. Use reconfigure() to avoid the destructor
# bug that closes the underlying buffer when the original wrapper is replaced.
if os.name == 'nt':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass


def load_jsonl(path):
    """Load structured review entries. Skip malformed lines silently."""
    entries = []
    if not os.path.exists(path):
        return entries
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
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


def load_legacy_log(path):
    """Parse the human-readable review.log for legacy data.

    Extracts timestamp, verdict, and files from each entry header.
    Review body text is ignored — stats only need metadata.
    Entries parsed this way have no session_id or cycle number, so
    session-level stats will be unavailable for them.
    """
    entries = []
    if not os.path.exists(path):
        return entries
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            content = f.read()
    except OSError:
        return []

    # Each entry header looks like:
    #   [YYYY-MM-DD HH:MM:SS] Verdict: <VERDICT>
    #   Files reviewed: <comma-separated list>
    pattern = re.compile(
        r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] Verdict: ([A-Z ]+?)\n'
        r'Files reviewed: ([^\n]*)\n'
    )
    for m in pattern.finditer(content):
        ts_str, verdict, files_str = m.groups()
        try:
            ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S').isoformat(timespec='seconds')
        except ValueError:
            ts = ts_str
        files = [f.strip() for f in files_str.split(',') if f.strip()]
        entries.append({
            "timestamp": ts,
            "session_id": None,
            "cycle": None,
            "verdict": verdict.strip(),
            "files": files,
        })
    return entries


def summarize(entries, recent_n=5):
    """Build a human-readable summary of the review entries."""
    total = len(entries)
    if total == 0:
        return "No reviews logged yet."

    # Verdict breakdown
    verdicts = Counter(e.get('verdict', 'UNKNOWN') for e in entries)

    # Group by session — only for entries with a known session_id
    by_session = defaultdict(list)
    for e in entries:
        sid = e.get('session_id')
        if sid:
            by_session[sid].append(e)

    approved_sessions = 0
    unresolved_sessions = 0
    cycles_to_approval = []
    for sid, session_entries in by_session.items():
        has_approved = any(e.get('verdict') == 'APPROVED' for e in session_entries)
        if has_approved:
            approved_sessions += 1
            # Cycle at which the first APPROVED verdict occurred in this session
            approved_cycles = [
                e.get('cycle') for e in session_entries
                if e.get('verdict') == 'APPROVED' and e.get('cycle') is not None
            ]
            if approved_cycles:
                cycles_to_approval.append(min(approved_cycles))
        else:
            unresolved_sessions += 1

    # File flag counts — only count files appearing in ISSUES FOUND entries
    file_flags = Counter()
    file_sessions = defaultdict(set)
    for e in entries:
        if e.get('verdict') == 'ISSUES FOUND':
            for fname in e.get('files', []):
                file_flags[fname] += 1
                sid = e.get('session_id')
                if sid:
                    file_sessions[fname].add(sid)

    lines = []
    lines.append("Viper Review Stats")
    lines.append("=" * 50)
    lines.append(f"Total reviews:  {total}")
    lines.append("")
    lines.append("Verdicts:")
    for v in ("APPROVED", "ISSUES FOUND", "NO RESPONSE"):
        count = verdicts.get(v, 0)
        pct = (100 * count // total) if total else 0
        lines.append(f"  {v:<14}  {count:>4}  ({pct}%)")
    # Any other unexpected verdicts
    for v, count in verdicts.items():
        if v not in ("APPROVED", "ISSUES FOUND", "NO RESPONSE"):
            pct = (100 * count // total) if total else 0
            lines.append(f"  {v:<14}  {count:>4}  ({pct}%)")
    lines.append("")

    if by_session:
        lines.append(f"Sessions tracked: {len(by_session)}")
        lines.append(f"  Reached APPROVED:    {approved_sessions}")
        lines.append(f"  Unresolved/open:     {unresolved_sessions}")
        if cycles_to_approval:
            avg = sum(cycles_to_approval) / len(cycles_to_approval)
            med = statistics.median(cycles_to_approval)
            lines.append(f"  Avg cycles to approval:    {avg:.1f}")
            lines.append(f"  Median cycles to approval: {med}")
        lines.append("")
    else:
        lines.append("(No session-level stats — legacy log format has no session_id.)")
        lines.append("")

    if file_flags:
        lines.append("Most-flagged files (ISSUES FOUND entries):")
        for fname, count in file_flags.most_common(10):
            sess_count = len(file_sessions.get(fname, set()))
            if sess_count:
                sess_note = f" across {sess_count} session{'s' if sess_count != 1 else ''}"
            else:
                sess_note = ""
            plural = 's' if count != 1 else ''
            lines.append(f"  {fname:<40}  {count:>3} flag{plural}{sess_note}")
        lines.append("")

    if recent_n and entries:
        # Entries are appended chronologically; the last N are the most recent.
        recent = entries[-recent_n:]
        lines.append(f"Recent {len(recent)} review{'s' if len(recent) != 1 else ''}:")
        for e in recent:
            ts = e.get('timestamp', '?')
            v = e.get('verdict', '?')
            files = ', '.join(e.get('files', []))
            if len(files) > 50:
                files = files[:47] + '...'
            lines.append(f"  {ts}  {v:<14}  {files}")

    return '\n'.join(lines)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)

    project = argv[0] if argv else os.getcwd()
    if not os.path.isdir(project):
        print(f"Error: not a directory: {project}", file=sys.stderr)
        sys.exit(1)

    viper_dir = os.path.join(project, '.viper')
    jsonl_path = os.path.join(viper_dir, 'review.jsonl')
    log_path = os.path.join(viper_dir, 'review.log')

    entries = load_jsonl(jsonl_path)
    source = "review.jsonl"

    if not entries and os.path.exists(log_path):
        entries = load_legacy_log(log_path)
        source = "review.log (legacy — no session stats)"

    if not entries:
        print(f"No review data found in {viper_dir}")
        print(f"  (Looked for {jsonl_path} and {log_path})")
        sys.exit(0)

    print(f"Project: {project}")
    print(f"Source:  {source}")
    print()
    print(summarize(entries))


if __name__ == '__main__':
    main()
