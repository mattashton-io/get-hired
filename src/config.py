"""Central configuration and path management for get-hired."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _resolve_default_gcp_region() -> str:
    """Resolve GCP region from env or gcloud config without hardcoding."""
    env_region = os.getenv("GOOGLE_CLOUD_REGION") or os.getenv("CLOUDSDK_COMPUTE_REGION") or os.getenv("CLOUDSDK_RUN_REGION")
    if env_region:
        return env_region.strip()
    for prop in ("run/region", "compute/region"):
        try:
            val = subprocess.check_output(
                ["gcloud", "config", "get-value", prop],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            ).strip()
            if val and val != "(unset)":
                return val
        except Exception:
            pass
    try:
        zone = subprocess.check_output(
            ["gcloud", "config", "get-value", "compute/zone"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
        if zone and zone != "(unset)" and "-" in zone:
            return zone.rsplit("-", 1)[0]
    except Exception:
        pass
    return ""


@dataclass
class Settings:
    project_root: Path = PROJECT_ROOT
    skills_dir: Path = PROJECT_ROOT / "skills"
    data_dir: Path = PROJECT_ROOT / "data"
    persona_path: Path = PROJECT_ROOT / "persona.md"
    master_resume_path: Path = PROJECT_ROOT / "data" / "master_resume.json"
    target_companies_path: Path = PROJECT_ROOT / "data" / "target_companies.json"
    ledger_db_path: Path = PROJECT_ROOT / "data" / "applications_ledger.db"
    compiled_resumes_dir: Path = PROJECT_ROOT / "data" / "compiled_resumes"
    screenshots_dir: Path = PROJECT_ROOT / "data" / "screenshots"
    outbox_dir: Path = PROJECT_ROOT / "data" / "outbox"
    session_states_dir: Path = PROJECT_ROOT / "data" / "session_states"

    # Gemini / Google Cloud configuration
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(
        default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    )
    google_cloud_project_id: str = field(
        default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT_ID", "")
    )
    google_cloud_region: str = field(
        default_factory=_resolve_default_gcp_region
    )

    # Gmail OAuth2 configuration
    candidate_email: str = field(
        default_factory=lambda: os.getenv("CANDIDATE_EMAIL", "Erykasmith@gmail.com")
    )
    gmail_credentials_path: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "GMAIL_CREDENTIALS_PATH",
                str(PROJECT_ROOT / "config" / "gmail_credentials.json"),
            )
        )
    )
    gmail_token_path: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "GMAIL_TOKEN_PATH",
                str(PROJECT_ROOT / "config" / "gmail_token.json"),
            )
        )
    )

    # Execution & Guardrails
    dry_run: bool = field(
        default_factory=lambda: os.getenv("DRY_RUN", "false").lower()
        in {"1", "true", "yes"}
    )
    timezone: str = "America/Chicago"
    daily_application_cap: int = 10
    daily_queue_limit: int = 12
    min_queue_score: int = 75
    auto_apply_min_score: int = 85
    salary_hard_floor: int = 105000
    salary_target_min: int = 110000
    salary_target_max: int = 150000
    salary_stretch_max: int = 175000
    salary_default_anchor: int = 125000
    jitter_min_minutes: int = 25
    jitter_max_minutes: int = 55

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.compiled_resumes_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self.session_states_dir.mkdir(parents=True, exist_ok=True)
        (self.project_root / "config").mkdir(parents=True, exist_ok=True)

        # If /app/data is a freshly mounted GCS FUSE volume, hydrate seed JSON files
        seed_dir = self.project_root / "seed_data"
        if seed_dir.is_dir():
            for fname, target in (
                ("master_resume.json", self.master_resume_path),
                ("target_companies.json", self.target_companies_path),
            ):
                src_file = seed_dir / fname
                if not target.exists() and src_file.exists():
                    shutil.copy2(src_file, target)

    def load_persona_text(self) -> str:
        return self.persona_path.read_text(encoding="utf-8")

    def load_master_resume(self) -> dict[str, Any]:
        return json.loads(self.master_resume_path.read_text(encoding="utf-8"))

    def load_skill(self, skill_filename: str) -> str:
        return (self.skills_dir / skill_filename).read_text(encoding="utf-8")


def get_settings(dry_run: bool | None = None) -> Settings:
    settings = Settings()
    if dry_run is not None:
        settings.dry_run = dry_run
    settings.ensure_dirs()
    return settings

