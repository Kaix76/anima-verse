"""Instagram Social Reactions - Characters reagieren auf Posts anderer Characters.

Wenn ein Character einen Instagram-Post erstellt, "sehen" andere Characters
den Post basierend auf der Popularitaet des Posters. Reaktionen werden als
Knowledge-Eintraege fuer beide Characters gespeichert.

Laeuft in der BackgroundQueue (ein Task gleichzeitig).
"""
import base64
import os
import random
from typing import Any, Dict, Optional

from app.models.character import (
    list_available_characters,
    get_character_config,
    get_character_skill_config)
from app.models.memory import upsert_relationship_memory as upsert_character_relationship
from app.models.instagram import add_character_like, get_instagram_dir, load_image_meta
from app.models.relationship import record_interaction as _record_rel
from app.core.background_queue import get_background_queue

from app.core.log import get_logger
logger = get_logger("social_reactions")


def _handle_instagram_reaction(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Background-Queue Handler fuer instagram_reaction Tasks.

    payload: user_id, poster_name, post (dict mit caption, image_prompt, id, ...)
    """
    user_id = payload["user_id"]
    poster_name = payload["poster_name"]
    post = payload["post"]
    caption = post.get("caption", "")
    post_id = post.get("id", "")

    logger.info("=" * 50)
    logger.info("Post von %s: %s", poster_name, caption[:80])
    logger.info("=" * 50)

    # Popularitaet: zuerst aus character_config, Fallback auf Instagram-Skill-Config
    poster_config = get_character_config(poster_name)
    popularity = poster_config.get("popularity", None)
    if popularity is None:
        poster_ig_config = get_character_skill_config(poster_name, "instagram")
        popularity = int(poster_ig_config.get("popularity", 50))
    else:
        popularity = int(popularity)
    logger.debug("Popularitaet von %s: %d%%", poster_name, popularity)

    # Bild-Beschreibung: zuerst gespeicherte Meta-Analyse nutzen, dann Vision-LLM
    image_description = None
    image_filename = post.get("image_filename", "")
    if image_filename:
        meta = load_image_meta(image_filename)
        if meta and meta.get("image_analysis"):
            image_description = meta["image_analysis"]
            logger.debug("Bildanalyse aus Meta: %s", image_description[:120])
        else:
            image_path = get_instagram_dir() / image_filename
            if image_path.exists():
                image_description = _analyze_image(str(image_path), poster_name)
                if image_description:
                    logger.debug("Bildanalyse (Vision-LLM): %s", image_description[:120])
                else:
                    logger.debug("Bildanalyse fehlgeschlagen, Fallback auf Caption")

    # Alle anderen Characters mit Instagram
    all_characters = list_available_characters()
    reactions = []
    likes = []

    for char_name in all_characters:
        if char_name == poster_name:
            continue

        char_ig_config = get_character_skill_config(char_name, "instagram")
        if not char_ig_config.get("enabled", False):
            continue

        # Zufalls-Roll gegen Popularitaet
        roll = random.randint(1, 100)
        if roll > popularity:
            logger.debug("  %s: nicht gesehen (roll=%d > %d)", char_name, roll, popularity)
            continue

        logger.debug("  %s: sieht den Post (roll=%d <= %d)", char_name, roll, popularity)

        # Like: hoehere Wahrscheinlichkeit als Kommentar (80%)
        like_roll = random.randint(1, 100)
        if like_roll <= 80:
            try:
                liked = add_character_like(post_id, char_name)
                if liked:
                    likes.append(char_name)
                    logger.debug("  -> %s liked den Post", char_name)

                    # Knowledge fuer POSTER
                    upsert_character_relationship(
                        character_name=poster_name,
                        related_character=char_name,
                        new_fact=f"{char_name} liked my Instagram post: \"{caption[:60]}\"",
                        replace_prefix=f"{char_name} liked my Instagram post:")
                    # Relationship Graph: Like = minimale positive Interaktion
                    try:
                        _record_rel(char_name, poster_name,
                                    "instagram_like",
                                    f"Liked {poster_name}'s post: {caption[:60]}",
                                    strength_delta=0.5, sentiment_delta_a=0.005)
                    except Exception:
                        pass
            except Exception as e:
                logger.error("  Like-Fehler bei %s: %s", char_name, e)

        # Kommentar: der Grossteil der Viewer bekommt einen Forced-Thought (85%)
        # — der LLM darf trotzdem SKIPpen, aber seltener (Prompt unten fordert
        # explizit zu einem Kommentar auf).
        comment_roll = random.randint(1, 100)
        if comment_roll > 85:
            logger.debug("  %s: liked aber kommentiert nicht (roll=%d > 85)", char_name, comment_roll)
            continue

        # Forcierter Gedanke: char_name sieht den Post + entscheidet ob er
        # kommentiert (via InstagramComment Tool).
        try:
            _img_part = f"\nBildbeschreibung: {image_description[:200]}" if image_description else ""
            # Popularitaet in Worte fassen — das LLM reagiert auf Semantik,
            # nicht auf nackte Zahlen. Bei hoher Popularitaet eher enthusiastisch,
            # bei niedriger eher zurueckhaltend.
            if popularity >= 80:
                _pop_line = f"{poster_name} ist ein extrem populaerer Creator — die Posts werden viel beachtet und kommentiert."
            elif popularity >= 60:
                _pop_line = f"{poster_name} ist ein sehr populaerer Creator mit aktiver Community."
            elif popularity >= 40:
                _pop_line = f"{poster_name} ist bekannt und hat eine treue Follower-Basis."
            elif popularity >= 20:
                _pop_line = f"{poster_name} hat ein kleines aber engagiertes Publikum."
            else:
                _pop_line = f"{poster_name} ist eher unbekannt — wenig Reichweite, aber authentisch."

            context_hint = (
                f"Du siehst einen neuen Instagram-Post, den {poster_name} "
                f"veroeffentlicht hat (post_id={post_id}).\n"
                f"{_pop_line}\n"
                f"Caption (geschrieben von {poster_name}): \"{caption[:200]}\"{_img_part}\n"
                f"\nWICHTIG: Du bist nur Betrachterin dieses Posts. "
                f"DU hast diesen Post NICHT erstellt, und die Caption ist NICHT deine.\n"
                f"\nSCHREIBE EINEN KURZEN KOMMENTAR (1-2 Saetze, in deiner "
                f"Persoenlichkeit) via Tool InstagramComment im Format "
                f"'post_id: dein Kommentar'. Kommentiere zum Bild, zur "
                f"Caption, zu {poster_name} oder zur Situation. Emojis und "
                f"lockerer Tonfall sind erlaubt.\n"
                f"Nur in Ausnahmefaellen SKIP (du bist voellig unbeteiligt "
                f"UND hast keine Meinung dazu). Default ist: Kommentieren."
            )
            get_background_queue().submit("forced_thought", {
                "user_id": "",
                "character_name": char_name,
                "context_hint": context_hint,
                # Kurzreaktion → Tool-LLM statt Chat-LLM (deutlich schneller).
                "fast": True,
                # Nur InstagramComment erlaubt — andere Skills haetten hier
                # keine sinnvolle Funktion und wuerden Rollenkonfusion oder
                # Halluzinationen beguenstigen (z.B. "Kira postet Lunas
                # Angebot nach").
                "tool_whitelist": ["InstagramComment"],
                # Narrativ-Text nicht als Chat/Notification speichern —
                # der Hint enthaelt fremden Content, der nicht als Gedanke
                # in die History leaken darf.
                "suppress_notification": True,
            })
            reactions.append({"character": char_name, "delegated": True})
            logger.debug("  -> forced_thought eingestellt fuer %s", char_name)
        except Exception as e:
            logger.error("  Forced-Thought Fehler bei %s: %s", char_name, e)

    logger.info("Fertig: %d Reaktionen, %d Likes", len(reactions), len(likes))

    # === Creator-Antworten: forcierter Gedanke fuer den Poster ===
    # Statt synchroner LLM-Calls fuer jeden Kommentar wird EIN Gedanke fuer den
    # Poster getriggert. Spaeter (nachdem Reaktoren tatsaechlich kommentiert
    # haben) prueft ein zweiter Pfad pro neuem Kommentar.
    # Hier nur Hint dass er sich gleich Kommentare anschauen sollte.

    logger.info("Fertig: %d Reactor-Gedanken eingestellt, %d Likes",
                len(reactions), len(likes))
    logger.info("=" * 50)

    return {
        "post_id": post_id,
        "poster": poster_name,
        "delegated_reactions": len(reactions),
        "likes": likes,
    }


def _analyze_image(image_path: str, poster_name: str) -> Optional[str]:
    """Analysiert ein Instagram-Bild via Vision-LLM und gibt eine Beschreibung zurueck."""
    from app.core.llm_router import resolve_llm
    from app.core.llm_queue import get_llm_queue, Priority

    if not os.path.exists(image_path):
        return None

    try:
        with open(image_path, 'rb') as f:
            image_bytes = f.read()
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
    except Exception as e:
        logger.error("Bild laden fehlgeschlagen: %s", e)
        return None

    instance = resolve_llm("image_recognition", agent_name=poster_name)
    if not instance:
        logger.error("Kein image_recognition LLM verfuegbar")
        return None

    llm = instance.create_llm(temperature=0.4, max_tokens=300)

    prompt_text = (
        f"Describe this Instagram photo by {poster_name} in detail. "
        f"Focus on: what is shown, the setting/location, mood, and any people visible. "
        f"Be factual and concise (2-4 sentences)."
    )

    image_url = f"data:image/png;base64,{base64_image}"
    message = {"role": "user", "content": [
        {"type": "text", "text": prompt_text},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]}

    # Vision-Calls verwenden messages-Struktur mit image_url-Parts —
    # deshalb hier direkt die Queue (llm_call unterstuetzt nur text-prompts).
    try:
        response = get_llm_queue().submit(
            task_type="image_recognition",
            priority=Priority.LOW,
            llm=llm,
            messages_or_prompt=[message],
            agent_name=poster_name)
        text = response.content.strip()
        return text if text else None
    except Exception as e:
        logger.error("Vision-LLM Fehler: %s", e)
        return None


def _handle_user_comment_reaction(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Background-Queue Handler: Character soll auf einen User-Kommentar reagieren.

    Triggert einen forcierten Gedanken fuer den Poster — der entscheidet via
    Tool-Call (InstagramReply) wie er antwortet.

    payload: user_id, character_name, post_id, commenter_name, comment_text, post
    """
    user_id = payload["user_id"]
    character_name = payload["agent_name"]
    post_id = payload["post_id"]
    commenter_name = payload["commenter_name"]
    comment_text = payload["comment_text"]
    post = payload.get("post", {})
    caption = post.get("caption", "")

    logger.info("User-Kommentar Trigger: %s kommentierte %s's Post: %s",
                commenter_name, character_name, comment_text[:80])

    image_description = ""
    image_filename = post.get("image_filename", "")
    if image_filename:
        from app.models.instagram import load_image_meta
        meta = load_image_meta(image_filename)
        image_description = meta.get("image_analysis", "") if meta else ""

    _img_part = f"\nBildbeschreibung: {image_description[:200]}" if image_description else ""
    context_hint = (
        f"{commenter_name} hat einen Kommentar zu DEINEM Instagram-Post "
        f"(post_id={post_id}) hinterlassen.\n"
        f"Deine Caption: \"{caption[:200]}\"{_img_part}\n"
        f"Kommentar von {commenter_name}: \"{comment_text}\"\n"
        f"\nFalls du antworten willst, rufe ausschliesslich das Tool "
        f"InstagramReply mit Format '{post_id} @{commenter_name}: dein Antworttext' auf."
    )

    try:
        get_background_queue().submit("forced_thought", {
            "user_id": "",
            "character_name": character_name,
            "context_hint": context_hint,
            # Kurzreaktion (InstagramReply) → Tool-LLM statt Chat-LLM.
            "fast": True,
            # Nur InstagramReply erlaubt — siehe Kommentar im
            # instagram_reaction Handler.
            "tool_whitelist": ["InstagramReply"],
            # Narrativ-Text nicht in Chat-History persistieren.
            "suppress_notification": True,
        })
        return {"success": True, "delegated_to": "forced_thought"}
    except Exception as e:
        logger.error("User-Kommentar Trigger fehlgeschlagen: %s", e)
        return {"error": str(e)}


def register_social_reaction_handler():
    """Registriert den Handler bei der BackgroundQueue. Beim Startup aufrufen."""
    bq = get_background_queue()
    bq.register_handler("instagram_reaction", _handle_instagram_reaction)
    bq.register_handler("instagram_user_comment_reaction", _handle_user_comment_reaction)


def trigger_social_reactions(poster_name: str, post: Dict[str, Any]):
    """Einstiegspunkt: Gibt einen instagram_reaction Task in die Queue.

    Wird nach create_post() aufgerufen.
    """
    enabled = os.environ.get("SOCIAL_REACTIONS_ENABLED", "true").lower()
    if enabled not in ("true", "1", "yes"):
        return

    bq = get_background_queue()
    bq.submit("instagram_reaction", {
        "user_id": "",
        "poster_name": poster_name,
        "post": post,
    })


def trigger_user_comment_reaction(character_name: str, post_id: str,
                                   commenter_name: str, comment_text: str,
                                   comment_id: str = "", post: dict = None):
    """Triggert Character-Reaktion auf einen User-Kommentar."""
    enabled = os.environ.get("SOCIAL_REACTIONS_ENABLED", "true").lower()
    if enabled not in ("true", "1", "yes"):
        return

    bq = get_background_queue()
    bq.submit("instagram_user_comment_reaction", {
        "user_id": "",
        "agent_name": character_name,
        "post_id": post_id,
        "commenter_name": commenter_name,
        "comment_text": comment_text,
        "comment_id": comment_id,
        "post": post or {},
    })
