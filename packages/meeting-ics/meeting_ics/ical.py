"""iCalendar primitives: escape, fold, VTIMEZONE generation (rules R12, R13)."""
import datetime

BS = chr(92)


def esc(text):
    """TEXT escaping: backslash, semicolon, comma, newline."""
    return (text.replace("\\", "\\\\").replace(";", BS + ";").replace(",", "\\,")
            .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n"))


def fold(line):
    """Fold a content line at 75 octets; never split a UTF-8 character."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]
    out, cur, curlen, limit = [], "", 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if curlen + n > limit:
            out.append(cur)
            cur, curlen, limit = " ", 1, 75
        cur += ch
        curlen += n
    out.append(cur)
    return out


def ics_utc(iso):
    """"2026-10-28T03:03:04Z" -> "20261028T030304Z"."""
    d = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    return d.strftime("%Y%m%dT%H%M%SZ")


def _fmt_off(minutes):
    sign = "-" if minutes < 0 else "+"
    m = abs(minutes)
    return "%s%02d%02d" % (sign, m // 60, m % 60)


def us_rule_vtimezone(tzid, std_minutes, std_name, dst_name):
    """US rule in force since 2007: DAYLIGHT from 2nd Sunday of March 02:00, STANDARD from 1st Sunday of
    November 02:00, both RRULE-based. For America/Denver this is the exact block of the original builder."""
    s, d = _fmt_off(std_minutes), _fmt_off(std_minutes + 60)
    return ["BEGIN:VTIMEZONE", "TZID:" + tzid,
            "BEGIN:STANDARD", "DTSTART:20071104T020000", "TZOFFSETFROM:" + d, "TZOFFSETTO:" + s,
            "TZNAME:" + std_name, "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU", "END:STANDARD",
            "BEGIN:DAYLIGHT", "DTSTART:20070311T020000", "TZOFFSETFROM:" + s, "TZOFFSETTO:" + d,
            "TZNAME:" + dst_name, "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU", "END:DAYLIGHT",
            "END:VTIMEZONE"]


def fixed_vtimezone(tzid, minutes, name):
    o = _fmt_off(minutes)
    return ["BEGIN:VTIMEZONE", "TZID:" + tzid, "BEGIN:STANDARD", "DTSTART:19700101T000000",
            "TZOFFSETFROM:" + o, "TZOFFSETTO:" + o, "TZNAME:" + name, "END:STANDARD", "END:VTIMEZONE"]


# US-style zones with the 2007+ rule, and US zones with fixed offsets. Everything else uses zoneinfo.
_US_RULE = {
    "America/Denver": (-420, "MST", "MDT"),
    "America/New_York": (-300, "EST", "EDT"),
    "America/Chicago": (-360, "CST", "CDT"),
    "America/Los_Angeles": (-480, "PST", "PDT"),
}
_FIXED = {"America/Phoenix": (-420, "MST"), "Pacific/Honolulu": (-600, "HST"), "UTC": (0, "UTC")}


def _zoneinfo(tzid):
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(tzid)
    except Exception as e:  # ImportError, ZoneInfoNotFoundError, ValueError
        raise RuntimeError("VTIMEZONE for %r needs python zoneinfo with tz data, which is unavailable (%s: %s); "
                           "install tzdata or use a zone with a built-in rule (%s)"
                           % (tzid, type(e).__name__, e, ", ".join(sorted(list(_US_RULE) + list(_FIXED))))) from e


def _off_at(zi, utc_dt):
    return utc_dt.astimezone(zi).utcoffset()


def zoneinfo_vtimezone(tzid, years):
    """Derive STANDARD/DAYLIGHT observances from zoneinfo transitions in [min(years), max(years)]."""
    zi = _zoneinfo(tzid)
    utc = datetime.timezone.utc
    y0, y1 = min(years), max(years)
    day = datetime.datetime(y0, 1, 1, tzinfo=utc)
    end = datetime.datetime(y1 + 1, 1, 1, tzinfo=utc)
    first = _off_at(zi, day)
    d0 = day.astimezone(zi)
    comps = [("DAYLIGHT" if d0.dst() else "STANDARD", datetime.datetime(y0, 1, 1), first, first, d0.tzname(), first)]
    cur = first
    while day < end:
        nxt = day + datetime.timedelta(days=1)
        if _off_at(zi, nxt) != cur:
            lo, hi = day, nxt
            while hi - lo > datetime.timedelta(minutes=1):
                mid = lo + (hi - lo) / 2
                mid = mid.replace(second=0, microsecond=0)
                if mid <= lo:
                    break
                if _off_at(zi, mid) == cur:
                    lo = mid
                else:
                    hi = mid
            new = _off_at(zi, hi)
            wall = (hi + cur).replace(tzinfo=None)  # wall clock just before the change, in the old offset
            h = hi.astimezone(zi)
            kind = "DAYLIGHT" if h.dst() else "STANDARD"
            comps.append((kind, wall, cur, new, h.tzname(), new))
            cur = new
        day = nxt
    lines = ["BEGIN:VTIMEZONE", "TZID:" + tzid]
    for kind, wall, ofrom, oto, name, _ in comps:
        lines += ["BEGIN:" + kind, "DTSTART:" + wall.strftime("%Y%m%dT%H%M%S"),
                  "TZOFFSETFROM:" + _fmt_off(int(ofrom.total_seconds() // 60)),
                  "TZOFFSETTO:" + _fmt_off(int(oto.total_seconds() // 60)),
                  "TZNAME:" + name, "END:" + kind]
    lines.append("END:VTIMEZONE")
    return lines


def vtimezone(tzid, years):
    """VTIMEZONE lines for any IANA zone. Built-in US rule or fixed zones need no tz data; others derive from
    zoneinfo for the given years and raise RuntimeError if zoneinfo is unavailable."""
    if tzid in _US_RULE:
        return us_rule_vtimezone(tzid, *_US_RULE[tzid])
    if tzid in _FIXED:
        return fixed_vtimezone(tzid, *_FIXED[tzid])
    return zoneinfo_vtimezone(tzid, sorted(set(years)))


def nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def us_dst_dates(year):
    """(DST start, DST end) dates under the US 2007+ rule."""
    return nth_sunday(year, 3, 2), nth_sunday(year, 11, 1)
