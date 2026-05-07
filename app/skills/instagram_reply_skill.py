"""InstagramReply Skill — Creator antwortet auf einen Kommentar unter eigenem Post.

Wird typischerweise aus einem forcierten Gedanken aufgerufen, der durch einen
neuen Kommentar getriggert wurde.

Input-Format: "post_id @commenter: text" oder JSON {post_id, commenter, text}
"""
from typing import Any, Dict

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
logger = get_logger("instagram_reply")

from app.models.instagram import add_comment, get_post


class InstagramReplySkill(BaseSkill):
    """Antwortet als Creator auf einen Kommentar unter dem eigenen Post."""

    SKILL_ID = "instagram_reply"
    ALWAYS_LOAD = True

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        from app.core.prompt_templates import load_skill_meta
        meta = load_skill_meta("instagram_reply")
        self.name = meta["name"]
        self.description = meta["description"]
        self._defaults = {"enabled": True}
        logger.info("InstagramReply Skill initialized")

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "InstagramReply Skill is disabled."

        ctx = self._parse_base_input(raw_input)
        creator = ctx.get("agent_name", "").strip()
        if not creator:
            return "Error: agent context missing."

        post_id, commenter, text = _parse_input(ctx)
        if not post_id:
            return "Error: post_id missing."
        if not text:
            return "Error: empty reply."

        post = get_post(post_id)
        if not post:
            return f"Error: post {post_id} not found."
        if post.get("agent_name", "") and post.get("agent_name", "") != creator:
            return f"Error: post {post_id} belongs to {post.get('agent_name', '')}, not you."

        body = f"@{commenter} {text}" if commenter else text
        try:
            add_comment(post_id=post_id, commenter_name=creator, text=body)
            logger.info("Instagram-Reply: %s -> %s on %s: %s",
                        creator, commenter, post_id, text[:80])
        except Exception as e:
            logger.error("Reply add failed: %s", e)
            return f"Error: {e}"

        return f"Reply posted on {post_id}: {body[:120]}"

    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        from app.core.tool_formats import format_example
        fmt = format_name or "tag"
        return format_example(fmt, self.name, "post_abc @Pixel: Danke fuer den lieben Kommentar!")

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            func=self.execute)


def _parse_input(ctx: Dict[str, Any]) -> tuple:
    """Extrahiert (post_id, commenter, text) aus dem Input."""
    post_id = (ctx.get("post_id") or "").strip()
    commenter = (ctx.get("commenter") or "").strip()
    text = (ctx.get("text") or ctx.get("reply") or "").strip()
    if post_id and text:
        return post_id, commenter, text

    raw = (ctx.get("input") or "").strip()
    if not raw:
        return "", "", ""

    # Format: "post_id @commenter: text"
    parts = raw.split(":", 1)
    if len(parts) == 2:
        head = parts[0].strip()
        text = parts[1].strip()
        if "@" in head:
            pid, mention = head.split("@", 1)
            return pid.strip(), mention.strip(), text
        return head.strip(), commenter, text

    return raw.strip(), commenter, text
