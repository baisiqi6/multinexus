"""Content validator for memory extraction — REJECT_PATTERNS + ALLOWED_TARGETS."""

import re
from typing import NamedTuple


class ValidationResult(NamedTuple):
    valid: bool
    reason: str | None  # None when valid


# 12 patterns that disqualify content from being stored as a memory.
# Order: more specific / longer patterns first.
REJECT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 1. OpenAI-style API key
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "openai_api_key"),
    # 2. Generic pk-... API key
    (re.compile(r"\bpk-[A-Za-z0-9]{20,}\b"), "api_key_pk"),
    # 3. GitHub personal-access token
    (re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"), "github_token"),
    # 4. Slack bot token
    (re.compile(r"\bxoxb-[A-Za-z0-9\-]{40,}\b"), "slack_token"),
    # 5. Discord bot token (header.timestamp.hmac structure)
    (re.compile(r"[MN][A-Za-z0-9]{23}\.[A-Za-z0-9]{6}\.[A-Za-z0-9]{27}"), "discord_token"),
    # 6. PEM private key block (any key type)
    (re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----"), "pem_private_key"),
    # 7. .env-style KEY=value line (broad: 3+ uppercase chars, no whitespace after =)
    (re.compile(r"^[A-Z_][A-Z0-9_]{2,}=[^\s]+", re.MULTILINE), "env_key_value"),
    # 8. Crypto private key / base58 address (43-44 char Solana keypair size)
    (re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{43,44}\b"), "crypto_private_key"),
    # 9. Credit card number (common formats)
    (re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"), "credit_card"),
    # 10. US Social Security Number
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
    # 11. Mnemonic / seed phrase references
    (re.compile(r"\b(?:mnemonic|seed phrase|recovery phrase)\b", re.IGNORECASE), "mnemonic_phrase"),
    # 12. Windows path leaking local username
    (re.compile(r"[Cc]:[/\\][Uu]sers[/\\][A-Za-z0-9_.\-]+[/\\]"), "windows_path_pii"),
]

# Memory types accepted by the extraction pipeline.
# Keys match the `type` column CHECK constraint in the memories table.
ALLOWED_TARGETS: dict[str, str] = {
    "fact": "A verifiable fact about the user or their context",
    "preference": "A stated or inferred preference of the user",
    "context": "Background context relevant to ongoing work or conversation",
}


def validate_content(content: str) -> ValidationResult:
    """Validate memory content against REJECT_PATTERNS.

    Returns ValidationResult(valid=True, reason=None) if content passes all checks,
    or ValidationResult(valid=False, reason=<category>) on the first match.
    """
    if not content or not content.strip():
        return ValidationResult(valid=False, reason="empty_content")
    for pattern, category in REJECT_PATTERNS:
        if pattern.search(content):
            return ValidationResult(valid=False, reason=category)
    return ValidationResult(valid=True, reason=None)
