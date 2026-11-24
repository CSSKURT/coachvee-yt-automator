"""
Google OAuth for the two DIFFERENT accounts (0% AI).

- Drive  : state.json drive_email   -> tokens/drive_<name>.json   (read-only scope)
- YouTube: state.json youtube_email -> tokens/youtube_<name>.json

Guardrails:
- The interactive login only runs when the GUI asks for it (after the user ticks the
  confirmation box). The background uploader never opens a login window.
- After login, the signed-in email is checked against the expected one. If it does not
  match, the token is NOT saved and an AuthError is raised.
"""
from __future__ import annotations

from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials

from common import TOKENS_DIR

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",     # needed for captions.insert
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",        # only to verify which account signed in
]


class AuthError(Exception):
    pass


def _local_part(email: str) -> str:
    return email.split("@")[0].strip().lower()


def token_path(kind: str, email: str) -> Path:
    return TOKENS_DIR / f"{kind}_{_local_part(email)}.json"


def _load_saved(path: Path, scopes: list[str], log=None) -> Credentials | None:
    if not path.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(path), scopes)
    except Exception as e:
        if log:
            log(f"Saved token {path.name} is unreadable ({e}); a new login is needed.")
        return None
    if not set(scopes).issubset(set(creds.scopes or scopes)):
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except RefreshError as e:
            if log:
                log(f"Saved login {path.name} expired or was revoked ({e}).")
            return None
        except Exception as e:                     # offline etc.: keep the token, surface the error
            raise AuthError(f"Could not refresh the Google login (network?): {e}")
    return None


def _drive_email(creds: Credentials) -> str:
    from googleapiclient.discovery import build
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    return svc.about().get(fields="user(emailAddress)").execute()["user"]["emailAddress"]


def _userinfo_email(creds: Credentials) -> str:
    r = AuthorizedSession(creds).get("https://openidconnect.googleapis.com/v1/userinfo", timeout=30)
    r.raise_for_status()
    return r.json().get("email", "")


def _interactive_login(client_secret: Path, scopes: list[str], expected_email: str) -> Credentials:
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not client_secret.exists():
        raise AuthError(f"client_secret.json not found at:\n{client_secret}\n\nSee SETUP_GUIDE.md, step 2.")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), scopes)
    return flow.run_local_server(
        port=0,
        open_browser=True,
        login_hint=expected_email,                 # pre-selects the right Google account
        prompt="consent select_account",
        authorization_prompt_message="",
        success_message=("Login complete for " + expected_email +
                         ". You can close this tab and return to the Coach Vee Automator."),
        timeout_seconds=600,
    )


def get_credentials(kind: str, cfg: dict, expected_email: str, interactive: bool, log=None) -> Credentials:
    """kind = 'drive' | 'youtube'. Returns verified credentials for `expected_email`."""
    scopes = DRIVE_SCOPES if kind == "drive" else YOUTUBE_SCOPES
    path = token_path(kind, expected_email)
    creds = _load_saved(path, scopes, log)
    if creds:
        return creds
    if not interactive:
        raise AuthError(f"The {kind} login for {expected_email} is missing or expired. "
                        f"Open the Coach Vee Automator and run that step again to sign in.")
    if log:
        log(f"Opening the browser for Google sign-in as {expected_email} ...")
    creds = _interactive_login(Path(cfg["client_secret_path"]), scopes, expected_email)
    actual = _drive_email(creds) if kind == "drive" else _userinfo_email(creds)
    if actual.strip().lower() != expected_email.strip().lower():
        raise AuthError(f"You signed in as {actual}, but this step must use {expected_email}.\n"
                        f"Nothing was saved. Run the step again and pick {expected_email}.")
    path.write_text(creds.to_json(), encoding="utf-8")
    if log:
        log(f"Signed in as {actual}. Token saved to tokens/{path.name}")
    return creds


def has_saved_token(kind: str, email: str) -> bool:
    return token_path(kind, email).exists()


def forget_token(kind: str, email: str) -> None:
    p = token_path(kind, email)
    if p.exists():
        p.unlink()
