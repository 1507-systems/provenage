# Changelog

## 0.2.0 (unreleased)

### Fixed (review follow-ups)

- Lone UTF-16 surrogates (U+D800-U+DFFF) are rejected in text and URL fields instead of failing at encode time.
- The duplicate-UID check runs for every record with a valid `uid_namespace` and `record_id`, so it is reported
  in the same pass as other errors.
- URLs: a malformed URL is reported as "URL is malformed"; URLs with a user name or password (`@` in the
  host part) or a backslash are rejected.
- `atomic_write` creates a unique temporary file exclusively in the target directory, so it never follows a
  symlink planted at a predictable temporary name.
- `BuildOptions` rejects non-positive and non-numeric durations (default duration and end rules) and a
  `prodid` containing `,` `;` `\\` or `"`; the CLI reports bad config values as `meeting_ics: error:` (exit 2).
- `parse_ics` unfolds continuation lines in linear time.

### Added

- CSV adapter `--adapter csv_template` (`meeting_ics/adapters/csv_template.py`) for `build`, `build-set` and
  `validate`, reading the shipped template (`meeting_ics template`). It trims cells, normalizes times
  (`H:MM`, `HH:MM:00`), lowercases status (`canceled` is `cancelled`), skips blank rows and `#` rows/columns, and
  hands each row to the shared validator; errors carry `row N.<column>` paths and are all reported at once.
  New flag `--retrieved-at TS|now` (csv_template only) supplies `retrieved_at` for rows that leave it blank.
- Feed sets: `meeting_ics build-set --input IN --config SET.json --ledger LEDGER --out DIR [--overwrite]
  [--adapter NAME --uid-namespace NS]` and `meeting_ics.feedset` (`build_feedset`, `write_feedset`,
  `load_set_config`, `encode_index`). Records are grouped by `jurisdiction_slug` and `body_slug` into
  `calendar/<j>/<b>.ics`, a combined `calendar/<j>/all.ics` and `calendar/index.json` (schema
  `provenage-calendar-index/1`, documented in `docs/CALENDAR.md`). Records are sorted by (date, start_time, UID);
  one ledger covers the set; reruns are byte-identical. Every built feed is checked with `check.validate` before
  anything is written, and `write_feedset` refuses a symlinked `calendar` directory and any target that resolves
  outside the output directory. `uid_domain` is required, `calendar_name` is rejected, and the body slug `all` is
  reserved.

- Input contract and validation (`meeting_ics/validate.py`). Every record is checked before a build, all
  problems are reported together as `<path>: <message>` lines, and nothing is written on error.
- `meeting_ics validate --input FILE [--adapter NAME --uid-namespace NS]`: checks an input file and prints
  `ok records=N`, or each problem and `meeting_ics: N error(s); input is invalid` (exit 2).
- `RecordError` (a `ValueError` with `.path`, `.message`, `.record_id`, `.errors`), `parse_input`,
  `validate_records`, `load_input`, `SLUG_RE`, `STATUS_EMIT`, `STATUS_CANONICAL`, `STATUS_ALIASES`.
- Input wrapper form `{"records": [...], "schema_version": 1}`; the bare array still works.
- Record fields `remote_access` (`join_url`, `dial_in`; shown in DESCRIPTION, hashed only when set),
  `jurisdiction_slug` and `body_slug` (routing only, not emitted or hashed), and `body_name` (JSON name for
  `body`; `body` remains accepted as a legacy alias).
- Statuses `scheduled`, `rescheduled`, `postponed`, `completed` and the alias `canceled`. Rescheduled, completed
  and postponed emit `<x_prefix>-LIFECYCLE`; postponed is `STATUS:TENTATIVE` with a `Postponed: ` SUMMARY prefix.

- Client-compatibility options: `method` (default `PUBLISH`), `refresh_interval` (default `PT1H`, written as
  `REFRESH-INTERVAL;VALUE=DURATION` and `X-PUBLISHED-TTL`), and `calendar_description` (`X-WR-CALDESC`).
  `BuildOptions.validate()` checks every option, including end-rule basis labels, before a build.
- `meeting_ics/check.py` (`parse_ics`, `unescape_text`, `validate`) and `meeting_ics check-feed FILE
  [--expect-events N]`, a strict stdlib validator for built feeds (exit 0, 1 or 2).
- `meeting_ics template [--output PATH]` and the packaged CSV header `adapters/csv_template.csv`. The CSV adapter
  itself is not part of this change.
- Ledger: `Ledger.loads`, `Ledger.dumps`, `monotonic_problems`, atomic `save`, and `ValueError`
  (`malformed ledger: ...`) for any ledger that is not well formed.
- `build` warns on stderr when `uid_domain` is the placeholder `example.invalid`.
- Test-only dependency `icalendar` (pinned in `requirements-dev.txt`) for an independent round-trip test.

### Changed

- Header order is now `METHOD`, `X-WR-CALNAME`, `X-WR-CALDESC`, `X-WR-TIMEZONE`, `REFRESH-INTERVAL`,
  `X-PUBLISHED-TTL`, then the VTIMEZONEs. Feeds gain `METHOD:PUBLISH`, `REFRESH-INTERVAL` and `X-PUBLISHED-TTL`
  (all PT1H) once; none is hashed, so SEQUENCE does not change. Set `method` and `refresh_interval` to null to keep
  the 0.1.x bytes.
- `BuildOptions.from_dict` raises `ValueError` (was `TypeError`) for an unknown key.
- `esc` deletes control characters other than tab, and the end-basis line is escaped.
- A ledger file that is empty, not JSON or malformed now raises instead of being silently replaced by an empty
  ledger (which would have reset every SEQUENCE). A missing file is still a fresh ledger.
- The `.ics` and ledger are written atomically (temporary file, then rename).

- An unknown or null status is now an error (before, it built with an `unmapped-status` AMBIGUITY note).
- An empty input is refused (before, it built an empty calendar).
- All CLI failures exit 2 and print `meeting_ics: error: <message>` (non-input problems) or the validation
  lines above. Before, a missing `--uid-namespace` exited 1.
- A record with an empty string or whitespace-only value in an optional string field (`location`, a link, a
  document URL) is treated as null. A record built by 0.1.x with `""` there gets one SEQUENCE bump after the
  upgrade. Data that already uses null, such as the Longmont normalized feed, is unaffected.
- Documents whose `url` or `kind` is null no longer raise `TypeError` when sorted for the change hash; inputs that
  sorted before keep their order, so no SEQUENCE bump.
- An end time earlier than the start (after midnight) is accepted only if the meeting lasts at most 12 hours.

Existing feeds for valid records without the new fields are byte-identical to 0.1.0 (tested against a golden
fixture), and existing ledgers do not bump.
