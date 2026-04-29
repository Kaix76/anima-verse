"""Model-spezifische Prompt-Adapter.

Jeder Image-Backend (Z-Image, Qwen, Flux) erwartet einen anderen Prompt-Stil.
Diese Adapter rendern den strukturierten `PromptVariables`-Zustand deterministisch
in den jeweiligen Ziel-Stil. Optional kann ein LLM-Enhancer den Template-Output
ueberschreiben (gesteuert ueber die Workflow-Konfiguration, nicht pro Character).

Architektur:
    PromptVariables (strukturiert)
        ↓ render_<target_model>()
    Template-Prompt (deterministisch)
        ↓ maybe_enhance_via_llm() — optional, via Workflow-Config
    Finaler Prompt → Image-Backend
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from app.core.log import get_logger
from app.core.prompt_builder import PromptVariables

logger = get_logger("prompt_adapters")


# Gueltige Target-Model-Werte. Wird im Workflow-Config-Feld `image_model` gesetzt.
TARGET_MODELS = ("z_image", "qwen", "flux")

# Fallback-Mapping wenn `image_model` im Workflow-Config leer ist.
WORKFLOW_FILE_FALLBACK: Dict[str, str] = {
    "text2img_workflow_z-image_api.json": "z_image",
    "text2img_workflow_qwen_api.json": "qwen",
    "text2img_workflow_flux2_api.json": "flux",
}


def get_target_model(image_model: str = "", workflow_file: str = "", backend_model: str = "") -> str:
    """Bestimmt das Target-Model aus Workflow-Config mit Dateinamen-Fallback.

    Args:
        image_model: Wert aus ComfyWorkflow.image_model (Admin-UI).
        workflow_file: Dateiname des Workflows (fuer Fallback).
        backend_model: Modell-ID/URN des Backends (fuer Cloud-Backends ohne
            Workflow, z.B. "Qwen/Qwen-Image-2.0", "black-forest-labs/FLUX.2-pro",
            "urn:air:zimageturbo:checkpoint:civitai:..."). Wird als letzter
            Fallback aus dem Modellnamen abgeleitet.

    Returns:
        "z_image" | "qwen" | "flux". Default "z_image" wenn unbekannt.
    """
    normalized = (image_model or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in TARGET_MODELS:
        return normalized

    # Fallback: aus Dateiname ableiten
    if workflow_file:
        base = os.path.basename(workflow_file)
        if base in WORKFLOW_FILE_FALLBACK:
            return WORKFLOW_FILE_FALLBACK[base]
        lower = base.lower()
        if "qwen" in lower:
            return "qwen"
        if "flux" in lower:
            return "flux"
        if "z-image" in lower or "z_image" in lower:
            return "z_image"

    # Fallback: aus Backend-Modellname ableiten (Cloud-Backends ohne Workflow).
    # WICHTIG: Reihenfolge — z_image vor qwen pruefen, weil Z-Image-Modelle
    # auf CivitAI URN als "zimage..." erscheinen (NICHT als "qwen") und sonst
    # faelschlich als qwen klassifiziert werden koennten.
    if backend_model:
        bm = backend_model.lower()
        if "z_image" in bm or "z-image" in bm or "zimage" in bm:
            return "z_image"
        if "qwen" in bm and "image" in bm:
            # nur Qwen-Image-Modelle (nicht Qwen-Text-LLMs) als qwen-Style
            return "qwen"
        if "flux" in bm:
            return "flux"

    return "z_image"  # konservativer Default




def _person_with_outfit(pv: PromptVariables, idx: int, natural: bool) -> str:
    """Baut Personen-Beschreibung fuer einen Slot. `natural`=True liefert Satz."""
    person_txt = pv.prompt_persons.get(idx, "")
    outfit_txt = pv.prompt_outfits.get(idx, "")
    if not natural:
        parts = [p for p in (person_txt, outfit_txt) if p]
        return ", ".join(parts)

    # Natuerlich-sprachlich: Outfit als eigenen Satz
    if person_txt and outfit_txt:
        return f"{person_txt}. {outfit_txt}"
    return person_txt or outfit_txt


def dict_to_canonical(data: Dict[str, Any]) -> PromptVariables:
    """Rekonstruiert PromptVariables aus einem gespeicherten canonical-Dict.

    Inverse zu canonical_to_dict. Wird vom Re-Creation-Dialog genutzt, damit
    Bilder mit den ORIGINAL-Werten neu erzeugt werden koennen (nicht mit dem
    aktuellen Character-State).
    """
    from app.core.prompt_builder import Person  # lokaler Import gegen Zirkularitaet
    pv = PromptVariables()
    pv.persons = [
        Person(
            name=p.get("name", ""),
            appearance=p.get("appearance", ""),
            gender=p.get("gender", ""),
            actor_label=p.get("label", "") or p.get("name", ""),
            is_agent=bool(p.get("is_agent", False)),
            is_user=bool(p.get("is_user", False)))
        for p in (data.get("persons") or [])
    ]
    pv.prompt_persons = {int(k): v for k, v in (data.get("person_prompts") or {}).items()}
    pv.prompt_outfits = {int(k): v for k, v in (data.get("outfits") or {}).items()}
    pv.prompt_pose = data.get("pose", "") or ""
    pv.prompt_expression = data.get("expression", "") or ""
    pv.scene_prompt = data.get("scene", "") or ""
    pv.prompt_mood = data.get("mood", "") or ""
    pv.prompt_activity = data.get("activity", "") or ""
    pv.prompt_location = data.get("location", "") or ""
    pv.personality = data.get("personality_hint", "") or ""
    pv.profile_image_hint = data.get("profile_image_hint", "") or ""
    pv.prompt_style = data.get("style", "") or ""
    pv.negative_prompt = data.get("negative", "") or ""
    return pv


def canonical_to_dict(pv: PromptVariables) -> Dict[str, Any]:
    """Serialisiert PromptVariables in ein JSON-taugliches Dict fuer Metadaten."""
    return {
        "persons": [
            {
                "label": p.actor_label or p.name,
                "name": p.name,
                "appearance": p.appearance,
                "gender": p.gender,
                "is_agent": p.is_agent,
            }
            for p in pv.persons
        ],
        "person_prompts": dict(pv.prompt_persons),
        "outfits": dict(pv.prompt_outfits),
        "pose": pv.prompt_pose,
        "expression": pv.prompt_expression,
        "scene": pv.scene_prompt,
        "mood": pv.prompt_mood,
        "activity": pv.prompt_activity,
        "location": pv.prompt_location,
        "personality_hint": pv.personality,
        "profile_image_hint": pv.profile_image_hint,
        "style": pv.prompt_style,
        "negative": pv.negative_prompt,
    }


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

def render_z_image(pv: PromptVariables) -> Dict[str, str]:
    """Z-Image / SDXL-Stil: Komma-Keywords."""
    parts = []

    for idx in sorted(pv.prompt_persons.keys()):
        parts.append(_person_with_outfit(pv, idx, natural=False))

    if pv.prompt_pose:
        parts.append(pv.prompt_pose)
    if pv.prompt_expression:
        parts.append(pv.prompt_expression)
    if pv.scene_prompt:
        parts.append(pv.scene_prompt)
    if pv.prompt_mood:
        parts.append(pv.prompt_mood)
    if pv.prompt_activity:
        parts.append(pv.prompt_activity)
    _loc_clean = _clean_location_for_prompt(pv.prompt_location)
    if _loc_clean:
        parts.append(_loc_clean)
    if pv.personality:
        parts.append(pv.personality[:200])
    if pv.profile_image_hint:
        parts.append(pv.profile_image_hint)

    without_style = ", ".join(p for p in parts if p)
    style_full = pv.prompt_style.strip().rstrip(".").strip() or "photorealistic"
    positive = f"{style_full}, {without_style}" if without_style else style_full
    return {
        "input_prompt_positiv": positive,
        "input_prompt_negativ": pv.negative_prompt,
        "prompt_without_style": without_style,
    }


def _outfit_clause(outfit_txt: str) -> str:
    """Extrahiert den Outfit-Teil aus "Name is wearing X" → "wearing X"."""
    if not outfit_txt:
        return ""
    if " is wearing " in outfit_txt:
        _, desc = outfit_txt.split(" is wearing ", 1)
        return f"wearing {desc.strip()}"
    return outfit_txt


def _clean_location_for_prompt(loc: str) -> str:
    """Bereinigt Location-String fuer den Prompt:
    - Strippt Raum-/Location-Name-Praefix ("Hauptraum, A cozy living room...") → "A cozy living room..."
    - Strippt Klammer-Form ("Hauptraum (A cozy ...)") → "A cozy ..."
    - Entfernt "no people"/"no text"/"no person"/"no humans" am Ende
    - Entfernt fuehrendes "setting:"-Label (von alten Backfill-Werten)
    """
    if not loc:
        return ""
    import re as _re
    s = loc.strip()
    # Fuehrendes "setting:" Label entfernen (alte Backfill-Werte)
    s = _re.sub(r"^\s*setting\s*:\s*", "", s, flags=_re.IGNORECASE)
    # "Name, Description" — wenn Name kurz und Description deutlich laenger
    parts = s.split(",", 1)
    if len(parts) == 2:
        head, rest = parts[0].strip(), parts[1].strip()
        if len(head) <= 25 and len(rest) > 30:
            s = rest
    # "Name (Description)" → "Description"
    m = _re.match(r"^([^(]{1,25})\(\s*(.+?)\s*\)\s*$", s)
    if m:
        s = m.group(2)
    # "no people"/"no text"/"no person"/"no humans" am Ende entfernen
    s = _re.sub(r"[,\s]+no\s+(people|text|person|humans|one)\s*\.?\s*$", "", s, flags=_re.IGNORECASE)
    return s.strip(", .").strip()


def _render_labeled_sections(pv: PromptVariables) -> Dict[str, str]:
    """Gemeinsames Labeled-Sections-Format fuer Qwen und Flux.

    Aufbau:
        {Summary-Zeile}

        Characters:
        - {Name1}: {appearance}, wearing {outfit}.
        - {Name2}: {appearance}, wearing {outfit}.

        Action: {pose}, {expression}. {activity}. {scene}.
        Setting: {location}.
        Style: {prompt_style complete} [, {mood} atmosphere].
    """
    sections: list[str] = []

    # --- Style + Summary-Adjektiv aus pv.prompt_style ---
    # Das ganze Feld geht in die Style-Zeile. Fuer die Summary nehmen wir das
    # erste Komma-Token als Adjektiv (z.B. "photorealistic" aus
    # "photorealistic, 8k, cinematic lighting").
    style_full = pv.prompt_style.strip().rstrip(".").strip() or "photorealistic"
    style_word = style_full.split(",", 1)[0].strip() or "photorealistic"
    article = "An" if style_word[:1].lower() in "aeiou" else "A"

    n = len([p for p in pv.prompt_persons.values() if p])
    _num_words = {2: "two", 3: "three", 4: "four", 5: "five",
                  6: "six", 7: "seven", 8: "eight", 9: "nine"}
    if n == 0:
        summary = f"{article} {style_word} scene"
    elif n == 1:
        summary = f"{article} {style_word} photo of a person"
    else:
        count_word = _num_words.get(n, str(n))
        summary = f"{article} {style_word} group photo of {count_word} people"
    _loc_clean = _clean_location_for_prompt(pv.prompt_location)
    if _loc_clean:
        summary += f" in {_loc_clean}"
    sections.append(summary + ".")

    # --- Characters ---
    if pv.prompt_persons:
        char_lines = ["Characters:"]
        for idx in sorted(pv.prompt_persons.keys()):
            person_txt = pv.prompt_persons.get(idx, "").strip()
            outfit_clause = _outfit_clause(pv.prompt_outfits.get(idx, "").strip())
            if not person_txt:
                continue
            line = f"- {person_txt}"
            if outfit_clause:
                line += f", {outfit_clause}"
            if not line.endswith("."):
                line += "."
            char_lines.append(line)
        if len(char_lines) > 1:
            sections.append("\n".join(char_lines))

    # --- Action ---
    action_parts = []
    if pv.prompt_pose:
        action_parts.append(pv.prompt_pose)
    if pv.prompt_expression:
        action_parts.append(f"{pv.prompt_expression} expression")
    if pv.prompt_activity:
        action_parts.append(pv.prompt_activity)
    if pv.scene_prompt:
        action_parts.append(pv.scene_prompt)
    if action_parts:
        sections.append("Action: " + ", ".join(action_parts) + ".")

    # --- Mood (eigene Zeile, nicht in Style) ---
    if pv.prompt_mood:
        sections.append(f"Mood: {pv.prompt_mood}.")

    # --- Setting ---
    if _loc_clean:
        sections.append(f"Setting: {_loc_clean}.")

    # --- Style (komplett aus Config) ---
    sections.append(f"Style: {style_full}.")

    # --- Personality (optional) ---
    if pv.personality:
        sections.append(f"Body language: {pv.personality[:200]}.")
    if pv.profile_image_hint:
        sections.append(pv.profile_image_hint)

    full = "\n\n".join(sections)
    return {
        "input_prompt_positiv": full,
        "input_prompt_negativ": pv.negative_prompt,
        "prompt_without_style": full,
    }


def render_qwen(pv: PromptVariables) -> Dict[str, str]:
    """Qwen: Labeled-Sections-Format (strukturiert mit Characters/Action/Setting/Style)."""
    return _render_labeled_sections(pv)


def render_flux(pv: PromptVariables) -> Dict[str, str]:
    """Flux (2.x / 4B / 9B): Labeled-Sections-Format (identisch zu Qwen)."""
    return _render_labeled_sections(pv)


_RENDERERS = {
    "z_image": render_z_image,
    "qwen": render_qwen,
    "flux": render_flux,
}


def render(pv: PromptVariables, target_model: str) -> Dict[str, str]:
    """Dispatch an den passenden Adapter."""
    renderer = _RENDERERS.get(target_model, render_z_image)
    return renderer(pv)


# ---------------------------------------------------------------------------
# Optionaler LLM-Enhancer (zentral, nicht pro Character)
# ---------------------------------------------------------------------------

def maybe_enhance_via_llm(
    template_prompt: str,
    pv: PromptVariables,
    *,
    target_model: str,
    prompt_instruction: str = "") -> Tuple[str, str]:
    """Optionale LLM-Veredelung des Template-Prompts.

    Enhancer nur aktiv wenn:
      - `prompt_instruction` gesetzt (Workflow-Config, zentral)
      - globale LLM-Instance mit task="image_prompt" vorhanden

    Args:
        template_prompt: Ergebnis des deterministischen Adapters.
        pv: PromptVariables fuer Kontext.
        target_model: "z_image" | "qwen" | "flux".
        prompt_instruction: Freitext-Anweisung aus Workflow-Config.

    Returns:
        (final_prompt, method) wobei method in {"template", "template+llm"}.
    """
    if not prompt_instruction.strip():
        return template_prompt, "template"

    # System-Prompt-Template aus Admin-UI Config (mit Platzhaltern), Fallback auf Default
    _default_template = (
        "You are an image prompt enhancer for the {target_model} image model. "
        "{prompt_instruction} "
        "Rewrite the following prompt in the style requested, keeping ALL factual content "
        "(persons, outfits, pose, expression, scene, location, mood). "
        "Do NOT add new visual elements, do NOT remove any. "
        "Respond with ONLY the rewritten prompt, no preamble, no commentary."
    )
    template = os.environ.get("REBUILD_LLM_SYSTEM_TEMPLATE", "").strip() or _default_template
    try:
        system = template.format(
            target_model=target_model,
            prompt_instruction=prompt_instruction.strip())
    except (KeyError, IndexError, ValueError) as _fmt_err:
        # Bei kaputtem Template-Format Fallback auf Default
        logger.warning("REBUILD_LLM_SYSTEM_TEMPLATE Format-Fehler: %s — nutze Default", _fmt_err)
        system = _default_template.format(
            target_model=target_model,
            prompt_instruction=prompt_instruction.strip())

    # Diagnose-Log: welches Target-Modell wurde gewaehlt? Damit im Live-Log
    # sofort sichtbar ist, ob die Auswahl korrekt war (z.B. qwen fuer
    # Together-Qwen, flux fuer Together-Flux). Gleicher Wert landet auch im
    # System-Prompt-Template (Platzhalter {target_model}).
    logger.info("prompt_enhance: target_model=%s", target_model)
    try:
        from app.core.llm_router import llm_call
        response = llm_call(
            task="image_prompt",
            system_prompt=system,
            user_prompt=template_prompt)
        enhanced = (response.content or "").strip()
        if not enhanced:
            return template_prompt, "template"
        return enhanced, "template+llm"
    except RuntimeError as e:
        logger.debug("prompt_enhance: kein LLM fuer image_prompt verfuegbar (%s)", e)
        return template_prompt, "template"
    except Exception as e:
        logger.warning("prompt_enhance fehlgeschlagen, Template wird verwendet: %s", e)
        return template_prompt, "template"
