"""Tests for the scheduling loop, interval parsing and the liveness heartbeat.

These cover the deployment behaviour that made the container restart-loop on
Coolify: the process has to stay alive, survive a transient failure, and stop
promptly when the platform sends SIGTERM.
"""

import argparse
import json
import tempfile
import time
import unittest
from pathlib import Path

from o2sync import cli, heartbeat
from o2sync.errors import (
    CalendarAPIError,
    GoogleAuthRequired,
    InvalidCredentials,
    ServiceUnavailable,
)


class IntervalTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(cli.parse_interval("90s"), 90)
        self.assertEqual(cli.parse_interval("30m"), 1800)
        self.assertEqual(cli.parse_interval("6h"), 21600)
        self.assertEqual(cli.parse_interval("1d"), 86400)

    def test_bare_number_is_seconds(self):
        self.assertEqual(cli.parse_interval("3600"), 3600)

    def test_rejects_nonsense(self):
        for value in ["", "soon", "6x", "-", "h"]:
            with self.subTest(value=value), self.assertRaises(SystemExit):
                cli.parse_interval(value)

    def test_rejects_an_interval_that_would_hammer_o2(self):
        with self.assertRaises(SystemExit):
            cli.parse_interval("5s")


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "beat.json"
        self.addCleanup(self.dir.cleanup)

    def test_missing_heartbeat_is_unhealthy(self):
        healthy, message = heartbeat.check(self.path)
        self.assertFalse(healthy)
        self.assertIn("no heartbeat", message)

    def test_fresh_heartbeat_is_healthy(self):
        heartbeat.write(self.path, 3600, "ok", "1 created")
        healthy, message = heartbeat.check(self.path)
        self.assertTrue(healthy)
        self.assertIn("alive", message)

    def test_stale_heartbeat_is_unhealthy(self):
        heartbeat.write(self.path, 3600, "ok")
        # Two intervals plus the grace period have passed.
        future = time.time() + 2 * 3600 + heartbeat.GRACE_SECONDS + 1
        healthy, message = heartbeat.check(self.path, now=future)
        self.assertFalse(healthy)
        self.assertIn("stuck", message)

    def test_allowance_scales_with_the_interval(self):
        heartbeat.write(self.path, 86400, "ok")
        # Well past a short interval's allowance, but fine for a daily one.
        healthy, _ = heartbeat.check(self.path, now=time.time() + 7200)
        self.assertTrue(healthy)

    def test_a_failed_cycle_still_counts_as_alive(self):
        # Restarting the container cannot fix a remote outage, so liveness must
        # not depend on the sync succeeding.
        heartbeat.write(self.path, 3600, "error", "O2 unreachable")
        healthy, message = heartbeat.check(self.path)
        self.assertTrue(healthy)
        self.assertIn("error", message)

    def test_corrupt_heartbeat_is_unhealthy(self):
        self.path.write_text("{not json", encoding="utf-8")
        healthy, _ = heartbeat.check(self.path)
        self.assertFalse(healthy)

    def test_malformed_heartbeat_is_unhealthy(self):
        self.path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
        healthy, message = heartbeat.check(self.path)
        self.assertFalse(healthy)
        self.assertIn("malformed", message)

    def test_write_never_raises_on_a_bad_path(self):
        heartbeat.write(Path("/proc/nope/beat.json"), 3600, "ok")  # must not raise

    def test_writes_are_atomic(self):
        heartbeat.write(self.path, 3600, "ok")
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class LoopTest(unittest.TestCase):
    """The loop is driven with a fake ``_sync_once`` so nothing touches a network."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "beat.json"
        self.addCleanup(self.dir.cleanup)
        self._real_sync_once = cli._sync_once
        self.addCleanup(setattr, cli, "_sync_once", self._real_sync_once)

    def _args(self):
        return argparse.Namespace(heartbeat_file=str(self.path), no_prompt=False)

    def _drive(self, outcomes, interval=0.01):
        """Run the loop, returning after ``outcomes`` is exhausted.

        The interval is passed straight to the loop rather than through
        ``parse_interval``, so the tests are not bound by its 60s floor.
        """
        calls = []

        def fake_sync_once(args):
            calls.append(1)
            outcome = outcomes[len(calls) - 1]
            if len(calls) >= len(outcomes):
                # Ask the loop to stop once we've delivered every outcome.
                cli.signal.raise_signal(cli.signal.SIGINT)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        cli._sync_once = fake_sync_once
        code = cli._sync_loop(self._args(), interval)
        return code, calls

    def test_runs_repeatedly_until_stopped(self):
        code, calls = self._drive(["1 created", "0 created"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(len(calls), 2)

    def test_survives_a_transient_failure_and_keeps_going(self):
        code, calls = self._drive(
            [ServiceUnavailable("O2 down"), CalendarAPIError("Google 503"), "0 created"]
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(len(calls), 3)
        healthy, _ = heartbeat.check(self.path)
        self.assertTrue(healthy)

    def test_stops_on_bad_o2_credentials(self):
        # Retrying forever would hammer the login endpoint until somebody noticed.
        code, calls = self._drive([InvalidCredentials("nope")])
        self.assertEqual(code, cli.EXIT_AUTH)
        self.assertEqual(len(calls), 1)

    def test_stops_when_google_authorisation_is_needed(self):
        code, calls = self._drive([GoogleAuthRequired("run o2sync auth")])
        self.assertEqual(code, cli.EXIT_AUTH)
        self.assertEqual(len(calls), 1)

    def test_records_the_failure_in_the_heartbeat(self):
        self._drive([ServiceUnavailable("O2 down"), "0 created"])
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "ok")

    def test_never_prompts_in_loop_mode(self):
        args = self._args()

        def fake_sync_once(inner_args):
            self.assertTrue(inner_args.no_prompt)
            cli.signal.raise_signal(cli.signal.SIGINT)
            return "0 created"

        cli._sync_once = fake_sync_once
        cli._sync_loop(args, 0.01)


class HealthcheckCommandTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "beat.json"
        self.addCleanup(self.dir.cleanup)

    def test_exit_code_reflects_health(self):
        self.assertEqual(cli.main(["healthcheck", "--heartbeat-file", str(self.path)]), 1)
        heartbeat.write(self.path, 3600, "ok")
        self.assertEqual(cli.main(["healthcheck", "--heartbeat-file", str(self.path)]), 0)


if __name__ == "__main__":
    unittest.main()
