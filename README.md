# Viper 2.0

**Automated code review gate for Claude Code** — Viper intercepts Claude before it finishes and has a second AI (OpenAI Codex) review the changes. If issues are found, Claude is blocked from stopping and must fix them first.

## How It Works

Viper is a **Claude Code stop hook**. Here's the full flow:

1. **Claude finishes working** and is about to stop responding.
2. **Claude Code triggers the stop hook**, passing the session context to Viper via stdin.
3. **Viper checks git** for any changed, staged, or untracked files in the working directory.
4. **If files changed**, Viper reads their contents (up to 15 files, 20KB total) and builds a review prompt.
5. **The prompt is piped to `codex exec`** (OpenAI Codex CLI) via stdin, running in read-only sandbox mode.
6. **Codex reviews the code** for bugs, logic errors, security issues, and missing edge cases.
7. **Based on the verdict:**
   - **APPROVED** — Claude is allowed to stop. State is cached so the same session isn't re-reviewed.
   - **ISSUES FOUND** — Viper **blocks** Claude from stopping and injects the review feedback. Claude sees the issues and automatically fixes them.
8. **The cycle repeats** up to `max_review_cycles` (default: 3) to prevent infinite loops.

If Codex CLI is not installed, Viper falls back to the **OpenRouter API** (configurable model, default GPT-4o).

```
Claude works on code
        |
        v
   Claude stops
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
  Read file contents
        |
        v
  Send to Codex CLI (stdin)
        |
        v
  APPROVED? --YES--> Claude stops normally
        |
        NO
        |
        v
  Block Claude + inject feedback
        |
        v
  Claude fixes issues, tries to stop again
        |
        v
  (cycle repeats up to max_review_cycles)
```

## Getting Started

### Prerequisites

- **Python 3.8+**
- **Claude Code** CLI installed and working
- **Codex CLI** (`npm install -g @openai/codex`) — or an OpenRouter API key for the fallback

### Installation

1. **Clone the repo** into your Claude Code hooks directory:

   ```bash
   # Windows
   git clone https://github.com/postgigg/viper-2.0.git "%APPDATA%\.claude\hooks\viper"

   # macOS/Linux
   git clone https://github.com/postgigg/viper-2.0.git ~/.claude/hooks/viper
   ```

2. **Register the stop hook** in your Claude Code settings (`~/.claude/settings.json`):

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

   On Windows, use the batch wrapper instead:

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

3. **Install Codex CLI** (recommended):

   ```bash
   npm install -g @openai/codex
   ```

   Make sure your `OPENAI_API_KEY` environment variable is set.

4. **(Optional) Configure OpenRouter fallback** — edit `config.json` and add your API key:

   ```json
   {
     "openrouter_api_key": "sk-or-..."
   }
   ```

### Configuration

Edit `config.json` in the viper directory:

| Key | Default | Description |
|-----|---------|-------------|
| `codex_timeout` | `180` | Seconds to wait for Codex CLI response |
| `max_review_cycles` | `3` | Max review/fix cycles before allowing stop |
| `openrouter_api_key` | `""` | OpenRouter API key (fallback when Codex unavailable) |
| `fallback_model` | `"openai/gpt-4o"` | Model to use via OpenRouter |
| `max_context_chars` | `20000` | Max total characters of file content to send for review |

### Troubleshooting

- **"The command line is too long"** — Update to the latest version. Viper 2.0 pipes prompts via stdin instead of command-line arguments.
- **Hook never triggers** — Verify the hook is registered in `settings.json` under the `Stop` event.
- **Stuck in a review loop** — Delete `.viper/state.json` in your project directory, or set `"approved": true` in it.
- **Codex not found** — Make sure `codex` is on your PATH. Run `codex --version` to verify. On Windows, the npm global bin (`%APPDATA%\npm`) must be in PATH.
- **Encoding errors on Windows** — Update to the latest version. Viper 2.0 forces UTF-8 encoding for subprocess output.

## How It Fails

Viper is designed to **fail open** — if anything goes wrong (no git repo, codex unavailable, API error, timeout), Claude is allowed to stop normally. Your workflow is never blocked by a broken review.

## License

MIT
