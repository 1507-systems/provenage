# Changelog

## 0.2.0 (unreleased)

### Added

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

### Changed

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
