"""Zentrale Prompt-Builder Pipeline fuer die Bildgenerierung.

Konsolidiert die bisher ueber chat.py, image_generation_skill.py und
image_regenerate.py verteilte Prompt-Logik in eine einzelne Pipeline.

Ablauf:
    1. detect_persons()        - Personen erkennen, neutrale Akteure zuweisen
    2. collect_context()       - Kontext-Variablen pro Akteur sammeln
    3. apply_exclusion_rules() - Workflow-abhaengige Ausschlussregeln
    4. assemble_prompt()       - Finalen Prompt zusammenbauen
    5. resolve_reference_slots() - Reference-Image Slots befuellen
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.log import get_logger
from app.models.character import (
    get_character_appearance,
    get_character_config,
    get_character_current_activity,
    get_character_current_feeling,
    get_character_current_location,
    get_character_current_room,
    get_character_images_dir,
    get_character_personality,
    get_character_profile,
    get_character_profile_image,
    build_equipped_outfit_prompt,
    list_available_characters)
from app.models.character_template import resolve_profile_tokens, get_template
from app.models.account import (
    get_active_character,
    get_user_appearance,
    get_user_gender,
    get_user_profile,
    get_user_profile_image,
    get_user_images_dir)
from app.models.world import get_activity, get_background_path, get_location, get_room_by_id

logger = get_logger("prompt_builder")


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------

@dataclass
class Person:
    """Eine erkannte Person."""
    name: str                   # Character-Name (z.B. "Kira", "Kai")
    appearance: str             # Appearance-Text aus Profil
    gender: str                 # "female", "male", ""
    actor_label: str = ""       # Wird auf name gesetzt (ehemals Woman1/Man1)
    is_agent: bool = False      # Ist der erzeugende Character
    is_user: bool = False       # Ist der User


@dataclass
class PromptVariables:
    """Alle separaten Prompt-Variablen fuer die Pipeline."""
    # Pro Person (Index = Slot 1-based)
    persons: List[Person] = field(default_factory=list)
    prompt_persons: Dict[int, str] = field(default_factory=dict)    # {1: "Woman1, blonde hair..."}
    prompt_outfits: Dict[int, str] = field(default_factory=dict)    # {1: "Woman1 is wearing red dress"}
    ref_images: Dict[int, str] = field(default_factory=dict)        # {1: "/path/to/image.png"}

    # Items als Props (markiert im Room-Items Panel)
    # items: [{"id": "...", "name": "chair", "description": "...", "image": "/path"}]
    items: List[Dict[str, Any]] = field(default_factory=list)

    # Globale Variablen
    prompt_mood: str = ""
    prompt_activity: str = ""
    prompt_location: str = ""
    ref_image_room: str = ""        # Slot 4 = Raum

    # Interne Variablen fuer Separated-Prompt-Workflows
    prompt_pose: str = ""
    prompt_expression: str = ""

    # LLM-generierter Szenen-Prompt
    scene_prompt: str = ""

    # Zusaetze
    prompt_style: str = ""           # Stil-Adjektiv (z.B. "photorealistic", "anime illustration")
    negative_prompt: str = ""
    personality: str = ""           # Nur bei Profilbild
    profile_image_hint: str = ""    # Zusatz "Profilbild"

    # Kontext-Flags
    photographer_mode: bool = False
    is_selfie: bool = False
    set_profile: bool = False
    no_person_detected: bool = False


@dataclass
class EntryPointConfig:
    """Konfiguration pro Entry-Point: welche Variablen werden befuellt."""
    name: str
    auto_enhance: bool = True
    include_persons: bool = True
    include_outfit: bool = True
    include_mood: bool = True
    include_activity: bool = True
    include_location: bool = True
    show_dialog: bool = False

    @classmethod
    def chat(cls) -> "EntryPointConfig":
        return cls(name="chat")

    @classmethod
    def instagram(cls) -> "EntryPointConfig":
        return cls(name="instagram")

    @classmethod
    def story(cls) -> "EntryPointConfig":
        return cls(name="story", auto_enhance=False, include_mood=False,
                   include_activity=False, include_outfit=False)

    @classmethod
    def telegram(cls) -> "EntryPointConfig":
        return cls(name="telegram")

    @classmethod
    def outfit_variant(cls) -> "EntryPointConfig":
        return cls(name="outfit_variant", include_location=False)

    @classmethod
    def outfit_change(cls) -> "EntryPointConfig":
        return cls(name="outfit_change")

    @classmethod
    def dialog_recreation(cls) -> "EntryPointConfig":
        return cls(name="dialog_recreation", show_dialog=True)

    @classmethod
    def dialog_outfit(cls) -> "EntryPointConfig":
        return cls(name="dialog_outfit", show_dialog=True,
                   include_mood=False, include_activity=False, include_location=False)

    @classmethod
    def dialog_location(cls) -> "EntryPointConfig":
        return cls(name="dialog_location", show_dialog=True,
                   include_persons=False, include_outfit=False,
                   include_mood=False, include_activity=False)

    @classmethod
    def dialog_profile(cls) -> "EntryPointConfig":
        return cls(name="dialog_profile", show_dialog=True,
                   include_outfit=False, include_mood=False,
                   include_activity=False, include_location=False)


# ---------------------------------------------------------------------------
# Pronomen-Listen
# ---------------------------------------------------------------------------

_AGENT_PRONOUNS = [
    " ich ", " mir ", " mich ", " mein ", " meinem ", " meiner ",
    " meinen ", " meine ", " i am ", " my ", " me ", " myself ",
]

_USER_PRONOUNS = [
    " du ", " dir ", " dich ", " dein ", " deinem ", " deiner ",
    " deinen ", " deine ", " you ", " your ",
]

SELFIE_KEYWORDS = (
    "selfie", "selbstportrait", "self-portrait", "self portrait",
    "foto von mir", "photo of me", "bild von mir", "picture of me")
_SELFIE_KEYWORDS = SELFIE_KEYWORDS  # Abwaertskompatibel


def is_photographer_mode(character_name: str) -> bool:
    """Liest photographer_mode aus Character-Config, tolerant gegen string/bool."""
    if not character_name:
        return False
    cfg = get_character_config(character_name) or {}
    raw = cfg.get("photographer_mode", False)
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes")
    return bool(raw)


def detect_selfie(text: str) -> bool:
    """True wenn Text ein explizites Selfie-Wort enthaelt."""
    if not text:
        return False
    return any(kw in text.lower() for kw in SELFIE_KEYWORDS)

_CAMERA_KEYWORDS = (
    "camera", "photograph", "photo shoot", "taking photo", "taking a photo",
    "taking picture", "snap a photo", "lens", "shutter", "viewfinder",
    "tripod", "flash", "canon", "nikon", "sony alpha", "ready for shot",
    "holding camera", "adjusting light", "behind the camera", "shoots",
    "kamera", "fotografier", "foto mach", "objektiv", "ausloeser")

_COMMON_WORDS = frozenset({
    "with", "from", "that", "this", "have", "been", "were", "they",
    "their", "them", "will", "would", "could", "should", "about",
    "into", "over", "after", "before", "between", "under", "through",
    "each", "other", "some", "than", "then", "also", "just", "like",
    "very", "much", "more", "most", "long", "young", "looks", "looking",
    "eine", "einen", "einem", "einer", "eine", "sind", "sein", "wird",
    "haben", "kann", "auch", "noch", "oder", "aber", "wenn", "sich",
    "nicht", "mehr", "nach", "schon", "noch",
})

_PERSON_KEYWORDS = (
    "woman", "man", "girl", "boy", "person", "people", "frau", "mann",
    "mädchen", "junge", "portrait", "face", "gesicht", "selfie",
    "intern", "she ", "he ", "her ", "his ")


# ---------------------------------------------------------------------------
# PromptBuilder
# ---------------------------------------------------------------------------

class PromptBuilder:
    """Zentrale Pipeline fuer den Aufbau von Image-Generation Prompts."""

    def __init__(self, character_name: str):
        self.user_id = ""
        self.character_name = character_name

    # ------------------------------------------------------------------
    # Schritt 1: Personen erkennen
    # ------------------------------------------------------------------

    def detect_persons(
        self,
        text: str,
        *,
        character_names: Optional[List[str]] = None,
        explicit_appearances: Optional[List[Dict[str, str]]] = None) -> List[Person]:
        """Erkennt Personen im Text und weist neutrale Akteur-Labels zu.

        Args:
            text: Prompt-Text oder Chat-Text.
            character_names: Explizite Namensliste (ueberspringt Auto-Detection).
            explicit_appearances: Bereits aufgeloeste Appearances (ueberspringt alles).

        Returns:
            Liste von Person-Objekten mit neutralen Akteur-Labels.
        """
        if explicit_appearances is not None:
            persons = self._persons_from_appearances(explicit_appearances)
        elif character_names is not None:
            persons = self._resolve_by_names(character_names)
        else:
            persons = self._auto_detect(text)

        self._assign_actor_labels(persons)
        return persons

    def _avatar_name(self) -> str:
        """Name des User-Avatars (aktiver Character, sonst Login-Name)."""
        return (
            get_active_character()
            or get_user_profile().get("user_name", "")
        )

    def _is_avatar_name(self, name: str) -> bool:
        """True wenn name den User-Avatar referenziert (active_character ODER Login-Name)."""
        if not name:
            return False
        active = get_active_character()
        login = get_user_profile().get("user_name", "")
        return name == active or (bool(login) and name == login)

    def _persons_from_appearances(self, appearances: List[Dict[str, str]]) -> List[Person]:
        """Konvertiert bestehende Appearance-Dicts in Person-Objekte."""
        persons = []
        avatar_name = self._avatar_name()

        for app in appearances:
            name = app.get("name", "")
            if not name:
                continue
            if self._is_avatar_name(name):
                gender = get_user_gender().strip().lower()
                persons.append(Person(
                    name=avatar_name, appearance=app.get("appearance", ""),
                    gender=gender, is_user=True))
            else:
                profile = get_character_profile(name)
                gender = profile.get("gender", "").strip().lower()
                persons.append(Person(
                    name=name, appearance=app.get("appearance", ""),
                    gender=gender, is_agent=(name == self.character_name)))
        return persons

    def _resolve_by_names(self, names: List[str]) -> List[Person]:
        """Loest Appearances fuer eine explizite Namensliste auf."""
        persons = []
        avatar_name = self._avatar_name()

        for name in names:
            if self._is_avatar_name(name):
                appearance = get_user_appearance() or ""
                gender = get_user_gender().strip().lower()
                persons.append(Person(
                    name=avatar_name, appearance=appearance,
                    gender=gender, is_user=True))
            else:
                appearance = self._resolve_character_appearance(name)
                profile = get_character_profile(name)
                gender = profile.get("gender", "").strip().lower()
                if appearance:
                    persons.append(Person(
                        name=name, appearance=appearance,
                        gender=gender, is_agent=(name == self.character_name)))
        return persons

    def _auto_detect(self, text: str) -> List[Person]:
        """Erkennt Personen automatisch aus Prompt-Text (Pronomen + Namen)."""
        padded = f" {text.lower()} "
        persons = []

        # 1. Agent: Name oder Ich-Pronomen
        agent_mentioned = (
            bool(re.search(r'\b' + re.escape(self.character_name.lower()) + r'\b', padded))
            or any(p in padded for p in _AGENT_PRONOUNS)
        )
        if agent_mentioned:
            appearance = self._resolve_character_appearance(self.character_name)
            if appearance:
                profile = get_character_profile(self.character_name)
                gender = profile.get("gender", "").strip().lower()
                persons.append(Person(
                    name=self.character_name, appearance=appearance,
                    gender=gender, is_agent=True))

        # 2. User-Avatar: Avatar-Name, Login-Name oder Du-Pronomen
        avatar_name = self._avatar_name()
        login_name = get_user_profile().get("user_name", "")
        if avatar_name:
            name_candidates = {avatar_name.lower()}
            if login_name:
                name_candidates.add(login_name.lower())
            user_mentioned = (
                any(re.search(r'\b' + re.escape(n) + r'\b', padded) for n in name_candidates)
                or any(p in padded for p in _USER_PRONOUNS)
            )
            if user_mentioned:
                user_app = get_user_appearance()
                if user_app:
                    gender = get_user_gender().strip().lower()
                    persons.append(Person(
                        name=avatar_name, appearance=user_app,
                        gender=gender, is_user=True))

        # 3. Andere Characters nach Name (Duplikat-Check: Agent + User ueberspringen)
        # Location-Strict: andere Characters duerfen nur ins Bild wenn sie am
        # gleichen Ort wie der Agent sind. Schliesst aus dass Tool-LLM zufaellig
        # Namen erwaehnt und der Code Charaktere ins Bild zieht die woanders
        # sind ("Bianca trifft Diego" obwohl Diego schlaefft).
        already_added = {p.name for p in persons}
        all_chars = list_available_characters()
        _agent_loc = (get_character_current_location(self.character_name) or "").strip()
        for char_name in all_chars:
            if char_name == self.character_name or char_name in already_added:
                continue
            if re.search(r'\b' + re.escape(char_name.lower()) + r'\b', padded):
                # Location-Check (strict): anderer Ort -> nicht ins Bild
                if _agent_loc:
                    _other_loc = (get_character_current_location(char_name) or "").strip()
                    if _other_loc and _other_loc != _agent_loc:
                        logger.info(
                            "Person '%s' im Text erwaehnt aber NICHT ergaenzt — "
                            "anderer Ort (Agent=%s, %s=%s)",
                            char_name, _agent_loc, char_name, _other_loc)
                        continue
                appearance = self._resolve_character_appearance(char_name)
                if appearance:
                    profile = get_character_profile(char_name)
                    gender = profile.get("gender", "").strip().lower()
                    persons.append(Person(
                        name=char_name, appearance=appearance,
                        gender=gender))

        # 4. Fallback: Wenn NIEMAND erkannt wurde und ein Agent aktiv ist,
        # den Agent als Default hinzufuegen (z.B. Group Chat Tool-Call mit
        # unspezifischem Prompt "a beautiful sunset"). Ohne Fallback fehlen
        # Appearance + Referenzbild komplett.
        if not persons and self.character_name:
            appearance = self._resolve_character_appearance(self.character_name)
            if appearance:
                profile = get_character_profile(self.character_name)
                gender = profile.get("gender", "").strip().lower()
                persons.append(Person(
                    name=self.character_name, appearance=appearance,
                    gender=gender, is_agent=True))
                logger.debug("Auto-Detect: Keine Personen erkannt — Agent '%s' als Default", self.character_name)

        return persons

    def _resolve_character_appearance(self, name: str) -> str:
        """Loest Appearance-Text mit Template-Token-Ersetzung auf."""
        appearance = get_character_appearance(name)
        if appearance and "{" in appearance:
            profile = get_character_profile(name)
            tmpl = get_template(profile.get("template", "")) if profile.get("template") else None
            appearance = resolve_profile_tokens(
                appearance, profile, template=tmpl, target_key="character_appearance"
            )
        return appearance or ""

    def _assign_actor_labels(self, persons: List[Person]) -> None:
        """Setzt actor_label auf den echten Character-Namen."""
        for person in persons:
            person.actor_label = person.name

    # ------------------------------------------------------------------
    # Schritt 1b: Photographer-Filter (zentral, nach detect_persons aufrufen)
    # ------------------------------------------------------------------

    def apply_photographer_filter(
        self,
        persons: List[Person],
        *,
        photographer_mode: bool,
        is_selfie: bool,
        set_profile: bool) -> List[Person]:
        """Entfernt den Agent aus persons, wenn er Photograph (nicht Subject) ist.

        Regeln (siehe done/image-creation.md 4.2.2 + plan-image-creation-redesign.md 5):

        - photographer_mode=False  -> persons unveraendert
        - is_selfie=True           -> Filter aus, Agent bleibt drin (Self-Portrait)
        - set_profile=True         -> Filter aus (Profilbild des Agents)
        - photographer_mode + Agent erkannt + kein anderes Subject
                                   -> User-Avatar als Default-Subject einfuegen
        - photographer_mode + Agent NICHT erkannt + keine Subjects
                                   -> persons leer lassen (Stranger-Photo)

        Idempotent — mehrfacher Aufruf hat keinen Effekt.
        """
        if not photographer_mode or is_selfie or set_profile:
            return persons

        agent_was_detected = any(p.is_agent for p in persons)
        filtered = [p for p in persons if not p.is_agent]

        if agent_was_detected and not filtered:
            user = self._create_user_person()
            if user:
                filtered.append(user)
                logger.info(
                    "Photographer-Filter: Agent erkannt, kein Subject "
                    "-> User-Avatar '%s' als Default", user.name)

        if filtered != persons:
            logger.info(
                "Photographer-Filter: Agent '%s' entfernt, Subjects=%s",
                self.character_name, [p.name for p in filtered])

        return filtered

    # ------------------------------------------------------------------
    # Schritt 2: Kontext-Variablen sammeln
    # ------------------------------------------------------------------

    def collect_context(
        self,
        persons: List[Person],
        config: EntryPointConfig,
        *,
        prompt_text: str = "",
        photographer_mode: bool = False,
        set_profile: bool = False,
        item_ids: Optional[List[str]] = None) -> PromptVariables:
        """Sammelt alle Kontext-Variablen fuer die erkannten Personen.

        Args:
            persons: Erkannte Personen aus detect_persons().
            config: Entry-Point-Konfiguration.
            prompt_text: Original-Prompt (fuer Selfie-Erkennung).
            photographer_mode: Photographer-Modus aktiv.
            set_profile: Profilbild-Modus.
            item_ids: Item-IDs die als Props ins Bild sollen (freie Ref-Slots).
        """
        variables = PromptVariables()
        variables.persons = persons
        variables.photographer_mode = photographer_mode
        variables.set_profile = set_profile
        variables.is_selfie = any(kw in prompt_text.lower() for kw in _SELFIE_KEYWORDS)
        # Items fruehzeitig sammeln — spaetere Schritte sehen sie via variables.items
        if item_ids:
            variables.items = self._collect_items(item_ids)

        # Photographer-Filter wird jetzt vor collect_context aufgerufen
        # (siehe apply_photographer_filter()). Hier nur noch der Stranger-Photo-
        # Fallback fuer leere Personen-Listen.
        agent_person = next((p for p in persons if p.is_agent), None)

        # Keine Person erkannt?
        if not persons:
            if config.include_persons and any(kw in prompt_text.lower() for kw in _PERSON_KEYWORDS):
                variables.no_person_detected = False
                if not photographer_mode:
                    # Non-Photographer Fallback: Agent als Default nehmen
                    agent_person = self._create_agent_person()
                    if agent_person:
                        persons = [agent_person]
                        variables.persons = persons
                        logger.info("Fallback: Person-Keywords erkannt -> Agent '%s' hinzugefuegt",
                                    self.character_name)
                else:
                    # Photographer-Modus: Der Fotograf ist nicht im Bild.
                    # Der Prompt beschreibt eine Person (Person-Keyword
                    # erkannt), aber kein bekannter Character ist namentlich
                    # genannt — also FIKTIVE Person (Stranger-Photo).
                    # persons leer lassen, kein User-Avatar zwingen, kein
                    # FaceSwap. Renderer generiert die Person aus dem Prompt.
                    logger.info("Photographer-Stranger: keine bekannte Person — "
                                "fiktives Motiv, kein FaceSwap")
            else:
                variables.no_person_detected = True
                logger.debug("Keine Person erkannt -> reines Ort-/Raum-Bild")

        # Agent sicherstellen (fuer set_profile oder wenn andere Personen da sind)
        if config.include_persons and not any(p.is_agent for p in persons):
            if set_profile or (persons and not photographer_mode):
                agent_person = self._create_agent_person()
                if agent_person:
                    persons.insert(0, agent_person)
                    variables.persons = persons
                    logger.info("Agent '%s' automatisch hinzugefuegt", self.character_name)

        # Personen-Variablen befuellen
        if config.include_persons:
            for idx, person in enumerate(persons, 1):
                variables.prompt_persons[idx] = f"{person.actor_label}, {person.appearance}"

        # Outfit + Reference-Bilder pro Person.
        # Bei set_profile (Profilbild-Erstellung) kein Outfit-Text und keine
        # Reference-Bilder — sonst Self-Reference-Loop, und der Profilbild-
        # Workflow braucht eh nur Kopf + Gesichtsbeschreibung.
        if config.include_outfit and not set_profile:
            self._collect_outfits(persons, variables, photographer_mode)

        # Stimmung
        if config.include_mood and persons:
            self._collect_mood(persons, variables, photographer_mode)

        # Aktivitaet
        if config.include_activity and persons:
            self._collect_activity(persons, variables, photographer_mode)

        # Location
        if config.include_location:
            self._collect_location(variables)

        # Personality (nur bei Profilbild)
        if set_profile:
            personality = get_character_personality(self.character_name) or ""
            variables.personality = personality

        self._log_variables(variables)
        return variables

    def _create_agent_person(self) -> Optional[Person]:
        """Erstellt ein Person-Objekt fuer den Agent."""
        appearance = self._resolve_character_appearance(self.character_name)
        if not appearance:
            return None
        profile = get_character_profile(self.character_name)
        gender = profile.get("gender", "").strip().lower()
        person = Person(
            name=self.character_name, appearance=appearance,
            gender=gender, is_agent=True)
        # Label zuweisen
        self._assign_actor_labels([person])
        return person

    def _create_user_person(self) -> Optional[Person]:
        """Erstellt ein Person-Objekt fuer den User-Avatar (aktiver Character)."""
        avatar_name = self._avatar_name()
        if not avatar_name:
            return None
        appearance = get_user_appearance()
        if not appearance:
            return None
        gender = get_user_gender().strip().lower()
        person = Person(
            name=avatar_name, appearance=appearance,
            gender=gender, is_user=True)
        self._assign_actor_labels([person])
        return person

    def _collect_items(self, item_ids: List[str]) -> List[Dict[str, Any]]:
        """Laedt Item-Details (name, description, image_path) fuer die angegebenen IDs.

        Liefert nur Items die ein existierendes PNG haben — andere koennen zwar
        im Prompt-Text auftauchen, werden aber nicht als Ref-Slot belegt.
        """
        from app.models.inventory import get_item, get_item_image_path
        out: List[Dict[str, Any]] = []
        for iid in item_ids or []:
            it = get_item(iid)
            if not it:
                continue
            img_path = ""
            try:
                p = get_item_image_path(iid)
                if p and Path(p).exists():
                    img_path = str(p)
            except Exception:
                img_path = ""
            out.append({
                "id": iid,
                "name": it.get("name", iid),
                "description": (it.get("description") or "").strip(),
                "image": img_path,
            })
        logger.debug("Items gesammelt: %d von %d (mit Bild: %d)",
                     len(out), len(item_ids or []),
                     sum(1 for x in out if x["image"]))
        return out

    def _collect_outfits(
        self, persons: List[Person], variables: PromptVariables,
        photographer_mode: bool) -> None:
        """Sammelt Outfit-Prompts und Reference-Bilder pro Person."""
        for idx, person in enumerate(persons, 1):
            outfit_text = ""
            ref_path = ""

            if person.is_user:
                # User-Avatar: gleiche Outfit-Kette wie Character.
                active_char = get_active_character() or person.name
                if active_char:
                    outfit_text = build_equipped_outfit_prompt(active_char) or ""
                    ref_path = self._resolve_person_ref_image(
                        Person(name=active_char, appearance=person.appearance,
                               gender=person.gender, is_agent=False)
                    )
                if not ref_path:
                    profile_img = get_user_profile_image()
                    if profile_img:
                        images_dir = get_user_images_dir()
                        candidate = images_dir / profile_img
                        if candidate.exists():
                            ref_path = str(candidate)
            else:
                outfit_text = build_equipped_outfit_prompt(person.name) or ""
                ref_path = self._resolve_person_ref_image(person)

            if outfit_text:
                # build_equipped_outfit_prompt liefert "wearing: <fragments>"
                # — den Prefix strippen damit nicht "is wearing wearing: ..."
                # entsteht.
                _ot = outfit_text.lstrip()
                if _ot.lower().startswith("wearing: "):
                    outfit_text = _ot[len("wearing: "):]
                elif _ot.lower().startswith("wearing:"):
                    outfit_text = _ot[len("wearing:"):].lstrip()
                variables.prompt_outfits[idx] = f"{person.actor_label} is wearing {outfit_text}"
            if ref_path:
                variables.ref_images[idx] = ref_path

    def _resolve_person_ref_image(self, person: Person, profile_only: bool = False) -> str:
        """Loest das beste Reference-Bild fuer eine Person auf (Fallback-Kette).

        Args:
            person: Person-Objekt.
            profile_only: True = nur Profilbild (z.B. Outfit-Erstellung).
        """
        # User: Profilbild
        if person.is_user:
            profile_img = get_user_profile_image()
            if profile_img:
                images_dir = get_user_images_dir()
                candidate = images_dir / profile_img
                if candidate.exists():
                    logger.debug("RefImage [%s]: User-Profilbild", person.name)
                    return str(candidate)
            return ""

        # profile_only: direkt zum Profilbild springen (z.B. Outfit-Erstellung)
        if profile_only:
            profile_img = get_character_profile_image(person.name)
            if profile_img:
                images_dir = get_character_images_dir(person.name)
                candidate = images_dir / profile_img
                if candidate.exists():
                    logger.debug("RefImage [%s]: Profilbild (profile_only)", person.name)
                    return str(candidate)
            return ""

        # Character: Prio 1: Expression-Variante (Equipped + Mood + Activity)
        try:
            from app.core.expression_regen import get_cached_expression
            from app.models.inventory import get_equipped_pieces, get_equipped_items
            mood = get_character_current_feeling(person.name) or ""
            activity = get_character_current_activity(person.name) or ""
            try:
                eq_p = get_equipped_pieces(person.name)
                eq_i = get_equipped_items(person.name)
            except Exception:
                eq_p, eq_i = None, None
            cached = get_cached_expression(person.name, mood, activity,
                equipped_pieces=eq_p, equipped_items=eq_i)
            if cached and cached.exists():
                logger.debug("RefImage [%s]: Expression-Variante", person.name)
                return str(cached)
        except (ImportError, Exception):
            pass

        # Prio 2: Profilbild
        profile_img = get_character_profile_image(person.name)
        if profile_img:
            images_dir = get_character_images_dir(person.name)
            candidate = images_dir / profile_img
            if candidate.exists():
                logger.debug("RefImage [%s]: Profilbild", person.name)
                return str(candidate)

        return ""

    def _collect_mood(
        self, persons: List[Person], variables: PromptVariables,
        photographer_mode: bool) -> None:
        """Sammelt Stimmung pro Person."""
        # Mood-Tracking muss aktiviert sein
        char_config = get_character_config(self.character_name) if self.character_name else {}
        if not char_config.get("mood_tracking", False):
            return

        mood_parts = []
        for person in persons:
            if person.is_user:
                continue  # User hat kein Mood-Tracking
            feeling = get_character_current_feeling(person.name) or ""
            if feeling:
                mood_parts.append(f"{person.actor_label} looks {feeling}")

        if mood_parts:
            variables.prompt_mood = ", ".join(mood_parts)

    def _collect_activity(
        self, persons: List[Person], variables: PromptVariables,
        photographer_mode: bool) -> None:
        """Sammelt Aktivitaet pro Person."""
        activity_parts = []
        for person in persons:
            if person.is_user:
                continue
            raw = get_character_current_activity(person.name)
            if raw:
                from app.models.activity_library import find_library_activity_by_name, get_library_activity, get_localized_field
                from app.models.character import get_character_language
                _plang = get_character_language(person.name)
                act_data = get_library_activity(raw) or find_library_activity_by_name(raw) or get_activity(raw)
                desc = get_localized_field(act_data, "description", _plang) if act_data else ""
                if desc:
                    activity_parts.append(f"{person.actor_label} is {desc}")
                else:
                    activity_parts.append(f"{person.actor_label} is {raw}")

        if activity_parts:
            variables.prompt_activity = ", ".join(activity_parts)

    def _collect_location(self, variables: PromptVariables) -> None:
        """Sammelt Location/Room-Daten."""
        raw_location = get_character_current_location(self.character_name)
        if not raw_location:
            return

        loc_data = get_location(raw_location)
        current_room_id = get_character_current_room(self.character_name)
        hour = datetime.now().hour

        location_name = ""
        location_desc = ""

        def _safe_desc_fallback(desc: str) -> str:
            """description darf nur als Fallback dienen wenn sie wie eine
            Bild-Beschreibung aussieht — keine First-Person-Narrative
            oder Chat-Monologe (das passiert wenn ein Tool-Call sie aus
            Versehen ueberschreibt)."""
            if not desc:
                return ""
            d = desc.strip().lower()
            # First-Person-Marker: Indizien fuer Chat-Monolog statt Raum-Desc
            _bad_starts = ("mein blick", "ich ", "ich.", "i feel", "i am",
                           "i'm ", "my ", "we ", "wir ", "sollte ich",
                           "should i ", "let me ", "lass mich")
            if any(d.startswith(s) for s in _bad_starts):
                logger.warning(
                    "Location-description verworfen (First-Person-Marker): %s",
                    desc[:80])
                return ""
            return desc

        # Raum-Prompt hat Prioritaet
        if current_room_id and loc_data:
            room_data = get_room_by_id(loc_data, current_room_id)
            if room_data:
                location_name = room_data.get("name", "")
                if 6 <= hour < 18:
                    location_desc = (room_data.get("image_prompt_day", "")
                                     or _safe_desc_fallback(room_data.get("description", "")))
                else:
                    location_desc = (room_data.get("image_prompt_night", "")
                                     or _safe_desc_fallback(room_data.get("description", "")))

        if not location_name:
            # Fallback: Location-Level
            location_name = loc_data.get("name", raw_location) if loc_data else raw_location
            if loc_data:
                if 6 <= hour < 18:
                    location_desc = (loc_data.get("image_prompt_day", "")
                                     or _safe_desc_fallback(loc_data.get("description", "")))
                else:
                    location_desc = (loc_data.get("image_prompt_night", "")
                                     or _safe_desc_fallback(loc_data.get("description", "")))

        if location_desc:
            variables.prompt_location = f"{location_name}, {location_desc}"
        elif location_name:
            variables.prompt_location = location_name

        # Room Reference-Bild (Slot 4)
        bg_path = get_background_path(raw_location)
        if bg_path and bg_path.exists():
            variables.ref_image_room = str(bg_path)

    def _log_variables(self, variables: PromptVariables) -> None:
        """Loggt alle gesammelten Variablen."""
        for idx, prompt in variables.prompt_persons.items():
            logger.info("prompt_person_%d: %s...", idx, prompt[:80])
        for idx, prompt in variables.prompt_outfits.items():
            logger.debug("prompt_outfit_%d: %s", idx, prompt[:100])
        for idx, path in variables.ref_images.items():
            logger.debug("ref_image_%d: %s", idx, path.split("/")[-1] if "/" in path else path)
        if variables.prompt_mood:
            logger.debug("prompt_mood: %s", variables.prompt_mood)
        if variables.prompt_activity:
            logger.debug("prompt_activity: %s", variables.prompt_activity)
        if variables.prompt_location:
            logger.debug("prompt_location: %s", variables.prompt_location[:100])
        if variables.ref_image_room:
            logger.debug("ref_image_4 (room): %s", Path(variables.ref_image_room).name)

    # ------------------------------------------------------------------
    # Schritt 3: Ausschlussregeln anwenden
    # ------------------------------------------------------------------

    def apply_exclusion_rules(
        self,
        variables: PromptVariables,
        *,
        kind: Optional[str] = None,
        has_style_conditioning: bool = False) -> PromptVariables:
        """Wendet Workflow-abhaengige Ausschlussregeln an.

        Strippen geschieht NUR bei QWEN_STYLE — dort steuern die Reference-Bilder
        Aussehen, Outfit und Location via Style-Conditioning, Doppel-Beschreibung
        im Text waere kontraproduktiv.

        Bei FLUX_BG/Z_IMAGE bleibt alles im Text, weil Charaktere ueber Post-
        Processing (FaceSwap/MultiSwap) reinkommen und das Outfit ohne Style-
        Conditioning sonst fehlen wuerde.

        Args:
            variables: Gesammelte Prompt-Variablen.
            kind: WorkflowKind als String (qwen_style/flux_bg/z_image).
                  None -> abgeleitet aus has_style_conditioning (Backward-Compat).
            has_style_conditioning: Backward-Compat-Flag fuer Caller, die noch
                  kein kind durchreichen. Wenn True -> wie QWEN_STYLE behandeln.
        """
        kind_value = (kind or "").strip().lower()
        if not kind_value:
            kind_value = "qwen_style" if has_style_conditioning else ""

        if kind_value != "qwen_style":
            return variables

        # Regel 1: Ref-Bild einer Person vorhanden -> Outfit-Prompt entfaellt
        for idx in list(variables.prompt_outfits.keys()):
            if idx in variables.ref_images:
                variables.prompt_outfits.pop(idx)
                logger.debug("Ausschluss: prompt_outfit_%d entfernt (Ref-Bild vorhanden)", idx)

        # Regel 2: Room-Ref-Bild vorhanden -> Location-Prompt entfaellt
        if variables.ref_image_room and variables.prompt_location:
            logger.debug("Ausschluss: prompt_location entfernt (Room-Ref-Bild vorhanden)")
            variables.prompt_location = ""

        # Regel 3: Style-Conditioning -> Activity entfaellt (Mood bleibt!)
        if variables.prompt_activity:
            logger.debug("Ausschluss: prompt_activity entfernt (Style-Conditioning)")
            variables.prompt_activity = ""

        return variables

    # ------------------------------------------------------------------
    # Schritt 3b: scene_prompt bereinigen (Defense-in-Depth)
    # ------------------------------------------------------------------

    def sanitize_scene_prompt(
        self,
        scene_prompt: str,
        variables: PromptVariables) -> str:
        """Bereinigt den LLM-generierten scene_prompt von Duplikaten.

        Entfernt Appearance-Phrasen und Outfit-Beschreibungen, die bereits
        in den strukturierten Variablen (prompt_persons, prompt_outfits)
        enthalten sind. Siehe Plan 4.2.1b.

        Args:
            scene_prompt: LLM-generierter Szene-Prompt.
            variables: Gesammelte Prompt-Variablen (fuer Vergleich).

        Returns:
            Bereinigter scene_prompt.
        """
        if not scene_prompt:
            return scene_prompt

        original = scene_prompt

        # 0. Photographer-Referenzen entfernen (Plan 4.2.2)
        #    Im Photographer-Modus darf der Agent nicht im scene_prompt vorkommen.
        #    Entfernt Segmente die den Agent-Namen oder Kamera-Aktionen enthalten.
        if variables.photographer_mode:
            character_name_lower = self.character_name.lower()
            segments = [s.strip() for s in scene_prompt.split(",")]
            cleaned = []
            for s in segments:
                s_lower = s.lower()
                # Character-Name im Segment
                if character_name_lower in s_lower:
                    logger.debug("scene_prompt: Photographer-Segment entfernt (Name): '%s'", s[:80])
                    continue
                # Kamera-/Fotografie-Aktionen (gehoeren zum Fotografen, nicht zum Subject)
                if any(kw in s_lower for kw in _CAMERA_KEYWORDS):
                    logger.debug("scene_prompt: Photographer-Segment entfernt (Kamera): '%s'", s[:80])
                    continue
                cleaned.append(s)
            scene_prompt = ", ".join(s for s in cleaned if s)

        # 0b. Doppelte Namens-Einleitungen entfernen
        #     LLM schreibt manchmal "Kai, a handsome man" obwohl Appearance separat kommt.
        #     Entfernt Segmente wie "<Name>, a/an <adjective> man/woman/person"
        known_names = {p.name.lower() for p in variables.persons if p.name}
        if known_names:
            segments = [s.strip() for s in scene_prompt.split(",")]
            cleaned = []
            for s in segments:
                s_stripped = s.strip().lower()
                # Pruefen ob Segment nur "Name" ist oder "a/an ... man/woman/person/guy/girl"
                is_name_intro = False
                for name in known_names:
                    if s_stripped == name:
                        is_name_intro = True
                        break
                    if s_stripped.startswith(("a ", "an ")) and any(
                        w in s_stripped for w in ("man", "woman", "person", "guy", "girl", "male", "female")
                    ):
                        is_name_intro = True
                        break
                if is_name_intro:
                    logger.debug("scene_prompt: Namens-Einleitung entfernt: '%s'", s[:80])
                else:
                    cleaned.append(s)
            scene_prompt = ", ".join(s for s in cleaned if s)

        # 1. Outfit-Phrasen aus dem scene_prompt extrahieren UND als
        #    prompt_outfits[1] (Agent) uebernehmen, damit der Tool-LLM-Wunsch
        #    ("posing in sports bra and leggings") das DB-Outfit ueberschreibt.
        #    Ohne diese Uebernahme wuerden Tool-LLM-Outfit und DB-Outfit
        #    parallel als Garbage im Final-Prompt landen.
        #
        #    Erkannte Patterns (case-insensitive, je bis Komma/Punkt):
        #      - "(is) wearing X"
        #      - "posing in X" / "dressed in X" / "clad in X"
        #      - "in X attire" / "in X clothes" / "in X outfit"
        _outfit_patterns = [
            re.compile(r',?\s*(?:is\s+)?wearing\s+([^,\.]+)(?=[,\.]|$)', re.IGNORECASE),
            re.compile(r',?\s*(?:posing|dressed|clad)\s+in\s+([^,\.]+)(?=[,\.]|$)', re.IGNORECASE),
            re.compile(r',?\s*in\s+([^,\.]+?\s+(?:attire|clothes|outfit))(?=[,\.]|$)', re.IGNORECASE),
        ]
        _extracted_outfits: List[str] = []
        for _pat in _outfit_patterns:
            for _m in _pat.finditer(scene_prompt):
                _phrase = _m.group(1).strip()
                if _phrase:
                    _extracted_outfits.append(_phrase)
            scene_prompt = _pat.sub('', scene_prompt).strip()

        # 1b. Implizite Kleidungs-Erwaehnungen ohne "wearing"-Verb erkennen.
        #     Wenn der Tool-LLM "young woman's legs in a short black skirt"
        #     oder "lace thong, business heels" schreibt, soll das Outfit-Feld
        #     komplett uebernommen werden (nicht das DB-Outfit ploetzlich
        #     parallel im Bild landen). Ein Bild = ein logisches Outfit.
        _CLOTHING_KEYWORDS = (
            "skirt", "dress", "blazer", "shirt", "blouse", "pants", "trousers",
            "jeans", "shorts", "leggings", "stockings", "tights", "socks",
            "shoes", "boots", "heels", "sneakers", "sandals", "sneekers",
            "jacket", "coat", "hoodie", "sweater", "cardigan", "vest",
            "bra", "bikini", "panties", "thong", "underwear", "lingerie",
            "lederhose", "rock", "kleid", "hose", "stiefel", "schuhe",
            "blazer", "weste", "mantel",
        )
        _segments = [s.strip() for s in scene_prompt.split(",")]
        _cleaned_segments = []
        for _seg in _segments:
            _seg_low = _seg.lower()
            _has_clothing = any(
                re.search(r'\b' + re.escape(kw) + r'\b', _seg_low)
                for kw in _CLOTHING_KEYWORDS
            )
            if _has_clothing:
                # "young woman's? " Praefix entfernen — der Tool-LLM
                # nennt das Subject manchmal so statt beim Namen.
                _clean = re.sub(
                    r"\byoung\s+woman'?s?\s+", "",
                    _seg, flags=re.IGNORECASE).strip()
                # Reine "Personenbeschreibung-mit-Kleidung"-Praefixe
                # entfernen die uebrig bleiben.
                _clean = re.sub(
                    r"^(her|his|their)\s+", "",
                    _clean, flags=re.IGNORECASE).strip()
                if _clean:
                    _extracted_outfits.append(_clean)
                    logger.debug(
                        "scene_prompt: Implizites Outfit-Segment extrahiert: '%s'",
                        _clean[:80])
            else:
                _cleaned_segments.append(_seg)
        scene_prompt = ", ".join(s for s in _cleaned_segments if s)

        if _extracted_outfits and variables.persons:
            # Wenn der Agent erkannt wurde, das Outfit ihm zuordnen — sonst
            # erste Person der Liste. Tool-LLM-Outfit verdraengt DB-Outfit
            # voellig (statt zu mergen): das Tool-LLM beschreibt eine konkrete
            # Szene, das DB-Outfit ist nur Default-Annahme.
            _agent_idx = next(
                (i + 1 for i, p in enumerate(variables.persons) if p.is_agent),
                1)
            _agent_label = variables.persons[_agent_idx - 1].actor_label or variables.persons[_agent_idx - 1].name
            _new_outfit = ", ".join(_extracted_outfits)
            variables.prompt_outfits[_agent_idx] = f"{_agent_label} is wearing {_new_outfit}"
            logger.info(
                "scene_prompt: Outfit aus Tool-LLM extrahiert -> prompt_outfits[%d] = '%s'",
                _agent_idx, _new_outfit[:120])

            # Wenn der Tool-LLM eine konkrete Szene beschreibt (= eigenes
            # Outfit drin), sind die DB-State Felder fuer DIESES Bild irrelevant:
            # - prompt_activity: "sitting at desk" passt nicht zur Gym-Szene
            # - prompt_mood: DB-Mood-Expression ueberschreibt die im Tool-LLM
            #                explizit beschriebene Expression ("shy smile")
            # - prompt_location: "Buero" passt nicht zum "gym setting"
            # Die Szene-Details (Pose, Expression, Setting) bleiben im
            # bereinigten scene_prompt erhalten und gehen so ins Bild.
            if variables.prompt_activity:
                logger.info(
                    "scene_prompt: Outfit-Override -> DB-Activity '%s' geloescht "
                    "(Tool-LLM-Szene gilt)", variables.prompt_activity[:80])
                variables.prompt_activity = ""
            if variables.prompt_mood:
                logger.info(
                    "scene_prompt: Outfit-Override -> DB-Mood '%s' geloescht",
                    variables.prompt_mood[:80])
                variables.prompt_mood = ""
            if variables.prompt_location:
                logger.info(
                    "scene_prompt: Outfit-Override -> DB-Location '%s' geloescht",
                    variables.prompt_location[:80])
                variables.prompt_location = ""

        # 2. Appearance-Phrasen der erkannten Personen entfernen
        #    Prueft jede Person ob signifikante Appearance-Woerter im scene_prompt vorkommen
        for person in variables.persons:
            if not person.appearance:
                continue
            # Appearance in Woerter zerlegen (nur signifikante, >3 Zeichen)
            app_words = set(
                w.lower() for w in re.findall(r'\b\w+\b', person.appearance)
                if len(w) > 3 and w.lower() not in _COMMON_WORDS
            )
            if not app_words:
                continue

            # scene_prompt in Segmente (Komma-getrennt) zerlegen
            segments = [s.strip() for s in scene_prompt.split(",")]
            cleaned_segments = []
            for segment in segments:
                seg_words = set(
                    w.lower() for w in re.findall(r'\b\w+\b', segment)
                    if len(w) > 3 and w.lower() not in _COMMON_WORDS
                )
                # Wenn >60% der Segment-Woerter in Appearance vorkommen -> entfernen
                if seg_words and len(seg_words & app_words) / len(seg_words) > 0.6:
                    logger.debug("scene_prompt: Appearance-Segment entfernt fuer '%s': '%s'",
                                 person.name, segment[:80])
                else:
                    cleaned_segments.append(segment)
            scene_prompt = ", ".join(s for s in cleaned_segments if s)

        # Aufraumen: mehrfache Kommas/Leerzeichen
        scene_prompt = re.sub(r'\s*,\s*,\s*', ', ', scene_prompt).strip(", ")

        if scene_prompt != original:
            logger.info("scene_prompt bereinigt: '%s' -> '%s'",
                        original[:120], scene_prompt[:120])

        return scene_prompt

    # ------------------------------------------------------------------
    # Schritt 4: Prompt zusammenbauen
    # ------------------------------------------------------------------
    # Der finale Zusammenbau erfolgt jetzt in app/core/prompt_adapters.render()
    # pro Target-Model (z_image / qwen / flux). Dieser Schritt entfaellt hier.

    # ------------------------------------------------------------------
    # Schritt 5: Reference-Image Slots aufloesen
    # ------------------------------------------------------------------

    def resolve_reference_slots(
        self,
        variables: PromptVariables,
        max_slots: int = 4,
        *,
        kind: Optional[str] = None) -> Dict[str, Any]:
        """Baut die Reference-Image Slot-Map fuer ComfyUI-Workflows.

        Dispatch je nach WorkflowKind:
            QWEN_STYLE:  Slots 1-3=Personen, Slot 4=Location (Style-Conditioning)
            FLUX_BG:     ein Background-Slot mit Use-Schalter
            Z_IMAGE:     keine Ref-Slots

        Args:
            variables: Gesammelte Prompt-Variablen.
            max_slots: Anzahl Ref-Slots (nur QWEN_STYLE relevant).
            kind: WorkflowKind-Wert. None -> wie QWEN_STYLE (Backward-Compat).

        Returns:
            Dict mit keys:
                reference_images: {node_title: file_path}
                boolean_inputs:   {node_title: bool}
                string_inputs:    {node_title: str}
                workflow_has_faceswap: bool   (True wenn Personen-Slots belegt)
        """
        # WorkflowKind-Werte werden als String uebergeben (vermeidet Import-Zyklus
        # zu image_generation_skill.WorkflowKind). Default: Qwen-Style.
        kind_value = (kind or "").strip().lower() or "qwen_style"

        if kind_value == "flux_bg":
            return self._resolve_flux_bg_slots(variables)
        if kind_value == "z_image":
            return {
                "reference_images": {}, "boolean_inputs": {},
                "string_inputs": {}, "workflow_has_faceswap": False}

        return self._resolve_qwen_slots(variables, max_slots)

    def _resolve_qwen_slots(
        self, variables: PromptVariables, max_slots: int) -> Dict[str, Any]:
        """QWEN_STYLE: Slots 1..max_slots-1 = Personen/Items, Slot max_slots = Room."""
        reference_images: Dict[str, str] = {}
        boolean_inputs: Dict[str, bool] = {}
        string_inputs: Dict[str, str] = {}

        max_person_slots = max_slots - 1
        next_free_slot = 1

        for idx, person in enumerate(variables.persons, 1):
            if idx > max_person_slots:
                logger.debug("RefSlot: %s uebersprungen (max %d Personen-Slots)",
                             person.name, max_person_slots)
                break

            ref_path = variables.ref_images.get(idx, "")
            if not ref_path:
                continue

            if not Path(ref_path).exists():
                logger.warning("RefSlot %d: Datei nicht gefunden: %s — uebersprungen", idx, ref_path)
                continue

            norm_gender = self._normalize_gender(person.gender)
            reference_images[f"input_reference_image_{idx}"] = ref_path
            boolean_inputs[f"input_faceswap_{idx}"] = True
            string_inputs[f"input_reference_image_{idx}_type"] = norm_gender or "no"
            logger.debug("RefSlot %d: %s (gender=%s)", idx, person.name, norm_gender or "no")
            next_free_slot = idx + 1

        workflow_has_faceswap = len(reference_images) > 0

        for it in (variables.items or []):
            if next_free_slot > max_person_slots:
                logger.debug("Item-Slots voll: %s uebersprungen", it.get("name", "?"))
                break
            img_path = it.get("image", "")
            if not img_path or not Path(img_path).exists():
                continue
            reference_images[f"input_reference_image_{next_free_slot}"] = img_path
            boolean_inputs[f"input_faceswap_{next_free_slot}"] = False
            string_inputs[f"input_reference_image_{next_free_slot}_type"] = "item"
            logger.info("RefSlot %d: Item '%s'", next_free_slot, it.get("name", "?"))
            next_free_slot += 1

        if variables.ref_image_room and Path(variables.ref_image_room).exists():
            reference_images[f"input_reference_image_{max_slots}"] = variables.ref_image_room
            boolean_inputs[f"input_faceswap_{max_slots}"] = False
            string_inputs[f"input_reference_image_{max_slots}_type"] = "location"
            logger.debug("RefSlot %d: Room/Location", max_slots)

        if workflow_has_faceswap:
            logger.info("RefSlots: %d Face-Slots belegt", len(
                [k for k, v in boolean_inputs.items() if v]))
        logger.debug("RefSlots: %d Slots insgesamt", len(reference_images))

        return {
            "reference_images": reference_images,
            "boolean_inputs": boolean_inputs,
            "string_inputs": string_inputs,
            "workflow_has_faceswap": workflow_has_faceswap,
        }

    def _resolve_flux_bg_slots(self, variables: PromptVariables) -> Dict[str, Any]:
        """FLUX_BG: nur input_reference_image_background mit Use-Schalter.

        Charaktere kommen ueber Post-Processing — keine Person-Refs hier.
        Wenn kein Room-Bild vorhanden, bleibt reference_images leer; das
        Backend laedt automatisch das 8x8-Placeholder in den BG-LoadImage-Node
        und das input_reference_image_use Switch-Node wird auf False gesetzt.
        """
        reference_images: Dict[str, str] = {}
        boolean_inputs: Dict[str, bool] = {}

        if variables.ref_image_room and Path(variables.ref_image_room).exists():
            reference_images["input_reference_image_background"] = variables.ref_image_room
            # input_reference_image_use wird vom Backend automatisch aktiviert,
            # sobald ein Switch-Node mit dem Title gefunden wird (siehe
            # image_backends._activated_switches Logik).
            logger.debug("FLUX_BG: Background-Slot belegt: %s", Path(variables.ref_image_room).name)
        else:
            # Kein Background -> Use-Switch explizit auf False
            boolean_inputs["input_reference_image_use"] = False
            logger.debug("FLUX_BG: kein Background, use=False")

        # Charaktere kommen via Post-Processing (FaceSwap/MultiSwap).
        # workflow_has_faceswap=False signalisiert: kein Style-Conditioning,
        # Post-Processing ist noetig (entscheidet die execute()-Weiche).
        return {
            "reference_images": reference_images,
            "boolean_inputs": boolean_inputs,
            "string_inputs": {},
            "workflow_has_faceswap": False,
        }

    @staticmethod
    def _normalize_gender(gender: str) -> str:
        """Normalisiert Gender-String fuer ComfyUI-Workflows und Akteur-Labels."""
        g = gender.strip().lower()
        if g in ("female", "f", "weiblich", "w", "woman", "girl", "frau", "mädchen", "maedchen"):
            return "female"
        if g in ("male", "m", "männlich", "maennlich", "man", "boy", "mann", "junge"):
            return "male"
        return ""
