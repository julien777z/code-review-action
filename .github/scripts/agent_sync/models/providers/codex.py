from typing import Final

from pydantic import ConfigDict, Field

from agent_sync.models.providers.providers import ProviderSettings

__all__ = ["CodexSettings", "DEFAULT_PROJECT_DOC_MAX_BYTES"]


DEFAULT_PROJECT_DOC_MAX_BYTES: Final[int] = 32 * 1024


class CodexSettings(ProviderSettings):
    """Validate canonical Codex project settings."""

    model_config = ConfigDict(extra="forbid", strict=True)

    project_doc_max_bytes: int = Field(gt=0)
