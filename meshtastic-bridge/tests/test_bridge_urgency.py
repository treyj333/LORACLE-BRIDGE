"""Tests for bridge.urgency — heuristic urgency classifier."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.urgency import HeuristicUrgencyClassifier


class TestHeuristicUrgencyClassifier(unittest.TestCase):
    def setUp(self):
        self.c = HeuristicUrgencyClassifier()

    def test_empty_not_urgent(self):
        self.assertFalse(self.c.is_urgent(""))
        self.assertFalse(self.c.is_urgent(None))
        self.assertFalse(self.c.is_urgent("   "))

    def test_distress_keywords(self):
        for text in [
            "Emergency at grid 42N 18E",
            "SOS SOS SOS",
            "Mayday — losing altitude",
            "Need help immediately",
            "URGENT: power substation down",
        ]:
            self.assertTrue(self.c.is_urgent(text), text)

    def test_medical_keywords(self):
        for text in [
            "Casualty taken to FOB",
            "Medic needed on stage",
            "Medevac inbound ETA 12 min",
            "Two injured, one bleeding",
            "Subject is unconscious",
        ]:
            self.assertTrue(self.c.is_urgent(text), text)

    def test_fire_disaster_keywords(self):
        for text in [
            "Fire at building 4",
            "Smoke visible on ridge",
            "Flooding in lower pass",
            "Earthquake 5.2 felt",
        ]:
            self.assertTrue(self.c.is_urgent(text), text)

    def test_threat_keywords(self):
        for text in [
            "Shots fired near gate",
            "Under attack — request support",
            "Hostile contact bearing 210",
        ]:
            self.assertTrue(self.c.is_urgent(text), text)

    def test_stuck_lost_keywords(self):
        for text in [
            "We're stranded at the pass",
            "Vehicle trapped in mud",
            "Party missing, last known bearing",
            "Plane crashed 3km east",
        ]:
            self.assertTrue(self.c.is_urgent(text), text)

    def test_chatter_never_urgent(self):
        for text in ["hi", "hello", "hey!", "roger", "copy that", "wilco",
                     "ok", "thanks!!!", "lol", "haha"]:
            self.assertFalse(self.c.is_urgent(text), text)

    def test_casual_channel_traffic(self):
        for text in [
            "Anyone want coffee before the op?",
            "Moving to checkpoint 3",
            "Weather is clear here",
            "See you at 1800.",
        ]:
            self.assertFalse(self.c.is_urgent(text), text)

    def test_exclamation_plus_uppercase(self):
        # Weak heuristic: multiple ! with uppercase = probable urgency
        self.assertTrue(self.c.is_urgent("CONTACT CONTACT CONTACT!!"))

    def test_single_exclamation_not_urgent(self):
        self.assertFalse(self.c.is_urgent("arrived!"))

    def test_extra_keywords_matched(self):
        c = HeuristicUrgencyClassifier(
            extra_urgent_keywords=["exfil", "compromised"]
        )
        self.assertTrue(c.is_urgent("team is compromised"))
        self.assertTrue(c.is_urgent("EXFIL now"))
        self.assertFalse(c.is_urgent("routine check"))

    def test_extra_keywords_word_boundary(self):
        # Avoid false positive on partial match (e.g. "comPROMISed" but
        # in-word match should still hit since we lowercase first)
        c = HeuristicUrgencyClassifier(extra_urgent_keywords=["tank"])
        self.assertTrue(c.is_urgent("tank spotted at bridge"))
        # "thanks" should not match "tank" since \b prevents sub-word
        self.assertFalse(c.is_urgent("thanks for the update"))


if __name__ == "__main__":
    unittest.main()
