# Viper Review Brief

Before completing any task that modifies code, write a review brief to `.viper/brief.md`. This file is read by the Viper stop hook and passed to a second AI reviewer (Codex) that QCs your work before you can finish.

The brief should contain:

```markdown
## Task
What was requested — the requirement or goal.

## Approach
What you did and why. Key architectural decisions and tradeoffs.

## Changed Files
- `path/to/file.py` — what this change does
- `path/to/other.js` — what this change does

## Edge Cases
What you considered, what you didn't, and any known limitations.
```

Keep it concise. The reviewer uses this to verify your implementation matches the intent — not just that the code compiles, but that it solves the right problem the right way.
