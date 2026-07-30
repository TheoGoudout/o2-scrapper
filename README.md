# o2-scrapper

Mirrors your planned services (ménage, garde d'enfants, jardinage, …) from the O2
customer extranet at [client.o2.fr](https://client.o2.fr) into a dedicated Google
Calendar.

Services change state over time — cancelled, reprogrammed, reassigned to a
different intervenant — so existing calendar events are **updated in place, never
recreated**. Cancelled services stay visible and marked; services that disappear
from the O2 planning are removed.

## Install

Python 3.9+ (uses `zoneinfo`). Use a virtualenv — it keeps the Google libraries
away from your system Python, which also sidesteps the `cryptography` clash noted
under [Troubleshooting](#troubleshooting).

```bash
git clone https://github.com/TheoGoudout/o2-scrapper.git
cd o2-scrapper

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

Check it works before touching Google:

```bash
python -m o2sync --version
python -m o2sync --help
```

Every later `python -m o2sync …` command assumes the virtualenv is active (you'll
see `(.venv)` in your prompt). If you open a new terminal, re-run the `source` line.

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

Same window flags as `fetch`. The target calendar comes from `--calendar-id` or
`$O2_CALENDAR_ID` — run [`calendar-init`](#calendar-init--create-the-calendar-once)
once to get it. `--interval` turns this into a long-running loop, see
[Running it on a schedule](#running-it-on-a-schedule).

### `calendar-init` — create the calendar, once

```bash
python -m o2sync calendar-init   # prints the O2_CALENDAR_ID to save
```

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

The consent screen moved: it is now *APIs & Services → **Google Auth Platform***,
split into **Branding**, **Audience**, **Clients** and **Data Access**. The old
single "OAuth consent screen" page no longer exists.

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project.
2. *APIs & Services → Library* → enable the **Google Calendar API**.
3. *Google Auth Platform → **Branding***: set an app name and your support e-mail.
   You cannot create a client until this is filled in.
4. *Google Auth Platform → **Audience***: user type **External**, then click
   **Publish app** so the status reads *In production* — **not** *Testing*. See the
   warning below; this matters more than it looks.
5. *Google Auth Platform → **Clients*** → *Create client* → application type
   **Desktop app** → download the JSON and save it as `credentials.json` next to
   the code.
6. Run `python -m o2sync auth`.

> **Publish the app, don't leave it in Testing.** While the publishing status is
> *Testing*, Google expires every refresh token after **7 days**
> ([docs](https://support.google.com/cloud/answer/15549945),
> [explainer](https://www.unipile.com/google-oauth-refresh-token/)). The sync would
> work for a week and then fail with `invalid_grant` until you re-authorised by
> hand — which is exactly what you don't want on a server. *In production* issues
> refresh tokens that do not expire on a timer.

Because the app is published but unverified, the consent screen will say
**"Google hasn't verified this app"**. That is expected for a personal tool: click
**Advanced → Go to … (unsafe)** and continue. Unverified apps are capped at 100
users, which is 99 more than you need. You do not need to submit for verification,
and you do not need to add test users at all once the app is published.

### No computer? Browser-only setup

`python -m o2sync auth` needs a browser *and* a loopback listener on the same
machine, so it can't be done from a phone. You can get the same three credentials
using [Google's OAuth Playground](https://developers.google.com/oauthplayground/),
which needs nothing installed — the tool never has to run locally at all, because
the server reads its Google credentials from environment variables.

Instead of steps 5–6 above:

1. *Google Auth Platform → **Data Access*** → *Add or remove scopes* → paste
   `https://www.googleapis.com/auth/calendar.app.created` and save.
2. *Google Auth Platform → **Clients*** → *Create client* → application type
   **Web application** (not Desktop — the Playground needs a real redirect URI).
   Under *Authorised redirect URIs* add exactly:
   `https://developers.google.com/oauthplayground`
   Save, and keep the **Client ID** and **Client secret**.
3. Open [the Playground](https://developers.google.com/oauthplayground/), tap the
   **⚙ gear** (top right) and set:
   - *OAuth flow*: **Server-side**
   - *Access type*: **Offline** — without this you get no refresh token
   - tick **Use your own OAuth credentials**, and paste the client ID and secret
4. In *Step 1*, in the "Input your own scopes" box, enter
   `https://www.googleapis.com/auth/calendar.app.created` → **Authorize APIs**.
   Sign in and accept (via **Advanced → Go to … (unsafe)** on the unverified-app
   warning).
5. In *Step 2*, tap **Exchange authorization code for tokens** and copy the
   **refresh token**.
6. In Coolify's *Environment Variables*, set `O2_GOOGLE_CLIENT_ID`,
   `O2_GOOGLE_CLIENT_SECRET` and `O2_GOOGLE_REFRESH_TOKEN` to those three values,
   along with `O2_EMAIL` and `O2_PASSWORD`.
7. Bootstrap the calendar once — in Coolify, add a **Scheduled Task** (or use the
   container *Terminal*) running `python -m o2sync calendar-init`, and copy the
   `O2_CALENDAR_ID=…` it prints into the environment variables too. See
   [The calendar id](#the-calendar-id) for why this step exists. Then deploy.

A Web-application client works fine here: refreshing a token only needs the client
id, secret and refresh token, and is identical for both client types. You never
need `credentials.json` or `token.json` on the server. Publishing the app (step 4
above) still matters — a *Testing* app's refresh token dies after 7 days either way.

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
| `O2_CALENDAR_ID` | which calendar to sync into — see [below](#the-calendar-id) |
| `O2_SYNC_INTERVAL` | re-sync every N (`6h`); makes `sync` a long-running loop |
| `O2_HEARTBEAT_FILE` | where the loop writes its liveness file |

### The calendar id

`calendar.app.created` lets this tool **create** a calendar but not **list** any:
`calendarList.list` requires `calendar.readonly` / `calendar` /
`calendar.calendarlist*`, and deliberately none of those are requested. So the tool
cannot go looking for its own calendar by name — the id has to be recorded once:

```bash
python -m o2sync calendar-init      # prints: O2_CALENDAR_ID=…@group.calendar.google.com
```

Set that as `O2_CALENDAR_ID` (or pass `--calendar-id`) and every later run targets it
directly. If your credentials happen to carry a broader scope, discovery by name is
tried first and this is optional.

`sync` never creates a calendar as a fallback: with no way to find it again, it would
make a new one on every single cycle.

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
4. Run `python -m o2sync calendar-init` once (Coolify *Terminal*, or a one-off
   Scheduled Task) and add the `O2_CALENDAR_ID` it prints to the environment.
5. Deploy. The container should stay **running**, logging one summary line per
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

90 tests, no network: normalisation, the calendar event mapping, every branch of the
diff engine including the deletion fences and the `409` fallback, calendar resolution
(including refusing to create a duplicate when listing is forbidden), the scheduling
loop (interval parsing, surviving a transient outage, fatal-on-bad-credentials,
`SIGTERM` shutdown, heartbeat staleness), and credential handling (wrong OAuth client
type, corrupt token, headless machine).

CI runs them on Python 3.9–3.13, lints with `ruff`, builds the Docker image and
smoke-tests the container. Fixtures
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
