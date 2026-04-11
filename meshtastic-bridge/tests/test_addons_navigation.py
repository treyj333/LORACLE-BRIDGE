"""Tests for Navigation addon — haversine, bearing, coord parsing (Phase 5)."""

import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from addons.navigation.addon import NavigationAddon


class TestParseCoords(unittest.TestCase):
    def test_valid_coords(self):
        self.assertEqual(NavigationAddon._parse_coords("34.0522,-118.2437"), (34.0522, -118.2437))

    def test_with_spaces(self):
        self.assertEqual(NavigationAddon._parse_coords("34.05 , -118.24"), (34.05, -118.24))

    def test_missing_comma(self):
        self.assertIsNone(NavigationAddon._parse_coords("34.0522"))

    def test_out_of_range_lat(self):
        self.assertIsNone(NavigationAddon._parse_coords("91.0,-118.0"))
        self.assertIsNone(NavigationAddon._parse_coords("-91.0,-118.0"))

    def test_out_of_range_lon(self):
        self.assertIsNone(NavigationAddon._parse_coords("34.0,-181.0"))
        self.assertIsNone(NavigationAddon._parse_coords("34.0,181.0"))

    def test_non_numeric(self):
        self.assertIsNone(NavigationAddon._parse_coords("abc,def"))

    def test_empty_string(self):
        self.assertIsNone(NavigationAddon._parse_coords(""))


class TestHaversine(unittest.TestCase):
    def test_same_point_zero_distance(self):
        d = NavigationAddon._haversine_km(34.0, -118.0, 34.0, -118.0)
        self.assertAlmostEqual(d, 0.0, places=3)

    def test_known_distance_la_to_sf(self):
        """LA (34.0522, -118.2437) to SF (37.7749, -122.4194) ~559 km."""
        d = NavigationAddon._haversine_km(34.0522, -118.2437, 37.7749, -122.4194)
        self.assertAlmostEqual(d, 559.0, delta=5.0)

    def test_symmetry(self):
        d1 = NavigationAddon._haversine_km(34.0, -118.0, 40.0, -74.0)
        d2 = NavigationAddon._haversine_km(40.0, -74.0, 34.0, -118.0)
        self.assertAlmostEqual(d1, d2, places=3)


class TestInitialBearing(unittest.TestCase):
    def test_due_north(self):
        b = NavigationAddon._initial_bearing(34.0, -118.0, 35.0, -118.0)
        self.assertAlmostEqual(b, 0.0, delta=1.0)

    def test_due_east(self):
        b = NavigationAddon._initial_bearing(0.0, 0.0, 0.0, 1.0)
        self.assertAlmostEqual(b, 90.0, delta=1.0)

    def test_due_south(self):
        b = NavigationAddon._initial_bearing(35.0, -118.0, 34.0, -118.0)
        self.assertAlmostEqual(b, 180.0, delta=1.0)

    def test_due_west(self):
        b = NavigationAddon._initial_bearing(0.0, 1.0, 0.0, 0.0)
        self.assertAlmostEqual(b, 270.0, delta=1.0)


class TestCompassPoint(unittest.TestCase):
    def test_cardinal_points(self):
        self.assertEqual(NavigationAddon._compass_point(0), "N")
        self.assertEqual(NavigationAddon._compass_point(90), "E")
        self.assertEqual(NavigationAddon._compass_point(180), "S")
        self.assertEqual(NavigationAddon._compass_point(270), "W")

    def test_intercardinals(self):
        self.assertEqual(NavigationAddon._compass_point(45), "NE")
        self.assertEqual(NavigationAddon._compass_point(135), "SE")

    def test_near_360(self):
        self.assertEqual(NavigationAddon._compass_point(359), "N")


if __name__ == "__main__":
    unittest.main()
