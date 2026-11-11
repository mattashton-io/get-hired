"""Phase 4.1: Gmail API OAuth2 Client (`gmail.send` & `gmail.readonly`) + Local Outbox."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import uuid
from datetime import datetime, timezone
from email.encoders import encode_base64
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from src.config import Settings

logger = logging.getLogger(__name__)

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


class GmailClient:
    """
    Authenticated OAuth2 Gmail client targeting `Erykasmith@gmail.com`.
    Supports live Gmail API (`gmail.send`, `gmail.readonly`) when OAuth credentials/token are
    present, and seamlessly archives `.html` + `.eml` deliverables to `data/outbox/` in `DRY_RUN`
    or pre-OAuth mode.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.credentials_path = settings.gmail_credentials_path
        self.token_path = settings.gmail_token_path
        self.outbox_dir = settings.outbox_dir
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self._threads_index_path = self.outbox_dir / "threads_index.json"
        self._simulated_replies_path = self.outbox_dir / "simulated_replies.json"
        self._service: Any | None = None

    def is_live_authenticated(self) -> bool:
        return self.token_path.exists()

    def authenticate_interactive(self) -> Path:
        """Runs local browser OAuth2 consent flow for `Erykasmith@gmail.com` and saves `gmail_token.json`."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self.credentials_path.exists():
                    raise FileNotFoundError(
                        f"Gmail OAuth2 client secret file not found at {self.credentials_path}. "
                        "Download Desktop OAuth 2.0 Client JSON from Google Cloud Console and place it there."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)

            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(creds.to_json(), encoding="utf-8")
        return self.token_path

    def _get_service(self) -> Any | None:
        if self._service is not None:
            return self._service
        if not self.token_path.exists():
            return None
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_file(str(self.token_path), GMAIL_SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self.token_path.write_text(creds.to_json(), encoding="utf-8")
            if creds and creds.valid:
                self._service = build("gmail", "v1", credentials=creds)
                return self._service
        except Exception as exc:
            logger.warning("Gmail OAuth token load warning: %s", exc)
        return None

    def send_html_email(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        attachments: list[Path] | None = None,
        tag: str = "email",
    ) -> dict[str, str]:
        """Builds RFC 2822 MIME message, saves `.html` & `.eml` in outbox, and sends via Gmail API if live."""
        msg = MIMEMultipart("mixed")
        msg["To"] = to_email
        msg["From"] = self.settings.candidate_email
        msg["Subject"] = subject

        alt = MIMEMultipart("alternative")
        plain_fallback = BeautifulSoup(html_body, "html.parser").get_text("\n", strip=True)
        alt.attach(MIMEText(plain_fallback, "plain", "utf-8"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)

        for file_path in attachments or []:
            if not file_path or not Path(file_path).exists():
                continue
            p = Path(file_path)
            ctype, _ = mimetypes.guess_type(str(p))
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            part = MIMEBase(maintype, subtype)
            part.set_payload(p.read_bytes())
            encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{p.name}"')
            msg.attach(part)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        short_id = uuid.uuid4().hex[:8]
        html_path = self.outbox_dir / f"{ts}_{tag}_{short_id}.html"
        eml_path = self.outbox_dir / f"{ts}_{tag}_{short_id}.eml"
        html_path.write_text(html_body, encoding="utf-8")
        eml_path.write_bytes(msg.as_bytes())

        service = None if self.settings.dry_run else self._get_service()
        if service is not None:
            raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
            sent = (
                service.users()
                .messages()
                .send(userId="me", body={"raw": raw_b64})
                .execute()
            )
            thread_id = sent.get("threadId") or sent.get("id")
            message_id = sent.get("id", "")
            mode = "gmail_api_live"
        else:
            thread_id = f"thread-{short_id}"
            message_id = f"msg-{short_id}"
            mode = "dry_run_outbox" if self.settings.dry_run else "local_outbox_pending_oauth"

        self._record_outbox_thread(
            thread_id=thread_id,
            message_id=message_id,
            to_email=to_email,
            subject=subject,
            html_path=str(html_path),
            eml_path=str(eml_path),
            mode=mode,
        )
        return {
            "thread_id": thread_id,
            "message_id": message_id,
            "html_path": str(html_path),
            "eml_path": str(eml_path),
            "delivery_mode": mode,
        }

    def get_thread_replies(self, thread_id: str) -> list[str]:
        """Fetches replies on a Gmail thread via `gmail.readonly` (or simulated replies in dry-run/tests)."""
        replies: list[str] = []

        # Check simulated replies store (used in dry-run & automated integration tests)
        if self._simulated_replies_path.exists():
            try:
                sim_data = json.loads(
                    self._simulated_replies_path.read_text(encoding="utf-8")
                )
                if thread_id in sim_data:
                    entries = sim_data[thread_id]
                    if isinstance(entries, list):
                        replies.extend(str(x) for x in entries)
                    elif isinstance(entries, str):
                        replies.append(entries)
            except json.JSONDecodeError:
                pass

        service = self._get_service()
        if service is not None and not thread_id.startswith("thread-"):
            try:
                thread = (
                    service.users()
                    .threads()
                    .get(userId="me", id=thread_id, format="full")
                    .execute()
                )
                messages = thread.get("messages", [])
                # Skip message[0] (our outbound HITL request) and inspect subsequent replies
                for msg in messages[1:]:
                    body_text = self._extract_message_text(msg.get("payload", {}))
                    if body_text.strip():
                        replies.append(body_text.strip())
            except Exception as exc:
                logger.warning("Failed polling Gmail thread %s: %s", thread_id, exc)

        return replies

    def inject_simulated_reply(self, thread_id: str, reply_text: str) -> None:
        """Helper for dry-run verification and automated tests to simulate a candidate Gmail reply."""
        data: dict[str, list[str]] = {}
        if self._simulated_replies_path.exists():
            try:
                data = json.loads(self._simulated_replies_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        data.setdefault(thread_id, []).append(reply_text)
        self._simulated_replies_path.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

    def clear_simulated_reply(self, thread_id: str) -> None:
        if not self._simulated_replies_path.exists():
            return
        try:
            data = json.loads(self._simulated_replies_path.read_text(encoding="utf-8"))
            if thread_id in data:
                del data[thread_id]
                self._simulated_replies_path.write_text(
                    json.dumps(data, indent=2), encoding="utf-8"
                )
        except json.JSONDecodeError:
            return

    def _record_outbox_thread(self, **entry: str) -> None:
        records: list[dict[str, str]] = []
        if self._threads_index_path.exists():
            try:
                records = json.loads(self._threads_index_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                records = []
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        records.append(entry)
        self._threads_index_path.write_text(
            json.dumps(records, indent=2), encoding="utf-8"
        )

    @classmethod
    def _extract_message_text(cls, payload: dict[str, Any]) -> str:
        mime_type = payload.get("mimeType", "")
        body_data = (payload.get("body") or {}).get("data")
        if body_data and mime_type in {"text/plain", "text/html"}:
            decoded = base64.urlsafe_b64decode(body_data.encode("utf-8")).decode(
                "utf-8", errors="replace"
            )
            if mime_type == "text/html":
                return BeautifulSoup(decoded, "html.parser").get_text("\n", strip=True)
            return decoded

        for part in payload.get("parts") or []:
            extracted = cls._extract_message_text(part)
            if extracted:
                return extracted
        return ""

