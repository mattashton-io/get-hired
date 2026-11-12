"""Scale-to-Zero Cloud Run Web OAuth2 Onboarding Portal for Remote Candidate Sign-In."""

from __future__ import annotations

import html
import http.cookies
import json
import logging
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from src.config import Settings, get_settings
from src.integrations.gmail_client import GMAIL_SCOPES
from src.orchestrator.pipeline import AutonomousJobOrchestrator

logger = logging.getLogger(__name__)


def _resolve_client_config(settings: Settings) -> dict[str, Any] | None:
    """Loads OAuth2 Web Client config from GCS /app/data, /app/config, or env var."""
    env_raw = os.getenv("GMAIL_CLIENT_SECRET_JSON", "").strip()
    if env_raw:
        try:
            return json.loads(env_raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in GMAIL_CLIENT_SECRET_JSON")

    for candidate in (
        settings.data_dir / "gmail_credentials.json",
        settings.gmail_credentials_path,
    ):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed reading OAuth client config at %s: %s", candidate, exc)
    return None


def _allowed_emails(settings: Settings) -> set[str]:
    raw = os.getenv(
        "ALLOWED_AUTH_EMAILS",
        f"{settings.candidate_email},{settings.notify_emails},ashton0801@gmail.com",
    )
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _connected_account_status(settings: Settings) -> str | None:
    token_file = settings.data_dir / "gmail_token.json"
    if not token_file.exists():
        token_file = settings.gmail_token_path
    if not token_file.exists():
        return None
    try:
        meta = json.loads(token_file.read_text(encoding="utf-8"))
        return meta.get("authorized_email") or "Connected (Active Refresh Token)"
    except Exception:
        return "Connected"


def _external_origin(handler: BaseHTTPRequestHandler) -> str:
    proto = handler.headers.get("X-Forwarded-Proto", "http")
    host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host", "localhost:8080")
    return f"{proto}://{host}"


def _render_page(title: str, body_html: str) -> bytes:
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{html.escape(title)}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      background: #f3f4f6;
      color: #1f2937;
      margin: 0;
      padding: 32px 16px;
    }}
    .card {{
      max-width: 600px;
      margin: 0 auto;
      background: #ffffff;
      border-radius: 12px;
      box-shadow: 0 4px 20px rgba(17, 42, 70, 0.08);
      overflow: hidden;
      border: 1px solid #e5e7eb;
    }}
    .header {{
      background: #112a46;
      color: #ffffff;
      padding: 24px 28px;
    }}
    .header h1 {{
      margin: 0;
      font-size: 20px;
      font-weight: 700;
    }}
    .header p {{
      margin: 6px 0 0 0;
      font-size: 13px;
      color: #cbd5e1;
    }}
    .content {{
      padding: 28px;
      line-height: 1.6;
      font-size: 14px;
    }}
    .badge-ok {{
      display: inline-block;
      background: #ecfdf5;
      color: #047857;
      border: 1px solid #a7f3d0;
      padding: 6px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      margin-bottom: 16px;
    }}
    .badge-pending {{
      display: inline-block;
      background: #fffbeb;
      color: #b45309;
      border: 1px solid #fde68a;
      padding: 6px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      margin-bottom: 16px;
    }}
    .btn {{
      display: inline-block;
      background: #2563eb;
      color: #ffffff !important;
      text-decoration: none;
      font-weight: 600;
      font-size: 15px;
      padding: 12px 22px;
      border-radius: 8px;
      margin-top: 12px;
    }}
    .btn:hover {{
      background: #1d4ed8;
    }}
    .box {{
      background: #f9fafb;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      padding: 14px 16px;
      margin: 16px 0;
      font-size: 13px;
    }}
    code {{
      background: #eef2ff;
      color: #1e3a8a;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 12px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>Autonomous Job Discovery &amp; Application Agent</h1>
      <p>Eryka Smith, CFE &bull; DFW &amp; US Remote Certified Small/Diverse Employers</p>
    </div>
    <div class="content">
      {body_html}
    </div>
  </div>
</body>
</html>"""
    return doc.encode("utf-8")


class OAuthPortalHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_html(
        self,
        status: int,
        title: str,
        body_html: str,
        cookie_header: str | None = None,
    ) -> None:
        payload = _render_page(title, body_html)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if cookie_header:
            self.send_header("Set-Cookie", cookie_header)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        settings = get_settings(dry_run=False)

        if route == "/healthz":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if route == "/":
            connected = _connected_account_status(settings)
            client_cfg = _resolve_client_config(settings)
            origin = _external_origin(self)
            callback_uri = f"{origin}/oauth2callback"

            if connected:
                badge = f'<div class="badge-ok">&#10003; Gmail Connected: {html.escape(connected)}</div>'
            else:
                badge = '<div class="badge-pending">&#9679; Action Needed: Connect Gmail to Activate Live Digests &amp; Approvals</div>'

            setup_note = ""
            if client_cfg is None:
                setup_note = f"""
                <div class="box" style="border-color:#fca5a5;background:#fef2f2;color:#991b1b;">
                  <strong>Admin Setup Pending:</strong> Upload OAuth Web Client JSON to
                  <code>gs://{html.escape(settings.google_cloud_project_id)}-get-hired-data/gmail_credentials.json</code>
                  with Authorized Redirect URI:<br/>
                  <code>{html.escape(callback_uri)}</code>
                </div>"""

            body_html = f"""
              {badge}
              {setup_note}
              <p>
                Welcome! Connecting your Gmail enables your autonomous job agent to:
              </p>
              <div class="box">
                <ul style="margin:0;padding-left:18px;">
                  <li>Send your <strong>6:00 PM CST Daily Digest</strong> summarizing roles applied, salary bands, and direct outreach blurbs.</li>
                  <li>Send <strong>[ACTION REQUIRED]</strong> review emails when a role needs your quick sign-off — just reply <code>APPROVE</code> or <code>SKIP</code> right from your phone.</li>
                </ul>
              </div>
              <p>
                Click below to sign in with Google and immediately trigger a live <strong>Welcome &amp; Test Digest</strong> email:
              </p>
              <a class="btn" href="/login">Connect Gmail &amp; Send Test Email &rarr;</a>
            """
            self._send_html(200, "Connect Gmail — Autonomous Job Agent", body_html)
            return

        if route == "/login":
            client_cfg = _resolve_client_config(settings)
            origin = _external_origin(self)
            callback_uri = f"{origin}/oauth2callback"
            if client_cfg is None:
                self._send_html(
                    503,
                    "OAuth Client Not Configured Yet",
                    f"<p>OAuth Web Client JSON has not been uploaded yet.</p>"
                    f"<p>Expected Redirect URI: <code>{html.escape(callback_uri)}</code></p>",
                )
                return

            if callback_uri.startswith("http://"):
                os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

            flow = Flow.from_client_config(
                client_cfg,
                scopes=GMAIL_SCOPES,
                redirect_uri=callback_uri,
            )
            state = secrets.token_urlsafe(32)
            # PKCE code_verifier if generated by google-auth-oauthlib
            auth_url, _ = flow.authorization_url(
                access_type="offline",
                include_granted_scopes="true",
                prompt="consent",
                state=state,
            )
            code_verifier = getattr(flow, "code_verifier", "") or ""
            state_payload = json.dumps({"s": state, "v": code_verifier})
            # Persist state + PKCE verifier in /app/data/session_states so any Cloud Run instance can validate it
            state_dir = settings.session_states_dir
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / f"oauth_state_{state}.json").write_text(state_payload, encoding="utf-8")

            secure_flag = "; Secure" if callback_uri.startswith("https://") else ""
            cookie = f"gh_oauth_state={state}; Path=/; Max-Age=600; HttpOnly; SameSite=Lax{secure_flag}"

            self.send_response(302)
            self.send_header("Location", auth_url)
            self.send_header("Set-Cookie", cookie)
            self.end_headers()
            return

        if route == "/oauth2callback":
            params = parse_qs(parsed.query)
            if "error" in params:
                err_msg = params["error"][0]
                self._send_html(
                    400,
                    "Authorization Cancelled",
                    f"<p>Google returned: <code>{html.escape(err_msg)}</code></p>"
                    f'<p><a class="btn" href="/">Try Again</a></p>',
                )
                return

            state = (params.get("state") or [""])[0]
            code = (params.get("code") or [""])[0]

            cookie_jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            cookie_state = cookie_jar["gh_oauth_state"].value if "gh_oauth_state" in cookie_jar else ""
            state_file = settings.session_states_dir / f"oauth_state_{state}.json"

            if not state or not code or (cookie_state and cookie_state != state) or not state_file.exists():
                self._send_html(
                    400,
                    "Invalid or Expired Session State",
                    '<p>Your login session expired or did not match. Please start again.</p>'
                    '<p><a class="btn" href="/login">Restart Sign-In</a></p>',
                )
                return

            saved_state = json.loads(state_file.read_text(encoding="utf-8"))
            state_file.unlink(missing_ok=True)

            client_cfg = _resolve_client_config(settings)
            origin = _external_origin(self)
            callback_uri = f"{origin}/oauth2callback"
            if callback_uri.startswith("http://"):
                os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

            try:
                flow = Flow.from_client_config(
                    client_cfg,
                    scopes=GMAIL_SCOPES,
                    state=state,
                    redirect_uri=callback_uri,
                )
                if saved_state.get("v"):
                    flow.code_verifier = saved_state["v"]
                # Relax scope mismatch error when Google returns additional previously-granted scopes
                os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
                flow.fetch_token(code=code)
                creds = flow.credentials

                gmail_svc = build("gmail", "v1", credentials=creds)
                profile = gmail_svc.users().getProfile(userId="me").execute()
                authed_email = (profile.get("emailAddress") or "").strip()

                allowed = _allowed_emails(settings)
                if authed_email.lower() not in allowed:
                    logger.warning("Rejected unauthorized OAuth user: %s", authed_email)
                    self._send_html(
                        403,
                        "Unauthorized Account",
                        f"<p>The account <code>{html.escape(authed_email)}</code> is not on the authorized allowlist for this agent.</p>"
                        '<p>Please sign in with <code>Erykasmith@gmail.com</code>.</p>'
                        '<p><a class="btn" href="/login">Sign In With Authorized Account</a></p>',
                    )
                    return

                # Persist token JSON (annotated with authorized_email) to mounted GCS volume /app/data/gmail_token.json
                token_dict = json.loads(creds.to_json())
                token_dict["authorized_email"] = authed_email
                target_token_path = settings.data_dir / "gmail_token.json"
                target_token_path.parent.mkdir(parents=True, exist_ok=True)
                target_token_path.write_text(json.dumps(token_dict, indent=2), encoding="utf-8")

                # Immediately send live Welcome / Test Daily Digest email to Eryka & Matt
                fresh_settings = get_settings(dry_run=False)
                orch = AutonomousJobOrchestrator(fresh_settings)
                digest_res = orch.hitl.send_daily_digest(
                    to_email=fresh_settings.notify_emails
                )
                delivery_mode = digest_res.get("delivery", {}).get("delivery_mode", "unknown")

                self._send_html(
                    200,
                    "Gmail Connected!",
                    f'<div class="badge-ok">&#10003; Connected as {html.escape(authed_email)}</div>'
                    f"<p>Your Gmail account is now securely linked to your scale-to-zero Cloud Run Job agent.</p>"
                    f'<div class="box">'
                    f"<strong>Live Test Digest Triggered:</strong><br/>"
                    f"Recipients: <code>{html.escape(fresh_settings.notify_emails)}</code><br/>"
                    f"Delivery Status: <code>{html.escape(delivery_mode)}</code>"
                    f"</div>"
                    f"<p>You can close this tab — your scheduled runs (6:00 AM sourcing, 8:00 AM–4:00 PM applications, and 6:00 PM CST Daily Digest) are active!</p>",
                )
                return
            except Exception as exc:
                logger.exception("OAuth callback error")
                self._send_html(
                    500,
                    "OAuth Connection Error",
                    f"<p>Failed completing OAuth token exchange: <code>{html.escape(str(exc))}</code></p>"
                    '<p><a class="btn" href="/login">Try Again</a></p>',
                )
                return

        self._send_html(404, "Not Found", '<p>Page not found. <a href="/">Return Home</a></p>')


def run_web_auth_server(host: str = "0.0.0.0", port: int | None = None) -> None:
    listen_port = port or int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer((host, listen_port), OAuthPortalHandler)
    logger.info("Starting OAuth onboarding portal on http://%s:%d", host, listen_port)
    server.serve_forever()
