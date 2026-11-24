"""The strict .ics parser and validator (meeting_ics.check)."""
import unittest

from meeting_ics import build
from meeting_ics.check import IcsFormatError, parse_ics, unescape_text, validate

from tests.helpers import OPTS, rec


def good():
    return build([rec(), rec(record_id="2", status="cancelled")], OPTS)


def errors_for(data, **kw):
    return validate(data, **kw)[0]


class Valid(unittest.TestCase):
    def test_built_feeds_validate(self):
        self.assertEqual(validate(good()), ([], []))
        for tz in ("Europe/Berlin", "America/Chicago", "UTC", "America/Phoenix", "America/Denver"):
            with self.subTest(tz):
                self.assertEqual(errors_for(build([rec(timezone=tz)], OPTS)), [])

    def test_two_zones_and_expect_events(self):
        data = build([rec(), rec(record_id="2", timezone="Europe/Berlin")], OPTS)
        self.assertEqual(errors_for(data, expect_events=2), [])
        self.assertEqual(errors_for(data, expect_events=1), ["expected 1 events, found 2"])

    def test_structure_access(self):
        cal = parse_ics(good())
        self.assertEqual(len(cal.events), 2)
        self.assertEqual(cal.event("t-1@example.org").get("SEQUENCE"), "0")
        self.assertEqual(cal.event("t-2@example.org").get("STATUS"), "CANCELLED")
        self.assertEqual(cal.event("t-1@example.org").all("DTSTART")[0].params["TZID"], "America/Denver")
        with self.assertRaises(KeyError):
            cal.event("missing")


class Mutations(unittest.TestCase):
    def mutate(self, old, new, count=1):
        data = good()
        self.assertIn(old, data)
        return data.replace(old, new, count)

    def assertFails(self, data, text):
        errs = errors_for(data)
        self.assertTrue(any(text in e for e in errs), (text, errs))

    def test_bare_lf(self):
        self.assertFails(self.mutate(b"\r\n", b"\n"), "bare LF present")

    def test_long_line(self):
        self.assertFails(self.mutate(b"CALSCALE:GREGORIAN\r\n", b"CALSCALE:" + b"G" * 80 + b"\r\n"),
                         "is 89 octets (>75)")

    def test_unescaped_comma(self):
        self.assertFails(self.mutate(b"SUMMARY:Board Meeting", b"SUMMARY:a,b"), "unescaped , or ; in TEXT SUMMARY")

    def test_double_backslash_then_comma_is_unescaped(self):
        self.assertFails(self.mutate(b"SUMMARY:Board Meeting", b"SUMMARY:a\\\\,b"), "unescaped , or ; in TEXT")

    def test_invalid_escape(self):
        self.assertFails(self.mutate(b"SUMMARY:Board Meeting", b"SUMMARY:a\\qb"), "invalid escape in SUMMARY")

    def test_attendee_and_rsvp(self):
        self.assertFails(self.mutate(b"BEGIN:VEVENT\r\n", b"BEGIN:VEVENT\r\nATTENDEE:mailto:x@y\r\n"),
                         "forbidden in a public feed")
        self.assertFails(self.mutate(b"BEGIN:VEVENT\r\n", b"BEGIN:VEVENT\r\nX-A;RSVP=TRUE:1\r\n"),
                         "forbidden in a public feed")
        self.assertFails(self.mutate(b"BEGIN:VEVENT\r\n", b"BEGIN:VEVENT\r\nORGANIZER:mailto:x@y\r\n"),
                         "forbidden in a public feed")

    def test_no_components(self):
        data = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//y//EN\r\nEND:VCALENDAR\r\n"
        self.assertEqual(errors_for(data), ["VCALENDAR has no components (RFC 5545 3.6 requires at least one)"])

    def test_refresh_interval_needs_duration(self):
        self.assertFails(self.mutate(b"REFRESH-INTERVAL;VALUE=DURATION:PT1H", b"REFRESH-INTERVAL:PT1H"),
                         "REFRESH-INTERVAL needs VALUE=DURATION")

    def test_event_problems(self):
        self.assertFails(self.mutate(b"STATUS:CANCELLED", b"STATUS:BOGUS"), "STATUS must be one of")
        self.assertFails(self.mutate(b"UID:t-2@example.org", b"UID:t-1@example.org"), "duplicate UID")
        self.assertFails(self.mutate(b"UID:t-2@example.org", b"UID:nodomain"), "UID must look like local@domain")
        self.assertFails(self.mutate(b"SEQUENCE:0", b"SEQUENCE:x"), "SEQUENCE must be a number")
        self.assertFails(self.mutate(b"DTSTAMP:20260101T000000Z", b"DTSTAMP:20260101T000000"), "DTSTAMP must be UTC")
        self.assertFails(self.mutate(b"DTSTART;TZID=America/Denver:20260301T180000",
                                     b"DTSTART:20260301T180000Z"), "must be a local time with a TZID")
        self.assertFails(self.mutate(b"DTSTART;TZID=America/Denver:20260301T180000",
                                     b"DTSTART;TZID=Nowhere/Land:20260301T180000"), "which no VTIMEZONE defines")
        self.assertFails(self.mutate(b"DTEND;TZID=America/Denver:20260301T210000",
                                     b"DTEND;TZID=America/Denver:20260301T170000"), "DTEND is not after DTSTART")
        self.assertFails(self.mutate(b"SUMMARY:Board Meeting\r\n", b""), "expected exactly one SUMMARY")

    def test_timezone_problems(self):
        self.assertFails(self.mutate(b"TZOFFSETTO:-0600", b"TZOFFSETTO:oops"), "TZOFFSETTO must look like")
        self.assertFails(self.mutate(b"TZID:America/Denver\r\n", b"", 1), "expected exactly one TZID")

    def test_header_problems(self):
        self.assertFails(self.mutate(b"VERSION:2.0", b"VERSION:1.0"), "VERSION:2.0")
        self.assertFails(self.mutate(b"PRODID:", b"X-NOPRODID:"), "PRODID")


class Parser(unittest.TestCase):
    def test_unbalanced(self):
        with self.assertRaises(IcsFormatError):
            parse_ics(b"BEGIN:VCALENDAR\r\nEND:VEVENT\r\n")
        with self.assertRaises(IcsFormatError):
            parse_ics(b"BEGIN:VCALENDAR\r\n")
        with self.assertRaises(IcsFormatError):
            parse_ics(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\nBEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
        with self.assertRaises(IcsFormatError):
            parse_ics(b"")

    def test_no_final_crlf_and_garbage(self):
        data = good()
        self.assertIn("does not end with CRLF", errors_for(data[:-2])[0])
        self.assertEqual(len(errors_for(b"BEGIN:VCALENDAR\r\nthis is not a line\r\nEND:VCALENDAR\r\n")), 1)

    def test_control_character_rejected(self):
        with self.assertRaises(IcsFormatError):
            parse_ics(b"BEGIN:VCALENDAR\r\nX-A:b\x07c\r\nEND:VCALENDAR\r\n")

    def test_invalid_utf8(self):
        self.assertTrue(any("not valid UTF-8" in e for e in errors_for(good().replace(b"Board", b"B\xe9ard"))))

    def test_folded_lines_and_quoted_params(self):
        data = (b"BEGIN:VCALENDAR\r\nX-LONG:abc\r\n def\r\nX-P;A=\"x;y\":v\r\nEND:VCALENDAR\r\n")
        cal = parse_ics(data)
        self.assertEqual(cal.get("X-LONG"), "abcdef")
        self.assertEqual(cal.all("X-P")[0].params, {"A": "x;y"})

    def test_unescape(self):
        self.assertEqual(unescape_text("a\\,b\\;c\\\\d\\ne\\Nf"), "a,b;c\\d\ne\nf")
        self.assertEqual(unescape_text("end\\"), "end\\")


class Unfolding(unittest.TestCase):
    def test_many_continuation_lines_unfold_correctly(self):
        from meeting_ics.check import parse_ics
        n = 20000
        body = "X-BIG:a\r\n" + " b\r\n" * n
        data = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + body + "END:VCALENDAR\r\n").encode()
        cal = parse_ics(data)
        self.assertEqual(cal.get("X-BIG"), "a" + "b" * n)


if __name__ == "__main__":
    unittest.main()
