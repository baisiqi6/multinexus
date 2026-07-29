"""Output scanning and secret redaction for defense-in-depth.

Scans all agent responses before they are sent to Discord.
Catches secrets that might have been echoed back by an LLM
(e.g. from conversation context or prompt injection attempts).

This module is reusable defense-in-depth. Callers must invoke ``scan_output``
explicitly; the current Discord bridge does not yet apply it globally.
"""

import os
import re

# Regex patterns that should never appear in bot output
_REDACT_PATTERNS = [
    # Discord bot tokens: base64-encoded user ID.timestamp.HMAC
    re.compile(r"[MN][A-Za-z\d]{23,}\.[\w-]{6}\.[\w-]{27,}"),
    # Generic long base64 strings (potential tokens/keys, 40+ chars, must contain letters)
    re.compile(
        r"(?<![A-Za-z\d])(?=[A-Za-z\d+/]*[A-Za-z][A-Za-z\d+/]*)"
        r"[A-Za-z\d+/]{40,}={0,2}(?![A-Za-z\d])"
    ),
    # key=value assignments with secrets
    re.compile(
        r"(?:token|key|secret|password|credential|api_key)\s*[:=]\s*\S{8,}",
        re.IGNORECASE,
    ),
    # Environment variable assignments with known sensitive names
    re.compile(
        r"(?:DISCORD_TOKEN|LMSTUDIO_API_KEY|OPENCLAW_GATEWAY_TOKEN|"
        r"API_KEY|SECRET_KEY|BOT_TOKEN|AUTH_TOKEN)\s*=\s*\S+",
        re.IGNORECASE,
    ),
    # Bearer/Basic auth headers
    re.compile(r"(?:Bearer|Basic)\s+[A-Za-z\d+/_.~-]{20,}", re.IGNORECASE),
    # SSH private key markers
    re.compile(r"-----BEGIN\s+\w+\s+PRIVATE\s+KEY-----"),
    # Hex strings that look like hashes/keys (64+ chars)
    re.compile(r"(?<![A-Za-z\d])[0-9a-fA-F]{64,}(?![A-Za-z\d])"),
]

# Runtime-loaded literal secret values (set once at startup)
_LITERAL_SECRETS: list[str] = []

REDACTION_MARKER = "[REDACTED]"

# Environment variable names that hold secrets to match literally in output
_SENSITIVE_ENV_VARS = (
    "DISCORD_TOKEN",
    "LMSTUDIO_API_KEY",
    "OPENCLAW_GATEWAY_TOKEN",
)


def load_secret_literals():
    """Load actual secret values from env to match literally in output.

    Call once at startup after dotenv is loaded. This ensures that even if
    an LLM echoes back the exact token value, it gets redacted.
    """
    for var in _SENSITIVE_ENV_VARS:
        val = os.getenv(var)
        if val and len(val) >= 20:
            _LITERAL_SECRETS.append(val)


def scan_output(text: str) -> str:
    """Scan text for sensitive patterns and redact them.

    Defense-in-depth: even if an LLM echoes back a secret from
    the conversation context, this catches it before Discord.

    Args:
        text: The agent response text to scan.

    Returns:
        The text with any detected secrets replaced by REDACTION_MARKER.
    """
    result = text

    # Check literal secret values first (exact match, fastest)
    for secret in _LITERAL_SECRETS:
        if secret in result:
            result = result.replace(secret, REDACTION_MARKER)

    # Then regex patterns
    for pattern in _REDACT_PATTERNS:
        result = pattern.sub(REDACTION_MARKER, result)

    return result
