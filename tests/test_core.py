import unittest
from datetime import datetime, timedelta, timezone

from ais_alert_bot.bot import (
    angular_difference_degrees,
    calculate_predicted_alert_at,
    calculate_predicted_alert_at_from_movement,
    calculate_projected_speed_toward_center,
    format_jerusalem_time,
    is_stale_position,
    parse_radius_nm,
    parse_vessel_identifier,
    parse_watch_args,
    stale_position_prefix,
)
from ais_alert_bot.geo import haversine_km, initial_bearing_degrees
from ais_alert_bot.sheets import calculate_best_difference_nm


class GeoTests(unittest.TestCase):
    def test_haversine_same_point(self):
        self.assertAlmostEqual(haversine_km(37.941, 23.646, 37.941, 23.646), 0.0)

    def test_haversine_known_short_distance(self):
        distance = haversine_km(37.941, 23.646, 37.951, 23.646)
        self.assertAlmostEqual(distance, 1.11, places=1)

    def test_initial_bearing_degrees_due_north(self):
        self.assertAlmostEqual(initial_bearing_degrees(0, 0, 1, 0), 0.0, places=1)


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

    def test_format_aisstream_utc_suffix_with_nanoseconds(self):
        self.assertEqual(
            format_jerusalem_time("2026-05-20 12:43:15.681189958 +0000 UTC"),
            "20/05/26 15:43",
        )

    def test_stale_position_prefix_warns_for_old_timestamp(self):
        old_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        self.assertIn("זה אינו מידע חדש", stale_position_prefix(old_timestamp, 60))

    def test_stale_position_prefix_ignores_fresh_timestamp(self):
        fresh_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.assertEqual(stale_position_prefix(fresh_timestamp, 60), "")

    def test_is_stale_position_treats_missing_timestamp_as_stale(self):
        self.assertTrue(is_stale_position(None, 60))

    def test_is_stale_position_accepts_fresh_timestamp(self):
        fresh_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.assertFalse(is_stale_position(fresh_timestamp, 60))

    def test_angular_difference_wraps_around_zero(self):
        self.assertEqual(angular_difference_degrees(5, 355), 10)

    def test_calculate_predicted_alert_at_requires_speed(self):
        self.assertIsNone(calculate_predicted_alert_at(10, 5, 0))

    def test_calculate_predicted_alert_at_returns_future_time(self):
        predicted = calculate_predicted_alert_at(10, 8, 20)
        self.assertIsNotNone(predicted)

    def test_calculate_predicted_alert_at_uses_closing_speed(self):
        previous = datetime(2026, 5, 23, 0, 0, tzinfo=timezone.utc)
        current = previous + timedelta(minutes=30)
        predicted = calculate_predicted_alert_at_from_movement(10, 8, 7, previous, current, 20)
        self.assertEqual(predicted, current + timedelta(minutes=15))

    def test_calculate_predicted_alert_at_rejects_non_closing_track(self):
        previous = datetime(2026, 5, 23, 0, 0, tzinfo=timezone.utc)
        current = previous + timedelta(minutes=30)
        predicted = calculate_predicted_alert_at_from_movement(8, 10, 7, previous, current, 20)
        self.assertIsNone(predicted)

    def test_calculate_predicted_alert_at_falls_back_before_second_sample(self):
        current = datetime(2026, 5, 23, 0, 0, tzinfo=timezone.utc)
        predicted = calculate_predicted_alert_at_from_movement(None, 10, 8, None, current, 20)
        self.assertEqual(predicted, current + timedelta(minutes=6))

    def test_projected_speed_uses_course_toward_center(self):
        projected = calculate_projected_speed_toward_center(0, 0, 1, 0, 12, 0)
        self.assertAlmostEqual(projected, 12.0, places=1)

    def test_projected_speed_rejects_course_away_from_center(self):
        projected = calculate_projected_speed_toward_center(0, 0, 1, 0, 12, 180)
        self.assertIsNone(projected)

    def test_sheets_difference_prefers_gps_distance(self):
        self.assertEqual(calculate_best_difference_nm(5, 4.7, 4.2), 0.2999999999999998)

    def test_sheets_difference_uses_calculated_when_gps_missing(self):
        self.assertEqual(calculate_best_difference_nm(5, None, 4.2), 0.7999999999999998)


if __name__ == "__main__":
    unittest.main()
