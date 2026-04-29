"""Instagram Feed Datenmodell - Unified Feed pro User"""
import json
import re
import uuid
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional

from app.core.log import get_logger
from app.core.db import get_connection, transaction

logger = get_logger("instagram_model")

from app.models.character import get_user_characters_dir


def get_instagram_dir() -> Path:
    """Gibt das Instagram-Verzeichnis auf User-Ebene zurueck, erstellt es bei Bedarf."""
    instagram_dir = get_user_characters_dir().parent / "instagram"
    instagram_dir.mkdir(parents=True, exist_ok=True)
    return instagram_dir


def get_feed_path() -> Path:
    """Gibt den Pfad zur unified feed.json zurueck."""
    return get_instagram_dir() / "feed.json"


def get_image_meta_path(image_filename: str) -> Path:
    """Gibt den Pfad zur Metadaten-Datei eines Bildes zurueck (gleicher Name wie Bild, .json)."""
    stem = Path(image_filename).stem
    return get_instagram_dir() / f"{stem}.json"


def load_image_meta(image_filename: str) -> Optional[Dict[str, Any]]:
    """Laedt die Metadaten eines Bildes. Gibt None zurueck wenn nicht vorhanden."""
    meta_path = get_image_meta_path(image_filename)
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def save_image_meta(image_filename: str, meta: Dict[str, Any]):
    """Speichert die Metadaten eines Bildes als separate JSON-Datei."""
    meta_path = get_image_meta_path(image_filename)
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    logger.debug("Image meta gespeichert: %s", meta_path.name)


def load_feed() -> List[Dict[str, Any]]:
    """Laedt den unified Feed aus der DB (neueste zuerst), Fallback JSON."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT ts, character_name, payload FROM events "
            "WHERE kind='instagram_post' ORDER BY ts DESC"
        ).fetchall()
        if rows:
            feed = []
            for r in rows:
                try:
                    post = json.loads(r[2] or "{}")
                    if not post.get("timestamp"):
                        post["timestamp"] = r[0] or ""
                    if not post.get("agent_name"):
                        post["agent_name"] = r[1] or ""
                    feed.append(post)
                except Exception:
                    pass
            return feed
    except Exception as e:
        logger.warning("load_feed DB-Fehler: %s", e)

    # Fallback: JSON-Datei
    feed_path = get_feed_path()
    if feed_path.exists():
        try:
            data = json.loads(feed_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def save_feed(feed: List[Dict[str, Any]]):
    """Speichert den unified Feed in die DB (Upsert) + JSON-Backup.

    Instagram-Posts werden in der events-Tabelle mit kind='instagram_post' gespeichert.
    Lookup fuer Updates: post_id ist in payload["id"].
    """
    try:
        with transaction() as conn:
            # Vorhandene Posts laden: db_id -> post_id
            existing_rows = conn.execute(
                "SELECT id, payload FROM events WHERE kind='instagram_post'"
            ).fetchall()
            db_to_post: Dict[int, str] = {}
            post_to_db: Dict[str, int] = {}
            for db_id, payload_str in existing_rows:
                try:
                    p = json.loads(payload_str or "{}")
                    pid = p.get("id", str(db_id))
                except Exception:
                    pid = str(db_id)
                db_to_post[db_id] = pid
                post_to_db[pid] = db_id

            new_post_ids = {p.get("id") for p in feed if p.get("id")}

            # Geloeschte Posts entfernen
            for db_id, pid in db_to_post.items():
                if pid not in new_post_ids:
                    conn.execute("DELETE FROM events WHERE id=?", (db_id,))

            # Upsert
            for post in feed:
                pid = post.get("id")
                if not pid:
                    continue
                ts = post.get("timestamp", datetime.now().isoformat())
                char = post.get("agent_name", "")
                payload_str = json.dumps(post, ensure_ascii=False)
                if pid in post_to_db:
                    conn.execute(
                        "UPDATE events SET ts=?, character_name=?, payload=? WHERE id=?",
                        (ts, char, payload_str, post_to_db[pid]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO events (ts, kind, character_name, payload) "
                        "VALUES (?, 'instagram_post', ?, ?)",
                        (ts, char, payload_str),
                    )
    except Exception as e:
        logger.error("save_feed DB-Fehler: %s", e)

    # JSON-Backup
    try:
        feed_path = get_feed_path()
        feed_path.write_text(
            json.dumps(feed, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception:
        pass


def create_post(character_name: str,
    image_filename: str,
    caption: str,
    hashtags: List[str] = None,
    image_prompt: str = "",
    image_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Erstellt einen neuen Instagram-Post und fuegt ihn am Anfang des Feeds ein.

    Bild-Metadaten (Prompt, Backend, Workflow etc.) werden in einer separaten
    JSON-Datei neben dem Bild gespeichert (gleicher Name, .json), nicht im Feed.
    """
    timestamp = int(time.time())
    post_id = f"post_{timestamp}_{uuid.uuid4().hex[:8]}"

    post = {
        "id": post_id,
        "image_filename": image_filename,
        "caption": caption,
        "hashtags": hashtags or [],
        "timestamp": datetime.now().isoformat(),
        "agent_name": character_name,
        "likes": 0,
        "liked_by": [],
        "comments": [],
    }

    feed = load_feed()
    feed.insert(0, post)  # Neueste zuerst
    save_feed(feed)

    # Bild-Metadaten separat speichern
    meta = {
        "image_filename": image_filename,
        "prompt": image_prompt or "",
        "backend": "",
        "backend_type": "",
        "workflow": "",
        "faceswap": False,
        "duration_s": 0.0,
        "image_analysis": "",
    }
    if image_meta:
        meta.update(image_meta)
    # prompt aus image_meta ueberschreibt nicht, wenn image_prompt explizit gesetzt
    if image_prompt:
        meta["prompt"] = image_prompt
    save_image_meta(image_filename, meta)

    # Social Reactions im Hintergrund triggern
    try:
        from app.core.social_reactions import trigger_social_reactions
        trigger_social_reactions(character_name, post)
    except Exception as e:
        logger.error("Social reactions trigger error: %s", e)

    return post


def get_post(post_id: str) -> Optional[Dict[str, Any]]:
    """Gibt einen einzelnen Post zurueck oder None."""
    feed = load_feed()
    for post in feed:
        if post.get("id") == post_id:
            return post
    return None


def add_post_image(post_id: str, image_filename: str) -> bool:
    """Fuegt ein weiteres Bild zu einem bestehenden Post hinzu (Carousel).

    Migriert den Post von single-image (image_filename) zu multi-image (image_filenames).
    """
    feed = load_feed()
    for post in feed:
        if post.get("id") == post_id:
            # Migration: Sicherstellen, dass image_filenames existiert
            if "image_filenames" not in post:
                post["image_filenames"] = [post.get("image_filename", "")]
            post["image_filenames"].append(image_filename)
            save_feed(feed)
            logger.info("Bild %s zu Post %s hinzugefuegt (%d Bilder)",
                        image_filename, post_id, len(post["image_filenames"]))
            return True
    return False


def remove_post_image(post_id: str, image_filename: str) -> bool:
    """Entfernt ein einzelnes Bild aus einem Carousel-Post.

    Wenn nur noch ein Bild uebrig bleibt, wird image_filenames entfernt
    und image_filename auf das verbleibende Bild gesetzt.
    Returns False wenn Post oder Bild nicht gefunden.
    """
    feed = load_feed()
    for post in feed:
        if post.get("id") != post_id:
            continue
        filenames = post.get("image_filenames", [])
        if not filenames:
            # Single-image Post — kein Bild entfernbar ohne Post zu loeschen
            return False
        if image_filename not in filenames:
            return False
        filenames.remove(image_filename)
        if len(filenames) <= 1:
            # Zurueck zu Single-Image
            post["image_filename"] = filenames[0] if filenames else ""
            post.pop("image_filenames", None)
        else:
            # Hauptbild aktualisieren falls es das geloeschte war
            if post.get("image_filename") == image_filename:
                post["image_filename"] = filenames[0]
        save_feed(feed)

        # Bild-Datei und Meta loeschen
        instagram_dir = get_instagram_dir()
        img_path = instagram_dir / image_filename
        if img_path.exists():
            try:
                img_path.unlink()
            except Exception:
                pass
        meta_path = get_image_meta_path(image_filename)
        if meta_path.exists():
            try:
                meta_path.unlink()
            except Exception:
                pass
        # Video loeschen falls vorhanden
        video_path = instagram_dir / (Path(image_filename).stem + ".mp4")
        if video_path.exists():
            try:
                video_path.unlink()
            except Exception:
                pass

        logger.info("Bild %s aus Post %s entfernt (%d Bilder verbleiben)",
                    image_filename, post_id, len(filenames))
        return True
    return False


def delete_post(post_id: str) -> bool:
    """Loescht einen Post. Gibt True zurueck wenn gefunden und geloescht."""
    feed = load_feed()
    new_feed = [p for p in feed if p.get("id") != post_id]
    if len(new_feed) < len(feed):
        save_feed(new_feed)
        return True
    return False


def add_comment(post_id: str,
    commenter_name: str,
    text: str) -> Optional[Dict[str, Any]]:
    """Fuegt einen Kommentar zu einem Post hinzu. Gibt den Kommentar zurueck oder None."""
    feed = load_feed()
    for post in feed:
        if post.get("id") == post_id:
            comment = {
                "id": f"c_{uuid.uuid4().hex[:8]}",
                "author": commenter_name,
                "text": text,
                "timestamp": datetime.now().isoformat(),
            }
            if "comments" not in post:
                post["comments"] = []
            post["comments"].append(comment)
            save_feed(feed)
            return comment
    return None


def toggle_like(post_id: str) -> Optional[int]:
    """Erhoeht den Like-Zaehler um 1. Gibt neuen Zaehler zurueck oder None."""
    feed = load_feed()
    for post in feed:
        if post.get("id") == post_id:
            post["likes"] = post.get("likes", 0) + 1
            save_feed(feed)
            return post["likes"]
    return None


def add_character_like(post_id: str, character_name: str) -> bool:
    """Fuegt einen Character-Like zu einem Post hinzu (mit Name in liked_by).

    Erhoeht auch den likes-Zaehler. Verhindert doppelte Likes.
    Returns True wenn erfolgreich, False wenn bereits geliked oder Post nicht gefunden.
    """
    feed = load_feed()
    for post in feed:
        if post.get("id") == post_id:
            liked_by = post.setdefault("liked_by", [])
            if character_name in liked_by:
                return False
            liked_by.append(character_name)
            post["likes"] = post.get("likes", 0) + 1
            save_feed(feed)
            return True
    return False


# --- Automatische Extraktion von Interaktionen aus Chat-Text ---

def _parse_like_count(text: str) -> int:
    """Parst Like-Angaben wie '15k', '2.5k', '1m', '500'."""
    text = text.strip().lower()
    # Dezimal-Notation: 2.5k, 1.2m
    m = re.match(r'(\d+[.,]\d+)\s*k', text)
    if m:
        return int(float(m.group(1).replace(",", ".")) * 1000)
    m = re.match(r'(\d+[.,]\d+)\s*m', text)
    if m:
        return int(float(m.group(1).replace(",", ".")) * 1000000)
    # Ganzzahl: 15k, 1m
    m = re.match(r'(\d+)\s*k', text)
    if m:
        return int(m.group(1)) * 1000
    m = re.match(r'(\d+)\s*m', text)
    if m:
        return int(m.group(1)) * 1000000
    # Reine Zahl: 500
    m = re.match(r'(\d+)', text)
    if m:
        return int(m.group(1))
    return 0


def extract_instagram_interactions(text: str) -> Optional[Dict[str, Any]]:
    """Extrahiert fiktive Instagram-Interaktionen (Likes, Kommentare) aus Chat-Text.

    Erkennt Muster wie:
        **Likes:** 15k
        Likes: 500
        Kommentare:
            user_9087: Text...
            `user_3421`: Text...

    Returns:
        Dict mit 'likes' (int) und 'comments' (list) oder None wenn nichts gefunden.
    """
    likes = 0
    comments = []

    # Likes extrahieren: "**Likes:** 15k", "Likes: 500"
    likes_match = re.search(
        r'\*{0,2}Likes:?\*{0,2}\s*(\d[\d.,]*\s*[kKmM]?)',
        text
    )
    if likes_match:
        likes = _parse_like_count(likes_match.group(1))

    # Kommentare extrahieren: "`user_9087`: Text" oder "user_9087: Text"
    comment_pattern = re.findall(
        r'[*\s]*`?(\w+(?:[-_]\w+)*)`?\s*:\s*(.+?)(?:\n|$)',
        text
    )
    # Nur User-artige Kommentare (nicht "Likes:", "Kommentare:", "Bild:" etc.)
    skip_keys = {
        'likes', 'kommentare', 'comments', 'bild', 'image', 'caption',
        'captions', 'hashtags', 'interaktionen', 'interactions',
        'new_assignment', 'assignment_update', 'assignment_done',
    }
    for author, comment_text in comment_pattern:
        if author.lower() in skip_keys:
            continue
        # Nur echte User-Handles akzeptieren: @name, oder user_1234 (Underscore + Ziffer).
        # Reine Ziffern (Zeitstempel wie "13:35") werden ignoriert.
        is_at_mention = author.startswith('@')
        is_user_handle = '_' in author and re.search(r'\d', author) is not None
        if is_at_mention or is_user_handle:
            cleaned = comment_text.strip().rstrip('*').strip()
            if cleaned:
                comments.append({"author": author, "text": cleaned})

    if likes > 0 or comments:
        return {"likes": likes, "comments": comments}
    return None


def apply_interactions_to_latest_post(character_name: str, interactions: Dict[str, Any]
) -> bool:
    """Wendet extrahierte Interaktionen auf den neuesten Post des Characters an.

    Returns:
        True wenn erfolgreich angewendet.
    """
    feed = load_feed()
    if not feed:
        return False

    # Neuesten Post dieses Characters finden (Feed ist neueste-zuerst sortiert)
    post = None
    for p in feed:
        if p.get("agent_name") == character_name:
            post = p
            break

    if not post:
        return False

    changed = False

    if interactions.get("likes", 0) > 0:
        post["likes"] = interactions["likes"]
        changed = True
        logger.info("Likes gesetzt: %d auf Post %s", interactions["likes"], post["id"])

    for comment_data in interactions.get("comments", []):
        comment = {
            "id": f"c_{uuid.uuid4().hex[:8]}",
            "author": comment_data["author"],
            "text": comment_data["text"],
            "timestamp": datetime.now().isoformat(),
        }
        if "comments" not in post:
            post["comments"] = []
        post["comments"].append(comment)
        changed = True
        logger.info("Kommentar hinzugefuegt: %s", comment_data["author"])

    if changed:
        save_feed(feed)
    return changed
