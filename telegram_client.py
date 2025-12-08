"""
Async Telegram client for sending messages and photos
"""
import logging
from typing import Optional
import aiohttp


class TelegramClient:
    """Async Telegram bot client"""
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        
    async def send_message(
        self, 
        text: str, 
        reply_to_message_id: Optional[int] = None
    ) -> Optional[int]:
        """
        Send text message to Telegram
        Returns message_id or None if failed
        """
        if not self.bot_token or not self.chat_id:
            logging.warning("Telegram credentials not set, skipping message")
            return None
        
        url = f"{self.base_url}/sendMessage"
        data = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": False,
        }
        
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = reply_to_message_id
            data["allow_sending_without_reply"] = True
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if not resp.ok:
                        text = await resp.text()
                        logging.warning("Failed to send Telegram message: %s", text)
                        return None
                    
                    js = await resp.json()
                    return js.get("result", {}).get("message_id")
        except Exception as e:
            logging.exception("Telegram message error: %s", e)
            return None
    
    async def send_photo(
        self,
        photo_bytes: bytes,
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None
    ) -> Optional[int]:
        """
        Send photo to Telegram
        Returns message_id or None if failed
        """
        if not self.bot_token or not self.chat_id:
            logging.warning("Telegram credentials not set, skipping photo")
            return None
        
        url = f"{self.base_url}/sendPhoto"
        data = aiohttp.FormData()
        data.add_field("chat_id", self.chat_id)
        data.add_field("photo", photo_bytes, filename="chart.png", content_type="image/png")
        
        if caption:
            data.add_field("caption", caption)
        if reply_to_message_id is not None:
            data.add_field("reply_to_message_id", str(reply_to_message_id))
            data.add_field("allow_sending_without_reply", "true")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if not resp.ok:
                        text = await resp.text()
                        logging.warning("Failed to send Telegram photo: %s", text)
                        return None
                    
                    js = await resp.json()
                    return js.get("result", {}).get("message_id")
        except Exception as e:
            logging.exception("Telegram photo error: %s", e)
            return None
