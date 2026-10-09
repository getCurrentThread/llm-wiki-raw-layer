import base64
import hashlib
import io
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib import error, request
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from gmail.auth import pkce, callback_code, authorize, refresh, token_request, credential_path, SCOPE
from tools.archiveio import CollectorError, load
from .fixtures import Fixture, Response
from . import helpers as h


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(h.force_rmtree, self.fx.base)
        self.client_file, self.token_file = self.fx.base / "client.json", self.fx.base / "token.json"
        self.client_file.write_text(json.dumps({"installed": {"client_id": "synthetic-client", "client_secret": "synthetic-secret"}}))

    def authorize_serving(self, opener, timeout=30):
        # authorize() on its real loopback server in a thread; the consent URL
        # arrives on stderr exactly as in the documented manual flow.
        err, box = io.StringIO(), {}

        def serve():
            try:
                with patch("sys.stderr", err), patch("rawlayer.capture.precheck.check"):
                    box["value"] = authorize(self.client_file, self.token_file, opener=opener, timeout=timeout)
            except Exception as e:
                box["error"] = e

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        for _ in range(200):
            lines = err.getvalue().strip().splitlines()
            if lines:
                return lines[0], box, thread
            time.sleep(0.05)
        thread.join(5)
        self.fail("consent URL not printed")

    def callback(self, url, **params):
        # One real HTTP request to the OS-chosen loopback port; no external network.
        redirect = parse_qs(urlsplit(url).query)["redirect_uri"][0]
        try:
            with request.urlopen(redirect + "?" + urlencode(params), timeout=10) as response:
                return response.status, response.read()
        except error.HTTPError as e:
            code, body = e.code, e.read()
            e.close()
            return code, body

    def test_pkce_challenge_and_entropy(self):
        verifier, challenge = pkce()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertLessEqual(len(verifier), 128)
        self.assertEqual(challenge, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="))
        self.assertNotEqual(verifier, pkce()[0])

    def test_state_mismatch_duplicate_and_error(self):
        for url in ("/callback?state=wrong&code=synthetic", "/callback?state=right&state=wrong&code=x"):
            with self.assertRaisesRegex(CollectorError, "state_mismatch"):
                callback_code(url, "right")
        with self.assertRaisesRegex(CollectorError, "auth_failed"):
            callback_code("/callback?state=right&error=access_denied", "right")

    def test_loopback_real_http_exchange_and_refresh(self):
        calls = []
        def opener(req, **kwargs):
            calls.append(parse_qs(req.data.decode()))
            return Response({"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"})
        url, box, thread = self.authorize_serving(opener)
        consent = parse_qs(urlsplit(url).query)
        self.assertTrue(consent["redirect_uri"][0].startswith("http://127.0.0.1:"))
        self.assertEqual(consent["scope"], [SCOPE])
        self.assertEqual(consent["code_challenge_method"], ["S256"])
        status, body = self.callback(url, state=consent["state"][0], code="synthetic-code")
        self.assertEqual(status, 200)
        self.assertIn(b"Authentication finished", body)
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(box.get("value"), {"status": "auth_ready"})
        self.assertIsNone(box.get("error"))
        verifier = calls[0]["code_verifier"][0]
        self.assertEqual(consent["code_challenge"][0], base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="))
        self.assertEqual(calls[0]["grant_type"], ["authorization_code"])
        self.assertEqual(calls[0]["redirect_uri"], [consent["redirect_uri"][0]])
        with patch("rawlayer.capture.precheck.check"):
            self.assertEqual(refresh(self.client_file, self.token_file, opener=opener), "synthetic-access")
        self.assertEqual(calls[1]["grant_type"], ["refresh_token"])
        self.assertEqual(load(self.token_file), {"refresh_token": "synthetic-refresh"})
        self.assertFalse(list(self.fx.base.glob("*.tmp")))

    def test_loopback_state_mismatch_rejected(self):
        with patch("gmail.auth.token_request") as exchange:
            url, box, thread = self.authorize_serving(lambda *a, **k: self.fail("exchange must not run"))
            status, _ = self.callback(url, state="wrong", code="synthetic")
            self.assertEqual(status, 400)
            thread.join(30)
            self.assertFalse(thread.is_alive())
            self.assertRegex(str(box.get("error")), "state_mismatch")
            exchange.assert_not_called()
        self.assertFalse(self.token_file.exists())

    def test_loopback_error_parameter_rejected(self):
        with patch("gmail.auth.token_request") as exchange:
            url, box, thread = self.authorize_serving(lambda *a, **k: self.fail("exchange must not run"))
            state = parse_qs(urlsplit(url).query)["state"][0]
            status, _ = self.callback(url, state=state, error="access_denied")
            self.assertEqual(status, 400)
            thread.join(30)
            self.assertFalse(thread.is_alive())
            self.assertRegex(str(box.get("error")), "auth_failed")
            exchange.assert_not_called()
        self.assertFalse(self.token_file.exists())

    def test_invalid_grant_and_redaction(self):
        def opener(req, **kwargs):
            raise HTTPError("https://example.com", 400, "synthetic-secret", {},
                            io.BytesIO(b'{"error":"invalid_grant","description":"synthetic-secret"}'))
        with self.assertRaisesRegex(CollectorError, "auth_required") as caught:
            token_request({}, opener)
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_protected_paths_and_missing_token(self):
        with self.assertRaisesRegex(CollectorError, "unsafe_token_path"):
            credential_path("relative-token.json")
        with patch("rawlayer.capture.precheck.check"):
            with self.assertRaisesRegex(CollectorError, "auth_required"):
                refresh(self.client_file, self.token_file, opener=lambda *a, **k: self.fail("network"))

    def test_auth_missing_refresh_not_persisted(self):
        url, box, thread = self.authorize_serving(lambda *a, **k: Response({"access_token": "synthetic"}))
        state = parse_qs(urlsplit(url).query)["state"][0]
        status, _ = self.callback(url, state=state, code="synthetic")
        self.assertEqual(status, 200)
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertRegex(str(box.get("error")), "auth_required")
        self.assertFalse(self.token_file.exists())

    def test_failed_token_write_keeps_previous_token(self):
        self.token_file.write_text(json.dumps({"refresh_token": "old-synthetic-refresh"}))
        def opener(req, **kwargs):
            return Response({"access_token": "new-synthetic", "refresh_token": "new-synthetic-refresh"})
        with patch("rawlayer.capture.precheck.check"), patch("time.sleep", lambda n: None), \
                patch("os.replace", side_effect=PermissionError(13, "synthetic refusal")):
            with self.assertRaisesRegex(CollectorError, "archive_busy"):
                refresh(self.client_file, self.token_file, opener=opener)
        self.assertEqual(load(self.token_file), {"refresh_token": "old-synthetic-refresh"})
        self.assertFalse(list(self.fx.base.glob("*.tmp")))
