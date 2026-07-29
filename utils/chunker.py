"""Message chunking for Discord's 2000-character limit with code block awareness.

Splits long agent responses into multiple messages while preserving
Markdown code block fences across chunk boundaries.
"""

DISCORD_MAX = 2000
# Leave room for code fence continuity markers
CHUNK_MAX = DISCORD_MAX - 20


def chunk_message(text: str) -> list[str]:
    """Split a message into chunks that fit Discord's 2000-char limit.

    Preserves code block fences across chunks — if a chunk splits inside
    a code block, the closing ``` is appended and a new opening ``` is
    prepended to the next chunk.

    Args:
        text: The full message text to split.

    Returns:
        List of message chunks, each <= DISCORD_MAX characters.
    """
    if len(text) <= DISCORD_MAX:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= CHUNK_MAX:
            chunks.append(remaining)
            break

        split_at = _find_split(remaining, CHUNK_MAX)
        chunk = remaining[:split_at]
        remaining = remaining[split_at:].lstrip("\n")

        # Handle code block continuity
        if _has_unclosed_code_block(chunk):
            chunk += "\n```"
            remaining = "```\n" + remaining

        chunks.append(chunk)

    return chunks


def _find_split(text: str, max_len: int) -> int:
    """Find the best split point at or before max_len.

    Prefers splitting at paragraph breaks, then line breaks, then spaces.
    Hard-splits at max_len as a last resort.
    """
    # Prefer splitting at double newline (paragraph break)
    idx = text.rfind("\n\n", 0, max_len)
    if idx > max_len // 2:
        return idx

    # Then single newline
    idx = text.rfind("\n", 0, max_len)
    if idx > max_len // 2:
        return idx

    # Then space
    idx = text.rfind(" ", 0, max_len)
    if idx > max_len // 2:
        return idx

    # Hard split as last resort
    return max_len


def _has_unclosed_code_block(text: str) -> bool:
    """Check if text has an odd number of ``` markers (i.e., unclosed block)."""
    return text.count("```") % 2 == 1
