"""Discord-sized text chunking shared by bridge response paths."""

MAX_DISCORD_MSG_LEN = 1900


def chunk_message(text: str) -> list[str]:
    if len(text) <= MAX_DISCORD_MSG_LEN:
        return [text] if text.strip() else []
    chunks = []
    while text:
        if len(text) <= MAX_DISCORD_MSG_LEN:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, MAX_DISCORD_MSG_LEN)
        if cut < MAX_DISCORD_MSG_LEN // 2:
            cut = text.rfind(" ", 0, MAX_DISCORD_MSG_LEN)
        if cut < MAX_DISCORD_MSG_LEN // 2:
            cut = MAX_DISCORD_MSG_LEN
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n ")
    return chunks
