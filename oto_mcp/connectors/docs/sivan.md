## prerequisite — sivan api key

a Sivan **administrator** generates an API key at the **group** level, then pastes it into oto. The key is sent as the `X-Api-Key` header.
- byo-only: no shared oto key — these are your personnel data, each organization sets its own
- ⚠️ **it is a GROUP key**: it reads every company of the group, not one in particular
- read-only: the connector never writes into Sivan
- the "test connection" button lists the group's companies (`list_companies`): no side effect

## usage — schedule, punches, absences and worked time

- "the companies and their employees" → `sivan_companies()` — every enabled company of the group, with employees, contracts and locations
- "the employees, with their hours counter" → `sivan_salaries(with_counter=True)` (and `with_absences_counter=True` for the absences counter)
- "next week's planned schedule" → `sivan_events(start="…", end="…")` — grouped by company then employee; `companies_ids` / `salaries_ids` narrow it
- "the clock-in/out punches" → `sivan_badged_events(start="…", end="…")`
- "the pending absence requests" → `sivan_absences(start="…", end="…", status="PENDING")` (statuses: PENDING / ACCEPTED / REFUSED; default: all)
- "the worked-time totals per employee" → `sivan_timesheets(start="…", end="…")`

## note — windows and modules

- ⚠️ **a window of at most 31 days** for `sivan_events`, `sivan_badged_events` and `sivan_timesheets`: a wider `start`..`end` is refused. `sivan_absences` has **no** such cap
- ⚠️ **`sivan_badged_events` needs the badging module** (badgeuse) enabled on the Sivan side; without it Sivan refuses (403)
- **durations are in seconds** throughout
