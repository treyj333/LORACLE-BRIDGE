"""Tests for CoverageLogger — JSONL signal coverage logging (Phase 5)."""

import json
import os
import tempfile
import time
import unittest
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coverage_logger import CoverageLogger, MIN_TIME_DELTA_S, MIN_DIST_M


class TestCoverageLogger(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = os.path.join(self.tmpdir, "coverage.jsonl")
        self.logger = CoverageLogger(self.log_path)

    def test_first_sample_always_logged(self):
        result = self.logger.record("!node1", 34.0, -118.0, rssi=-85, snr=7.5)
        self.assertTrue(result)
        samples = self.logger.read_all()
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["node"], "!node1")

    def test_throttled_same_location_same_time(self):
        """Same node, same location, within time delta — should throttle."""
        self.logger.record("!node1", 34.0, -118.0, ts=1000.0)
        result = self.logger.record("!node1", 34.0, -118.0, ts=1001.0)
        self.assertFalse(result)
        self.assertEqual(len(self.logger.read_all()), 1)

    def test_not_throttled_after_time_delta(self):
        """Same node, same location, but enough time has passed."""
        self.logger.record("!node1", 34.0, -118.0, ts=1000.0)
        result = self.logger.record("!node1", 34.0, -118.0, ts=1000.0 + MIN_TIME_DELTA_S + 1)
        self.assertTrue(result)
        self.assertEqual(len(self.logger.read_all()), 2)

    def test_not_throttled_after_distance(self):
        """Same node, enough distance moved, even within time window."""
        self.logger.record("!node1", 34.0, -118.0, ts=1000.0)
        # Move ~111 meters north (0.001 degrees latitude)
        result = self.logger.record("!node1", 34.001, -118.0, ts=1001.0)
        self.assertTrue(result)

    def test_different_nodes_not_throttled(self):
        self.logger.record("!node1", 34.0, -118.0)
        result = self.logger.record("!node2", 34.0, -118.0)
        self.assertTrue(result)

    def test_clear(self):
        self.logger.record("!node1", 34.0, -118.0)
        self.logger.record("!node2", 35.0, -117.0)
        removed = self.logger.clear()
        self.assertEqual(removed, 2)
        self.assertEqual(len(self.logger.read_all()), 0)

    def test_read_all_with_limit(self):
        for i in range(20):
            self.logger.record(f"!node{i}", 34.0 + i * 0.01, -118.0, ts=float(i * 100))
        samples = self.logger.read_all(limit=5)
        self.assertEqual(len(samples), 5)

    def test_stats(self):
        self.logger.record("!node1", 34.0, -118.0, rssi=-85, snr=7.5, ts=1000.0)
        self.logger.record("!node2", 35.0, -117.0, rssi=-90, snr=5.0, ts=2000.0)
        stats = self.logger.stats()
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["nodes"], 2)
        self.assertIsNotNone(stats["bbox"])

    def test_stats_empty(self):
        stats = self.logger.stats()
        self.assertEqual(stats["count"], 0)

    def test_rssi_snr_optional(self):
        """record() should work without rssi/snr."""
        result = self.logger.record("!node1", 34.0, -118.0)
        self.assertTrue(result)
        samples = self.logger.read_all()
        self.assertIsNone(samples[0].get("rssi"))


if __name__ == "__main__":
    unittest.main()
