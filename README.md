<p align="center">
  <img src="https://img.shields.io/badge/Claude_Code-Hook-blueviolet?style=for-the-badge" alt="Claude Code Hook">
  <img src="https://img.shields.io/badge/Reviewer-OpenAI_Codex-00A67E?style=for-the-badge" alt="Codex Reviewer">
  <img src="https://img.shields.io/badge/Status-Active-brightgreen?style=for-the-badge" alt="Active">
</p>

# Viper 2.0

### Claude writes code. Codex reviews it. Bugs get caught before you see them.

Viper sits between Claude and the finish line. Every time Claude tries to stop, Viper hands the code to a second AI — OpenAI Codex — for an independent review. If Codex finds problems, Claude **can't stop**. It has to fix them first.

No config. No dashboards. No manual review queues. Just a Python script that says "you're not done yet."

---

### What it looks like in practice

Claude builds an invoice manager. Tries to stop. Gets blocked:

```
[Viper Code Review - Cycle 1/3]

ISSUES FOUND

- app.html:53   amount is a string from input, not a number.
                 total += inv.amount concatenates instead of adding.
                 total.toFixed(2) throws TypeError.

- app.html:55   innerHTML with unsanitized user data.
                 customer_name and notes can carry script payloads.
                 Stored DOM XSS.

- app.html:109  eval() on user-provided template string.
                 Full script execution in page context.

- app.html:114  Recursive object merge without blocking __proto__.
                 Prototype pollution.

Fix the issues above. Do NOT explain what you're doing — just fix them.
```

Claude fixes all 4. Tries to stop again. Codex re-reviews. **APPROVED.** Claude stops.

Zero human intervention. Zero context switching. The bugs never reach your terminal.

---

### It doesn't just scan files — it traces through your code

Codex has full filesystem access (read-only). It reads every changed file, follows imports, checks callers, and traces data flow across boundaries. Here's a real test on a 3-file order management system (`db.py` -> `orders.py` -> `api.py`):

```
[Viper Code Review - Cycle 1/3]

ISSUES FOUND

- api.py:17    The cancel endpoint authorizes based only on the user_id
               path parameter. There is no authenticated user check
               anywhere in the request flow. Any caller who knows another
               user's ID can cancel their orders. This does not satisfy
               "users should only be able to cancel their own orders."

- orders.py:15 order_id comes from Flask path as a string, while
               o["id"] from SQLite is an integer. The equality check
               always fails — cancellation is silently non-functional.

- orders.py:14 Cancellation is read-then-write across separate queries
  + db.py:25   and connections. A concurrent request can change the order
               state after the read, and this code still overwrites it
               to cancelled. Race condition violates the state machine.

- orders.py:23 Return value of update_order_status is ignored. If the
               update affects zero rows, cancel_order still returns
               success — false 200 responses.
```

Every finding required reading multiple files and tracing the connections between them. The type mismatch (`str` vs `int`) spans Flask -> business logic -> SQLite. The race condition spans business logic -> data layer. The auth gap spans the route handler -> the brief's stated requirement.

That's not a linter. That's a senior engineer reading your code.

### It reviews against intent, not just syntax

When Claude writes a `.viper/brief.md` before stopping (what it built, why, what the requirements were), Codex doesn't just look for bugs — it checks whether the code actually solves the right problem. The auth finding above was only caught because the brief stated "users should only cancel their own orders." Without it, the code *looks* fine.

---

<details>
<summary><h2>How It Works (technical details)</h2></summary>

### The Flow

```
Claude works on code
        |
        v
   Claude tries to stop
        |
        v
  Viper stop hook fires
        |
        v
  Any changed files? --NO--> Claude stops normally
        |
       YES
        |
        v
  Brief exists? --NO--> Block: "Write .viper/brief.md first"
        |                          |
       YES                    Claude writes brief, retries
        |                          |
        v  <-----------------------+
  Tell Codex which files changed
        |
        v
  Codex reads files + git diff + related code
  (full filesystem access, read-only sandbox)
        |
        v
  APPROVED? --YES--> Claude stops normally
        |
        NO
        |
        v
  Block Claude + inject review feedback
        |
        v
  Claude fixes issues, tries to stop again
        |
        v
  (cycle repeats up to max_review_cycles)
```

### Why Codex reads the files itself

Earlier versions stuffed file contents into the prompt (up to 20KB, 15 files, truncated). This caused:
- Windows command-line length limits (8191 chars)
- Truncated files = partial context = confident wrong answers
- No ability to follow imports or read related code

Now Viper just passes the file *paths*. Codex runs in read-only sandbox mode with full filesystem access — it reads the actual files, runs `git diff`, follows imports, checks callers. No truncation. No context limits.

### The Review Brief

The secret sauce. On first stop, if no `.viper/brief.md` exists, Viper blocks Claude and asks it to write one:

```
[Viper] Write a review brief before stopping.

Create .viper/brief.md with:
- Task: What was requested
- Approach: What you did and why
- Key decisions: Architectural choices, tradeoffs made
- Changed files: What each file change does
- Edge cases: What you considered and what you didn't
```

Claude already has all this context — it just did the work. Writing it down takes seconds. But it transforms the review from "does this code have bugs" to "does this code solve the right problem the right way."

### Fail-Open Design

If anything goes wrong — no git repo, Codex unavailable, API error, rate limit, timeout — Viper lets Claude stop normally. Your workflow is never blocked by a broken reviewer.

</details>

---

<details>
<summary><h2>Getting Started</h2></summary>

### Prerequisites

- **Python 3.8+**
- **Claude Code** CLI
- **Codex CLI** (`npm install -g @openai/codex`) with **ChatGPT Plus** ($20/mo)

### Install

```bash
# Clone into your hooks directory
# Windows
git clone https://github.com/postgigg/viper-2.0.git "%APPDATA%\.claude\hooks\viper"

# macOS/Linux
git clone https://github.com/postgigg/viper-2.0.git ~/.claude/hooks/viper
```

### Register the hook

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "type": "command",
        "command": "python ~/.claude/hooks/viper/viper.py"
      }
    ]
  }
}
```

On Windows, use the batch wrapper:

```json
{
  "hooks": {
    "Stop": [
      {
        "type": "command",
        "command": "C:/Users/YOUR_USER/.claude/hooks/viper/viper.bat"
      }
    ]
  }
}
```

### Authenticate Codex

```bash
npm install -g @openai/codex
codex login
```

### Enable architectural reviews

Copy `CLAUDE_SNIPPET.md` into your project's `CLAUDE.md`. This tells Claude to write a review brief (`.viper/brief.md`) before stopping — giving Codex the context to catch design problems, not just bugs.

Add `.viper/` to your `.gitignore`.

### (Optional) OpenRouter fallback

If Codex is unavailable, Viper can fall back to the OpenRouter API. Edit `config.json`:

```json
{
  "openrouter_api_key": "sk-or-..."
}
```

</details>

---

<details>
<summary><h2>Configuration</h2></summary>

Edit `config.json` in the viper directory:

| Key | Default | Description |
|-----|---------|-------------|
| `codex_timeout` | `180` | Seconds to wait for Codex CLI response |
| `max_review_cycles` | `3` | Max review/fix cycles before allowing stop |
| `openrouter_api_key` | `""` | OpenRouter API key (fallback when Codex unavailable) |
| `fallback_model` | `"openai/gpt-4o"` | Model to use via OpenRouter |
| `max_context_chars` | `20000` | Max total characters of file content to send (API fallback only) |

</details>

---

<details>
<summary><h2>Troubleshooting</h2></summary>

| Problem | Fix |
|---------|-----|
| **"The command line is too long"** | Update to latest — prompts are piped via stdin now |
| **Hook never triggers** | Check `settings.json` has the hook under `Stop` event |
| **Stuck in review loop** | Delete `.viper/state.json` or set `"approved": true` in it |
| **Codex not found** | Run `codex --version`. Ensure npm global bin is in PATH |
| **Rate limited** | Run `codex logout && codex login` to refresh auth. Requires ChatGPT Plus |
| **Encoding errors (Windows)** | Update to latest — forces UTF-8 for subprocess output |

</details>

---

<details>
<summary><h2>Limitations</h2></summary>

**With a review brief, Viper catches architectural problems too.** When Claude writes `.viper/brief.md`, Codex can verify the implementation matches the intent — wrong abstractions, misunderstood requirements, missing functionality. Without the brief, it still catches code-level bugs but can't review against intent.

**Codex has full filesystem access** (read-only) and can follow imports, read related files, and run `git diff`. This is not a truncated-snippet reviewer.

**Your code is sent to OpenAI.** Codex CLI runs locally but calls OpenAI's API. The OpenRouter fallback also sends code externally. If you're working on proprietary code, evaluate whether that's acceptable.

**This does not replace code review.** It's an automated QC gate — but not a shallow one. Codex has full filesystem access, reads the actual files, follows imports, checks callers, and reviews against the stated intent. Treat it like a senior dev diving into your code, not just glancing at the diff.

</details>

---

## License

MIT
