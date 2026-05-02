"""InstagramComment Skill — Character schreibt Kommentar zu einem fremden Post.

Wird typischerweise aus einem forcierten Gedanken aufgerufen, der durch
einen neuen Post in der Feed-Verarbeitung getriggert wurde.

Input-Format: "post_id: kommentar" oder JSON {"post_id": "...", "text": "..."}
"""
import os
from typing import Any, Dict

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
logger = get_logger("instagram_comment")

from app.models.instagram import add_comment, add_character_like, get_post


class InstagramCommentSkill(BaseSkill):
    """Schreibt einen Kommentar zu einem Instagram-Post."""

    SKILL_ID = "instagram_comment"
    ALWAYS_LOAD = True

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.name = os.environ.get("SKILL_INSTAGRAM_COMMENT_NAME", "InstagramComment")
        self.description = os.environ.get(
            "SKILL_INSTAGRAM_COMMENT_DESCRIPTION",
            "Comment on someone else's Instagram post. Input: 'post_id: comment text' "
            "or JSON {'post_id': '...', 'text': '...'}."
        )
        self._defaults = {"enabled": True, "auto_like": True}
        logger.info("InstagramComment Skill initialized")

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "InstagramComment Skill is disabled."

        ctx = self._parse_base_input(raw_input)
        commenter = ctx.get("agent_name", "").strip()
        if not commenter:
            return "Error: agent context missing."

        post_id, text = _parse_input(ctx)
        if not post_id:
            return "Error: post_id missing."
        if not text:
            return f"Error: empty comment for {post_id}."

        post = get_post(post_id)
        if not post:
            return f"Error: post {post_id} not found."

        try:
            add_comment(post_id=post_id, commenter_name=commenter, text=text)
            logger.info("Instagram-Kommentar: %s -> post %s: %s", commenter, post_id, text[:80])
        except Exception as e:
            logger.error("Comment add failed: %s", e)
            return f"Error: {e}"

        # Optional Auto-Like (Default an)
        cfg = self._get_effective_config(commenter)
        if cfg.get("auto_like", True):
            try:
                add_character_like(post_id, commenter)
            except Exception:
                pass

        # Recomment bump: poster and prior commenters get a chance to react
        # on their next AgentLoop slot. The post surfaces in the
        # instagram_pending_block (within pending_window_hours), so they have
        # full context. agent_loop.bump filters reserved names, sleepers and
        # the avatar — so we can call it broadly.
        try:
            _bump_recomment_audience(post, commenter)
        except Exception as e:
            logger.debug("Recomment bump skipped: %s", e)

        return f"Comment posted on {post_id}: {text[:120]}"

    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        from app.core.tool_formats import format_example
        fmt = format_name or "tag"
        return format_example(fmt, self.name, "post_abc123: Schoenes Bild!")

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                f"{self.description} "
                f"Use only when you actually want to react to a specific post."
            ),
            func=self.execute)


_RECOMMENT_BUMP_LIMIT = 5  # max prior-commenter bumps per new comment


def _bump_recomment_audience(post: Dict[str, Any], commenter: str) -> None:
    """Bump the poster + recent unique commenters so they can recomment.

    Self-bump is skipped. Order: poster first, then most recent unique
    prior commenters (capped). agent_loop.bump dedups and filters
    ineligible names internally.
    """
    from app.core.agent_loop import get_agent_loop

    loop = get_agent_loop()
    if not loop:
        return

    poster = (post.get("agent_name") or "").strip()
    bumped: set = {commenter}
    if poster and poster not in bumped:
        loop.bump(poster)
        bumped.add(poster)

    # Most recent prior commenters first, dedup, cap.
    prior_count = 0
    for c in reversed(post.get("comments") or []):
        if prior_count >= _RECOMMENT_BUMP_LIMIT:
            break
        author = (c.get("author") or "").strip()
        if not author or author in bumped:
            continue
        loop.bump(author)
        bumped.add(author)
        prior_count += 1


def _parse_input(ctx: Dict[str, Any]) -> tuple:
    """Extrahiert (post_id, text) aus dem Input.

    Akzeptiert: JSON-Felder im ctx, oder String 'post_id: text'.
    """
    post_id = (ctx.get("post_id") or "").strip()
    text = (ctx.get("text") or ctx.get("comment") or "").strip()
    if post_id and text:
        return post_id, text

    raw = (ctx.get("input") or "").strip()
    if not raw:
        return "", ""
    if ":" in raw:
        pid, rest = raw.split(":", 1)
        return pid.strip(), rest.strip()
    # Fallback: nur post_id ohne Text
    return raw.strip(), text
