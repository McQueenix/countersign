"""Runtime settings, read once from the environment (.env supported)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# .env is the documented name. Windows often saves it as ".env.txt" without showing the extension,
# so those names are accepted too, in the project folder or the folder the server was started from.
ENV_CANDIDATES = [base / name for base in (ROOT, Path.cwd()) for name in (".env", ".env.txt", "env.txt")]
ENV_FILE = next((path for path in ENV_CANDIDATES if path.is_file()), None)
if ENV_FILE:
    load_dotenv(ENV_FILE)

# Countersign is a prototype and is hard-wired to the PayPal sandbox.
# There is deliberately no switch to point it at live money.
PAYPAL_SANDBOX_BASE = "https://api-m.sandbox.paypal.com"


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    paypal_client_id: str
    paypal_client_secret: str
    paypal_base: str
    paypal_webhook_id: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_reasoning_effort: str
    db_path: str
    public_base_url: str
    business_name: str
    contractor_email: str
    contractor2_email: str
    client_email: str
    vendor_email: str
    offline_preview: bool

    @property
    def paypal_configured(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)


def load_settings() -> Settings:
    return Settings(
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", "").strip(),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", "").strip(),
        paypal_base=PAYPAL_SANDBOX_BASE,
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", "").strip(),
        llm_base_url=os.getenv(
            "LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
        ).strip().rstrip("/"),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "gemini-flash-latest").strip(),
        llm_reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "").strip(),
        db_path=os.getenv("COUNTERSIGN_DB", "countersign.db").strip(),
        public_base_url=(os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8000").strip().rstrip("/"),
        business_name=os.getenv("BUSINESS_NAME", "Kestrel Studio").strip(),
        contractor_email=os.getenv("DEMO_CONTRACTOR_EMAIL", "priya.nair@personal.example.com").strip().lower(),
        contractor2_email=os.getenv("DEMO_CONTRACTOR2_EMAIL", "marco.silva@personal.example.com").strip().lower(),
        client_email=os.getenv("DEMO_CLIENT_EMAIL", "accounts@harborandpine.example.com").strip().lower(),
        vendor_email=os.getenv("DEMO_VENDOR_EMAIL", "sales@lumenstock.example.com").strip().lower(),
        offline_preview=_flag("COUNTERSIGN_OFFLINE_PREVIEW"),
    )
