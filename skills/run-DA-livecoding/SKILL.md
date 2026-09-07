---
name: run-DA-livecoding
description: >
  This skill should be used when the user wants to run a live SQL interview —
  e.g. "run a SQL interview", "start the livecoding interview", "SQL
  livecoding session", "prepara la entrevista de SQL", "corre la entrevista de
  livecoding", "give me the interview link", "list the SQL exercises", "show
  me the interview solutions", "what has the candidate run", or "end the
  interview". It hands the interviewer a ready-to-paste terminal command that
  starts a local, sandboxed SQL exercise app behind an ephemeral Cloudflare
  quick tunnel, and helps with exercises, solutions and troubleshooting.
metadata:
  version: "0.4.0"
---

# Qubika SQL Interview

Live SQL interviews from the interviewer's own laptop. The app (bundled in
`app/` next to this SKILL.md) serves SQL exercises against an embedded,
hardened DuckDB engine and prints every query the candidate runs. The public
link dies the moment the server process stops.

## The one rule: the interviewer owns the process

**Never start the app yourself — not in the foreground, not in the background,
not with `nohup`.** The interview server must run in the interviewer's own
terminal window, where they can see the live queries and stop it with Ctrl+C.
A server started from a chat session dies with that session and takes the
candidate's link down mid-interview.

Your job is to hand over the exact command, explain the flow, and help with
everything around it. The terminal banner itself carries the full
instructions, so you do not need to repeat them at length.

## Start an interview

1. Resolve `APP` = the absolute path of the `app/` directory next to this
   SKILL.md.
2. Preflight (these are read-only, safe to run yourself):
   - `which cloudflared` — if missing: `brew install cloudflared` (macOS).
   - `python3 -c "import duckdb"` — if missing:
     `python3 -m pip install --break-system-packages duckdb`.
   Fix these before handing over the command; a failure mid-interview is
   much worse than a 30-second check now.
3. Give the user this command in a `bash` code block, with `APP` already
   expanded to the real absolute path — one command, nothing else in the
   block, so it is one click to run:

   ```bash
   python3 "<APP>/serve.py"
   ```

   Tell them to run it **in their own terminal**. Optional flags, mention only
   if relevant: `--exercise exercise_01,exercise_02` (subset), `--ttl
   <minutes>` (default 180), `--port <port>` (default 8765), `--no-tunnel`
   (localhost only, candidate cannot reach it).
4. Tell them what to expect: in ~10 seconds the terminal prints a banner with
   the candidate link and the 4 numbered steps. They copy the link into the
   Meet chat, the candidate shares their screen, and every query streams into
   that window. Session logs go to `~/qubika-sql-interviews/sessions/` (unless
   `DATA_ANALYTICS_LIVECODING_DATA_DIR` says otherwise).

## End an interview

**Ctrl+C in the terminal window where it is running.** That is the whole
procedure — it kills the link instantly and prints a summary with the log
path. Say exactly that; do not run anything.

Only if the user cannot find or reach that window: identify the right process
first, then signal it by PID. **Never `pkill -f serve.py`** — it matches every
serve.py on the machine (the dev checkout and the plugin copy included) and
would end someone else's live interview.

```bash
pgrep -fl serve.py
```

The banner of each session prints its own `pid` and port, so the user can tell
them apart. Have them confirm which PID is theirs, then:

```bash
kill -INT <PID>
```

## During an interview

- **"What has the candidate run?"** — the live feed is in their terminal. If
  they want it here, read the newest session log:
  `~/qubika-sql-interviews/sessions/<newest>/session.jsonl` (one JSON line per
  query: SQL, status, rows, duration; plus each exercise's final editor text).
- **"Is my answer right?"** — compare against the reference solution (below).
- Never open or interact with the candidate link yourself; it is their
  session.

## Exercises and solutions

- `python3 "<APP>/serve.py" --list` shows each exercise with its
  interviewer-only `focus` (the technique it tests). The seed set is a
  difficulty ladder: COUNT → JOIN+filter → GROUP BY per country → HAVING →
  Top-N. Candidate-facing names are deliberately neutral ("Exercise 1"…).
- Reference answers: `<APP>/exercises/<name>/solution.sql` — never served to
  the candidate. When asked for "the solutions", read those files; you can
  also run them with duckdb against the seed CSVs to show expected outputs.
- To add or edit exercises: each is a folder under `<APP>/exercises/` with
  `exercise.json`, `statement.md`, `schema.sql`, `solution.sql` and
  `data/*.csv` (synthetic data ONLY — hard rule). Authoring rules: titles and
  statements must not hint at the solution technique (that goes in `focus`),
  statements must be explicit about the expected output shape, and metric
  names must match between statement, schema and solution. The seed set is
  generated by `<APP>/gen_exercises.py`.

## Troubleshooting

- **Port already in use** — a previous server is still running in another
  window. Either Ctrl+C there, or rerun with `--port 8766`. Two interviews can
  legitimately run at once, which is why killing by name is forbidden above.
- **Candidate's network blocks `trycloudflare.com`** — rerun with
  `--no-tunnel`, the interviewer shares their screen with the local link
  open, and the candidate dictates SQL.
- **Link stopped working mid-interview** — the tunnel dropped (the terminal
  prints a warning). Ctrl+C and start again; the new link must be re-pasted
  in the Meet chat. The candidate's typed SQL is not carried over.
- Full details, security model and E2E checklist: `<APP>/README.md`. Its
  commands are written to be run from the app directory; rewrite any command
  you relay as `python3 "<APP>/…"` with `APP` expanded.
- Sanity check after changes: `python3 -m unittest discover -s "<APP>/tests"`.

## Security notes (already enforced by the app — do not weaken)

Every route is token-gated; the DuckDB connection is read-only with external
access disabled (candidates cannot read local files, write, or change
config); 30s query timeout, 200-row cap, rate limiting. The candidate only
ever sees the exercise content.
