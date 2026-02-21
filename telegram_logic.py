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
from telethon.tl.types import MessageService, MessageMediaWebPage

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


class TelegramLogic:
    PROGRESS_FILE = "progress.json"
    TEMP_DIR = "temp_media"

    def __init__(
        self,
        api_id,
        api_hash,
        log_callback=None,
        auth_callback=None,
        start_date: Optional[datetime.datetime] = None,
    ):
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.log_callback = log_callback
        self.auth_callback = auth_callback
        self.start_date = start_date
        self.session_name = "cloner_session"
        self.progress = self._load_progress()
        self.is_running = False
        os.makedirs(self.TEMP_DIR, exist_ok=True)

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

    def _split_text(self, text: str) -> List[str]:
        chunks = []
        while text:
            chunks.append(text[:TEXT_LIMIT])
            text = text[TEXT_LIMIT:]
        return chunks

    def _trim_caption(self, text: Optional[str]):
        if not text:
            return None, None
        if len(text) <= CAPTION_LIMIT:
            return text, None
        return text[:CAPTION_LIMIT], text[CAPTION_LIMIT:]

    async def _send_text(self, client, target_id, message):
        if not message.message:
            return
        chunks = self._split_text(message.message)
        if not chunks:
            return

        await client.send_message(
            target_id,
            chunks[0],
            formatting_entities=message.entities,
            link_preview=isinstance(message.media, MessageMediaWebPage),
            silent=message.silent,
        )
        for chunk in chunks[1:]:
            await client.send_message(
                target_id,
                chunk,
                link_preview=False,
                silent=message.silent,
            )

    async def _send_media(self, client, target_id, message):
        file_path = await message.download_media(file=self.TEMP_DIR)
        caption = message.message or None
        caption_trimmed, caption_rest = self._trim_caption(caption)
        try:
            await client.send_file(
                target_id,
                file=file_path,
                caption=caption_trimmed,
                caption_entities=message.entities if caption_trimmed else None,
                link_preview=False,
                silent=message.silent,
                supports_streaming=bool(message.video),
                voice_note=bool(message.voice),
                video_note=bool(message.video_note),
                spoiler=bool(getattr(message, "has_spoiler", False)),
            )
        except Exception as exc:
            self.log(f"Ошибка send_file для {message.id}: {exc}")
            await client.forward_messages(
                entity=target_id,
                messages=message,
                from_peer=message.chat_id,
            )
            return

        if caption_rest:
            rest_chunks = self._split_text(caption_rest)
            for chunk in rest_chunks:
                await client.send_message(
                    target_id,
                    chunk,
                    link_preview=False,
                    silent=message.silent,
                )

        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

    async def _copy_message(self, client, target_id, message):
        if isinstance(message, MessageService):
            return
        if message.media:
            await self._send_media(client, target_id, message)
            return
        await self._send_text(client, target_id, message)

    async def _send_album(self, client, target_id, messages):
        files = []
        for msg in sorted(messages, key=lambda m: m.id):
            path = await msg.download_media(file=self.TEMP_DIR)
            if path:
                files.append(path)
        if not files:
            return
        caption_msg = next((msg for msg in messages if msg.message), None)
        caption = caption_msg.message if caption_msg else None
        caption_trimmed, caption_rest = self._trim_caption(caption)
        try:
            await client.send_file(
                target_id,
                file=files,
                caption=caption_trimmed,
                caption_entities=caption_msg.entities if caption_trimmed and caption_msg else None,
                supports_streaming=any(msg.video for msg in messages),
                spoiler=any(getattr(msg, "has_spoiler", False) for msg in messages),
                silent=caption_msg.silent if caption_msg else False,
            )
        except Exception as exc:
            self.log(f"Ошибка send_file альбома: {exc}")
            await client.forward_messages(
                entity=target_id,
                messages=[msg.id for msg in messages],
                from_peer=messages[-1].chat_id,
            )
        if caption_rest and caption_msg:
            rest_chunks = self._split_text(caption_rest)
            for chunk in rest_chunks:
                await client.send_message(
                    target_id,
                    chunk,
                    link_preview=False,
                    silent=caption_msg.silent,
                )
        for path in files:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

    async def _migrate_source(self, client, source_id: int, target_id: int):
        source_key = str(source_id)
        self.log(f"Начинаем миграцию из источника {source_id}")
        iterator_kwargs = {"reverse": True}
        if self.start_date:
            iterator_kwargs["offset_date"] = self.start_date
            self.log(f"Копируем с даты {self.start_date.strftime('%d.%m.%Y')}")
        else:
            last_id = self.progress.get(source_key, 0)
            iterator_kwargs["offset_id"] = last_id
            self.log(f"Продолжаем с сообщения после ID {last_id}")

        current_group = None
        group_buffer = []

        async for message in client.iter_messages(source_id, **iterator_kwargs):
            if not self.is_running:
                break
            if isinstance(message, MessageService):
                self._update_progress(source_key, message.id)
                continue
            if message.grouped_id:
                if current_group is None:
                    current_group = message.grouped_id
                if message.grouped_id == current_group:
                    group_buffer.append(message)
                    continue
                else:
                    await self._send_album(client, target_id, group_buffer)
                    self._update_progress(source_key, max(m.id for m in group_buffer))
                    group_buffer = [message]
                    current_group = message.grouped_id
                    continue
            if group_buffer:
                await self._send_album(client, target_id, group_buffer)
                self._update_progress(source_key, max(m.id for m in group_buffer))
                group_buffer = []
                current_group = None
            try:
                await self._copy_message(client, target_id, message)
                self._update_progress(source_key, message.id)
                self.log(f"Скопировано сообщение {message.id} источника {source_id}")
                await asyncio.sleep(2)
            except FloodWaitError as flood_exc:
                self.log(f"FloodWait: ждём {flood_exc.seconds} сек.")
                await asyncio.sleep(flood_exc.seconds)
            except Exception as exc:
                self.log(f"Ошибка при копировании {message.id}: {exc}")
                self._update_progress(source_key, message.id)
                await asyncio.sleep(5)

        if group_buffer:
            await self._send_album(client, target_id, group_buffer)
            self._update_progress(source_key, max(m.id for m in group_buffer))

    async def _monitor_new_posts(self, client, target_id: int, source_ids: List[int]):
        self.log("Отслеживание новых постов запущено.")

        async def handler(event):
            if not self.is_running:
                return
            message = event.message
            if isinstance(message, MessageService) or message.grouped_id:
                return
            try:
                await self._copy_message(client, target_id, message)
                self._update_progress(str(event.chat_id), message.id)
            except FloodWaitError as flood_exc:
                self.log(f"FloodWait при новом посте: {flood_wait.seconds}s.")
            except Exception as exc:
                self.log(f"Ошибка нового поста {message.id}: {exc}")

        async def album_handler(event):
            if not self.is_running:
                return
            try:
                await self._send_album(client, target_id, event.messages)
                self._update_progress(str(event.chat_id), max(m.id for m in event.messages))
            except FloodWaitError as flood_exc:
                self.log(f"FloodWait альбома: {flood_exc.seconds}s.")
            except Exception as exc:
                self.log(f"Ошибка альбома: {exc}")

        client.add_event_handler(handler, events.NewMessage(chats=source_ids))
        client.add_event_handler(album_handler, events.Album(chats=source_ids))

        while self.is_running:
            await asyncio.sleep(1)

        client.remove_event_handler(handler)
        client.remove_event_handler(album_handler)
        self.log("Отслеживание новых постов остановлено.")

    async def _run(self, source_ids: List[int], target_id: int):
        client = TelegramClient(self.session_name, self.api_id, self.api_hash)
        await client.connect()
        try:
            await self._authorize(client)
            for source in source_ids:
                if not self.is_running:
                    break
                await self._migrate_source(client, source, target_id)
            if self.is_running:
                await self._monitor_new_posts(client, target_id, source_ids)
        finally:
            await client.disconnect()
            self.log("Соединение с Telegram закрыто.")

    def start_migration(self, source_ids: List[int], target_id: int):
        if not source_ids:
            raise ValueError("Не указан ни один источник.")
        self.is_running = True
        try:
            asyncio.run(self._run(source_ids, target_id))
        except Exception as exc:
            self.log(f"Критическая ошибка: {exc}")
            raise
        finally:
            self.is_running = False

    def stop(self):
        self.is_running = False
