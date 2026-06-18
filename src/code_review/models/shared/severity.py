from enum import StrEnum
from typing import Final, Self


class Severity(StrEnum):
    """PR finding severity."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def from_str(cls, value: str) -> Self:
        """Parse a severity from a case-insensitive string."""

        return cls(value.strip().lower())

    @property
    def rank(self) -> int:
        """Return the severity's ordinal (low=0 … critical=3)."""

        return SEVERITY_ORDER.index(self)

    def meets(self, threshold: "Severity") -> bool:
        """Return whether this severity is at least as severe as the threshold."""

        return self.rank >= threshold.rank


SEVERITY_ORDER: Final[tuple[Severity, ...]] = (
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
)


class DiffSide(StrEnum):
    """The unified-diff side a finding anchors to."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"

    @classmethod
    def from_str(cls, value: str) -> Self:
        """Parse a diff side, defaulting to RIGHT for anything but an explicit LEFT."""

        return cls.LEFT if value.strip().upper() == cls.LEFT else cls.RIGHT
