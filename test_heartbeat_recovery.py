import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import healthcheck


ENV = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_TOKEN": "test",
    "GITHUB_REPOSITORY": "test/feed",
    "ROTL_RECOVER_UPDATER": "true",
}
STALE = {
    "id": 35135000522,
    "status": "completed",
    "conclusion": "success",
    "updated_at": "2026-09-16T18:32:58Z",
}


def fresh_run():
    return {
        "id": 999,
        "status": "completed",
        "conclusion": "success",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@mock.patch.dict("os.environ", ENV, clear=True)
class HeartbeatRecoveryTests(unittest.TestCase):
    @mock.patch("healthcheck.datetime", wraps=datetime)
    @mock.patch("healthcheck.actions_api")
    def test_historical_failure_recovers_after_new_success(self, api, clock):
        # Replay the failed health run and the updater that completed 47 seconds later.
        clock.now.side_effect = [
            datetime(2026, 9, 16, 21, 40, 45, tzinfo=timezone.utc),
            datetime(2026, 9, 16, 21, 41, 33, tzinfo=timezone.utc),
        ]
        recovered = dict(STALE, id=35153752002, updated_at="2026-09-16T21:41:32Z")
        api.side_effect = [
            {"workflow_runs": [STALE]}, {}, {"workflow_runs": [recovered, STALE]}
        ]
        fails = []
        healthcheck.check_updater_heartbeat(fails)
        self.assertEqual(fails, [])
        self.assertEqual(api.call_args_list[1].args[2:],
                         ("workflows/update.yml/dispatches", {"ref": "main"}))

    @mock.patch.dict("os.environ", {"ROTL_REFRESH_UPDATER": "true"})
    @mock.patch("healthcheck.actions_api", side_effect=OSError("API unavailable"))
    def test_manual_refresh_cannot_skip_initial_api_failure(self, api):
        fails = []
        healthcheck.check_updater_heartbeat(fails)
        self.assertIn("requested refresh unavailable", fails[0])
        api.assert_called_once()

    @mock.patch.dict("os.environ", {"ROTL_REFRESH_UPDATER": "true"})
    @mock.patch("healthcheck.actions_api")
    def test_manual_refresh_requires_a_different_success(self, api):
        previous = fresh_run()
        recovered = dict(previous, id=1000)
        api.side_effect = [
            {"workflow_runs": [previous]}, {},
            {"workflow_runs": [recovered, previous]},
        ]
        fails = []
        healthcheck.check_updater_heartbeat(fails)
        self.assertEqual(fails, [])
        self.assertEqual(api.call_args_list[1].args[2], "workflows/update.yml/dispatches")

    @mock.patch("healthcheck.actions_api")
    def test_fresh_success_does_not_dispatch(self, api):
        api.return_value = {"workflow_runs": [fresh_run()]}
        fails = []
        healthcheck.check_updater_heartbeat(fails)
        self.assertEqual(fails, [])
        api.assert_called_once()
        self.assertIn("branch=main", api.call_args.args[2])

    @mock.patch("healthcheck.actions_api")
    def test_existing_active_update_is_awaited(self, api):
        active = dict(STALE, id=999, conclusion=None, status="in_progress")
        api.side_effect = [
            {"workflow_runs": [active, STALE]},
            {"workflow_runs": [fresh_run(), STALE]},
        ]
        fails = []
        healthcheck.check_updater_heartbeat(fails)
        self.assertEqual(fails, [])
        self.assertTrue(all("/dispatches" not in call.args[2] for call in api.call_args_list))

    @mock.patch("healthcheck.time.sleep")
    @mock.patch("healthcheck.time.monotonic", side_effect=[0, 0, 0, 301])
    @mock.patch("healthcheck.actions_api")
    def test_persistent_failed_or_queued_recovery_still_fails(self, api, _clock, _sleep):
        api.side_effect = [
            {"workflow_runs": [STALE]}, {},
            {"workflow_runs": [dict(STALE, id=999, conclusion="failure"), STALE]},
        ]
        fails = []
        healthcheck.check_updater_heartbeat(fails)
        self.assertEqual(len(fails), 1)
        self.assertIn("recovery timed out", fails[0])

    @mock.patch("healthcheck.time.sleep")
    @mock.patch("healthcheck.time.monotonic", side_effect=[0, 0, 0, 301])
    @mock.patch("healthcheck.actions_api")
    def test_old_success_cannot_clear_alarm(self, api, _clock, _sleep):
        api.side_effect = [
            {"workflow_runs": [STALE]}, {},
            {"workflow_runs": [dict(STALE, id=123), STALE]},
        ]
        fails = []
        healthcheck.check_updater_heartbeat(fails)
        self.assertTrue(fails)

    @mock.patch("healthcheck.actions_api")
    def test_dispatch_denied_still_fails(self, api):
        api.side_effect = [{"workflow_runs": [STALE]}, PermissionError("HTTP 403")]
        fails = []
        healthcheck.check_updater_heartbeat(fails)
        self.assertIn("recovery failed", fails[0])

    @mock.patch("healthcheck.actions_api")
    def test_poll_error_does_not_clear_known_staleness(self, api):
        api.side_effect = [{"workflow_runs": [STALE]}, {}, OSError("API unavailable")]
        fails = []
        healthcheck.check_updater_heartbeat(fails)
        self.assertIn("recovery failed", fails[0])

    @mock.patch.dict("os.environ", {"GITHUB_REF": "refs/heads/test"})
    @mock.patch("healthcheck.actions_api")
    def test_other_branch_cannot_dispatch_production(self, api):
        api.return_value = {"workflow_runs": [STALE]}
        fails = []
        healthcheck.check_updater_heartbeat(fails)
        self.assertTrue(fails)
        api.assert_called_once()

    @mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "false"})
    @mock.patch("healthcheck.actions_api")
    def test_local_check_is_read_only(self, api):
        api.return_value = {"workflow_runs": [STALE]}
        fails = []
        healthcheck.check_updater_heartbeat(fails)
        self.assertTrue(fails)
        api.assert_called_once()

    @mock.patch("healthcheck.actions_api")
    def test_recovery_does_not_erase_feed_failure(self, api):
        api.side_effect = [
            {"workflow_runs": [STALE]}, {}, {"workflow_runs": [fresh_run()]}
        ]
        fails = ["Pages: INVALID XML"]
        healthcheck.check_updater_heartbeat(fails)
        self.assertEqual(fails, ["Pages: INVALID XML"])

    @mock.patch("healthcheck.actions_api")
    def test_no_success_can_recover(self, api):
        api.side_effect = [
            {"workflow_runs": []}, {}, {"workflow_runs": [fresh_run()]}
        ]
        fails = []
        healthcheck.check_updater_heartbeat(fails)
        self.assertEqual(fails, [])

    @mock.patch("healthcheck.urllib.request.urlopen")
    def test_dispatch_request_uses_json_and_accepts_empty_response(self, urlopen):
        urlopen.return_value.__enter__.return_value.read.return_value = b""
        with redirect_stdout(io.StringIO()) as output:
            result = healthcheck.actions_api(
                "secret-test-token", "test/feed", "workflows/update.yml/dispatches",
                {"ref": "main"},
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"ref": "main"})
        self.assertEqual(result, {})
        self.assertNotIn("secret-test-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
