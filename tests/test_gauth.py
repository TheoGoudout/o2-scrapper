"""Tests for Google credential handling.

Focused on the setup mistakes a first-time user actually makes — a missing file, a
"Web application" client instead of a Desktop one, a headless machine — because
each of those used to surface as a raw traceback.
"""

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from o2sync import gauth
from o2sync.errors import GoogleAuthRequired

DESKTOP_CLIENT = {
    "installed": {
        "client_id": "x.apps.googleusercontent.com",
        "client_secret": "s",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}
WEB_CLIENT = {"web": dict(DESKTOP_CLIENT["installed"])}


class ScopeTest(unittest.TestCase):
    def test_asks_only_for_app_created_calendars(self):
        # The whole safety story rests on this: the tool must not be able to reach
        # the user's primary calendar.
        self.assertEqual(gauth.SCOPES, ["https://www.googleapis.com/auth/calendar.app.created"])


class PathTest(unittest.TestCase):
    def test_defaults(self):
        secrets, token = gauth.resolve_paths()
        self.assertEqual(secrets.name, "credentials.json")
        self.assertEqual(token.name, "token.json")

    def test_explicit_paths_win(self):
        secrets, token = gauth.resolve_paths("a/secrets.json", "b/tok.json")
        self.assertEqual(str(secrets), "a/secrets.json")
        self.assertEqual(str(token), "b/tok.json")


class ConsentFlowValidationTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "credentials.json"

    def _expect_error(self, contains):
        with self.assertRaises(GoogleAuthRequired) as caught:
            gauth.run_consent_flow(str(self.path), str(Path(self.dir.name) / "token.json"))
        self.assertIn(contains, str(caught.exception))

    def test_missing_client_secrets(self):
        self._expect_error("No OAuth client secrets")

    def test_web_client_is_rejected_with_a_useful_message(self):
        self.path.write_text(json.dumps(WEB_CLIENT), encoding="utf-8")
        self._expect_error("not a Desktop app client")

    def test_unrecognised_json_is_rejected(self):
        self.path.write_text(json.dumps({"nope": 1}), encoding="utf-8")
        self._expect_error("not a Desktop app client")

    def test_invalid_json_is_rejected(self):
        self.path.write_text("not json", encoding="utf-8")
        self._expect_error("as JSON")

    def test_headless_machine_points_at_print_env(self):
        self.path.write_text(json.dumps(DESKTOP_CLIENT), encoding="utf-8")
        # No browser is available in CI or a container, which is the same situation
        # as running this over SSH.
        self._expect_error("--print-env")


class LoadCredentialsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.token = Path(self.dir.name) / "token.json"

    def test_missing_token_tells_you_to_authorise(self):
        with self.assertRaises(GoogleAuthRequired) as caught:
            gauth.load_credentials(token=str(self.token))
        self.assertIn("o2sync auth", str(caught.exception))

    def test_corrupt_token_tells_you_to_delete_it(self):
        self.token.write_text("not json", encoding="utf-8")
        with self.assertRaises(GoogleAuthRequired) as caught:
            gauth.load_credentials(token=str(self.token))
        self.assertIn("not a valid Google token file", str(caught.exception))

    def test_refresh_token_without_client_details_is_rejected(self):
        with unittest.mock.patch.dict(
            "os.environ", {gauth.ENV_REFRESH_TOKEN: "r"}, clear=False
        ):
            with self.assertRaises(GoogleAuthRequired) as caught:
                gauth.load_credentials(token=str(self.token))
        self.assertIn(gauth.ENV_CLIENT_ID, str(caught.exception))


class EnvBlockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.token = Path(self.dir.name) / "token.json"

    def test_renders_the_three_variables(self):
        self.token.write_text(
            json.dumps(
                {"client_id": "cid", "client_secret": "sec", "refresh_token": "ref"}
            ),
            encoding="utf-8",
        )
        block = gauth.env_block(str(self.token))
        self.assertIn(f"{gauth.ENV_CLIENT_ID}=cid", block)
        self.assertIn(f"{gauth.ENV_CLIENT_SECRET}=sec", block)
        self.assertIn(f"{gauth.ENV_REFRESH_TOKEN}=ref", block)

    def test_complains_about_a_token_with_no_refresh_token(self):
        self.token.write_text(
            json.dumps({"client_id": "cid", "client_secret": "sec"}), encoding="utf-8"
        )
        with self.assertRaises(GoogleAuthRequired) as caught:
            gauth.env_block(str(self.token))
        self.assertIn("refresh_token", str(caught.exception))

    def test_missing_token(self):
        with self.assertRaises(GoogleAuthRequired):
            gauth.env_block(str(self.token))


if __name__ == "__main__":
    unittest.main()
