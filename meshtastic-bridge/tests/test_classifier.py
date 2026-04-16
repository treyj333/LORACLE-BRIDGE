"""Classifier test corpus — >=80% accuracy target."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routing.classifier import classify, CLASSIFIER_VERSION
from routing.tiers import Tier
from routing.router import parse_prefix

# Each entry: (query, expected_tier)
CLASSIFIER_CORPUS = [
    # ── TINY: trivial / short / greetings ────────────────────────────
    ("hi", Tier.TINY),
    ("hello", Tier.TINY),
    ("thanks", Tier.TINY),
    ("thank you", Tier.TINY),
    ("ok", Tier.TINY),
    ("yes", Tier.TINY),
    ("no", Tier.TINY),
    ("what time is it", Tier.TINY),
    ("what day is it", Tier.TINY),
    ("who is the president", Tier.TINY),
    ("where is camp", Tier.TINY),
    ("when is sunset", Tier.TINY),
    ("what is a snake", Tier.TINY),
    ("gm", Tier.TINY),
    ("yo", Tier.TINY),

    # ── BIG: complex / analytical / medical ──────────────────────────
    ("how do i treat a snake bite in the field", Tier.BIG),
    ("explain photosynthesis", Tier.BIG),
    ("compare mesh flooding to next-hop routing", Tier.BIG),
    ("triage chest pain shortness of breath", Tier.BIG),
    ("walk me through setting up a shelter", Tier.BIG),
    ("step by step water purification", Tier.BIG),
    ("what are the pros and cons of solar charging", Tier.BIG),
    ("analyze the terrain for a good camp site near the river with wind protection and drainage", Tier.BIG),
    ("how does the meshtastic protocol handle packet collision and retransmission", Tier.BIG),
    ("debug why my radio keeps disconnecting", Tier.BIG),
    ("troubleshoot intermittent GPS signal", Tier.BIG),
    ("diagnose fever with rash after tick exposure", Tier.BIG),
    ("emergency treatment for hypothermia", Tier.BIG),
    ("explain the difference between LoRa and LoRaWAN", Tier.BIG),
    ("how would i build a mesh repeater", Tier.BIG),

    # ── STANDARD: medium complexity ──────────────────────────────────
    ("is the weather going to be bad", Tier.STANDARD),
    ("what plants are edible around here", Tier.STANDARD),
    ("how far is the nearest town", Tier.STANDARD),
    ("can you summarize the latest reports", Tier.STANDARD),
    ("what channel should i use for the group", Tier.STANDARD),
    ("tell me about this area", Tier.STANDARD),
    ("any updates from base camp", Tier.STANDARD),
    ("whats the best way to signal for help", Tier.STANDARD),
    ("how much battery does the radio have left", Tier.STANDARD),
    ("list the supplies we need for tomorrow", Tier.STANDARD),

    # ── Length-based BIG (>160 chars) ─────────────────────────────────
    ("I need to understand how the mesh network handles routing when there are multiple repeaters in range and some of them have weak signal strength. Can you break down the path selection algorithm and how hop limits affect delivery?", Tier.BIG),

    # ── Multi-question → BIG ─────────────────────────────────────────
    ("what is the altitude here? and how far to the ridge? and when does the sun set?", Tier.BIG),
]


class TestClassifier(unittest.TestCase):
    def test_corpus_accuracy(self):
        """Classifier must hit >=80% accuracy on the corpus."""
        correct = 0
        errors = []
        for query, expected in CLASSIFIER_CORPUS:
            result = classify(query)
            if result == expected:
                correct += 1
            else:
                errors.append(f"  '{query[:50]}...' expected {expected.value} got {result.value}")

        total = len(CLASSIFIER_CORPUS)
        accuracy = correct / total
        msg = f"Accuracy: {correct}/{total} ({accuracy:.0%})"
        if errors:
            msg += "\nMisclassified:\n" + "\n".join(errors[:10])
        self.assertGreaterEqual(accuracy, 0.80, msg)

    def test_classifier_is_fast(self):
        """Classifier should run in <5ms per query on average."""
        queries = [q for q, _ in CLASSIFIER_CORPUS]
        start = time.time()
        for _ in range(100):
            for q in queries:
                classify(q)
        elapsed = time.time() - start
        avg_ms = (elapsed / (100 * len(queries))) * 1000
        self.assertLess(avg_ms, 5, f"Avg {avg_ms:.3f}ms per classify (target <5ms)")

    def test_version_exists(self):
        self.assertEqual(CLASSIFIER_VERSION, "1.0")


class TestPrefixParsing(unittest.TestCase):
    def test_tiny_prefix(self):
        tier, query = parse_prefix("!tiny how are you")
        self.assertEqual(tier, Tier.TINY)
        self.assertEqual(query, "how are you")

    def test_std_prefix(self):
        tier, query = parse_prefix("!std what plants are safe")
        self.assertEqual(tier, Tier.STANDARD)
        self.assertEqual(query, "what plants are safe")

    def test_big_prefix(self):
        tier, query = parse_prefix("!big explain everything")
        self.assertEqual(tier, Tier.BIG)
        self.assertEqual(query, "explain everything")

    def test_case_insensitive(self):
        tier, query = parse_prefix("!BIG test query")
        self.assertEqual(tier, Tier.BIG)
        self.assertEqual(query, "test query")

    def test_no_prefix(self):
        tier, query = parse_prefix("just a normal message")
        self.assertIsNone(tier)
        self.assertEqual(query, "just a normal message")

    def test_prefix_only_no_query(self):
        tier, query = parse_prefix("!big")
        self.assertEqual(tier, Tier.BIG)
        self.assertEqual(query, "")

    def test_unknown_prefix_not_matched(self):
        tier, query = parse_prefix("!invalid something")
        self.assertIsNone(tier)
        self.assertEqual(query, "!invalid something")

    def test_command_not_matched(self):
        """!help is a command, not a tier prefix."""
        tier, query = parse_prefix("!help")
        self.assertIsNone(tier)
        self.assertEqual(query, "!help")


class TestRouter(unittest.TestCase):
    def setUp(self):
        from routing.tiers import TierConfig
        self.tiers = {
            Tier.TINY: TierConfig(Tier.TINY, "gemma3:4b", True),
            Tier.STANDARD: TierConfig(Tier.STANDARD, "qwen3:8b", True),
            Tier.BIG: TierConfig(Tier.BIG, "phi4:14b", True),
        }
        self.models = ["gemma3:4b", "qwen3:8b", "phi4:14b"]

    def test_auto_routing(self):
        from routing.router import route
        tier, model = route("hi", self.tiers, self.models)
        self.assertEqual(tier, Tier.TINY)
        self.assertEqual(model, "gemma3:4b")

    def test_override(self):
        from routing.router import route
        tier, model = route("hi", self.tiers, self.models, override=Tier.BIG)
        self.assertEqual(tier, Tier.BIG)
        self.assertEqual(model, "phi4:14b")

    def test_disabled_tier_raises(self):
        from routing.router import route
        from routing.tiers import TierConfig, ModelDisabledError
        tiers = dict(self.tiers)
        tiers[Tier.BIG] = TierConfig(Tier.BIG, "phi4:14b", False)
        with self.assertRaises(ModelDisabledError):
            route("explain this", tiers, self.models, override=Tier.BIG)

    def test_missing_model_raises(self):
        from routing.router import route
        from routing.tiers import ModelNotInstalledError
        with self.assertRaises(ModelNotInstalledError):
            route("hi", self.tiers, ["gemma3:4b"], override=Tier.BIG)

    def test_routing_disabled_uses_standard(self):
        from routing.router import route
        tier, model = route("explain quantum physics", self.tiers, self.models, auto_routing=False)
        self.assertEqual(tier, Tier.STANDARD)


if __name__ == "__main__":
    unittest.main()
