# o2-scrapper

Fetches your planned services (ménage, garde d'enfants, jardinage, …) from the O2
customer extranet at [client.o2.fr](https://client.o2.fr) and emits them as JSON.

This is the first half of the goal: mirroring the O2 planning into a dedicated
Google Calendar, updating existing events when a service is rescheduled or
cancelled rather than recreating them. **The Google Calendar side is not
implemented yet** — see [Next step](#next-step).

## Install

```bash
pip install -r requirements.txt   # just `requests`
```

Python 3.9+ (uses `zoneinfo`). No virtualenv is required, but it doesn't hurt.

## Usage

```bash
cp .env.example .env && $EDITOR .env      # or export O2_EMAIL / O2_PASSWORD

python -m o2sync --pretty                 # last 30 days + next 90, to stdout
python -m o2sync -o events.json --pretty  # to a file
python -m o2sync --start 2026-01-01 --end 2026-12-31 -o year.json
python -m o2sync --dump-raw raw.json -o events.json   # keep the untouched API response
```

Credentials are read from `--email`/`--password`, then `O2_EMAIL`/`O2_PASSWORD`,
then `.env`, then an interactive prompt. Prefer the environment or `.env`:
arguments on the command line are visible to other processes on the machine.

Useful flags: `--include-raw` (embed each event's original payload),
`--no-labels` (skip the two label lookups and use the built-in French labels),
`--chunk-days N` (split the window into several requests), `-v` / `-q`.

Exit codes: `0` success, `2` authentication problem (a human must act), `3` O2
unreachable or failing (worth retrying later), `1` anything else.

### Output

```json
{
  "generated_at": "2026-07-29T19:12:03.914+02:00",
  "source": "client.o2.fr",
  "timezone": "Europe/Paris",
  "window": { "start": "2026-06-29T00:00:00+02:00", "end": "2026-10-27T23:59:59+02:00" },
  "count": 18,
  "services": [
    {
      "event_id": "730187168",
      "start": "2026-07-14T11:30:00+02:00",
      "end": "2026-07-14T13:30:00+02:00",
      "actual_end": "2026-07-14T13:30:00+02:00",
      "duration_planned_min": 120,
      "duration_done_min": null,
      "status": "annul_cli_ok",
      "status_label": "Annulée dans les délais",
      "is_cancelled": true,
      "is_postponed": true,
      "service_type": "menage_repassage",
      "service_type_label": "ménage - repassage",
      "category": "menage",
      "colour": "#999999",
      "is_mandataire": false,
      "level": "M2",
      "level_label": "M2 (Ménage Confort)",
      "city": "Lyon 1er Arrondissement",
      "worker": { "civility": "mme", "first_name": "…", "last_name": "…", "display_name": "…" },
      "house_service_id": "730187147",
      "customer_id": "…",
      "contract_ref": "O2C…",
      "series_id": "…",
      "summary": "[ANNULÉ] ménage - repassage",
      "content_hash": "9f2c…"
    }
  ]
}
```

`event_id` is stable across runs even when a service moves, so it is the key a
calendar sync should use. `content_hash` covers only the fields a calendar event
would display — compare it to decide "update" versus "nothing changed". Labels are
deliberately excluded from the hash so a change in O2's wording cannot trigger
pointless calendar updates.

## The API

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
JavaScript, which renders it with its catch-all "Non réalisable". Unknown codes
are treated the same way here — labelled "Non réalisable", counted as cancelled,
and logged once per run so a new status doesn't pass unnoticed.

### Notes on behaviour

- A window covering five years returns exactly the same events as 59 chained
  31-day requests, so there is no server-side result cap to work around and the
  scraper issues a single request by default. `--chunk-days` remains available.
- The label endpoints echo the code back when they don't recognise it, so an
  unknown code is indistinguishable from a valid one — both are usable as labels.

## Tests

```bash
python -m unittest discover -s tests -t .
```

They cover normalisation only and hit no network. Fixtures mirror a real payload
but every value is synthetic — no personal data is committed to this repository.

## Scope and etiquette

`robots.txt` on client.o2.fr disallows `/wp-admin/`. The account owner raised this
with O2, who confirmed that a single customer reading their own planning is fine.
This tool makes a handful of requests per run against one account, and is not
intended for anything broader.

Output files contain personal data (names of the people assigned to your home,
your city, contract references) and are git-ignored.

## Next step

Google Calendar sync, deferred until the JSON above has been reviewed and an auth
method is chosen (OAuth refresh token, or a service account with the calendar
shared to it). The plan is to key events off `event_id` — a deterministic calendar
event id plus `o2EventId`/`content_hash` in `extendedProperties` — so the calendar
itself is the state store and no local file can drift. Cancelled services stay on
the calendar marked `[ANNULÉ]`; services that vanish from O2 entirely get deleted.
`Service.summary` and `Service.content_hash()` already exist for that purpose.
