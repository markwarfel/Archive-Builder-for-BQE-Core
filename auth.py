from __future__ import annotations
import base64, hashlib, json, secrets, urllib.error, urllib.parse, urllib.request
from pathlib import Path

PORT         = 8080
REDIRECT_URI = f"http://127.0.0.1:{PORT}/callback"
AUTH_ENDPOINT  = "https://api-identity.bqecore.com/idp/connect/authorize"
TOKEN_ENDPOINT = "https://api-identity.bqecore.com/idp/connect/token"
SCOPES = "read:core"

ROOT        = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
TOKENS_FILE = ROOT / "tokens.json"

# In-memory state
pending: dict = {}  # state → {code_verifier, client_id, popup}
tokens:  dict = {}  # access_token, refresh_token, endpoint, …


def load_tokens() -> None:
    global tokens
    if TOKENS_FILE.exists():
        try:
            tokens = json.loads(TOKENS_FILE.read_text())
        except Exception:
            tokens = {}


def save_tokens(data: dict) -> None:
    global tokens
    tokens = data
    TOKENS_FILE.write_text(json.dumps(data, indent=2))


def clear_tokens() -> None:
    global tokens
    tokens = {}
    if TOKENS_FILE.exists():
        TOKENS_FILE.unlink()


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def save_config(data: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    code_verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def exchange_code(code: str, code_verifier: str, client_id: str) -> dict:
    body = urllib.parse.urlencode({
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "client_id":     client_id,
        "code_verifier": code_verifier,
    }).encode()
    req = urllib.request.Request(TOKEN_ENDPOINT, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def refresh_access_token() -> bool:
    global tokens
    refresh_token = tokens.get("refresh_token")
    client_id = load_config().get("client_id")
    if not refresh_token or not client_id:
        return False
    body = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     client_id,
    }).encode()
    req = urllib.request.Request(TOKEN_ENDPOINT, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        if "endpoint" not in data and "endpoint" in tokens:
            data["endpoint"] = tokens["endpoint"]
        save_tokens(data)
        return True
    except Exception:
        return False
