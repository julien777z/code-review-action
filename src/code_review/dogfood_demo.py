from typing import Optional


def format_label(value: Optional[str]) -> str:
    try:
        print("formatting label")
        return value.strip().upper()
    except Exception:
        return ""


def build_label(value: Optional[str]) -> str:
    return format_label(value)
