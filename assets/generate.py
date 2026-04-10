#!/usr/bin/env python3
"""Generate the README PNGs for Viper.

Hand-rendered with PIL — no AI image generation, no SVG conversion, no
external services. Every pixel is a deliberate ImageDraw call. Re-run this
script any time you want to regenerate the assets:

    python ~/.claude/hooks/viper/assets/generate.py

Output files (overwritten on each run, written next to this script):
    assets/header.png   1280x320  — wordmark / banner
    assets/review.png   1280x720  — fake terminal showing a Viper review block
    assets/status.png   1280x720  — fake terminal showing `cli.py status`

Aesthetic:
    - GitHub-dark background (#0d1117)
    - Consolas monospace for code/terminal text
    - Segoe UI Bold for the wordmark
    - One green accent (#3fb950 — GitHub success green)
    - One red accent (#f85149 — GitHub danger red)
    - One subdued grey for chrome and labels
    - No gradients, no glows, no emoji, no slop

Note: this script currently hardcodes Windows font paths
(`C:/Windows/Fonts/...`). On macOS or Linux, edit the FONT_DIR constant
or replace the `font(name, size)` helper to use whatever monospace font
is available on your system (DejaVu Sans Mono, JetBrains Mono, etc.).
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).resolve().parent

# Color palette — GitHub dark theme
BG          = (13, 17, 23)        # #0d1117
PANEL       = (22, 27, 34)        # #161b22 — title bar
BORDER      = (48, 54, 61)        # #30363d
TEXT        = (230, 237, 243)     # #e6edf3
DIM         = (125, 133, 144)     # #7d8590 — labels, prompts, comments
GREEN       = (63, 185, 80)       # #3fb950
RED         = (248, 81, 73)       # #f85149
YELLOW      = (210, 153, 34)      # #d29922
BLUE        = (88, 166, 255)      # #58a6ff
ACCENT      = GREEN

# Traffic-light colors for the fake terminal chrome
TL_RED      = (255, 95, 86)
TL_YELLOW   = (255, 189, 46)
TL_GREEN    = (39, 201, 63)

FONT_DIR = "C:/Windows/Fonts"


def font(name, size):
    return ImageFont.truetype(f"{FONT_DIR}/{name}", size)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def draw_terminal_chrome(draw, x, y, w, h, title="viper"):
    """Draw the rounded-rect terminal window with traffic-light buttons."""
    radius = 12
    # Outer rounded rect (whole window)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=BG, outline=BORDER, width=1)
    # Title bar
    bar_h = 38
    draw.rounded_rectangle([x, y, x + w, y + bar_h], radius=radius, fill=PANEL)
    # Cover the bottom of the rounded title bar so the corners stay rounded
    # only at the top
    draw.rectangle([x, y + bar_h - radius, x + w, y + bar_h], fill=PANEL)
    # Bottom border line of title bar
    draw.line([(x, y + bar_h), (x + w, y + bar_h)], fill=BORDER, width=1)
    # Traffic lights
    cy = y + 19
    for i, color in enumerate((TL_RED, TL_YELLOW, TL_GREEN)):
        cx = x + 22 + i * 22
        draw.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], fill=color)
    # Title text (centered)
    f = font("segoeui.ttf", 14)
    bbox = draw.textbbox((0, 0), title, font=f)
    tw = bbox[2] - bbox[0]
    draw.text((x + (w - tw) // 2, y + 10), title, font=f, fill=DIM)
    return bar_h


def text(draw, x, y, s, font_obj, color):
    draw.text((x, y), s, font=font_obj, fill=color)


# ---------------------------------------------------------------------------
# 1. header.png — wordmark / banner
# ---------------------------------------------------------------------------

def make_header():
    W, H = 1280, 320
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Vertical accent bar on the left — single restrained design element
    bar_x = 80
    d.rectangle([bar_x, 100, bar_x + 6, H - 100], fill=ACCENT)

    # Wordmark
    wordmark_x = bar_x + 32
    fmark = font("segoeuib.ttf", 110)
    d.text((wordmark_x, 78), "VIPER", font=fmark, fill=TEXT)

    # Subtitle
    fsub = font("segoeui.ttf", 26)
    d.text((wordmark_x + 4, 200), "Claude writes code. Codex reviews it.", font=fsub, fill=DIM)
    d.text((wordmark_x + 4, 234), "Bugs get caught before you see them.", font=fsub, fill=DIM)

    # Right-side: small code-block-style label
    fmono = font("consola.ttf", 18)
    label_lines = [
        "stop hook",
        "plan review",
        "cycle aware",
        "drift check",
    ]
    label_x = W - 280
    label_y = 90
    # Subtle box around the labels
    d.rounded_rectangle(
        [label_x - 16, label_y - 14, label_x + 240, label_y + 24 * len(label_lines) + 6],
        radius=6, outline=BORDER, width=1
    )
    for i, ln in enumerate(label_lines):
        d.text((label_x, label_y + i * 24), "•", font=fmono, fill=ACCENT)
        d.text((label_x + 20, label_y + i * 24), ln, font=fmono, fill=TEXT)

    img.save(ASSETS / "header.png")
    print(f"wrote {ASSETS / 'header.png'}  ({W}x{H})")


# ---------------------------------------------------------------------------
# 2. review.png — fake terminal showing a Viper review block
# ---------------------------------------------------------------------------

def make_review():
    W, H = 1280, 720
    img = Image.new("RGB", (W, H), (8, 11, 16))  # slightly darker page bg
    d = ImageDraw.Draw(img)

    pad = 40
    win_x, win_y = pad, pad
    win_w, win_h = W - 2 * pad, H - 2 * pad
    bar_h = draw_terminal_chrome(d, win_x, win_y, win_w, win_h, title="viper — Stop hook")

    # Content area
    fmono = font("consola.ttf", 17)
    fbold = font("consolab.ttf", 17)
    line_h = 24
    content_x = win_x + 28
    y = win_y + bar_h + 24

    def line(s, color=TEXT, bold=False, dy=line_h):
        nonlocal y
        d.text((content_x, y), s, font=(fbold if bold else fmono), fill=color)
        y += dy

    # Prompt + invocation
    d.text((content_x, y), "$", font=fbold, fill=ACCENT)
    d.text((content_x + 18, y), "claude", font=fmono, fill=TEXT)
    y += line_h
    line("(... Claude finishes a refactor and tries to stop ...)", DIM)
    y += 6

    # The review block
    line("[Viper Code Review — Cycle 2/4]", RED, bold=True)
    y += 4

    line("[NOT FIXED FROM PREVIOUS CYCLE]", RED, bold=True)
    line("- api.py:17   Auth bypass: cancel endpoint trusts the user_id", TEXT)
    line("              path parameter without checking the authenticated", TEXT)
    line("              user. You renamed the variable in cycle 1 but did", TEXT)
    line("              not actually verify identity.", TEXT)
    y += 6

    line("### Plan drift", YELLOW, bold=True)
    line("- The approved plan said 'add cancel endpoint'. Implementation", TEXT)
    line("  also rewrites the email/SMS notifier and adds a new admin", TEXT)
    line("  override flag — neither was in the plan.", TEXT)
    y += 10

    line("VERDICT: ISSUES FOUND", RED, bold=True)
    y += 6

    line("Fix the issues above. Do NOT explain — just fix them.", DIM)

    # Bottom corner watermark
    fwm = font("consola.ttf", 12)
    d.text((win_x + win_w - 80, win_y + win_h - 24), "viper 2.0", font=fwm, fill=BORDER)

    img.save(ASSETS / "review.png")
    print(f"wrote {ASSETS / 'review.png'}  ({W}x{H})")


# ---------------------------------------------------------------------------
# 3. status.png — fake terminal showing `cli.py status`
# ---------------------------------------------------------------------------

def make_status():
    W, H = 1280, 720
    img = Image.new("RGB", (W, H), (8, 11, 16))
    d = ImageDraw.Draw(img)

    pad = 40
    win_x, win_y = pad, pad
    win_w, win_h = W - 2 * pad, H - 2 * pad
    bar_h = draw_terminal_chrome(d, win_x, win_y, win_w, win_h, title="viper — cli.py status")

    fmono = font("consola.ttf", 16)
    fbold = font("consolab.ttf", 16)
    line_h = 22
    content_x = win_x + 28
    y = win_y + bar_h + 22

    def line(s, color=TEXT, bold=False, dy=line_h, x_off=0):
        nonlocal y
        d.text((content_x + x_off, y), s, font=(fbold if bold else fmono), fill=color)
        y += dy

    def marker(label, status, desc, color):
        nonlocal y
        # status marker [OK] / [--] / [!!] in its color
        d.text((content_x, y), label, font=fbold, fill=color)
        # rest of the line in default
        offset = 50
        d.text((content_x + offset, y), status, font=fmono, fill=TEXT)
        d.text((content_x + offset + 240, y), desc, font=fmono, fill=DIM)
        y += line_h

    # Prompt
    d.text((content_x, y), "$", font=fbold, fill=ACCENT)
    d.text((content_x + 18, y), "python ~/.claude/hooks/viper/cli.py status", font=fmono, fill=TEXT)
    y += line_h + 6

    # Header
    line("Viper Status — bobs-diesel", TEXT, bold=True)
    line("=" * 56, DIM)
    y += 6

    # Hooks
    line("Hooks (in ~/.claude/settings.json):", TEXT, bold=True)
    marker("[OK]", "Stop hook (viper.py)", "", GREEN)
    marker("[OK]", "PreToolUse:ExitPlanMode (plan_review.py)", "", GREEN)
    y += 6

    # Artifacts
    line("Project artifacts (.viper/ in bobs-diesel):", TEXT, bold=True)
    rows = [
        ("[OK]", "rules.md",              "12 lines    2d ago",  GREEN),
        ("[OK]", "test_command",          "pytest -x   2d ago",  GREEN),
        ("[OK]", "review.jsonl",          "47 entries  3m ago",  GREEN),
        ("[OK]", "last_findings.md",      "auto        3m ago",  GREEN),
        ("[OK]", "last_approved_plan.md", "auto        12m ago", GREEN),
    ]
    for m, name, desc, color in rows:
        marker(m, name, desc, color)
    y += 8

    # Activity
    line("Recent activity (last 7 days, 47 reviews):", TEXT, bold=True)
    line("  Sessions:           18", TEXT)
    line("  Reached APPROVED:   14  (78%)", GREEN)
    line("  Avg cycles to APPROVED: 1.6   (max: 3)", TEXT)
    y += 6

    line("  Most-flagged files this week:", TEXT)
    flagged = [
        ("api/orders.py",   "8 flags"),
        ("db/queries.py",   "5 flags"),
        ("auth/sessions.py", "3 flags"),
    ]
    for fname, count in flagged:
        d.text((content_x + 32, y), fname, font=fmono, fill=TEXT)
        d.text((content_x + 380, y), count, font=fmono, fill=YELLOW)
        y += line_h
    y += 6

    line("Most recent review: 3m ago — APPROVED (cycle 2)", GREEN, bold=True)

    # watermark
    fwm = font("consola.ttf", 12)
    d.text((win_x + win_w - 80, win_y + win_h - 24), "viper 2.0", font=fwm, fill=BORDER)

    img.save(ASSETS / "status.png")
    print(f"wrote {ASSETS / 'status.png'}  ({W}x{H})")


if __name__ == "__main__":
    make_header()
    make_review()
    make_status()
    print("done.")
