from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path
from dotenv import load_dotenv


# Environment variables take precedence; local .env is only a development convenience.
load_dotenv(override=False)


@dataclass(frozen=True)
class Settings:
    database_path: str = os.getenv("DATABASE_PATH", "data/connector_intelligence.db")
    output_dir: str = os.getenv("OUTPUT_DIR", "outputs")
    whatsapp_verify_token: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    whatsapp_app_secret: str = os.getenv("WHATSAPP_APP_SECRET", "")
    whatsapp_selected_groups: str = os.getenv("WHATSAPP_SELECTED_GROUPS", "")
    # Dot-separated path relative to each Cloud API message object. Empty means disabled.
    whatsapp_group_id_path: str = os.getenv("WHATSAPP_GROUP_ID_PATH", "")
    google_client_secrets_file: str = os.getenv("GOOGLE_CLIENT_SECRETS_FILE", "credentials.json")
    google_token_file: str = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
    gmail_mailbox_owner: str = os.getenv("GMAIL_MAILBOX_OWNER", "")
    google_drive_token_file: str = os.getenv("GOOGLE_DRIVE_TOKEN_FILE", "drive_token.json")
    google_drive_account: str = os.getenv("GOOGLE_DRIVE_ACCOUNT", "")
    reference_date: str = os.getenv("REFERENCE_DATE", "")

    def selected_whatsapp_groups(self) -> set[str]:
        return {item.strip() for item in self.whatsapp_selected_groups.split(",") if item.strip()}

    def reference_day(self) -> date:
        return date.fromisoformat(self.reference_date) if self.reference_date else date.today()

    def ensure_dirs(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

