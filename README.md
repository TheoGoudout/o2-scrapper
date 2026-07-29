# o2-scrapper

Mirrors your planned services (ménage, garde d'enfants, jardinage, …) from the O2
customer extranet at [client.o2.fr](https://client.o2.fr) into a dedicated Google
Calendar.

Services change state over time — cancelled, reprogrammed, reassigned to a
different intervenant — so existing calendar events are **updated in place, never
recreated**. Cancelled services stay visible and marked; services that disappear
from the O2 planning are removed.

## Install

```bash
pip install -r requirements.txt
```

Python 3.9+ (uses `zoneinfo`).

## Quick start

```bash
cp .env.example .env && $EDITOR .env    # O2 credentials

python -m o2sync fetch --pretty         # 1. check the O2 side works
python -m o2sync auth                   # 2. one-time Google authorisation (opens a browser)
python -m o2sync sync --dry-run         # 3. see exactly what would change
python -m o2sync sync                   # 4. do it
```

`--dry-run` prints one line per change and writes nothing:

```
CREATE    2026-06-30 11:34 Ménage - repassage — Magda
CREATE    2026-07-14 11:30 [ANNULÉ] Ménage - repassage — Magda
UPDATE    2026-07-21 14:29 Ménage - repassage — Magda (content changed)
DELETE    [ANNULÉ] Ménage - repassage — Nadia (no longer in the O2 planning)
```

## Commands

### `fetch` — the planning as JSON

```bash
python -m o2sync fetch --pretty                       # to stdout
python -m o2sync fetch -o events.json --pretty        # to a file
python -m o2sync fetch --start 2026-01-01 --end 2026-12-31 -o year.json
python -m o2sync fetch --dump-raw raw.json -o events.json
```

Window defaults to 30 days back and 90 days forward (`--days-back` /
`--days-forward`). Other flags: `--include-raw`, `--no-labels`, `--chunk-days N`,
`-v` / `-q`.

### `sync` — into Google Calendar

```bash
python -m o2sync sync --dry-run
python -m o2sync sync --calendar-name "Prestations O2"
python -m o2sync sync --from-json raw.json --dry-run   # replay a dump, no O2 login
python -m o2sync sync --interval 6h                    # stay alive, re-sync every 6h
```

Same window flags as `fetch`. The dedicated calendar is created on first run
(named `O2 – Prestations` unless `--calendar-name` says otherwise); `--calendar-id`
targets one directly. `--interval` turns it into a long-running loop — see
[Running it on a schedule](#running-it-on-a-schedule).

### `healthcheck` — is the loop alive?

```bash
python -m o2sync healthcheck   # exit 0 while the --interval loop is on schedule
```

### `auth` — one-time Google authorisation

```bash
python -m o2sync auth              # opens a browser, stores token.json
python -m o2sync auth --print-env  # re-print the credentials as env vars
```

## Google setup

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project.
2. Enable the **Google Calendar API**.
3. Under *APIs & Services → Credentials*, create an **OAuth client ID** of type
   **Desktop app**, and download it as `credentials.json` next to the code.
4. On the OAuth consent screen, add your own address as a test user.
5. Run `python -m o2sync auth`.

The only scope requested is
[`calendar.app.created`](https://developers.google.com/calendar/api/auth): *make
secondary calendars, and manage events on them*. This tool therefore **cannot read
or modify your primary calendar, or any other calendar you own** — it can only
touch the one it created itself. The trade-off is that it cannot adopt a calendar
you made by hand; let it create its own.

## Credentials

| Variable | Purpose |
| --- | --- |
| `O2_EMAIL`, `O2_PASSWORD` | O2 account |
| `O2_GOOGLE_CLIENT_ID`, `O2_GOOGLE_CLIENT_SECRET`, `O2_GOOGLE_REFRESH_TOKEN` | Google, without any file on disk |
| `O2_GOOGLE_CLIENT_SECRETS`, `O2_GOOGLE_TOKEN` | alternative paths to `credentials.json` / `token.json` |
| `O2_SYNC_INTERVAL` | re-sync every N (`6h`); makes `sync` a long-running loop |
| `O2_HEARTBEAT_FILE` | where the loop writes its liveness file |

O2 credentials are read from flags, then the environment, then `.env`, then an
interactive prompt. Prefer the environment: command-line arguments are visible to
other processes.

Google credentials come from the three environment variables if present, otherwise
from `token.json`. The environment path needs no writable disk, which is what makes
scheduled containers work — see below.

## Running it on a schedule

`sync` runs once and exits by default. Give it `--interval` (or `$O2_SYNC_INTERVAL`)
and it stays alive, re-syncing on that schedule:

```bash
python -m o2sync sync --interval 6h
```

In loop mode it survives transient failures — an O2 or Google outage is logged and
retried at the next cycle rather than killing the process — and it stops promptly
and cleanly on `SIGTERM`. Bad credentials are treated as fatal instead: it exits 2
rather than hammering the login endpoint until somebody notices.

### Coolify

**Why a one-shot container does not work here:** Coolify hardcodes
`restart: unless-stopped` on applications
([discussion](https://github.com/coollabsio/coolify/discussions/3447)), so a
container that syncs and exits looks like a crash and gets restarted in a loop
forever. Coolify's own *Scheduled Tasks* can't rescue that either — they are
`docker exec` into an **already-running** container
([discussion](https://github.com/coollabsio/coolify/discussions/3152),
[issue](https://github.com/coollabsio/coolify/issues/8500)). So the container has
to stay up and own its schedule, which is what `--interval` is for. The bundled
`Dockerfile` defaults to it.

1. Locally, once: `python -m o2sync auth`, then `python -m o2sync auth --print-env`.
2. In Coolify: **+ New → Resource → your Git repository**, build pack
   **Docker Compose** (the repo has a `docker-compose.yaml`).
3. In *Environment Variables*, set `O2_EMAIL`, `O2_PASSWORD` and the three
   `O2_GOOGLE_*` values from step 1. Optionally `O2_SYNC_INTERVAL` (default `6h`).
4. Deploy. The container should stay **running**, logging one summary line per
   cycle.

The container needs no volumes and no ports — sync state lives in the Google
Calendar itself — and runs read-only as a non-root user.

To force a sync between cycles, add a Coolify **Scheduled Task** with command
`python -m o2sync sync` (it `docker exec`s into the running container). Coolify
accepts standard five-field cron plus shorthands like `@daily`
([syntax](https://coolify.io/docs/knowledge-base/cron-syntax)).

### Health check

The image ships a `HEALTHCHECK` that runs `python -m o2sync healthcheck`. The loop
writes a small JSON heartbeat after every cycle, recording the interval it is
running at, and the check works out its own staleness allowance from that
(2 × interval + 5 min) — so it stays correct whatever you set `O2_SYNC_INTERVAL` to.

It reports **liveness, not last-sync-success**: a failed cycle still refreshes the
heartbeat, because restarting the container cannot fix O2 or Google being down.
Only a genuinely wedged loop goes unhealthy. Check it by hand with:

```bash
python -m o2sync healthcheck        # "alive, last cycle 42s ago (ok)"
```

If Coolify's proxy complains about health checks, disable them in the UI — this is
not a web service and nothing routes to it.

### Plain cron

If you'd rather not keep a process alive, the one-shot mode is still there:

```cron
0 */6 * * * cd /opt/o2-scrapper && /usr/bin/python3 -m o2sync sync -q >> /var/log/o2sync.log 2>&1
```

### n8n

Two options, depending on how you self-host it:

- **Execute Command node** — if n8n runs where this code is installed, schedule a
  Cron trigger into an Execute Command node running `python -m o2sync sync` (the
  one-shot mode, no `--interval`). Simple, and keeps the tested logic. Not available
  on n8n Cloud.
- **Rebuild the flow natively** — n8n has HTTP Request nodes and a Google Calendar
  node, so you *can* rebuild this without Python: POST `ask_login`, POST
  `get_planning_events`, then a Code node for the diff. Be aware that the diff is
  the hard part — matching on the O2 `eventId`, comparing a content hash, and
  fencing deletions to the queried window — and that is exactly what
  `o2sync/sync.py` already does and has tests for. Rebuilding it in a Code node
  means reimplementing that logic without the tests.

### Make.com

Honestly: a poor fit. There is no arbitrary code execution, so the whole diff
engine would have to be rebuilt out of Data Store modules and routers. You would
spend more effort than the Coolify route and end up with something harder to
change. If you want to avoid a server entirely, a GitHub Actions scheduled workflow
with the five secrets is a closer match, and free.

## The O2 API

The extranet is a WordPress site whose planning screen is driven entirely by
`admin-ajax.php`, so **nothing here parses HTML**. The endpoints below were read
off the site's own production JavaScript
(`/wp-content/themes/extranet-client/js/scripts_common.js`) and then verified
against a live account.

All are `POST https://client.o2.fr/wp-admin/admin-ajax.php`, form-encoded:

| `action` | Parameters | Response |
| --- | --- | --- |
| `ask_login` | `login`, `pwd`, `updatePwd` | `{"state": "success"}` |
| `get_planning_events` | `startDate`, `endDate` (epoch **ms**) | JSON array, or `null`, or `fail` |
| `get_hs_type_label` | `HsTypeShortname` | plain-text label |
| `get_hs_level_label` | `HsLevelShortname` | plain-text label |
| `ask_logout` | — | — |

The session is a plain `PHPSESSID` cookie; there is no CSRF nonce. An
unauthenticated `get_planning_events` returns the literal string `fail`.

`ask_login` states: `success`, `unknown` (bad credentials), `UPDATE_PWD` (forced
password rotation), `customerActive` (account deactivated), `notCustomer`
(employee login used), `keycloak_unavailable` (transient backend failure). We
always send `updatePwd=false` so a script can never push the account into the
forced password-rotation flow.

### Event fields

```
eventId  eventName  eventType  eventDate  eventStart  eventTotalDurationPDF
eventPlanDur  eventDoneDur  eventStatus  houseServiceId  eventPostponed
hsType  hsLevel  idNewVisit  isVisitModificationExternal  customerId
lastName  firstName  civility  hsCliSerialNumber  hsCity
```

- Timestamps are epoch milliseconds; the extranet renders them in `Europe/Paris`
  (confirmed: `eventDate` lands exactly on Paris midnight).
- `eventStart` + `eventPlanDur` is the *planned* slot. `eventTotalDurationPDF` is
  an absolute timestamp of the actual clock-out, which differs by a few minutes on
  completed services.
- **`eventDoneDur` is absent** (not null) on cancelled services — the only field
  that is not always present.
- `eventPostponed` marks a service that was moved.
- `idNewVisit` is shared by every occurrence of the same recurring contract.

### Statuses

| Code | Meaning | Cancelled? |
| --- | --- | --- |
| `evt_status_planned` | Planifiée | no |
| `evt_status_in_progress` | En cours | no |
| `evt_status_done` | Réalisée | no |
| `annul_cli_ok` | Annulée dans les délais | yes |
| `annul_cli_HD` | Annulée hors délais | yes |
| `annul_vac_scol` | Annulée pour vacances scolaires | yes |
| `annul_empl` | Annulée par l'intervenant | yes |

`annul_empl` appears in live data but is **not** handled by the site's own
JavaScript, which renders it with its catch-all "Non réalisable". Unknown codes are
treated the same way here — labelled "Non réalisable", counted as cancelled, and
logged once per run so a new status doesn't pass unnoticed.

### Notes on behaviour

- A window covering five years returns exactly the same events as 59 chained
  31-day requests, so there is no server-side result cap to work around and the
  scraper issues a single request by default. `--chunk-days` remains available.
- The label endpoints echo the code back when they don't recognise it, so an
  unknown code is indistinguishable from a valid one — both are usable as labels.

## How the sync stays idempotent

- Each calendar event stores `o2EventId`, `o2ContentHash`, `o2Status` and an
  `o2Source` marker in `extendedProperties.private`. **The calendar is its own
  state store** — there is no local state file to lose or desync, and moving the
  tool to another machine changes nothing.
- Every read filters on `o2Source`, so the tool can only ever see, update or delete
  events it created.
- Calendar event ids are derived deterministically from the O2 `eventId`
  (`"o2" + sha1(...)`, which fits Google's `[a-v0-9]` id charset).
- An event is rewritten only when its stored `o2ContentHash` differs from the
  freshly computed one. The hash deliberately excludes labels, so a change in O2's
  wording cannot trigger pointless calendar updates.
- Deletion is fenced three ways: the event must carry our marker, its start must
  fall **inside the window that was queried**, and its `o2EventId` must be absent
  from the fetched planning. A narrow `--start/--end` run therefore cannot touch
  services outside it.
- Deleting a Google event reserves its id permanently, so creation falls back to
  an update on `409` — a service removed from O2 that later comes back syncs
  cleanly instead of failing forever.

Cancelled services keep their event but are retitled `[ANNULÉ] …`, greyed, marked
*free* so they don't make you look busy, and stripped of reminders so a service
that isn't happening never notifies you.

## Tests

```bash
python -m unittest discover -s tests -t .
```

72 tests, no network: normalisation, the calendar event mapping, every branch of the
diff engine including the deletion fences and the `409` fallback, and the scheduling
loop (interval parsing, surviving a transient outage, fatal-on-bad-credentials,
`SIGTERM` shutdown, heartbeat staleness). Fixtures
mirror a real payload but every value is synthetic — no personal data is committed
to this repository.

## Troubleshooting

- **`pyo3_runtime.PanicException` importing `cryptography`** — a distro-packaged
  `cryptography` is shadowing the one the Google libraries need. Fix with
  `pip install --upgrade --ignore-installed cryptography`, or use a virtualenv.
- **`Google authorisation needed`** (exit 2) — the token is missing, corrupt, or
  was revoked. Run `python -m o2sync auth` again.
- **Exit code 3** — O2 or Google was unreachable or failing. Transient; the next
  scheduled run will pick it up.

Exit codes: `0` success, `2` authentication problem (a human must act), `3` remote
service unreachable or failing (worth retrying), `1` anything else.

## Scope and etiquette

`robots.txt` on client.o2.fr disallows `/wp-admin/`. The account owner raised this
with O2, who confirmed that a single customer reading their own planning is fine.
This tool makes a handful of requests per run against one account, and is not
intended for anything broader.

Output files contain personal data (names of the people assigned to your home, your
city, contract references) and are git-ignored, as are all credential files.
