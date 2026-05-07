"""
Telegram Long Polling Manager

Polls Telegram Bot API via getUpdates for each character that has a
telegram_bot_token configured. No open port or webhook needed.

Each character gets its own polling loop and its own Telegram bot.
"""
import asyncio
import json
import re
import os
from typing import Dict, Any, Optional, Set
from pathlib import Path
from datetime import datetime

import aiohttp

from app.core.log import get_logger

logger = get_logger("telegram_polling")


class CharacterBotPoller:
    """Polls getUpdates for a single character's Telegram bot."""

    def __init__(
        self, character_name: str,
        bot_token: str,
        api_url: str = "https://api.telegram.org/bot"):
        self.user_id = ""
        self.character_name = character_name
        self.bot_token = bot_token
        self.api_url = api_url
        self._offset: int = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._bot_info: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _api(self, method: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        url = f"{self.api_url}{self.bot_token}/{method}"
        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(total=90)  # long-poll timeout
            async with session.post(url, json=params or {}, timeout=timeout) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("ok"):
                    return data.get("result")
                logger.error("[%s] API %s error (%d): %s", self.character_name, method, resp.status, data)
                return None
        except asyncio.TimeoutError:
            return None  # normal for long-poll
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[%s] API %s exception: %s", self.character_name, method, e)
            return None

    # ------------------------------------------------------------------
    # Telegram actions
    # ------------------------------------------------------------------

    async def send_typing(self, chat_id: int) -> None:
        await self._api("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    async def send_message(self, chat_id: int, text: str, parse_mode: str = "HTML") -> Optional[int]:
        # Telegram max 4096 chars
        if len(text) > 4096:
            last_id = None
            for i in range(0, len(text), 4096):
                last_id = await self.send_message(chat_id, text[i:i + 4096], parse_mode)
            return last_id

        result = await self._api("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        })
        if result:
            return result.get("message_id")
        return None

    async def verify_bot(self) -> bool:
        result = await self._api("getMe", {})
        if result:
            self._bot_info = result
            logger.info("[%s] Bot verified: @%s", self.character_name, result.get("username", "?"))
            return True
        return False

    async def delete_webhook(self) -> None:
        """Remove any existing webhook so getUpdates works."""
        await self._api("deleteWebhook", {"drop_pending_updates": False})

    async def _setup_bot_profile(self) -> None:
        """Set bot name and description from character profile."""
        try:
            from app.models.character import get_character_profile
            profile = get_character_profile(self.character_name)

            # Bot name = character name (may fail with 429 after frequent restarts — non-critical)
            name = profile.get("character_name", self.character_name)
            try:
                await self._api("setMyName", {"name": name[:64]})
            except Exception:
                pass

            # Short description (shown in profile, max 120 chars)
            task = profile.get("character_task", "")
            personality = profile.get("character_personality", "")
            short_desc = task[:120] if task else personality[:120]
            if short_desc:
                await self._api("setMyShortDescription", {"short_description": short_desc})

            # Full description (shown when user opens bot info, max 512 chars)
            desc_parts = []
            if personality:
                desc_parts.append(personality)
            if task and task != personality:
                desc_parts.append(task)
            full_desc = "\n\n".join(desc_parts)[:512]
            if full_desc:
                await self._api("setMyDescription", {"description": full_desc})

            logger.info("[%s] Bot profile updated", self.character_name)
        except Exception as e:
            logger.warning("[%s] Could not set bot profile: %s", self.character_name, e)

    async def _register_commands(self) -> None:
        """Register bot commands menu."""
        commands = [
            {"command": "start", "description": "Starten & Registrieren"},
            {"command": "info", "description": "Character-Profil anzeigen"},
            {"command": "gallery", "description": "Bildergalerie anzeigen"},
            {"command": "outfits", "description": "Outfits anzeigen & wechseln"},
        ]
        await self._api("setMyCommands", {"commands": commands})
        logger.info("[%s] Bot commands registered", self.character_name)

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        if self._running:
            return True
        if not await self.verify_bot():
            logger.error("[%s] Bot verification failed — not starting poller", self.character_name)
            return False
        await self.delete_webhook()
        await self._setup_bot_profile()
        await self._register_commands()
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("[%s] Polling started", self.character_name)
        return True

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("[%s] Polling stopped", self.character_name)

    async def _poll_loop(self) -> None:
        backoff = 1
        while self._running:
            try:
                updates = await self._api("getUpdates", {
                    "offset": self._offset,
                    "timeout": 60,  # Telegram long-poll (seconds)
                    "allowed_updates": ["message", "callback_query"],
                })
                if updates is None:
                    # Connection error or timeout — back off briefly
                    await asyncio.sleep(min(backoff, 30))
                    backoff = min(backoff * 2, 30)
                    continue

                backoff = 1  # reset on success

                for update in updates:
                    self._offset = update["update_id"] + 1
                    try:
                        await self._handle_update(update)
                    except Exception as e:
                        logger.error("[%s] Error handling update: %s", self.character_name, e, exc_info=True)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[%s] Poll loop error: %s", self.character_name, e, exc_info=True)
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_update(self, update: Dict[str, Any]) -> None:
        # Handle callback queries (inline button presses)
        callback_query = update.get("callback_query")
        if callback_query:
            await self._handle_callback_query(callback_query)
            return

        message = update.get("message")
        if not message:
            return

        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "")
        from_user = message.get("from", {})

        if not chat_id:
            return

        # Commands
        cmd = text.strip().split()[0].lower() if text.strip() else ""
        if cmd == "/start":
            await self._handle_start(chat_id, from_user)
            return
        if cmd == "/info":
            await self._handle_info(chat_id)
            return
        if cmd == "/gallery":
            await self._handle_gallery(chat_id)
            return
        if cmd == "/outfits":
            await self._handle_outfits(chat_id)
            return

        if not text:
            return

        # Process normal message
        await self._process_chat_message(chat_id, text, from_user)

    async def _handle_start(self, chat_id: int, from_user: Dict[str, Any]) -> None:
        """Handle /start — register user and send welcome."""
        display_name = from_user.get("first_name", "")
        if from_user.get("last_name"):
            display_name += f" {from_user['last_name']}"

        # Register mapping: chat_id → character owner ()
        from app.models.telegram_channel import get_telegram_channel
        telegram = get_telegram_channel()
        telegram.register_user(chat_id)

        welcome = (
            f"Hallo {display_name}! 👋\n\n"
            f"Ich bin <b>{self.character_name}</b>.\n"
            f"Du kannst mir einfach schreiben und ich antworte dir.\n\n"
            f"Viel Spaß! 🎭"
        )
        await self.send_message(chat_id, welcome)
        logger.info("[%s] /start from %s (chat_id=%s)", self.character_name, display_name, chat_id)

    async def _handle_info(self, chat_id: int) -> None:
        """Handle /info — show character profile."""
        try:
            from app.models.character import get_character_profile, get_character_current_location, get_character_current_activity
            from app.models.world import get_location_name

            profile = get_character_profile(self.character_name)
            name = profile.get("character_name", self.character_name)
            personality = profile.get("character_personality", "")
            task = profile.get("character_task", "")
            age = profile.get("age", "")
            gender = profile.get("gender", "")

            loc_id = get_character_current_location(self.character_name)
            location = get_location_name(loc_id) if loc_id else ""
            activity = get_character_current_activity(self.character_name)
            feeling = profile.get("current_feeling", "")

            lines = [f"👤 {name}"]
            if age or gender:
                details = ", ".join(filter(None, [f"{age} Jahre" if age else "", gender]))
                lines.append(details)
            lines.append("")
            if personality:
                lines.append(f"💭 {personality}")
                lines.append("")
            if task:
                lines.append(f"📋 {task}")
                lines.append("")
            if location:
                lines.append(f"📍 {location}")
            if activity:
                lines.append(f"🎯 {activity}")
            if feeling:
                lines.append(f"😊 {feeling}")

            # Send profile image if available
            profile_image = profile.get("profile_image", "")
            if profile_image:
                await self._send_profile_image(chat_id, profile_image, caption="\n".join(lines))
            else:
                await self.send_message(chat_id, "\n".join(lines), parse_mode="")
        except Exception as e:
            logger.error("[%s] /info error: %s", self.character_name, e, exc_info=True)
            await self.send_message(chat_id, "Profil konnte nicht geladen werden.", parse_mode="")

    async def _handle_gallery(self, chat_id: int) -> None:
        """Handle /gallery — send character images as album."""
        try:
            from app.models.character import get_character_images_dir

            images_dir = get_character_images_dir(self.character_name)
            if not images_dir.exists():
                await self.send_message(chat_id, "Keine Bilder vorhanden.", parse_mode="")
                return

            # Collect image files (png, jpg, webp), newest first, max 10
            image_files = sorted(
                [f for f in images_dir.iterdir()
                 if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                 and not f.name.endswith(".json")],
                key=lambda f: f.stat().st_mtime,
                reverse=True)[:10]

            if not image_files:
                await self.send_message(chat_id, "Keine Bilder vorhanden.", parse_mode="")
                return

            if len(image_files) == 1:
                await self._send_photo_file(chat_id, image_files[0])
            else:
                # Send as media group (album), max 10 per group
                await self._send_media_group(chat_id, image_files)

        except Exception as e:
            logger.error("[%s] /gallery error: %s", self.character_name, e, exc_info=True)
            await self.send_message(chat_id, "Galerie konnte nicht geladen werden.", parse_mode="")

    async def _send_profile_image(self, chat_id: int, image_filename: str, caption: str = "") -> None:
        """Send character profile image with optional caption."""
        from app.models.character import get_character_images_dir
        image_path = get_character_images_dir(self.character_name) / image_filename
        if image_path.exists():
            await self._send_photo_file(chat_id, image_path, caption=caption)
        elif caption:
            await self.send_message(chat_id, caption, parse_mode="")

    async def _send_photo_file(self, chat_id: int, file_path: Path, caption: str = "") -> None:
        """Send a local image file via Telegram sendPhoto."""
        url = f"{self.api_url}{self.bot_token}/sendPhoto"
        try:
            session = await self._get_session()
            data = aiohttp.FormData()
            data.add_field("chat_id", str(chat_id))
            data.add_field("photo", open(file_path, "rb"), filename=file_path.name)
            if caption:
                data.add_field("caption", caption[:1024])  # Telegram caption max 1024
            async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                result = await resp.json()
                if not result.get("ok"):
                    logger.error("[%s] sendPhoto error: %s", self.character_name, result)
        except Exception as e:
            logger.error("[%s] sendPhoto exception: %s", self.character_name, e)

    async def _send_video_file(self, chat_id: int, file_path: Path, caption: str = "") -> None:
        """Send a local video file via Telegram sendAnimation (autoplay, looped)."""
        url = f"{self.api_url}{self.bot_token}/sendAnimation"
        try:
            session = await self._get_session()
            data = aiohttp.FormData()
            data.add_field("chat_id", str(chat_id))
            data.add_field("animation", open(file_path, "rb"), filename=file_path.name)
            if caption:
                data.add_field("caption", caption[:1024])
            async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                result = await resp.json()
                if not result.get("ok"):
                    logger.error("[%s] sendAnimation error: %s", self.character_name, result)
        except Exception as e:
            logger.error("[%s] sendAnimation exception: %s", self.character_name, e)

    async def _send_media_group(self, chat_id: int, file_paths: list) -> None:
        """Send multiple images as album via Telegram sendMediaGroup."""
        url = f"{self.api_url}{self.bot_token}/sendMediaGroup"
        try:
            session = await self._get_session()
            data = aiohttp.FormData()
            data.add_field("chat_id", str(chat_id))

            media = []
            for i, fp in enumerate(file_paths):
                attach_name = f"photo{i}"
                data.add_field(attach_name, open(fp, "rb"), filename=fp.name)
                media.append({"type": "photo", "media": f"attach://{attach_name}"})

            data.add_field("media", json.dumps(media))
            async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                result = await resp.json()
                if not result.get("ok"):
                    logger.error("[%s] sendMediaGroup error: %s", self.character_name, result)
        except Exception as e:
            logger.error("[%s] sendMediaGroup exception: %s", self.character_name, e)

    # ------------------------------------------------------------------
    # Inline keyboard helpers
    # ------------------------------------------------------------------

    async def send_message_with_buttons(self, chat_id: int, text: str,
                                        buttons: list, parse_mode: str = "HTML") -> Optional[int]:
        """Send message with inline keyboard buttons.

        buttons: list of rows, each row is a list of {"text": ..., "callback_data": ...}
        """
        result = await self._api("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": json.dumps({"inline_keyboard": buttons}),
        })
        if result:
            return result.get("message_id")
        return None

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Acknowledge a callback query (removes loading spinner)."""
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        await self._api("answerCallbackQuery", params)

    async def edit_message_text(self, chat_id: int, message_id: int,
                                text: str, buttons: list = None,
                                parse_mode: str = "HTML") -> None:
        """Edit an existing message's text and optionally its buttons."""
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if buttons is not None:
            payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        await self._api("editMessageText", payload)

    # ------------------------------------------------------------------
    # Callback query handler
    # ------------------------------------------------------------------

    async def _handle_callback_query(self, callback_query: Dict[str, Any]) -> None:
        """Route inline button presses."""
        callback_id = callback_query.get("id", "")
        data = callback_query.get("data", "")
        message = callback_query.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")

        if not chat_id or not data:
            await self.answer_callback(callback_id)
            return

        try:
            if data.startswith("outfit_wear:"):
                outfit_id = data.split(":", 1)[1]
                await self._cb_outfit_wear(chat_id, message_id, callback_id, outfit_id)
            elif data.startswith("outfit_img:"):
                outfit_id = data.split(":", 1)[1]
                await self._cb_outfit_generate_image(chat_id, message_id, callback_id, outfit_id)
            elif data == "outfit_create":
                await self._cb_outfit_create(chat_id, message_id, callback_id)
            elif data == "outfit_back":
                await self.answer_callback(callback_id)
                await self._handle_outfits(chat_id, edit_message_id=message_id)
            else:
                await self.answer_callback(callback_id)
        except Exception as e:
            logger.error("[%s] Callback error (%s): %s", self.character_name, data, e, exc_info=True)
            await self.answer_callback(callback_id, "Fehler aufgetreten")

    # ------------------------------------------------------------------
    # /outfits command
    # ------------------------------------------------------------------

    async def _handle_outfits(self, chat_id: int, edit_message_id: int = None) -> None:
        """Handle /outfits — show outfit list with inline buttons."""
        try:
            from app.models.character import (
                get_character_outfits, get_character_outfits_dir)
            from app.models.inventory import get_equipped_pieces
            outfits = get_character_outfits(self.character_name)
            equipped_ids = set((get_equipped_pieces(self.character_name) or {}).values())
            outfits_dir = get_character_outfits_dir(self.character_name)

            def _is_current_preset(o: Dict[str, Any]) -> bool:
                ids = set(o.get("pieces") or [])
                return bool(ids) and ids == equipped_ids

            if not outfits:
                text = f"<b>{self.character_name}</b> hat noch keine Outfits.\n\nErstelle ein neues!"
                buttons = [[{"text": "✨ Neues Outfit erstellen", "callback_data": "outfit_create"}]]
                if edit_message_id:
                    await self.edit_message_text(chat_id, edit_message_id, text, buttons)
                else:
                    await self.send_message_with_buttons(chat_id, text, buttons)
                return

            # Build outfit list
            lines = [f"<b>👗 Outfits von {self.character_name}</b>\n"]
            buttons = []
            for outfit in outfits:
                oid = outfit.get("id", "")
                name = outfit.get("name", "Unbenannt")
                is_current = _is_current_preset(outfit)
                has_image = bool(outfit.get("image"))

                marker = " ✅" if is_current else ""
                lines.append(f"{'▸' if is_current else '▹'} <b>{name}</b>{marker}")

                # Row: [Anziehen] [Bild generieren] (if no image)
                row = []
                if not is_current:
                    row.append({"text": f"👗 {name}", "callback_data": f"outfit_wear:{oid}"})
                if not has_image:
                    row.append({"text": f"📸 Bild: {name}", "callback_data": f"outfit_img:{oid}"})
                if row:
                    buttons.append(row)

            # "Create new" button at bottom
            buttons.append([{"text": "✨ Neues Outfit erstellen", "callback_data": "outfit_create"}])

            text = "\n".join(lines)
            if edit_message_id:
                await self.edit_message_text(chat_id, edit_message_id, text, buttons)
            else:
                await self.send_message_with_buttons(chat_id, text, buttons)

                # Send outfit images only on first call (not on button-triggered refresh)
                for outfit in outfits:
                    img_name = outfit.get("image", "")
                    if img_name:
                        img_path = outfits_dir / img_name
                        if img_path.exists():
                            name = outfit.get("name", "")
                            is_current = _is_current_preset(outfit)
                            caption = f"{'✅ ' if is_current else ''}{name}"
                            await self._send_photo_file(chat_id, img_path, caption=caption)

        except Exception as e:
            logger.error("[%s] /outfits error: %s", self.character_name, e, exc_info=True)
            await self.send_message(chat_id, "Outfits konnten nicht geladen werden.", parse_mode="")

    # ------------------------------------------------------------------
    # Outfit callback handlers
    # ------------------------------------------------------------------

    async def _cb_outfit_wear(self, chat_id: int, message_id: int,
                              callback_id: str, outfit_id: str) -> None:
        """Switch to a different outfit — equips the preset's pieces."""
        from app.models.character import get_character_outfits
        from app.models.inventory import apply_equipped_pieces, get_item

        outfits = get_character_outfits(self.character_name)
        outfit = next((o for o in outfits if o.get("id") == outfit_id), None)
        if not outfit:
            await self.answer_callback(callback_id, "Outfit nicht gefunden")
            return

        # Preset.pieces ist eine Liste von item_ids — in slot->id mappen.
        piece_ids = list(outfit.get("pieces") or [])
        pieces_by_slot: Dict[str, str] = {}
        for pid in piece_ids:
            it = get_item(pid) or {}
            slot = ((it.get("outfit_piece") or {}).get("slot") or "").strip()
            if slot:
                pieces_by_slot[slot] = pid

        apply_equipped_pieces(self.character_name,
            pieces=pieces_by_slot,
            remove_slots=list(outfit.get("remove_slots") or []),
            source="telegram")

        await self.answer_callback(callback_id, f"✅ {outfit['name']} angezogen!")
        await self._handle_outfits(chat_id, edit_message_id=message_id)

    async def _cb_outfit_generate_image(self, chat_id: int, message_id: int,
                                        callback_id: str, outfit_id: str) -> None:
        """Generate an image for an outfit."""
        from app.models.character import (
            get_character_outfits, get_character_outfits_dir,
            get_character_appearance, resolve_outfit_placeholders,
            update_outfit_image, postprocess_outfit_image)

        outfits = get_character_outfits(self.character_name)
        outfit = next((o for o in outfits if o.get("id") == outfit_id), None)
        if not outfit:
            await self.answer_callback(callback_id, "Outfit nicht gefunden")
            return

        await self.answer_callback(callback_id, f"📸 Generiere Bild für {outfit['name']}...")
        await self.send_typing(chat_id)

        try:
            from app.core.dependencies import get_skill_manager

            sm = get_skill_manager()
            img_skill = sm.get_skill("image_generation")
            if not img_skill:
                await self.send_message(chat_id, "ImageGenerator nicht verfügbar.", parse_mode="")
                return

            # Build prompt: appearance + outfit. Prefix aus Admin-Config
            # (OUTFIT_IMAGE_PROMPT_PREFIX), Fallback "full body portrait".
            appearance = get_character_appearance(self.character_name)
            outfit_text = resolve_outfit_placeholders(
                outfit.get("outfit", ""), self.character_name
            )
            prefix = os.environ.get("OUTFIT_IMAGE_PROMPT_PREFIX", "full body portrait").strip()
            parts = [prefix, appearance, f"wearing {outfit_text}"]
            prompt_text = ", ".join(p for p in parts if p)

            # Skills expect JSON string with agent_name, user_id
            skill_input = json.dumps({
                "prompt": prompt_text,
                "agent_name": self.character_name,
                "user_id": self.user_id,
            })

            # Generate image
            import asyncio
            result = await asyncio.to_thread(img_skill.execute, skill_input)

            # Extract generated image path from result
            image_paths = self._extract_generated_images(result or "")
            if not image_paths:
                await self.send_message(chat_id, "Bild konnte nicht generiert werden.", parse_mode="")
                return

            src_path = image_paths[0]
            if not src_path.exists():
                await self.send_message(chat_id, "Generiertes Bild nicht gefunden.", parse_mode="")
                return

            # Move to outfits directory and post-process
            outfits_dir = get_character_outfits_dir(self.character_name)
            dst_path = outfits_dir / src_path.name
            import shutil
            shutil.move(str(src_path), str(dst_path))

            # Remove background + crop (may change filename to .png)
            # rembg/ONNX-Inferenz ist CPU-bound → Threadpool damit der Event-Loop nicht blockiert.
            try:
                dst_path = await asyncio.to_thread(postprocess_outfit_image, dst_path)
            except Exception as pp_err:
                logger.warning("[%s] Outfit image postprocess failed: %s", self.character_name, pp_err)

            # Update outfit record with image filename
            update_outfit_image(self.character_name, outfit_id, dst_path.name)

            # Send the image
            await self._send_photo_file(chat_id, dst_path, caption=f"📸 {outfit['name']}")
            logger.info("[%s] Outfit image generated: %s -> %s", self.character_name, outfit['name'], dst_path.name)

        except Exception as e:
            logger.error("[%s] Outfit image generation failed: %s", self.character_name, e, exc_info=True)
            await self.send_message(chat_id, "Fehler bei der Bildgenerierung.", parse_mode="")

    async def _cb_outfit_create(self, chat_id: int, message_id: int, callback_id: str) -> None:
        """Create a new outfit using the OutfitChange skill."""
        await self.answer_callback(callback_id, "✨ Erstelle neues Outfit...")
        await self.send_typing(chat_id)

        try:
            from app.core.dependencies import get_skill_manager

            sm = get_skill_manager()
            skill = sm.get_skill("outfit_change")
            if not skill:
                await self.send_message(chat_id, "OutfitChange Skill nicht verfügbar.", parse_mode="")
                return

            # Execute outfit creation (skill expects JSON with agent_name)
            import asyncio
            skill_input = json.dumps({
                "input": "create a new outfit",
                "agent_name": self.character_name,
                "user_id": self.user_id,
            })
            result = await asyncio.to_thread(skill.execute, skill_input)

            if result:
                clean = self._clean_for_telegram(result)
                if clean:
                    await self.send_message(chat_id, clean, parse_mode="")

                # Send outfit image if generated (skill returns ![Outfit](/absolute/path))
                for match in re.finditer(r'!\[[^\]]*\]\(([^)]+)\)', result):
                    img_path = Path(match.group(1))
                    if img_path.exists():
                        await self._send_photo_file(chat_id, img_path)
            else:
                await self.send_message(chat_id, "Outfit konnte nicht erstellt werden.", parse_mode="")

            # Show updated outfit list
            await self._handle_outfits(chat_id)

        except Exception as e:
            logger.error("[%s] Outfit creation failed: %s", self.character_name, e, exc_info=True)
            await self.send_message(chat_id, "Fehler beim Erstellen des Outfits.", parse_mode="")

    # ------------------------------------------------------------------
    # Chat message processing
    # ------------------------------------------------------------------

    async def _process_chat_message(self, chat_id: int, text: str, from_user: Dict[str, Any]) -> None:
        """Process an incoming message: send to LLM, return response via Telegram."""
        # Register if not yet mapped — always map to character owner
        from app.models.telegram_channel import get_telegram_channel
        telegram = get_telegram_channel()
        if chat_id not in telegram.chat_to_user_mapping:
            telegram.register_user(chat_id)

        # Show typing indicator
        await self.send_typing(chat_id)

        tool_image_urls = []
        try:
            response_text, tool_image_urls = await self._generate_response(text)
        except Exception as e:
            logger.error("[%s] Response generation failed: %s", self.character_name, e, exc_info=True)
            response_text = "Entschuldigung, da ist etwas schiefgelaufen. Versuche es nochmal."

        if response_text:
            # Clean meta-tags that make no sense in Telegram
            clean = self._clean_for_telegram(response_text)
            if clean:
                await self.send_message(chat_id, clean, parse_mode="")

            # Send generated images/videos as Telegram media (from ToolResultEvent)
            media_paths = self._extract_generated_images("\n".join(tool_image_urls))
            for media_path in media_paths:
                if not media_path.exists():
                    logger.warning("[%s] Generated media not found: %s", self.character_name, media_path)
                    continue
                if media_path.suffix.lower() == ".mp4":
                    await self._send_video_file(chat_id, media_path)
                else:
                    await self._send_photo_file(chat_id, media_path)

            # Save to unified chat history (use owner_id so Web and Telegram share history)
            if clean:
                self._save_chat_history(text, clean)

    async def _generate_response(self, user_input: str) -> tuple:
        """Generate agent response using the shared chat engine.

        Returns (response_text, tool_image_urls) where tool_image_urls are
        markdown image references extracted from ToolResultEvent.
        """
        from app.core.chat_engine import build_chat_context, post_process_response
        from app.core.streaming import StreamingAgent, ContentEvent, ToolResultEvent
        from app.core.llm_queue import get_llm_queue

        ctx = build_chat_context("", self.character_name, user_input, channel="telegram",
            speaker="user", medium="telegram")

        if ctx["llm"] is None:
            return "LLM nicht verfügbar. Bitte Konfiguration prüfen.", []

        # Register in LLM queue (same as web chat — shows in task queue/dashboard)
        from app.core.llm_router import resolve_llm as _resolve_tg
        _llm_queue = get_llm_queue()
        _llm_inst = _resolve_tg("chat_stream", agent_name=self.character_name)
        _chat_task_id = await _llm_queue.register_chat_active_async(
            self.character_name, llm_instance=_llm_inst,
            task_type="telegram_chat", label=f"Telegram: {self.character_name}")

        agent = StreamingAgent(
            llm=ctx["llm"],
            tool_format=ctx["tool_format"],
            tools_dict=ctx["tools_dict"],
            agent_name=self.character_name,
            max_iterations=ctx["max_iterations"],
            tool_llm=ctx["tool_llm"],
            log_task="telegram_chat",
            mode=ctx.get("mode", "no_tools"))

        # Tool executor: release queue during tool execution (prevents deadlock)
        _chat_state = {"task_id": _chat_task_id}

        async def _tool_executor(tool_name, tool_input):
            if _chat_state["task_id"]:
                _llm_queue.register_chat_done(_chat_state["task_id"])
                _chat_state["task_id"] = None
            try:
                tool_func = ctx["tools_dict"][tool_name]
                return await asyncio.to_thread(tool_func, tool_input)
            finally:
                _chat_state["task_id"] = await _llm_queue.register_chat_active_async(
                    self.character_name, llm_instance=_llm_inst,
                    task_type="telegram_chat", label=f"Telegram: {self.character_name}")

        agent.tool_executor = _tool_executor

        try:
            # Collect full response and tool result image URLs
            full_response = ""
            tool_image_urls = []
            async for event in agent.stream(ctx["system_content"], ctx["messages"], user_input):
                if isinstance(event, ContentEvent):
                    full_response += event.content
                elif isinstance(event, ToolResultEvent):
                    # Extract image URLs from tool results (same as web chat)
                    if event.result:
                        for _m in re.finditer(r'!\[[^\]]*\]\(/characters/[^)]+\)', event.result):
                            tool_image_urls.append(_m.group(0))
        finally:
            # Always unregister from queue
            if _chat_state["task_id"]:
                _llm_queue.register_chat_done(_chat_state["task_id"])

        # Post-processing: mood, location, memory, relationships (same as web chat)
        post_process_response(
            character_name=self.character_name,
            user_input=user_input,
            full_response=full_response,
            agent_config=ctx["agent_config"],
            llm=ctx["llm"],
            user_display_name=ctx["user_display_name"],
            full_chat_history=ctx["full_chat_history"],
            old_history=ctx.get("old_history"))

        return full_response, tool_image_urls

    def _extract_generated_images(self, text: str) -> list:
        """Extract local image file paths from markdown image syntax in tool results.

        Tool results contain e.g.: ![Generated Image 1](/characters/Kira/images/Kira_123_abc_1.png?user_id=Kai)
        Convert these to local storage paths.
        """
        from app.models.character import get_character_images_dir
        paths = []
        for match in re.finditer(r'!\[[^\]]*\]\(/characters/([^/]+)/images/([^?)]+)', text):
            character_name = match.group(1)
            filename = match.group(2)
            img_path = get_character_images_dir(character_name) / filename
            paths.append(img_path)
        return paths

    def _clean_for_telegram(self, text: str) -> str:
        """Remove meta-tags (mood, location, activity, intent) from response."""
        clean = text
        clean = re.sub(r'\n?\s*\*\*I\s+feel\s+[^*]+\*\*\s*', '', clean, flags=re.IGNORECASE)
        clean = re.sub(r'\n?\s*\*\*I\s+am\s+at\s+[^*]+\*\*\s*', '', clean, flags=re.IGNORECASE)
        clean = re.sub(r'\n?\s*\*\*I\s+do\s+[^*]+\*\*\s*', '', clean, flags=re.IGNORECASE)
        # Strip intent tags
        clean = re.sub(r'\n?\s*\[INTENT:[^\]]*\]\s*', '', clean)
        # Strip tool hallucinations
        clean = re.sub(r'<tool_call>.*?</tool_call>', '', clean, flags=re.DOTALL)
        clean = re.sub(r'</?tool_(?:call|result)>', '', clean)
        # Strip image markdown (won't render in Telegram plain text)
        clean = re.sub(r'!\[[^\]]*\]\([^)]+\)', '', clean)
        return clean.strip()

    def _save_chat_history(self, user_input: str, response: str) -> None:
        """Save both messages to unified chat history with Telegram channel marker."""
        try:
            from app.models.unified_chat import UnifiedChatManager
            from app.models.channel import Message, ChannelType
            from app.models.character import get_character_config
            timestamp = datetime.now().isoformat()

            # Partner = avatar controlled by the human on the Telegram side.
            # Comes from the bot's character config (telegram_partner_character).
            # Leerer Partner waere Limbo — Nachricht landet ohne Avatar-Tag.
            cfg = get_character_config(self.character_name) or {}
            partner = (cfg.get("telegram_partner_character") or "").strip()

            user_msg = Message(
                content=user_input,
                role="user",
                channel=ChannelType.TELEGRAM,
                timestamp=timestamp,
                speaker="user",
                medium="telegram")
            assistant_msg = Message(
                content=response,
                role="assistant",
                channel=ChannelType.TELEGRAM,
                timestamp=timestamp,
                speaker=self.character_name,
                medium="telegram")
            UnifiedChatManager.save_message(user_msg, self.character_name,
                                            partner_name=partner)
            UnifiedChatManager.save_message(assistant_msg, self.character_name,
                                            partner_name=partner)
        except Exception as e:
            logger.error("[%s] Failed to save chat history: %s", self.character_name, e)


# ======================================================================
# Polling Manager — manages all character bot pollers
# ======================================================================

class TelegramPollingManager:
    """
    Scans all users/characters for telegram_bot_token and starts
    a polling loop per unique bot token.
    """

    def __init__(self):
        self._pollers: Dict[str, CharacterBotPoller] = {}  # key: "user_id/character_name"
        self._started = False

    @property
    def pollers(self) -> Dict[str, CharacterBotPoller]:
        return dict(self._pollers)

    async def start(self) -> None:
        """Scan all characters and start pollers for those with bot tokens."""
        if self._started:
            return
        self._started = True

        from app.models.character import list_available_characters, get_character_config

        seen_tokens: Set[str] = set()

        try:
            characters = list_available_characters()
        except Exception:
            characters = []

        for char_name in characters:
            config = get_character_config(char_name)
            token = config.get("telegram_bot_token", "").strip()
            if not token:
                continue

            if token in seen_tokens:
                logger.warning("Duplicate bot token for %s — skipping", char_name)
                continue
            seen_tokens.add(token)

            poller = CharacterBotPoller(char_name, token)
            ok = await poller.start()
            if ok:
                self._pollers[char_name] = poller
                logger.info("Started Telegram bot for %s", char_name)
            else:
                logger.error("Failed to start Telegram bot for %s", char_name)

        logger.info("Telegram Polling Manager started: %d bot(s) active", len(self._pollers))

    async def stop(self) -> None:
        """Stop all polling loops."""
        for key, poller in self._pollers.items():
            await poller.stop()
        self._pollers.clear()
        self._started = False
        logger.info("Telegram Polling Manager stopped")

    async def start_character(self, character_name: str) -> bool:
        """Start polling for a specific character (e.g. after config change)."""
        from app.models.character import get_character_config
        config = get_character_config(character_name)
        token = config.get("telegram_bot_token", "").strip()
        if not token:
            return False

        key = character_name

        # Stop existing if running
        if key in self._pollers:
            await self._pollers[key].stop()

        poller = CharacterBotPoller(character_name, token)
        ok = await poller.start()
        if ok:
            self._pollers[key] = poller
        return ok

    async def stop_character(self, character_name: str) -> None:
        """Stop polling for a specific character."""
        key = character_name
        poller = self._pollers.pop(key, None)
        if poller:
            await poller.stop()

    def get_status(self) -> Dict[str, Any]:
        """Return status of all active pollers."""
        bots = {}
        for key, poller in self._pollers.items():
            bots[key] = {
                "character": poller.character_name,
                "user_id": poller.user_id,
                "running": poller._running,
                "bot_username": poller._bot_info.get("username", "") if poller._bot_info else "",
            }
        return {
            "active_bots": len(self._pollers),
            "bots": bots,
        }


# ======================================================================
# Global singleton
# ======================================================================

_polling_manager: Optional[TelegramPollingManager] = None


def get_polling_manager() -> TelegramPollingManager:
    global _polling_manager
    if _polling_manager is None:
        _polling_manager = TelegramPollingManager()
    return _polling_manager
