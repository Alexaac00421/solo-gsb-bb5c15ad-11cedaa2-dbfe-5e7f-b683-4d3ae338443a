"""时间解析与格式化测试。"""
import unittest

from app.timeutil import TimeError, format_time, parse_time


class TimeParseTests(unittest.TestCase):
    def test_utc_z(self):
        self.assertEqual(parse_time("2026-10-05T08:00:00Z"), 1791187200)

    def test_offset(self):
        self.assertEqual(
            parse_time("2026-10-05T16:00:00+08:00"),
            parse_time("2026-10-05T08:00:00Z"),
        )

    def test_naive_assumed_utc(self):
        self.assertEqual(
            parse_time("2026-10-05T08:00:00"),
            parse_time("2026-10-05T08:00:00Z"),
        )

    def test_integer_epoch(self):
        self.assertEqual(parse_time(0), 0)
        self.assertEqual(parse_time(1791187200), 1791187200)

    def test_rejects_bad_values(self):
        for bad in (True, 1.5, "2026-10-05T08:00:00.5Z", "not-a-time",
                    "", "2026-13-01T00:00:00Z", -1, None, []):
            with self.assertRaises(TimeError):
                parse_time(bad)

    def test_roundtrip(self):
        t = parse_time("2026-10-05T08:00:00Z")
        self.assertEqual(format_time(t), "2026-10-05T08:00:00Z")


if __name__ == "__main__":
    unittest.main()
