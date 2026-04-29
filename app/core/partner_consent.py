"""Partner-Consent fuer Aktivitaeten.

Wenn ein Character eine `requires_partner`-Activity startet, fragt der
Initiator den Partner via Chat-Engine (analog TalkTo) ob er mitmacht.
Partner-LLM antwortet natuerlich, die Antwort wird per Keyword-Heuristik
klassifiziert (Yes/No). Bei Yes laeuft der normale Auto-Transfer,
bei No bekommt der Initiator seine `fallback_activity`.

Keine LLM-Klassifikation — simple Wortliste, No schlaegt Yes.
"""
from typing import Tuple

from app.core.log import get_logger

logger = get_logger("partner_consent")


# Wortlisten fuer Klassifikation (DE + EN)
# No hat Prioritaet — ein "sorry" reicht zum Ablehnen.
_NO_WORDS = (
    "nein", "nee", "no", "nope",
    "keine lust", "nicht jetzt", "spaeter", "später",
    "sorry", "leider", "kann nicht", "geht nicht",
    "muss nicht", "lieber nicht", "lass mal", "ein andermal",
    "not now", "can't", "cannot", "won't", "not really")

_YES_WORDS = (
    "ja", "jawohl", "yes", "yeah", "yep", "sure",
    "gerne", "klar", "sicher", "okay", "ok",
    "komm", "lass uns", "let's", "alright", "auf jeden",
    "von mir aus", "warum nicht")


def _build_invitation_text(activity_def: dict) -> str:
    """Erzeugt den Einladungs-Text aus activity.invitation_text oder Fallback-Template."""
    custom = (activity_def.get("invitation_text") or "").strip()
    if custom:
        return custom
    label = (
        activity_def.get("name_de")
        or activity_def.get("name")
        or activity_def.get("id")
        or "etwas zusammen"
    )
    return f"Hast du Lust zu {label}?"


def _classify_response(text: str) -> bool:
    """True=accepted, False=declined.

    Regeln:
    - No-Woerter haben Prioritaet ueber Yes
    - Leere/unverstaendliche Antwort -> Yes (bestehende Semantik: Partner kommt mit)
    """
    t = (text or "").lower()
    if not t.strip():
        return True
    for w in _NO_WORDS:
        if w in t:
            return False
    for w in _YES_WORDS:
        if w in t:
            return True
    return True  # Ambiguitaet -> Yes


def ask_partner_to_join(initiator: str,
    partner: str,
    activity_def: dict) -> Tuple[bool, str]:
    """Fragt den Partner ob er bei der Activity mitmacht.

    Returns (accepted, reason_or_preview).
    - accepted=True: Partner sagt Ja (oder konservativer Default)
    - accepted=False: Partner lehnt ab / ist Player-Character / ist schlafend

    Effekte:
    - Chat-History-Eintrag (via run_chat_turn)
    - Beziehungs-Delta: +1 bei Ja, -1 bei Nein
    """
    # Partner ist Player-Character? -> Skip, Initiator bekommt fallback
    try:
        from app.models.account import get_active_character
        avatar = get_active_character() or ""
        if avatar and avatar == partner:
            logger.info("Partner %s ist Player-Character — Consent uebersprungen",
                        partner)
            return False, "player_character"
    except Exception:
        pass

    # Basis-Checks (hart, kein LLM)
    try:
        from app.models.character import is_character_sleeping
        if is_character_sleeping(partner):
            logger.info("Partner %s schlaeft — auto-declined", partner)
            return False, "partner_sleeping"
    except Exception:
        pass

    try:
        from app.core.activity_engine import is_character_interruptible
        can_interrupt, busy = is_character_interruptible(partner)
        if not can_interrupt:
            logger.info("Partner %s nicht unterbrechbar (%s) — auto-declined",
                        partner, busy)
            return False, f"partner_busy:{busy}"
    except Exception:
        pass

    invitation = _build_invitation_text(activity_def)

    # TalkTo-aequivalent: run_chat_turn direkt aufrufen, damit der Partner
    # natuerlich antwortet und die Konversation in der Chat-History landet.
    try:
        from app.core.chat_engine import run_chat_turn
        reply = run_chat_turn(
            owner_id="",
            responder=partner,
            speaker=initiator,
            incoming_message=invitation,
            medium="in_person",
            task_type="consent_ask")
    except Exception as e:
        logger.warning("Consent-Ask fehlgeschlagen (%s) — default Yes", e)
        return True, ""

    if not reply:
        logger.info("Partner %s antwortet nicht — default Yes", partner)
        return True, ""

    accepted = _classify_response(reply)
    preview = reply.strip()[:120]

    # Beziehungs-Delta
    try:
        from app.models.relationship import record_interaction
        act_label = (
            activity_def.get("name_de")
            or activity_def.get("name")
            or activity_def.get("id", "?")
        )
        if accepted:
            record_interaction(initiator, partner, "activity_consent",
                summary=f"{partner} stimmte zu: {act_label}",
                strength_delta=1.0)
        else:
            record_interaction(initiator, partner, "activity_decline",
                summary=f"{partner} lehnte ab: {act_label}",
                strength_delta=-1.0)
    except Exception as e:
        logger.debug("record_interaction failed: %s", e)

    logger.info(
        "Partner %s %s auf '%s' (preview: %s)",
        partner,
        "akzeptierte" if accepted else "lehnte ab",
        activity_def.get("name", "?"),
        preview[:60])
    return accepted, preview
