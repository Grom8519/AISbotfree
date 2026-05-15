import unittest

from ais_alert_bot.bot import format_jerusalem_time, parse_radius_nm, parse_vessel_identifier, parse_watch_args
from ais_alert_bot.geo import haversine_km


class GeoTests(unittest.TestCase):
    def test_haversine_same_point(self):
        self.assertAlmostEqual(haversine_km(37.941, 23.646, 37.941, 23.646), 0.0)

    def test_haversine_known_short_distance(self):
        distance = haversine_km(37.941, 23.646, 37.951, 23.646)
        self.assertAlmostEqual(distance, 1.11, places=1)


class ParserTests(unittest.TestCase):
    def test_parse_mmsi_watch(self):
        parsed = parse_watch_args(["mmsi", "538003913", "37.941", "23.646", "15", "5"], 5, 1)
        self.assertEqual(parsed["query_type"], "mmsi")
        self.assertEqual(parsed["query_value"], "538003913")
        self.assertEqual(parsed["interval_minutes"], 5)

    def test_reject_invalid_mmsi(self):
        with self.assertRaises(ValueError):
            parse_watch_args(["mmsi", "SUNNY", "37.941", "23.646", "15"], 5, 1)

    def test_parse_name_watch(self):
        parsed = parse_watch_args(["name", "SUNNY STAR", "37.941", "23.646", "15"], 5, 1)
        self.assertEqual(parsed["query_type"], "name")
        self.assertEqual(parsed["query_value"], "SUNNY STAR")

    def test_parse_dialog_mmsi(self):
        self.assertEqual(parse_vessel_identifier("MMSI 538003913"), ("mmsi", "538003913"))

    def test_parse_dialog_imo(self):
        self.assertEqual(parse_vessel_identifier("IMO 9353333"), ("imo", "9353333"))

    def test_parse_radius_nm_accepts_one_decimal(self):
        self.assertEqual(parse_radius_nm("3,5"), 3.5)

    def test_parse_radius_nm_rejects_more_decimals(self):
        with self.assertRaises(ValueError):
            parse_radius_nm("3.55")

    def test_format_jerusalem_time_winter(self):
        self.assertEqual(format_jerusalem_time("2026-01-15T10:05:00Z"), "15/01/26 12:05")

    def test_format_jerusalem_time_summer(self):
        self.assertEqual(format_jerusalem_time("2026-07-15T10:05:00Z"), "15/07/26 13:05")


if __name__ == "__main__":
    unittest.main()
