"""
Telegram Routes - Multi-Channel Chat Integration

Endpoints für Telegram Bot Webhook und User Registration
"""
from fastapi import APIRouter, Request, HTTPException
from typing import Dict, Any
import json
from app.core.log import get_logger

logger = get_logger("telegram")

from app.models.telegram_channel import get_telegram_channel
from app.models.unified_chat import get_unified_chat_manager
from app.models.channel import Message, ChannelType
from app.models.account import save_user_name

router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post("/webhook")
async def telegram_webhook(request: Request) -> Dict[str, Any]:
    """
    Webhook Endpoint für Telegram Bot
    
    Telegram sendet Updates hierher via HTTP POST
    
    Konfigurieren mit:
    curl -X POST https://api.telegram.org/botXXX:YYY/setWebhook \
         -d "url=https://yourdomain.com/telegram/webhook"
    """
    try:
        data = await request.json()
        
        # Validiere dass es ein Update ist
        if "update_id" not in data:
            return {"ok": True}  # Ignoriere ungültige Updates
        
        # Leite an Telegram Channel weiter
        telegram = get_telegram_channel()
        await telegram.handle_webhook(data)
        
        return {"ok": True}
        
    except Exception as e:
        logger.error("Webhook error: %s", e)
        return {"ok": False, "error": str(e)}


@router.post("/register/{user_id}")
async def register_telegram_user(request: Request) -> Dict[str, Any]:
    """
    Registriere Telegram Chat-ID für einen User
    
    Dies verbindet einen System-User mit seiner Telegram-ID
    
    Request Body:
    {
        "chat_id": 123456789,
        "first_name": "Kai"  # Optional
    }
    
    Beispiel:
    curl -X POST http://localhost:8000/telegram/register/kai_user \
         -H "Content-Type: application/json" \
         -d "{\"chat_id\": 123456789, \"first_name\": \"Kai\"}"
    """
    try:
        data = await request.json()
        
        chat_id = data.get("chat_id")
        if not chat_id:
            raise HTTPException(
                status_code=400,
                detail="chat_id erforderlich"
            )
        
        # Speichere Display Name falls vorhanden
        first_name = data.get("first_name")
        if first_name:
            save_user_name(first_name)
        
        # Registriere Chat-ID Mapping
        telegram = get_telegram_channel()
        telegram.register_user(int(chat_id))
        
        # Sende Willkommensnachricht zu Telegram
        await telegram.send_message(
            character_name="System",
            content="✓ Telegram erfolgreich registriert!\nDu kannst jetzt mit deinem Agenten chatten.",
            chat_id=int(chat_id),
            parse_mode="HTML"
        )
        
        return {
            "ok": True,
            "message": f"Telegram Chat {chat_id} registriert"
        }
        
    except Exception as e:
        logger.error("Register error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def telegram_status() -> Dict[str, Any]:
    """
    Health Check für Telegram Channel und Polling-Status

    Zeigt alle aktiven Bot-Pollers (pro Character) und den Channel-Status.
    """
    try:
        from app.core.telegram_polling import get_polling_manager
        pm = get_polling_manager()
        polling_status = pm.get_status()

        telegram = get_telegram_channel()

        return {
            "ok": True,
            "polling": polling_status,
            "registered_users": len(telegram.chat_to_user_mapping),
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


@router.post("/polling/start/{user_id}/{character_name}")
async def start_polling(character_name: str) -> Dict[str, Any]:
    """
    Starte Telegram Long Polling für einen Character.

    Der Character muss einen telegram_bot_token in seiner Config haben.
    """
    from app.core.telegram_polling import get_polling_manager
    pm = get_polling_manager()
    ok = await pm.start_character(character_name)
    if ok:
        return {"ok": True, "message": f"Polling gestartet für {character_name}"}
    return {"ok": False, "message": f"Konnte Polling nicht starten für {character_name} — kein Bot-Token oder ungültig"}


@router.post("/polling/stop/{user_id}/{character_name}")
async def stop_polling(character_name: str) -> Dict[str, Any]:
    """
    Stoppe Telegram Long Polling für einen Character.
    """
    from app.core.telegram_polling import get_polling_manager
    pm = get_polling_manager()
    await pm.stop_character(character_name)
    return {"ok": True, "message": f"Polling gestoppt für {character_name}"}


@router.post("/send_to_chat/{user_id}")
async def send_to_telegram(request: Request) -> Dict[str, Any]:
    """
    Sende Nachricht direktzum Telegram Chat eines Users
    
    Dies wird normalerweise vom Agent aufgerufen
    
    Request Body:
    {
        "content": "Die Nachricht",
        "agent_name": "Alfred",
        "parse_mode": "HTML"  # Optional
    }
    
    Beispiel:
    curl -X POST http://localhost:8000/telegram/send_to_chat/kai_user \
         -H "Content-Type: application/json" \
         -d "{\"content\": \"Hallo!\", \"agent_name\": \"Alfred\"}"
    """
    try:
        data = await request.json()
        
        content = data.get("content")
        character_name = data.get("agent_name", "Agent")
        parse_mode = data.get("parse_mode", "HTML")
        
        if not content:
            raise HTTPException(status_code=400, detail="content erforderlich")
        
        # Hole registrierte Chat-ID (single-world: erste verfuegbare)
        telegram = get_telegram_channel()
        chat_id = next(iter(telegram.chat_to_user_mapping), None)

        if not chat_id:
            raise HTTPException(
                status_code=404,
                detail="Keine Telegram Chat-ID registriert"
            )
        
        # Sende Nachricht
        message_id = await telegram.send_message(
            character_name=character_name,
            content=content,
            chat_id=chat_id,
            parse_mode=parse_mode
        )
        
        # Speichere im Chat-Verlauf
        unified_manager = get_unified_chat_manager()
        message = Message(
            content=content,
            role="assistant",
            channel=ChannelType.TELEGRAM,
            channel_message_id=message_id,
            metadata={"chat_id": chat_id}
        )
        unified_manager.UnifiedChatManager.save_message(message, character_name)
        
        return {
            "ok": True,
            "message_id": message_id,
            "chat_id": chat_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Send error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chat_history/{user_id}/{character_name}")
async def get_telegram_chat_history(character_name: str) -> Dict[str, Any]:
    """
    Hole Chat-Verlauf für einen User gefiltert nach Telegram Kanal
    
    Query Parameters:
    - limit: Maximale Anzahl von Nachrichten (default: 50)
    
    Beispiel:
    curl http://localhost:8000/telegram/chat_history/kai_user/Alfred?limit=20
    """
    try:
        limit = None  # Nicht begrenzt
        
        unified_manager = get_unified_chat_manager()
        history = unified_manager.UnifiedChatManager.get_chat_history(character_name,
            channel=ChannelType.TELEGRAM,
            limit=limit
        )
        
        # Konvertiere zu Dict für JSON
        messages = [msg.to_dict() for msg in history]
        
        return {
            "ok": True,
            "user_id": "",
            "agent_name": character_name,
            "channel": "telegram",
            "message_count": len(messages),
            "messages": messages
        }
        
    except Exception as e:
        logger.error("History error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
