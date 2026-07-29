"""Wiki knowledge store — atomic file-backed wiki for multinexus.

The wiki is a flat-file system of Markdown pages with a text index.
Agents can write pages via <!-- WIKI: page-name --> tags in their responses.
The private tier (wiki/private/) is only visible to agents with include_private=True.

Directory layout:
  wiki/
    index.md           — searchable page index
    pages/             — published pages
    drafts/            — pending review (auto-promoted after 24h)
    log.md             — append-only discovery log
    private/
      index.md         — private page index (not committed to git)
      pages/           — private published pages
      drafts/          — private drafts awaiting promotion
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("multinexus")


class WikiWriteError(Exception):
    """Raised when an atomic wiki file write fails."""


# ---------------------------------------------------------------------------
# Memory marker regex (SHA-256 hex, backreference ensures open/close match)
# ---------------------------------------------------------------------------

_MARKER_RE = re.compile(
    r"<!-- memory:([0-9a-f]{64}) -->([\s\S]*?)<!-- /memory:\1 -->",
    re.DOTALL,
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# ---------------------------------------------------------------------------
# Secret scrubber patterns — applied to all content before write
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bsk-[A-Za-z0-9]{20,}\b'), "api_key_sk"),
    (re.compile(r'\bpk-[A-Za-z0-9]{20,}\b'), "api_key_pk"),
    (re.compile(r'\bghp_[A-Za-z0-9]{36,}\b'), "github_token"),
    (re.compile(r'\bxoxb-[A-Za-z0-9\-]{40,}\b'), "slack_token"),
    # PEM private key blocks
    (re.compile(r'-----BEGIN[^-]*PRIVATE KEY-----[\s\S]*?-----END[^-]*PRIVATE KEY-----'), "private_key"),
    # Windows paths leaking usernames (both slash styles)
    (re.compile(r'[Cc]:[/\\][Uu]sers[/\\][A-Za-z0-9_.\-]+[/\\]'), "windows_path"),
    # .env KEY=value lines
    (re.compile(r'^[A-Z_][A-Z0-9_]{2,}=[^\s]+', re.MULTILINE), "env_value"),
]

# ---------------------------------------------------------------------------
# Index entry helpers
# ---------------------------------------------------------------------------

# Matches: - [name](path) — summary | aliases: a, b, c
_INDEX_ENTRY_RE = re.compile(
    r"^- \[(.+?)\]\((.+?)\) \u2014 (.+?)(?:\s*\|\s*aliases:\s*(.+))?$"
)


def _parse_index_line(line: str) -> dict | None:
    """Parse one index entry. Returns None if the line is not a valid entry."""
    m = _INDEX_ENTRY_RE.match(line.strip())
    if not m:
        return None
    name, path, summary, aliases_str = m.groups()
    aliases = [a.strip() for a in aliases_str.split(",")] if aliases_str else []
    return {"name": name, "path": path, "summary": summary, "aliases": aliases}


def _format_index_line(name: str, path: str, summary: str, aliases: list[str]) -> str:
    line = f"- [{name}]({path}) \u2014 {summary}"
    if aliases:
        line += f" | aliases: {', '.join(aliases)}"
    return line


def _page_rel_path(name: str, status: str, private: bool = False) -> str:
    """Return the wiki-relative path for a page given its status and tier."""
    prefix = "private/" if private else ""
    if status == "draft":
        return f"{prefix}drafts/{name}.md"
    return f"{prefix}pages/{name}.md"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_summary(content: str) -> str:
    """Return the first meaningful non-frontmatter line (max 120 chars)."""
    in_fm = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "---":
            in_fm = not in_fm
            continue
        if in_fm:
            continue
        clean = stripped.lstrip("#").strip()
        if clean:
            return clean[:120]
    return "(no summary)"


def _match_index(index_text: str, query: str) -> list[str]:
    """Return page names whose summary or aliases match any query token (case-insensitive).

    Tokens shorter than 3 characters are ignored to reduce noise.
    """
    tokens = [t for t in query.lower().split() if len(t) >= 3]
    if not tokens:
        return []
    matches = []
    for line in index_text.splitlines():
        parsed = _parse_index_line(line)
        if not parsed:
            continue
        haystack = (parsed["summary"] + " " + " ".join(parsed["aliases"])).lower()
        if any(t in haystack for t in tokens):
            matches.append(parsed["name"])
    return matches


# ---------------------------------------------------------------------------
# Curation output parser
# ---------------------------------------------------------------------------


def parse_curation_output(raw: str) -> list[dict]:
    """Parse structured curation output from a local LLM. Strips <think> tags first."""
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    actions = []
    blocks = re.split(r"^---\s*ACTION:\s*", text, flags=re.MULTILINE)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n", 1)
        action_type = lines[0].strip().lower()
        body = lines[1] if len(lines) > 1 else ""

        if action_type in ("create", "update"):
            page_match = re.search(r"^PAGE:\s*(.+)$", body, re.MULTILINE)
            alias_match = re.search(r"^ALIASES:\s*(.+)$", body, re.MULTILINE)
            content_match = re.search(
                r"^CONTENT:\n(.*?)(?=^---\s*END|\Z)", body, re.MULTILINE | re.DOTALL
            )
            if page_match and content_match:
                actions.append({
                    "action": action_type,
                    "page": page_match.group(1).strip(),
                    "aliases": [a.strip() for a in alias_match.group(1).split(",")]
                    if alias_match else [],
                    "content": content_match.group(1).strip(),
                })
        elif action_type == "skip":
            actions.append({"action": "skip"})
    return actions


# ---------------------------------------------------------------------------
# WikiStore
# ---------------------------------------------------------------------------


class WikiStore:
    """Atomic file-backed wiki store with secret scrubbing and keyword search.

    Parameters:
        wiki_path:    Path to the wiki root directory.
        pinned_pages: Page slugs always prepended to agent context regardless of query.
                      These pages must exist at wiki/pages/<slug>.md at startup.
    """

    def __init__(self, wiki_path: Path, pinned_pages: list[str] | None = None) -> None:
        self.wiki_path = wiki_path
        self._lock = asyncio.Lock()
        self._pinned_pages: list[str] = list(pinned_pages or [])
        # Startup guard: all pinned pages must exist and must not be in the private tier
        for slug in self._pinned_pages:
            page_path = self._page_path(f"pages/{slug}.md")
            try:
                page_path.relative_to((self.wiki_path / "private").resolve())
                raise RuntimeError(
                    f"Pinned page must not be in private wiki tier: {slug!r}"
                )
            except ValueError:
                pass  # not inside private/ — correct
            if not page_path.exists():
                raise RuntimeError(
                    f"Pinned wiki page not found at startup: {page_path}"
                )

    def _page_path(self, slug: str) -> Path:
        """Resolve a relative page slug to an absolute path within wiki_path.

        Raises ValueError if the resolved path escapes wiki_path (path traversal guard).
        """
        target = (self.wiki_path / slug).resolve()
        try:
            target.relative_to(self.wiki_path.resolve())
        except ValueError:
            raise ValueError(f"Page slug {slug!r} escapes wiki root")
        return target

    def _scrub_secrets(self, content: str) -> str:
        """Apply all secret patterns to content before any write."""
        scrubbed = content
        for pattern, category in _SECRET_PATTERNS:
            before = scrubbed
            scrubbed = pattern.sub(f"[REDACTED:{category}]", scrubbed)
            if scrubbed != before:
                log.warning("wiki: scrubbed %s pattern from content before write", category)
        return scrubbed

    def _atomic_write(self, path: Path, content: str) -> None:
        """Write content to path atomically via a .tmp intermediate."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    def _update_index_locked(
        self,
        name: str,
        rel_path: str,
        summary: str,
        aliases: list[str],
        private: bool = False,
    ) -> None:
        """Add or update the index entry for name. Merges aliases (union, dedup)."""
        if private:
            index_path = self.wiki_path / "private" / "index.md"
            index_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            index_path = self.wiki_path / "index.md"
        existing = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
        lines = existing.splitlines()

        new_entry: str | None = None
        entry_idx: int | None = None
        for i, line in enumerate(lines):
            parsed = _parse_index_line(line)
            if parsed and parsed["name"] == name:
                merged_aliases = list(dict.fromkeys(parsed["aliases"] + aliases))
                merged_summary = (
                    summary
                    if summary and summary != "(no summary)"
                    else parsed["summary"]
                )
                new_entry = _format_index_line(name, rel_path, merged_summary, merged_aliases)
                entry_idx = i
                break

        if entry_idx is not None:
            lines[entry_idx] = new_entry  # type: ignore[assignment]
        else:
            lines.append(_format_index_line(name, rel_path, summary, aliases))

        text = "\n".join(lines)
        if not text.endswith("\n"):
            text += "\n"
        self._atomic_write(index_path, text)

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def read_page(self, name: str) -> str | None:
        """Read a page from pages/ then drafts/. Returns None if not found."""
        for subdir in ("pages", "drafts"):
            path = self.wiki_path / subdir / f"{name}.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None

    async def write_page(
        self,
        name: str,
        content: str,
        *,
        author: str,
        source: str = "inline",
        source_message_id: str | None = None,
        status: str = "published",
        aliases: list[str] | None = None,
        private: bool = False,
    ) -> None:
        """Atomically write a page with provenance frontmatter; update index."""
        aliases = aliases or []
        content = self._scrub_secrets(content)
        now = _now_iso()
        confidence = "medium" if source == "curation" else "high"

        fm_parts = [
            "---",
            f"title: {name}",
            f"source_agent: {author}",
        ]
        if source_message_id is not None:
            fm_parts.append(f'source_message_id: "{source_message_id}"')
        fm_parts += [
            f"source: {source}",
            f"created: {now}",
            f"updated: {now}",
            f"status: {status}",
            f"confidence: {confidence}",
            "---",
            "",
        ]
        full_content = "\n".join(fm_parts) + content

        rel_path = _page_rel_path(name, status, private=private)
        abs_path = self.wiki_path / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        async with self._lock:
            # Guard: public writes must not shadow an existing private page
            if not private:
                if (
                    (self.wiki_path / "private" / "pages" / f"{name}.md").exists()
                    or (self.wiki_path / "private" / "drafts" / f"{name}.md").exists()
                ):
                    raise ValueError(
                        f"Page name '{name}' is reserved by the private wiki namespace"
                    )
            self._atomic_write(abs_path, full_content)
            self._update_index_locked(
                name, rel_path, _extract_summary(content), aliases, private=private
            )

    async def update_page(
        self,
        name: str,
        content: str,
        *,
        author: str,
        status: str = "draft",
        aliases: list[str] | None = None,
    ) -> None:
        """Append new content to an existing page (or create it). Atomic write."""
        aliases = aliases or []
        now = _now_iso()

        async with self._lock:
            existing_path: Path | None = None
            existing: str | None = None
            for subdir in ("pages", "drafts"):
                candidate = self.wiki_path / subdir / f"{name}.md"
                if candidate.exists():
                    existing_path = candidate
                    existing = candidate.read_text(encoding="utf-8")
                    break

            if existing is not None and existing_path is not None:
                updated = re.sub(
                    r"^(updated:\s*).*$",
                    rf"\g<1>{now}",
                    existing,
                    flags=re.MULTILINE,
                )
                full_content = updated.rstrip() + "\n\n" + content
                abs_path = existing_path
            else:
                fm_parts = [
                    "---",
                    f"title: {name}",
                    f"source_agent: {author}",
                    "source: update",
                    f"created: {now}",
                    f"updated: {now}",
                    f"status: {status}",
                    "confidence: medium",
                    "---",
                    "",
                ]
                full_content = "\n".join(fm_parts) + content
                abs_path = self.wiki_path / _page_rel_path(name, status)

            rel_path = abs_path.relative_to(self.wiki_path).as_posix()
            full_content = self._scrub_secrets(full_content)
            self._atomic_write(abs_path, full_content)
            self._update_index_locked(name, rel_path, _extract_summary(content), aliases)

    async def promote_page(self, name: str) -> bool:
        """Move a draft to pages/. Updates frontmatter status and index path.

        Returns False if no draft exists for name.
        """
        draft_path = self.wiki_path / "drafts" / f"{name}.md"
        pages_dir = self.wiki_path / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        pages_path = pages_dir / f"{name}.md"

        async with self._lock:
            if not draft_path.exists():
                return False
            content = draft_path.read_text(encoding="utf-8")
            now = _now_iso()
            content = re.sub(
                r"^(status:\s*).*$", r"\g<1>published",
                content, flags=re.MULTILINE,
            )
            content = re.sub(
                r"^(updated:\s*).*$", rf"\g<1>{now}",
                content, flags=re.MULTILINE,
            )
            self._atomic_write(pages_path, content)
            draft_path.unlink()
            self._update_index_locked(
                name, f"pages/{name}.md",
                _extract_summary(content), [],
            )
        return True

    async def reject_page(self, name: str) -> bool:
        """Delete a draft page. Returns False if no draft exists."""
        draft_path = self.wiki_path / "drafts" / f"{name}.md"

        async with self._lock:
            if not draft_path.exists():
                return False
            draft_path.unlink()
            index_path = self.wiki_path / "index.md"
            if index_path.exists():
                lines = index_path.read_text(encoding="utf-8").splitlines()
                lines = [
                    ln for ln in lines
                    if not (_parse_index_line(ln) or {}).get("name") == name
                ]
                text = "\n".join(lines)
                if not text.endswith("\n"):
                    text += "\n"
                self._atomic_write(index_path, text)
        return True

    async def list_drafts(self) -> list[dict]:
        """Return metadata for all draft pages (name, created, source_agent)."""
        drafts_dir = self.wiki_path / "drafts"
        if not drafts_dir.exists():
            return []

        result = []
        for path in sorted(drafts_dir.glob("*.md")):
            if path.name.startswith("curation-"):
                continue  # skip failure dumps
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            created = ""
            source_agent = ""
            for line in text.splitlines():
                if line.startswith("created:"):
                    created = line.split(":", 1)[1].strip()
                elif line.startswith("source_agent:"):
                    source_agent = line.split(":", 1)[1].strip()
                elif line.strip() == "---" and created:
                    break
            result.append({
                "name": path.stem,
                "created": created,
                "source_agent": source_agent,
            })
        return result

    async def write_private_draft(
        self,
        name: str,
        content: str,
        *,
        author: str,
        aliases: list[str] | None = None,
    ) -> None:
        """Write a private draft. Does NOT appear in the general wiki or agent context
        (unless include_private=True / agent_name='mac-openclaw')."""
        await self.write_page(
            name, content,
            author=author, source="inline", status="draft",
            aliases=aliases, private=True,
        )

    async def promote_private_page(self, name: str) -> bool:
        """Move a private draft to private/pages/. Returns False if draft not found."""
        draft_path = self.wiki_path / "private" / "drafts" / f"{name}.md"
        pages_dir = self.wiki_path / "private" / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        pages_path = pages_dir / f"{name}.md"

        async with self._lock:
            if not draft_path.exists():
                return False
            content = draft_path.read_text(encoding="utf-8")
            now = _now_iso()
            content = re.sub(r"^(status:\s*).*$", r"\g<1>published", content, flags=re.MULTILINE)
            content = re.sub(r"^(updated:\s*).*$", rf"\g<1>{now}", content, flags=re.MULTILINE)
            self._atomic_write(pages_path, content)
            draft_path.unlink()
            self._update_index_locked(
                name, f"private/pages/{name}.md",
                _extract_summary(content), [], private=True,
            )
        return True

    async def reject_private_page(self, name: str) -> bool:
        """Delete a private draft. Returns False if not found."""
        draft_path = self.wiki_path / "private" / "drafts" / f"{name}.md"

        async with self._lock:
            if not draft_path.exists():
                return False
            draft_path.unlink()
            index_path = self.wiki_path / "private" / "index.md"
            if index_path.exists():
                lines = index_path.read_text(encoding="utf-8").splitlines()
                lines = [ln for ln in lines if not (_parse_index_line(ln) or {}).get("name") == name]
                text = "\n".join(lines)
                if not text.endswith("\n"):
                    text += "\n"
                self._atomic_write(index_path, text)
        return True

    async def list_private_drafts(self) -> list[dict]:
        """Return metadata for all private draft pages."""
        drafts_dir = self.wiki_path / "private" / "drafts"
        if not drafts_dir.exists():
            return []

        result = []
        for path in sorted(drafts_dir.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            created = ""
            source_agent = ""
            for line in text.splitlines():
                if line.startswith("created:"):
                    created = line.split(":", 1)[1].strip()
                elif line.startswith("source_agent:"):
                    source_agent = line.split(":", 1)[1].strip()
                elif line.strip() == "---" and created:
                    break
            result.append({"name": path.stem, "created": created, "source_agent": source_agent})
        return result

    async def read_private_page(self, name: str) -> str | None:
        """Read a private page from private/pages/ then private/drafts/."""
        for subdir in ("pages", "drafts"):
            path = self.wiki_path / "private" / subdir / f"{name}.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None

    async def read_index(self) -> str:
        """Read index.md content."""
        index_path = self.wiki_path / "index.md"
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")
        return ""

    async def _read_private_index(self) -> str:
        """Read private/index.md content."""
        index_path = self.wiki_path / "private" / "index.md"
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")
        return ""

    async def update_index(
        self,
        page_name: str,
        summary: str,
        aliases: list[str] | None = None,
    ) -> None:
        """Add or update an index entry. Merges aliases (union, dedup)."""
        aliases = aliases or []
        rel_path = f"pages/{page_name}.md"
        if (self.wiki_path / "drafts" / f"{page_name}.md").exists():
            rel_path = f"drafts/{page_name}.md"

        async with self._lock:
            self._update_index_locked(page_name, rel_path, summary, aliases)

    async def ingest_discoveries(self, discoveries_path: Path) -> int:
        """Append new discovery log lines to wiki/log.md using a persisted cursor.

        Returns the number of new entries ingested.
        """
        if not discoveries_path.exists():
            return 0

        state_path = self.wiki_path / ".wiki-state.json"
        log_path = self.wiki_path / "log.md"

        async with self._lock:
            last_line = 0
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    cursor = state.get("last_line", 0)
                    if isinstance(cursor, int) and cursor >= 0:
                        last_line = cursor
                except (json.JSONDecodeError, OSError):
                    last_line = 0

            new_entries: list[str] = []
            total_lines = 0
            with discoveries_path.open(encoding="utf-8") as f:
                total_lines = sum(1 for _ in f)

            truncated = total_lines < last_line
            if truncated:
                log.warning(
                    "wiki: discoveries cursor reset from %d to 0 (file was truncated to %d lines)",
                    last_line, total_lines,
                )
                last_line = 0

            with discoveries_path.open(encoding="utf-8") as f:
                for line_number, raw_line in enumerate(f, start=1):
                    if line_number <= last_line:
                        continue
                    new_entries.append(self._scrub_secrets(raw_line.rstrip("\r\n")))

            if not new_entries:
                if truncated:
                    self._atomic_write(
                        state_path,
                        json.dumps({"last_line": total_lines}) + "\n",
                    )
                return 0

            log_path.parent.mkdir(parents=True, exist_ok=True)
            needs_newline = False
            if log_path.exists() and log_path.stat().st_size > 0:
                with log_path.open("rb") as existing_log:
                    existing_log.seek(-1, os.SEEK_END)
                    needs_newline = existing_log.read(1) not in {b"\n", b"\r"}

            with log_path.open("a", encoding="utf-8") as log_file:
                if needs_newline:
                    log_file.write("\n")
                for entry in new_entries:
                    log_file.write(f"{entry}\n")

            self._atomic_write(
                state_path,
                json.dumps({"last_line": total_lines}) + "\n",
            )
            return len(new_entries)

    async def search(
        self,
        query: str,
        budget_chars: int = 4000,
        agent_name: str | None = None,
    ) -> str:
        """Search index summaries + aliases; fall back to grepping page content.

        Returns concatenated matching page content within budget_chars.
        Pass agent_name='mac-openclaw' (or another agent with private access) to include
        the private tier in search results.
        """
        index_text = await self.read_index()
        matches = _match_index(index_text, query)
        if not matches:
            matches = await self._grep_pages(query)
        general = await self._load_pages_within_budget(matches, budget_chars)

        if agent_name in {"mac-openclaw", "local-agent"}:
            private_index = await self._read_private_index()
            private_matches = _match_index(private_index, query)
            if not private_matches:
                private_matches = await self._grep_private_pages(query)
            remaining = budget_chars - len(general)
            if remaining > 0 and private_matches:
                private = await self._load_private_pages_within_budget(private_matches, remaining)
                if private:
                    parts = [p for p in [general, f"## [Private Knowledge]\n{private}"] if p]
                    return "\n\n---\n\n".join(parts)

        return general

    async def get_relevant_context(
        self,
        query: str,
        budget_chars: int = 4000,
        channel_id: str | None = None,
        include_private: bool = False,
        agent_name: str | None = None,
    ) -> str:
        """Return wiki context relevant to query, fail-closed (returns '' on no match).

        Pinned pages are always prepended regardless of query match.
        Pass include_private=True or agent_name='mac-openclaw' for private-tier context.
        """
        pinned = await self._load_pinned_pages(budget_chars)
        remaining = budget_chars - len(pinned)

        index_text = await self.read_index()
        matches = [m for m in _match_index(index_text, query) if m not in self._pinned_pages]
        general = (
            await self._load_pages_within_budget(matches, remaining)
            if matches and remaining > 0
            else ""
        )

        should_include_private = include_private or agent_name in {
            "mac-openclaw",
            "local-agent",
        }
        if should_include_private:
            private_index = await self._read_private_index()
            private_matches = _match_index(private_index, query)
            priv_remaining = remaining - len(general)
            if priv_remaining > 0 and private_matches:
                private = await self._load_private_pages_within_budget(private_matches, priv_remaining)
                if private:
                    parts = [p for p in [pinned, general, f"## [Private Knowledge]\n{private}"] if p]
                    return "\n\n---\n\n".join(parts)

        parts = [p for p in [pinned, general] if p]
        return "\n\n---\n\n".join(parts)

    async def remove_marker(self, page_slug: str, marker_hash: str) -> bool:
        """Remove a memory marker block from a wiki page.

        Returns True if the marker was found and removed, False if not found.
        Raises WikiWriteError on write failure. Raises ValueError for invalid hash format.
        """
        if not _HASH_RE.match(marker_hash):
            raise ValueError(
                f"Invalid marker_hash {marker_hash!r}: expected [0-9a-f]{{64}}"
            )
        path = self._page_path(page_slug)

        async with self._lock:
            if not path.exists():
                return False
            content = path.read_text(encoding="utf-8")

            found = False

            def _sub(m: re.Match) -> str:
                nonlocal found
                if m.group(1) == marker_hash:
                    found = True
                    return ""
                return m.group(0)

            new_content = _MARKER_RE.sub(_sub, content)

            if not found:
                return False

            new_content = re.sub(r"\n{4,}", "\n\n\n", new_content)

            try:
                self._atomic_write(path, new_content)
            except OSError as exc:
                raise WikiWriteError(f"Failed to write {path}: {exc}") from exc

            return True

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _load_pinned_pages(self, budget_chars: int) -> str:
        """Return concatenated content of all pinned pages, within budget_chars."""
        parts: list[str] = []
        remaining = budget_chars
        for slug in self._pinned_pages:
            if remaining <= 0:
                break
            page_path = self.wiki_path / "pages" / f"{slug}.md"
            if page_path.exists():
                text = page_path.read_text(encoding="utf-8")
                chunk = text[:remaining]
                parts.append(chunk)
                remaining -= len(chunk)
        return "\n\n---\n\n".join(parts)

    async def _grep_pages(self, query: str) -> list[str]:
        """Return names of pages whose content contains query (case-insensitive)."""
        q = query.lower()
        matches: list[str] = []
        for subdir in ("pages", "drafts"):
            subdir_path = self.wiki_path / subdir
            if not subdir_path.exists():
                continue
            for md_file in sorted(subdir_path.glob("*.md")):
                try:
                    if q in md_file.read_text(encoding="utf-8").lower():
                        matches.append(md_file.stem)
                except OSError:
                    pass
        return matches

    async def _grep_private_pages(self, query: str) -> list[str]:
        """Return names of private pages whose content contains query (case-insensitive)."""
        q = query.lower()
        matches: list[str] = []
        for subdir in ("pages", "drafts"):
            subdir_path = self.wiki_path / "private" / subdir
            if not subdir_path.exists():
                continue
            for md_file in sorted(subdir_path.glob("*.md")):
                try:
                    if q in md_file.read_text(encoding="utf-8").lower():
                        matches.append(md_file.stem)
                except OSError:
                    pass
        return matches

    async def _load_private_pages_within_budget(
        self, page_names: list[str], budget_chars: int
    ) -> str:
        """Load private pages in order, truncating at budget_chars total characters."""
        parts: list[str] = []
        remaining = budget_chars
        for name in page_names:
            if remaining <= 0:
                break
            text = await self.read_private_page(name)
            if text is None:
                continue
            chunk = text[:remaining]
            parts.append(chunk)
            remaining -= len(chunk)
        return "\n\n---\n\n".join(parts)

    async def _load_pages_within_budget(
        self, page_names: list[str], budget_chars: int
    ) -> str:
        """Load pages in order, truncating at budget_chars total characters."""
        parts: list[str] = []
        remaining = budget_chars
        for name in page_names:
            if remaining <= 0:
                break
            text = await self.read_page(name)
            if text is None:
                continue
            chunk = text[:remaining]
            parts.append(chunk)
            remaining -= len(chunk)
        return "\n\n---\n\n".join(parts)
