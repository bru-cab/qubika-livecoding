# Qubika SQL Interview (plugin `qubika-livecoding`)

Run **live SQL interviews** from your own laptop — no third-party product, no
per-seat cost. One command starts a small local web app with 5 SQL exercises
(statement + schemas + editor + Run button + results) on an embedded DuckDB
engine, and exposes it through an **ephemeral public link** (Cloudflare quick
tunnel, free, no account) that you paste in the Google Meet chat. The
candidate shares their screen; your terminal shows every query they run.
**Stopping the process kills the link instantly**, and a JSONL log of the full
session is saved for later review.

## Components

| Component | Purpose |
| --- | --- |
| Skill: `run-DA-livecoding` | Preflight + hands you the one-line command to start an interview in your terminal; also lists exercises, shows solutions, reads session logs, helps add exercises and troubleshoot. Invoke it as `/qubika-livecoding:run-DA-livecoding` or in natural language. The full app ships inside the skill (`skills/run-DA-livecoding/app/`). |

## Install (as a Claude Code plugin)

The archive doubles as a single-plugin local marketplace
(`.claude-plugin/marketplace.json` is included). From the directory you
unzipped it into:

```bash
claude plugin marketplace add <install-dir> --scope project
```

then install `qubika-livecoding` from that marketplace. If the `claude
plugin` CLI is unavailable (e.g. managed-settings auth errors), declare it
directly in the project's `.claude/settings.json` instead — this is
equivalent and fully supported:

```json
{
  "extraKnownMarketplaces": {
    "qubika-livecoding-local": {
      "source": { "source": "directory", "path": "<install-dir>" }
    }
  },
  "enabledPlugins": { "qubika-livecoding@qubika-livecoding-local": true }
}
```

Restart the Claude Code session afterwards — plugins load at startup. Verify
with `/qubika-livecoding:run-DA-livecoding` or by asking "list the SQL exercises".

## Setup (one time)

```bash
brew install cloudflared
```

```bash
python3 -m pip install --break-system-packages duckdb
```

## Usage

**The interview runs in your own terminal, and you stop it there.** Run
**`/qubika-livecoding:run-DA-livecoding`** (or just ask: *"run a SQL interview"*) and you
get a one-line command to paste into your terminal. From that point on, that
window is the interview: it prints the candidate link, the steps, and every
query the candidate runs. **Ctrl+C in that window ends the interview** — the
link dies instantly.

Claude never launches the server itself, on purpose: a server started from a
chat session dies with that session and would drop the candidate's link
mid-interview.

Other things to ask Claude, any time:

- **"List the SQL exercises"** — the difficulty ladder with the technique each
  one tests (interviewer-only info).
- **"Show me the solutions"** — reference answers with expected outputs.
- **"What has the candidate run?"** — reads the live session log.
- **"Add an exercise about window functions"** — scaffolds it in the right
  format.

Session logs are written to `~/qubika-sql-interviews/sessions/` (never inside
the plugin).

## Security model (short version)

Every route requires a 128-bit session token; anything else is a 404. The
candidate's SQL runs on a read-only DuckDB connection with external access
disabled — no local file reads, no writes, no config changes — plus a 30s
query timeout, 200-row result cap and rate limiting. Exercise data is 100%
synthetic (hard rule for any exercise you add). The tunnel link dies with the
process and subdomains are never reused.

## Notes

- The tunnel subdomain is random (Cloudflare quick tunnels don't allow custom
  names); the app auto-discards off-putting names. A fixed branded URL
  (e.g. `sql-livecoding.qubika.com`) requires a Cloudflare named tunnel —
  see the app README's "Future evolution".
- Plan B if the candidate's network blocks `trycloudflare.com`: run with
  `--no-tunnel` and share your own screen.
