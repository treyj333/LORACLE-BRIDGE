"""Tests for bridge.rate_limit — per-direction sliding-window rate limiter."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.rate_limit import RelayRateLimiter


class TestRelayRateLimiter(unittest.TestCase):
    def test_allows_under_limit(self):
        rl = RelayRateLimiter(max_events=3, window_seconds=60)
        self.assertTrue(rl.allow("meshtastic", "meshcore", 0))
        self.assertTrue(rl.allow("meshtastic", "meshcore", 0))
        self.assertTrue(rl.allow("meshtastic", "meshcore", 0))

    def test_blocks_over_limit(self):
        rl = RelayRateLimiter(max_events=3, window_seconds=60)
        for _ in range(3):
            rl.allow("meshtastic", "meshcore", 0)
        self.assertFalse(rl.allow("meshtastic", "meshcore", 0))

    def test_independent_directions(self):
        # Exhausting one direction must not affect another.
        rl = RelayRateLimiter(max_events=2, window_seconds=60)
        rl.allow("meshtastic", "meshcore", 0)
        rl.allow("meshtastic", "meshcore", 0)
        self.assertFalse(rl.allow("meshtastic", "meshcore", 0))
        # Same protocols, different channel — fresh bucket.
        self.assertTrue(rl.allow("meshtastic", "meshcore", 1))
        # Reverse direction — fresh bucket.
        self.assertTrue(rl.allow("meshcore", "meshtastic", 0))

    def test_window_rolls_over(self):
        rl = RelayRateLimiter(max_events=2, window_seconds=0.05)
        rl.allow("mt", "mc", 0)
        rl.allow("mt", "mc", 0)
        self.assertFalse(rl.allow("mt", "mc", 0))
        time.sleep(0.07)
        self.assertTrue(rl.allow("mt", "mc", 0))

    def test_current(self):
        rl = RelayRateLimiter(max_events=5, window_seconds=60)
        self.assertEqual(rl.current("mt", "mc", 0), 0)
        rl.allow("mt", "mc", 0)
        rl.allow("mt", "mc", 0)
        self.assertEqual(rl.current("mt", "mc", 0), 2)

    def test_snapshot(self):
        rl = RelayRateLimiter(max_events=5, window_seconds=60)
        rl.allow("mt", "mc", 0)
        rl.allow("mt", "mc", 0)
        rl.allow("mc", "mt", 3)
        snap = rl.snapshot()
        self.assertEqual(snap.get("mt->mc:ch0"), 2)
        self.assertEqual(snap.get("mc->mt:ch3"), 1)

    def test_rejected_events_not_counted(self):
        # A rejected allow() should NOT consume quota — next window
        # starts with whatever was actually delivered.
        rl = RelayRateLimiter(max_events=2, window_seconds=60)
        rl.allow("mt", "mc", 0)
        rl.allow("mt", "mc", 0)
        self.assertFalse(rl.allow("mt", "mc", 0))
        # Still exactly 2 recorded, not 3
        self.assertEqual(rl.current("mt", "mc", 0), 2)

    def test_validates_ctor_args(self):
        with self.assertRaises(ValueError):
            RelayRateLimiter(max_events=0, window_seconds=60)
        with self.assertRaises(ValueError):
            RelayRateLimiter(max_events=-1, window_seconds=60)
        with self.assertRaises(ValueError):
            RelayRateLimiter(max_events=1, window_seconds=0)
        with self.assertRaises(ValueError):
            RelayRateLimiter(max_events=1, window_seconds=-5)


class TestRelayWithRateLimiter(unittest.TestCase):
    """Verify Relay actually honours the limiter."""

    def test_relay_blocks_rate_limited(self):
        from bridge.policy import AlwaysRelay
        from bridge.relay import Relay

        sends = []
        def fake_send(dest, text, channel):
            sends.append((dest, text, channel))

        rl = RelayRateLimiter(max_events=2, window_seconds=60)
        relay = Relay(send_fn=fake_send, policy=AlwaysRelay(), rate_limiter=rl)

        # Different text each time so dedup doesn't interfere
        relay.observe("meshtastic", "!abc", "msg 1", channel=0, is_dm=False)
        relay.observe("meshtastic", "!abc", "msg 2", channel=0, is_dm=False)
        relay.observe("meshtastic", "!abc", "msg 3", channel=0, is_dm=False)

        self.assertEqual(len(sends), 2)
        stats = relay.stats()
        self.assertEqual(stats["relayed"], 2)
        self.assertGreaterEqual(stats["rate_limited"], 1)

    def test_force_relay_bypasses_rate_limit(self):
        # !urgent should cross even when the channel is rate-limited.
        from bridge.policy import AlwaysRelay
        from bridge.relay import Relay

        sends = []
        def fake_send(dest, text, channel):
            sends.append((dest, text, channel))

        rl = RelayRateLimiter(max_events=1, window_seconds=60)
        relay = Relay(send_fn=fake_send, policy=AlwaysRelay(), rate_limiter=rl)

        relay.observe("meshtastic", "!abc", "normal msg", channel=0, is_dm=False)
        relay.observe("meshtastic", "!abc", "another one", channel=0, is_dm=False)
        # rate-limited
        self.assertEqual(len(sends), 1)
        # !urgent should still deliver
        relay.observe("meshtastic", "!abc", "!urgent incident", channel=0, is_dm=False)
        self.assertEqual(len(sends), 2)


if __name__ == "__main__":
    unittest.main()
