"""Split response text into handoff lines and display text."""

import re

_MAX_DISCORD_MSG_LEN = 1900
_FORMAL_HANDOFF_RE = re.compile(r"^\[handoff\]\s+<@!?\d+>(?:\s+.*)?$")
_MALFORMED_HANDOFF_DIAGNOSTIC = (
    "⚠️ handoff 未发送：格式无效；请使用 `[handoff] <@id> ...`。"
)


def split_handoff_lines(
    text: str,
    *,
    emit_diagnostic: bool = True,
) -> tuple[list[str], str]:
    """Extract [handoff] lines from text, returning (handoff_lines, clean_text).

    Each handoff line is stripped and truncated to Discord's message limit.
    """
    handoff_lines: list[str] = []
    clean_lines: list[str] = []
    in_code_fence = False
    malformed_candidate_seen = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            clean_lines.append(line)
        elif not in_code_fence and _FORMAL_HANDOFF_RE.match(stripped):
            handoff_lines.append(stripped[:_MAX_DISCORD_MSG_LEN])
        else:
            if not in_code_fence and stripped.startswith("[handoff]"):
                malformed_candidate_seen = True
            clean_lines.append(line)
    if malformed_candidate_seen and emit_diagnostic:
        clean_lines.append(_MALFORMED_HANDOFF_DIAGNOSTIC)
    return handoff_lines, "\n".join(clean_lines).strip()
