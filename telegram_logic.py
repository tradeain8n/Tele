import asyncio
import datetime
import json
import os
from typing import List, Optional

from telethon import TelegramClient, events
from telethon.errors.rpcerrorlist import (
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import MessageService, MessageMediaPhoto, MessageMediaDocument


class TelegramLogic:
    PROGRESS_FILE = "progress.json"

    def __init__(
        self,
        api_id,
        api_hash,
        log_callback=None,
        auth_callback=None,
        start_date: datetime.datetime = None,
    ):
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.log_callback = log_callback
        self.auth_callback = auth_callback
        self.start_date = start_date
        self.session_name = "cloner_session"
        self.progress = self._load_progress()
        self.is_running = False

    def log(self, message: str):
        if self.log_callback:
            self.log_callback(message)

    def _load_progress(self):
        if not os.path.exists(self.PROGRESS_FILE):
            return {}
        try:
            with open(self.PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_progress(self):
        with open(self.PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.progress, f, ensure_ascii=False, indent=2)

    def _update_progress(self, source_id, message_id):
        self.progress[str(source_id)] = message_id
        self._save_progress()

    async def _authorize(self, client: TelegramClient):
        if await client.is_user_authorized():
            self.log("Авторизация уже выполнена.")
            return

        self.log("Требуется авторизация...")
        phone = await client.loop.run_in_executor(None, self.auth_callback, "phone")
        if not phone:
            raise Exception("Авторизация отменена (телефон).")
        await client.send_code_request(phone)

        try:
            code = await client.loop.run_in_executor(None, self.auth_callback, "code")
            if not code:
                raise Exception("Авторизация отменена (код).")
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            self.log("Требуется пароль 2FA.")
            password = await client.loop.run_in_executor(None, self.auth_callback, "password")
            if not password:
                raise Exception("Пароль 2FA не введён.")
            await client.sign_in(password=password)
        except PhoneCodeInvalidError as exc:
            raise Exception(f"Неверный код: {exc}")
        self.log("Авторизация прошла успешно.")

    async def _send_message_copy(self, client, message, target_id):
        if not message:
            return
        try:
            if message.media:
                file_path = await message.download_media(file=bytes)
                caption = message.text or message.message
                await client.send_file(
                    target_id,
                    file=file_path,
                    caption=caption,
                    link_preview=False,
                    voice_note=message.voice_note if hasattr(message, "voice_note") else None,
                )
            else:
                await client.send_message(
                    target_id,
                    message.text or message.message,
                    link_preview=False,
                )
        except Exception as exc:
            # Fallback: обычное forward (без as_copy, без send_file)
            try:
                await client.forward_messages(entity=target_id, messages=message, from_peer=message.chat_id)
            except Exception as inner:
                self.log(f"Невозможно переслать сообщение {message.id}: {inner}")
                raise exc

    async def _migrate_source(self, client, source_id: int, target_id: int):
        source_key = str(source_id)
        self.log(f"Начинаем миграцию из {source_id}.")
        iterator_kwargs = {"reverse": True}

        if self.start_date:
            iterator_kwargs["offset_date"] = self.start_date
            self.log(f"Фильтр: с {self.start_date.strftime('%d.%m.%Y')}.")
        else:
            last_id = self.progress.get(source_key, 0)
            iterator_kwargs["offset_id"] = last_id
            self.log(f"Продолжаем после ID: {last_id}.")

        async for message in client.iter_messages(source_id, **iterator_kwargs):
            if not self.is_running:
                break
            if isinstance(message, MessageService):
                self._update_progress(source_key, message.id)
                continue
            try:
                await self._send_message_copy(client, message, target_id)
                self._update_progress(source_key, message.id)
                self.log(f"Сообщение {message.id} из {source_id} скопировано.")
                await asyncio.sleep(2)
            except FloodWaitError as exc:
                self.log(f"FloodWait: ждём {exc.seconds}s.")
                await asyncio.sleep(exc.seconds)
            except Exception as exc:
                self.log(f"Ошибка при копировании {message.id}: {exc}")
                self._update_progress(source_key, message.id)
                await asyncio.sleep(5)

    async def _monitor(self, client, target_id: int, source_ids: List[int]):
        self.log("Отслеживание новых постов запущено.")

        async def handler(event):
            if not self.is_running:
                return
            msg = event.message
            if isinstance(msg, MessageService):
                return
            try:
                await self._send_message_copy(client, msg, target_id)
                self._update_progress(event.chat_id, msg.id)
                self.log(f"Новый пост {msg.id} с {event.chat_id} скопирован.")
            except FloodWaitError as exc:
                self.log(f"FloodWait (новый пост): {exc.seconds}s.")
            except Exception as exc:
                self.log(f"Ошибка нового поста {msg.id}: {exc}")

        client.add_event_handler(handler, events.NewMessage(chats=source_ids))

        while self.is_running:
            await asyncio.sleep(1)

        client.remove_event_handler(handler)
        self.log("Отслеживание завершено.")

    async def _execute(self, source_ids, target_id):
        client = TelegramClient(self.session_name, self.api_id, self.api_hash)
        await client.connect()
        try:
            await self._authorize(client)
            for source in source_ids:
                if not self.is_running:
                    break
                await self._migrate_source(client, source, target_id)
            if self.is_running:
                await self._monitor(client, target_id, source_ids)
        finally:
            await client.disconnect()
            self.log("Сессия закрыта.")

    def start_migration(self, source_ids, target_id):
        if not source_ids:
            raise RuntimeError("Нет источников.")
        self.is_running = True
        try:
            asyncio.run(self._execute(source_ids, target_id))
        finally:
            self.is_running = False

    def stop(self):
        self.is_running = False
