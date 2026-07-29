"""Discord attachment processing — text embedding and image handling for agents."""

import base64
import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import discord

log = logging.getLogger(__name__)

MAX_TEXT_SIZE = 100 * 1024       # 100 KB
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB

SUPPORTED_IMAGE_CONTENT_TYPES = frozenset({
    "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp",
})

# Extensions treated as text even if content-type isn't set
_TEXT_EXTENSIONS = frozenset({
    ".md", ".txt", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".json", ".yaml", ".yml", ".toml", ".csv", ".log",
    ".sh", ".bash", ".zsh", ".rs", ".go", ".java", ".rb",
    ".html", ".css", ".xml", ".sql", ".env.example",
})


@dataclass
class ProcessedAttachments:
    """Result of processing Discord message attachments."""
    text_block: str = ""
    # Local file paths for Claude/Codex (subprocess agents with filesystem access).
    file_paths: list[str] = field(default_factory=list)
    # OpenAI vision-format content blocks for local agent (no local filesystem access).
    vision_blocks: list[dict] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return bool(self.text_block or self.file_paths or self.vision_blocks)


async def process_attachments(
    message: discord.Message,
    temp_dir: str,
) -> ProcessedAttachments:
    """Download and process message attachments for agent consumption.

    - Text files: fetched and embedded inline as fenced code blocks.
    - Images: saved to temp_dir (path injected into prompt for Claude/Codex);
      also base64-encoded as OpenAI vision blocks (for local agent via OpenClaw).
    - Unsupported types: noted in text_block so the agent is aware.
    """
    result = ProcessedAttachments()
    if not message.attachments:
        return result

    os.makedirs(temp_dir, exist_ok=True)
    text_parts: list[str] = []

    async with aiohttp.ClientSession() as session:
        for attachment in message.attachments:
            ct = (attachment.content_type or "").split(";")[0].strip().lower()
            suffix = Path(attachment.filename).suffix.lower()

            is_text = ct.startswith("text/") or suffix in _TEXT_EXTENSIONS
            is_image = ct in SUPPORTED_IMAGE_CONTENT_TYPES

            if is_text:
                if attachment.size > MAX_TEXT_SIZE:
                    text_parts.append(
                        f"*[{attachment.filename}: too large to embed "
                        f"({attachment.size // 1024} KB > 100 KB limit)]*"
                    )
                    continue
                try:
                    async with session.get(attachment.url) as resp:
                        if resp.status == 200:
                            content = await resp.text(errors="replace")
                            lang = suffix.lstrip(".") or ""
                            text_parts.append(
                                f"**{attachment.filename}:**\n```{lang}\n{content}\n```"
                            )
                        else:
                            log.warning(
                                "attachment: failed to fetch %s (HTTP %d)",
                                attachment.filename, resp.status,
                            )
                except Exception as e:
                    log.warning("attachment: error fetching %s: %s", attachment.filename, e)

            elif is_image:
                if attachment.size > MAX_IMAGE_SIZE:
                    text_parts.append(
                        f"*[{attachment.filename}: image too large "
                        f"({attachment.size // 1024 // 1024} MB > 10 MB limit)]*"
                    )
                    continue
                try:
                    async with session.get(attachment.url) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            # Deterministic filename by URL hash — deduplicates re-uploads.
                            url_hash = hashlib.sha1(attachment.url.encode()).hexdigest()[:12]
                            ext = suffix or ".png"
                            local_filename = f"{url_hash}{ext}"
                            local_path = os.path.join(temp_dir, local_filename)
                            with open(local_path, "wb") as fh:
                                fh.write(data)
                            result.file_paths.append(local_path)
                            text_parts.append(
                                f"*[Image `{attachment.filename}` → `{local_path}`]*"
                            )
                            # Vision block for local agent (OpenAI multimodal format).
                            b64 = base64.b64encode(data).decode()
                            media_type = ct if ct in SUPPORTED_IMAGE_CONTENT_TYPES else "image/png"
                            result.vision_blocks.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{media_type};base64,{b64}"},
                            })
                        else:
                            log.warning(
                                "attachment: failed to fetch image %s (HTTP %d)",
                                attachment.filename, resp.status,
                            )
                except Exception as e:
                    log.warning("attachment: error fetching image %s: %s", attachment.filename, e)

            else:
                text_parts.append(
                    f"*[{attachment.filename}: unsupported type "
                    f"({ct or 'unknown'}) — cannot process]*"
                )

    result.text_block = "\n\n".join(text_parts)
    return result
