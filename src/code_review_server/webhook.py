import hashlib
import hmac
from typing import Final

SIGNATURE_PREFIX: Final[str] = "sha256="


def verify_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify the X-Hub-Signature-256 HMAC of the raw webhook body against the shared secret."""

    if not secret or not signature_header or not signature_header.startswith(SIGNATURE_PREFIX):
        return False

    expected = f"{SIGNATURE_PREFIX}{hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"

    return hmac.compare_digest(expected, signature_header)
