# Qubika SQL Interview

Local **livecoding SQL interview** tool. The interviewer runs it on their
laptop with one command; the app generates an **ephemeral public link**
(a Cloudflare quick tunnel or localhost.run — both free, no account) to paste
in the Google Meet chat.
The candidate opens the link and sees the exercise statement, the table
schemas with sample rows, a SQL editor, a Run button and the results. The
candidate shares their screen over Meet; the interviewer's terminal shows
every executed query live, with whether its result matches the reference
solution and short SQL style flags. **Ctrl+C ends the session and the link
dies instantly.**

Engine: embedded DuckDB (zero cost, no network, no Databricks). Data is 100%
synthetic — hard rule: never load real data into `exercises/`.

## Setup (one time)

```bash
brew install cloudflared   # optional: without it the link comes from localhost.run
python3 -m pip install --break-system-packages duckdb
```

## Usage

Run it **in your own terminal window** and leave it open — that window *is*
the interview console:

```bash
python3 serve.py --candidate "Full Name"
```

`--candidate` is optional: the name is stored in the log, shown in the banner
and appended as a slug to the session folder name. All commands here assume the app directory is your working directory. If it
isn't — e.g. the app is installed as a plugin under
`~/.claude/plugins/…/skills/run-DA-livecoding/app/` — use the absolute path
instead: `python3 "<APP>/serve.py"`. `serve.py` resolves its exercises, static
files and log directory from its own location, so it runs correctly from any
working directory.

The startup banner carries the whole procedure: the candidate link to paste
in the Meet chat, the reminder to have the candidate share their screen, where
the live query feed appears, and that **Ctrl+C in that window ends the
session** (the link dies instantly). Nothing else needs to be remembered.

Never start it in the background (`&`, `nohup`) or from a chat session: when
that parent dies it takes the candidate's link with it, mid-interview.

Other invocations:

```bash
python3 serve.py --list                                  # exercises + what each tests
python3 serve.py --exercise exercise_01,exercise_02      # subset
python3 serve.py --tunnel localhost.run                  # force one link provider
python3 serve.py --no-tunnel                             # localhost only (testing)
```

## The public link

Two providers are started at the same time — a **Cloudflare quick tunnel**
(`*.trycloudflare.com`, needs `cloudflared`) and **localhost.run**
(`*.lhr.life`, an SSH remote forward, needs nothing but `ssh`) — and the app
fetches its own public URL until this server answers through it. The first
link that does is shown; the other tunnel is shut down. A link is therefore
never printed on faith: Cloudflare hands out a hostname before its DNS record
exists (routinely ~20 s, and during an outage never), which used to put a dead
link in the Meet chat. Progress is printed every 5 s while waiting.
`--tunnel <provider>` forces one; if every provider fails the error lists each
reason and points at `--no-tunnel`. Despite its name, localhost.run is a public
service: the link works from anywhere.

All options, combinable:

| Option | Effect |
| --- | --- |
| `--candidate "Full Name"` | Puts the name in the log, the banner, the farewell and the session folder name. Optional, but you will want it. |
| `--exercise exercise_01,exercise_03` | Serve a subset. Default: all five. |
| `--tunnel cloudflare` / `--tunnel localhost.run` | Force one link provider. Default: start both and keep the first one that proves reachable. |
| `--no-tunnel` | Localhost only, no public link. The fallback when no provider works: share your own screen and the candidate dictates the SQL. |
| `--ttl 120` | Session lifetime in minutes (default 180) — a backstop in case you forget Ctrl+C. |
| `--port 8766` | Port (default 8765). Only needed when a previous session is still running. |
| `--list` | Print the exercises with the technique each one tests, then exit. |

## The live feed

Each run is one line: time, exercise, status, rows, ms, **result**, **style**
and the start of the query. Both new columns are interviewer-only — they are
never part of any HTTP response.

`result` compares the candidate's **result set** with the result of
`solution.sql`, never the SQL text, so a CTE, a subquery or different aliases
all pass:

| label | meaning |
| --- | --- |
| `PASS` | same data as the reference |
| `NEAR:cols` | right data, columns in another order |
| `NEAR:order` | right rows, wrong row order (only for exercises whose statement fixes an order) |
| `FAIL:cols` | wrong number of columns |
| `FAIL:rows` | wrong number of rows (says extra / missing / duplicates) |
| `FAIL:vals` | right shape, wrong values or wrong column type |
| `n/a` | no usable reference for this exercise |
| `?` | the check itself failed; the reason line says why |
| *(blank)* | the query failed, or it is an EXPLAIN/DESCRIBE, not an answer |

`FAIL` and `NEAR` print the reason on an indented second line. Column names
are ignored; numbers are compared after rounding to the exercise's `decimals`
(2 by default), so `SUM(...)`, `SUM(...)::DOUBLE` and a VARCHAR cast agree; a
date equals its ISO string; only the ORDER BY key columns are compared for
order, so tied rows may come back either way.

`style` is `ok` or a fixed set of flags:

| flag | meaning |
| --- | --- |
| `T` | indented with tabs |
| `I` | sloppy indentation: a one-space indent, or two levels one space apart |
| `L` | a line over 100 chars, or a whole multi-clause query on one line |
| `S` | missing space around an operator or comma |
| `*` | `SELECT *` (`COUNT(*)` does not count) |
| `A` | expression column with no alias (`count(id)`) |
| *(blank)* | the style check was unavailable — not the same as `ok` |

Keyword casing is deliberately not judged: consistent lowercase and consistent
uppercase are both fine. `I` only reports indentation that is genuinely sloppy
(a one-space indent, or two levels one space apart), so river and
aligned-under-a-paren layouts pass. Full messages for every flag are in the log.

The banner's `Checks` line says how many exercises the result column can judge
and names the exceptions: `off` (no usable reference, so the cell shows `n/a`)
and `degraded` (a verdict is still given, but part of the check — so far only
the row order — is not being applied).

A Cloudflare subdomain is 4 random words; if an ugly or alarming name comes
up (e.g. "spyware-…"), the app discards it and mints another one before
showing it. localhost.run names are hexadecimal.

Everything is kept in a JSONL log for later review — queries, results,
rejections, each run's `check` verdict and `style` flags, the candidate's name
in `session_start`, and the final SQL of each exercise. It lands in
`<workspace>/data/data_analytics_livecoding/sessions/<ts>/` in this dev
checkout, or `~/qubika-sql-interviews/sessions/<ts>/` when the app is
installed elsewhere (e.g. as a plugin). Override with
`DATA_ANALYTICS_LIVECODING_DATA_DIR`.

## Suggested interview flow

1. 5 minutes before: run `serve.py` (see Usage for the path form) and check
   the link opens (ideally
   from your phone with WiFi off — that proves real remote access).
2. Paste the candidate link in the Meet chat and ask them to share their screen.
3. The 5 exercises are a difficulty ladder: count → JOIN+filter → GROUP BY
   per country → HAVING → Top-N. Candidate-facing names are deliberately
   neutral ("Exercise 1"…); the technique per exercise is in the `focus`
   field shown by `--list`, and each folder has a reference `solution.sql`
   (never served to the candidate).
4. When done: Ctrl+C. The link is dead and the log is saved.

**Plan B** if the candidate's corporate network blocks one provider's domain:
rerun with the other (`--tunnel localhost.run` / `--tunnel cloudflare`). If
both are blocked: `--no-tunnel`, share YOUR screen with the local page open
and have the candidate dictate the SQL.

## Exercise format

One folder per exercise under `exercises/`:

```
exercises/my_exercise/
├── exercise.json    {"title", "difficulty", "focus", "tables": [{"name","csv"}], "sample_rows", "check"}
├── statement.md     the prompt (simple markdown: headings, lists, **bold**, `code`)
├── schema.sql       CREATE TABLEs separated by ';' (standard types)
├── solution.sql     reference answer (interviewer only; also defines the expected result)
└── data/<table>.csv one CSV per table, with header (synthetic data ONLY)
```

The optional `check` block tunes the result column:

| key | default | meaning |
| --- | --- | --- |
| `enabled` | `true` | `false` switches the result column off for this exercise |
| `ordered` | `false` | `true` when the statement asks for a specific row order; naming `order_by` turns this on by itself |
| `order_by` | `[]` | solution column names that define that order (ties tolerated); `[]` with `ordered` means the whole row order must match. A name that is not a solution column is reported as `degraded` at startup |
| `decimals` | `2` | numbers are compared after rounding to this many decimals |

`solution.sql` runs once at startup on the same hardened connection. If it is
missing, fails, or returns more than 200 rows, the result column is switched
off for that exercise (with a warning) and the interview runs normally.

Authoring rules: candidate-facing titles and statements must not hint at the
solution technique (that goes in `focus`, interviewer-only); statements must
be explicit about the expected output shape; metric names must be consistent
between statement, schema and solution. The seed exercises are generated by
`gen_exercises.py` — edit and re-run it to tweak them.

It shows up automatically in `--list` and in the session. Porting the old
db-recruiter exercises is just a matter of dumping them into this format.

## Security (threat model: semi-trusted candidate + internet scanners)

- **Everything is token-gated**: every route requires `?t=<token>` (128 bits,
  constant-time comparison). Any other route → 404 — which is also how the
  startup check recognises its own server through the tunnel, without ever
  fetching the candidate page. The server only listens on 127.0.0.1; the
  tunnel is the only entry point. The link dies with the process.
- **The DuckDB connection is the real barrier**, not text validation:
  read-only + `enable_external_access=false` (blocks `read_csv('/etc/passwd')`,
  `ATTACH`, `COPY TO`, extensions) + `lock_configuration` (blocks SET/PRAGMA)
  + `memory_limit 512MB`. DB files live in a neutral temp dir so no path can
  leak the interviewer's username.
- Statement validation with DuckDB's real parser (single statement,
  SELECT/EXPLAIN only — DESCRIBE/SHOW count as SELECT).
- 30s timeout per query (interrupt), 200-row result cap, 2,000-char cell cap,
  1 concurrent query, min 2s between runs, 256 KB max body.
- Automatic `caffeinate` so the laptop doesn't sleep mid-interview.
- Result verdicts, style flags and the candidate's name never leave the
  process: `/api/run` returns exactly `{status, columns, rows, row_count,
  truncated, duration_ms, error}`.

## Tests

```bash
python3 -m unittest discover -s tests   # or: -s "<APP>/tests"
```

## Manual E2E checklist (before the first real interview)

1. `--no-tunnel`: tabs, run all 5 exercises, a refresh doesn't lose the SQL,
   Ctrl+C stops it.
2. With tunnel: open the link from your phone with WiFi off, run a query,
   Ctrl+C and verify the link is dead. Try `--tunnel cloudflare` and
   `--tunnel localhost.run` once each.
3. Paste attacks: `DROP TABLE orders` · `SELECT 1; SELECT 2` ·
   `SELECT * FROM read_csv('/etc/passwd')` · spam Run — all rejected and
   logged.
4. `--ttl 1` and watch the expiry ("Session ended" banner).
5. Answer one exercise wrongly (drop the `LIMIT`, return the id instead of the
   name) and then correctly but written differently (a CTE, other aliases):
   `FAIL:*` with a reason, then `PASS`.
6. `--candidate "Ana Test"`: the banner shows the name, the session folder ends
   in `_ana-test`, and Ctrl+C names the candidate in the farewell.

## Future evolution (deliberately out of v1)

- **Branded link (`sql-livecoding.qubika.com`)**: free quick tunnels get a
  random 4-word subdomain that cannot be chosen (the app already discards
  off-putting names and mints a new one). A fixed, Qubika-branded URL requires
  a Cloudflare *named tunnel*: a free Cloudflare account + a Qubika-owned
  domain/subdomain routed through it. ~1h of one-time setup, then
  `cloudflared tunnel run` with a config file replaces the quick tunnel.

- **Databricks SQL Warehouse engine**: same exercise format; reuse the
  CLI-subprocess pattern from `projects/bench_projections/fetch_bench.py`
  against the Statement Execution API (`on_wait_timeout: CANCEL`,
  `row_limit`), profile `qubika-training`. The app already isolates the
  engine in `engine.py`.
- Live web mirror (SSE) + admin page with "End session", if someday the
  candidate doesn't share their screen.
- Port the historical db-recruiter exercises.
