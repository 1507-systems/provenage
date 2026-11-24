"""Input contract tests for meeting_ics.validate (see README "Input schema")."""
import copy
import json
import os
import tempfile
import unittest
import zoneinfo
from unittest import mock

from meeting_ics import MeetingRecord, RecordError, load_input, parse_input, validate
from meeting_ics.validate import ERROR_CAP


def good():
    return {"uid_namespace": "demo", "record_id": "m1", "jurisdiction_slug": "demo-town",
            "body_slug": "city-council", "body_name": "City Council", "meeting_type": "Regular Session",
            "date": "2026-12-01", "start_time": "18:00", "timezone": "America/Denver", "status": "scheduled",
            "retrieved_at": "2026-11-24T12:00:00Z", "links": {"portal": "https://demo.example/m/1"},
            "location": "Town Hall"}


def P(x):
    return parse_input(x)


class Base(unittest.TestCase):
    def E(self, x, path, substr, nth=0):
        with self.assertRaises(RecordError) as cm:
            P(x)
        e = cm.exception.errors[nth]
        self.assertEqual(e.path, path, [str(z) for z in cm.exception.errors])
        self.assertIn(substr, e.message)
        return cm.exception

    def with_(self, **kw):
        d = good()
        for k, v in kw.items():
            if v is KeyError:
                d.pop(k, None)
            else:
                d[k] = v
        return [d]


class TopLevel(Base):
    def test_valid_shapes(self):
        r = P([good()])
        self.assertEqual((len(r), r[0].body, r[0].body_name), (1, "City Council", "City Council"))
        self.assertEqual(len(P({"records": [good()]})), 1)
        self.assertEqual(len(P({"schema_version": 1, "records": [good()]})), 1)

    def test_schema_version(self):
        for v in (2, True, "1", 1.0):
            self.E({"schema_version": v, "records": [good()]}, "<input>.schema_version", "unsupported")

    def test_wrapper_errors(self):
        self.E({"records": [good()], "meta": 1}, "<input>.meta", "unknown top-level key")
        self.E("x", "<input>", "got string")
        self.E({}, "<input>.records", "missing")
        self.E({"records": {}}, "<input>.records", "must be an array, got object")

    def test_empty(self):
        self.E([], "<input>", "no records: refusing to build an empty feed")
        self.E({"records": []}, "<input>", "no records")

    def test_record_not_object(self):
        self.E([5], "[0]", "record must be an object, got integer")


class Fields(Base):
    def test_required(self):
        self.E(self.with_(date=KeyError), "[0].date", "required field is missing")
        self.E(self.with_(date=None), "[0].date", "required field is null")

    def test_date(self):
        self.E(self.with_(date="2026-2-1"), "[0].date", "YYYY-MM-DD")
        self.E(self.with_(date="2026-02-30"), "[0].date", "not a real calendar date")
        self.E(self.with_(date="1969-12-31"), "[0].date", "between 1970 and 2100")
        self.E(self.with_(date="2101-01-01"), "[0].date", "between 1970 and 2100")

    def test_start_time(self):
        for v in ("6:00 PM", "24:00", "18:00:00"):
            self.E(self.with_(start_time=v), "[0].start_time", "HH:MM")

    def test_timezone(self):
        e = self.E(self.with_(timezone="America/Denvr"), "[0].timezone", "unknown IANA timezone")
        self.assertIn("did you mean 'America/Denver'", e.message)
        self.E(self.with_(timezone="america/denver"), "[0].timezone", "unknown IANA timezone")
        self.E(self.with_(timezone="MST"), "[0].timezone", "such as America/Denver")

    def test_timezone_data(self):
        if "Europe/Berlin" in zoneinfo.available_timezones():
            self.assertEqual(len(P(self.with_(timezone="Europe/Berlin"))), 1)
        with mock.patch.object(validate, "_available_zones", return_value=frozenset()):
            self.E(self.with_(timezone="Europe/Berlin"), "[0].timezone", "install the tzdata package")
            self.assertEqual(len(P(self.with_(timezone="America/Denver"))), 1)

    def test_retrieved_at(self):
        self.E(self.with_(retrieved_at="2026-11-24T12:00:00+00:00"), "[0].retrieved_at", "UTC timestamp")
        self.E(self.with_(retrieved_at="2026-02-30T00:00:00Z"), "[0].retrieved_at", "not a real date/time")

    def test_bad_urls(self):
        cases = {"www.demo.example": "http(s) URL", "javascript:alert(1)": "http(s) URL", "HTTPS://x": "http(s) URL",
                 "https://x/a b": "http(s) URL", "https:///x": "no host", "https://x:99999/": "invalid port",
                 "https://" + "x" * 2050: "longer than 2048"}
        for url, msg in cases.items():
            self.E(self.with_(links={"portal": url}), "[0].links.portal", msg)

    def test_links(self):
        self.E(self.with_(links={"website": "https://x"}), "[0].links.website", "allowed: portal, agenda")
        self.E(self.with_(links="x"), "[0].links", "must be an object or null")
        r = P(self.with_(links={"portal": ""}))
        self.assertIsNone(r[0].links["portal"])
        self.assertEqual(P(self.with_(links=None))[0].links, {})

    def test_remote_access(self):
        ra = {"join_url": "https://zoom.example/j/1", "dial_in": "+1 555 0100\nID 123"}
        self.assertEqual(P(self.with_(remote_access=ra))[0].remote_access, ra)
        self.E(self.with_(remote_access={"join_url": "zoom"}), "[0].remote_access.join_url", "http(s) URL")
        self.E(self.with_(remote_access={"phone": "x"}), "[0].remote_access.phone", "unknown key")
        self.assertIsNone(P(self.with_(remote_access={}))[0].remote_access)
        self.assertIsNone(P(self.with_(remote_access={"join_url": "", "dial_in": None}))[0].remote_access)
        self.assertEqual(P(self.with_(remote_access={"dial_in": "x"}))[0].remote_access,
                         {"join_url": None, "dial_in": "x"})

    def test_status(self):
        e = self.E(self.with_(status="Cancelled"), "[0].status", "unknown status")
        self.assertIn("try 'cancelled'", e.message)
        self.E(self.with_(status="tbd"), "[0].status", "expected one of: scheduled, rescheduled")
        self.E(self.with_(status=None), "[0].status", "null")

    def test_unknown_fields(self):
        d = good()
        d["statsu"] = "x"
        self.E([d], "[0].statsu", "did you mean 'status'")
        d = good()
        d["agenda_url"] = "https://x"
        self.E([d], "[0].agenda_url", "links go in links.agenda")

    def test_body_alias(self):
        d = good()
        d["body"] = "X"
        self.E([d], "[0].body", "not both")
        d = good()
        del d["body_name"]
        self.E([d], "[0].body_name", "missing")
        d["body"] = "Legacy"
        self.assertEqual(P([d])[0].body, "Legacy")

    def test_slugs(self):
        e = self.E(self.with_(body_slug="City_Council"), "[0].body_slug", "try 'city-council'")
        self.assertIn("slug", e.message)
        for bad in ("-x", "x-", "a--b", "a" * 65):
            self.E(self.with_(body_slug=bad), "[0].body_slug", "slug")
        self.assertIsNone(P(self.with_(body_slug=""))[0].body_slug)
        self.E(self.with_(jurisdiction_slug=KeyError), "[0].body_slug", "set jurisdiction_slug too")

    def test_end_time(self):
        self.E(self.with_(end_time="18:00"), "[0].end_time", "must be after start_time")
        self.E(self.with_(end_time="08:00"), "[0].end_time", "at most 12 hours")
        self.assertEqual(P(self.with_(end_time="06:00"))[0].end_time, "06:00")  # exactly 12 h
        self.assertEqual(P(self.with_(end_time="01:00"))[0].end_time, "01:00")
        self.E(self.with_(end_basis="x"), "[0].end_basis", "only together with end_time")
        self.E(self.with_(end_time="20:00", end_basis="a b"), "[0].end_basis", "must match")
        self.assertIsNone(P(self.with_(end_time="", end_basis=""))[0].end_time)

    def test_record_id(self):
        self.E(self.with_(record_id=True), "[0].record_id", "got boolean")
        self.E(self.with_(record_id=-1), "[0].record_id", ">= 0")
        self.E(self.with_(record_id="a b"), "[0].record_id", "may contain only")
        self.assertEqual(P(self.with_(record_id=7))[0].record_id, "7")

    def test_uid_namespace(self):
        self.E(self.with_(uid_namespace="a b"), "[0].uid_namespace", "may contain only")

    def test_duplicates(self):
        d1, d2 = good(), good()
        self.E([d1, d2], "[1].record_id", "already used by record [0]")
        d1["record_id"], d2["record_id"] = 1, "1"
        self.E([d1, d2], "[1].record_id", "already used by record [0]")
        d2["uid_namespace"] = "other"
        self.assertEqual(len(P([d1, d2])), 2)

    def test_text(self):
        self.E(self.with_(meeting_type="  "), "[0].meeting_type", "must not be blank")
        self.E(self.with_(body_name="A\tB"), "[0].body_name", "U+0009")
        self.E(self.with_(body_name="x" * 201), "[0].body_name", "longer than 200")
        self.E(self.with_(body_name=5), "[0].body_name", "must be a string, got integer")
        self.assertEqual(P(self.with_(location="L1\nL2"))[0].location, "L1\nL2")
        self.E(self.with_(location="L1\r\nL2"), "[0].location", "carriage return")
        self.assertIsNone(P(self.with_(location="   "))[0].location)

    def test_documents_topics_provenance(self):
        r = P(self.with_(documents=[{"kind": "agenda", "url": "", "publish_date": None}], topics=["a"],
                         provenance={"x": 1}))
        self.assertEqual(r[0].documents, [{"kind": "agenda", "url": None, "publish_date": None}])
        self.E(self.with_(documents=[{"kind": "a", "size": 1}]), "[0].documents[0].size", "unknown key")
        self.E(self.with_(documents=[{"url": None}]), "[0].documents[0].kind", "missing")
        self.E(self.with_(documents=["x"]), "[0].documents[0]", "must be an object")
        self.E(self.with_(documents={}), "[0].documents", "must be an array or null")
        self.E(self.with_(topics=[" "]), "[0].topics[0]", "must not be blank")
        self.E(self.with_(provenance=[]), "[0].provenance", "must be an object or null")
        self.assertEqual(P(self.with_(provenance=None))[0].provenance, {})


class ErrorCollection(Base):
    def test_multiple_errors_in_field_order(self):
        d = good()
        d.update(date="x", status="tbd")
        with self.assertRaises(RecordError) as cm:
            P([d])
        e = cm.exception
        self.assertEqual([x.path for x in e.errors], ["[0].date", "[0].status"])
        self.assertTrue(str(e).endswith("(+1 more)"))
        self.assertIn("(record_id 'm1')", str(e))
        self.assertNotIn("more", e.errors[1].line())

    def test_from_dict_has_no_index_prefix(self):
        with self.assertRaises(RecordError) as cm:
            MeetingRecord.from_dict({**good(), "date": "x"})
        self.assertEqual(cm.exception.path, "date")
        with self.assertRaises(RecordError) as cm:
            MeetingRecord.from_dict([])
        self.assertEqual(cm.exception.path, "<record>")
        self.assertEqual(MeetingRecord.from_dict(good()).body_name, "City Council")

    def test_error_cap(self):
        recs = []
        for i in range(150):
            d = good()
            d.update(record_id=str(i), date="x")
            recs.append(d)
        with self.assertRaises(RecordError) as cm:
            P(recs)
        errs = cm.exception.errors
        self.assertEqual(len(errs), ERROR_CAP + 1)
        self.assertEqual(errs[-1].message, "too many errors; stopped after 100")

    def test_inputs_not_mutated(self):
        d = good()
        d["record_id"] = 5
        d["location"] = ""
        before = copy.deepcopy(d)
        P([d])
        self.assertEqual(d, before)

    def test_meeting_record_inputs(self):
        rec = P([good()])[0]
        again = validate.validate_records([rec])
        self.assertEqual(again, [rec])
        self.assertIsNot(again[0], rec)


class LoadInput(unittest.TestCase):
    def load(self, text, raw=None):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "in.json")
            with open(p, "wb") as f:
                f.write(raw if raw is not None else text.encode("utf-8"))
            return load_input(p)

    def err(self, text, raw=None):
        with self.assertRaises(RecordError) as cm:
            self.load(text, raw)
        self.assertEqual(cm.exception.path, "<input>")
        return cm.exception.message

    def test_cases(self):
        g = json.dumps([good()])
        self.assertEqual(len(self.load(g)), 1)
        self.assertEqual(len(self.load("", raw=b"\xef\xbb\xbf" + g.encode())), 1)
        self.assertIn("duplicate JSON key 'date'", self.err('[{"date":"a","date":"b"}]'))
        self.assertIn("non-standard JSON constant", self.err('[{"x": NaN}]'))
        self.assertIn("not valid JSON", self.err("["))
        self.assertIn("not valid UTF-8", self.err("", raw=b"\xff\xfe\x00"))

    def test_missing_file(self):
        with self.assertRaises(RecordError) as cm:
            load_input(os.path.join(tempfile.gettempdir(), "no-such-meeting-ics-input.json"))
        self.assertIn("cannot read", cm.exception.message)


class Wave1Followups(Base):
    def test_lone_surrogates_rejected_in_text_and_url(self):
        for ch in ("\ud800", "\udbff", "\udc00", "\udfff"):
            self.E(self.with_(meeting_type="a" + ch + "b"), "[0].meeting_type", "invalid character (U+%04X)" % ord(ch))
            self.E(self.with_(location=ch), "[0].location", "invalid character")
            self.E(self.with_(links={"portal": "https://x/" + ch}), "[0].links.portal", "invalid character")

    def test_surrogate_via_json_escape_fails_validate_not_build(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "in.json")
            data = json.dumps([good()]).replace("Town Hall", "T\\ud800")
            with open(p, "w", encoding="utf-8") as f:
                f.write(data)
            with self.assertRaises(RecordError) as cm:
                load_input(p)
        self.assertIn("invalid character (U+D800)", cm.exception.errors[0].message)

    def test_duplicate_uid_reported_alongside_other_errors(self):
        for recs in ([good(), self.with_(date="x")[0]], [self.with_(date="x")[0], good()]):
            with self.assertRaises(RecordError) as cm:
                P(recs)
            msgs = [(e.path, e.message) for e in cm.exception.errors]
            self.assertEqual(len(msgs), 2, msgs)
            self.assertTrue(any(p == "[1].record_id" and "duplicate UID" in m for p, m in msgs), msgs)
            self.assertTrue(any(p.endswith(".date") for p, _ in msgs), msgs)

    def test_duplicate_not_reported_when_uid_fields_invalid(self):
        with self.assertRaises(RecordError) as cm:
            P(self.with_(record_id="bad id!") * 2)
        self.assertFalse(any("duplicate" in e.message for e in cm.exception.errors))

    def test_malformed_url_is_not_an_invalid_port(self):
        self.E(self.with_(links={"portal": "https://[::1/"}), "[0].links.portal", "URL is malformed")

    def test_url_userinfo_and_backslash_rejected(self):
        self.E(self.with_(links={"portal": "https://u:p@x/"}), "[0].links.portal", "user name or password")
        self.E(self.with_(links={"portal": "https://u@x/"}), "[0].links.portal", "user name or password")
        self.E(self.with_(links={"portal": "https://x\\@evil/"}), "[0].links.portal", "backslash")
        self.E(self.with_(links={"portal": "https://x/a\\b"}), "[0].links.portal", "backslash")
        self.assertEqual(P(self.with_(links={"portal": "https://x/a@b"}))[0].links["portal"], "https://x/a@b")


if __name__ == "__main__":
    unittest.main()
