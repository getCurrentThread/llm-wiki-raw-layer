"""Installed-app OAuth with loopback, state and PKCE; injected HTTP for tests."""
import base64
import hashlib
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import secrets
import sys
import time
from urllib import error, parse, request
from tools.archiveio import CollectorError, atomic, credential_path, load

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def client_config(path):
    try:
        obj = load(credential_path(path))["installed"]
        if not all(isinstance(obj.get(k), str) and obj[k] for k in ("client_id", "client_secret")):
            raise ValueError("client")
        return {k: obj[k] for k in ("client_id", "client_secret")}
    except (OSError, ValueError, TypeError, KeyError):
        raise CollectorError("auth_required") from None


def pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    return verifier, challenge


def token_request(fields, opener=request.urlopen):
    req = request.Request(TOKEN_URL, data=parse.urlencode(fields).encode("ascii"),
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener(req, timeout=60) as response:
            obj = json.loads(response.read())
    except error.HTTPError as e:
        try:
            obj = json.loads(e.read())
        except (OSError, ValueError):
            obj = {}
        finally:
            e.close()
        category = "auth_required" if obj.get("error") == "invalid_grant" or e.code == 401 else "auth_failed"
        raise CollectorError(category, e.code) from None
    except (OSError, error.URLError, ValueError):
        raise CollectorError("transport_error") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("access_token"), str) or not obj["access_token"]:
        raise CollectorError("auth_required" if isinstance(obj, dict) and obj.get("error") == "invalid_grant" else "auth_failed")
    return obj


def callback_code(url, state):
    q = parse.parse_qs(parse.urlsplit(url).query)
    states = q.get("state", [])
    if len(states) != 1 or not secrets.compare_digest(states[0], state):
        raise CollectorError("state_mismatch")
    codes = q.get("code", [])
    if q.get("error") or len(codes) != 1 or not codes[0]:
        raise CollectorError("auth_failed")
    return codes[0]


def authorize(client_file, token_file, opener=request.urlopen, timeout=300):
    client = client_config(client_file)
    path = credential_path(token_file)
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(32)
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if parse.urlsplit(self.path).path != "/callback":
                self.send_error(404)
                return
            try:
                result["code"] = callback_code(self.path, state)
            except CollectorError as e:
                result["error"] = e
            self.send_response(400 if "error" in result else 200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Authentication finished. You may close this window.")

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/callback"
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + parse.urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
            "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        print(url, file=sys.stderr)
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()
        if "error" in result:
            raise result["error"]
        if "code" not in result:
            raise CollectorError("auth_timeout")
        obj = token_request({**client, "code": result["code"], "code_verifier": verifier,
                             "redirect_uri": redirect, "grant_type": "authorization_code"}, opener)
    if not isinstance(obj.get("refresh_token"), str) or not obj["refresh_token"]:
        raise CollectorError("auth_required")
    atomic(path.parent, path, {"refresh_token": obj["refresh_token"]})
    return {"status": "auth_ready"}


def refresh(client_file, token_file, opener=request.urlopen):
    client, path = client_config(client_file), credential_path(token_file)
    try:
        obj = load(path)
        token = obj["refresh_token"]
        if not isinstance(token, str) or not token:
            raise ValueError("token")
    except (OSError, ValueError, KeyError, TypeError):
        raise CollectorError("auth_required") from None
    value = token_request({**client, "refresh_token": token, "grant_type": "refresh_token"}, opener)
    if value.get("refresh_token"):
        atomic(path.parent, path, {"refresh_token": value["refresh_token"]})
    return value["access_token"]
