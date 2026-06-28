from typing import Final

from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseSettings):
    """Webhook backend configuration: GitHub App credentials, the webhook secret, and the HTTP binding."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    github_app_id: str = ""
    github_app_private_key: str = ""
    github_webhook_secret: str = ""
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"

    @property
    def is_configured(self) -> bool:
        """Return whether the App credentials and webhook secret required to serve are all present."""

        return bool(self.github_app_id and self.github_app_private_key and self.github_webhook_secret)


SERVER_SETTINGS: Final[ServerSettings] = ServerSettings()
