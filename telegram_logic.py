import asyncio
import datetime
import json
import os
import time
from typing import List, Optional

import telethon
from telethon import TelegramClient, events
from telethon.errors.rpcerrorlist import (
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import MessageService, MessageMediaWebPage

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
TEMP_DIR = "temp_media"


class TelegramLogic:
    PROGRESS_FILE = "progress.json"
    SESSION_NAME = "cloner_session"

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
        self.progress = self._load_progress()
        self.is_running = False
        os.makedirs(TEMP_DIR, exist_ok=True)
        self.log_state(f"Telethon v{telethon.__version__}")

    def log(self, message: str):
        if self.log_callback:
            self.log_callback(message)

    def log_state(self, message: str):
        self.log(f"[state] {message}")

    def log_exception(self, message: str, exc: Exception):
        self.log(f"[error] {message}: {type(exc).__name__} - {exc}")

    def _load_progress(self):
        if not os.path.exists(self.PROGRESS_FILE):
            return {}
        try:
            with open(self.PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            self.log_exception("Не удалось загрузить progress.json", exc)
            return {}

    def _save_progress(self):
        try:
            with open(self.PROGRESS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.progress, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.log_exception("Не удалось сохранить прогресс", exc)

    def _update_progress(self, source_id, message_id):
        self.progress[str(source_id)] = message_id
        self._save_progress()

    def _message_meta(self, message):
        text = message.message or "<пустой текст>"
        snippet = text[:120].replace("\n", " ")
        entities = len(message.entities or [])
        media_type = type(message.media).__name__ if message.media else "нет медиа"
        return f"ID={message.id}, len={len(text)}, entities={entities}, media={media_type}, snippet={snippet!r}"

    async def _authorize(self, client: TelegramClient):
        if await client.is_user_authorized():
            self.log_state("Авторизация уже завершена, приветствие сессии.")
            return

        self.log_state("Требуется авторизация.")
        phone = await client.loop.run_in_executor(None, self.auth_callback, "phone")
        if not phone:
            raise Exception("Телефон не передан.")

        await client.send_code_request(phone)

        try:
            code = await client.loop.run_in_executor(None, self.auth_callback, "code")
            if not code:
                raise Exception("Код не получен.")
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            self.log_state("Телефон требует пароль 2FA.")
            password = await client.loop.run_in_executor(None, self.auth_callback, "password")
            if not password:
                raise Exception("Пароль 2FA не передан.")
            await client.sign_in(password=password)
        except PhoneCodeInvalidError as exc:
            raise Exception(f"Неверный код: {exc}")
        self.log_state("Авторизация завершена успешно.")

    def _split_text(self, text: str) -> List[str]:
        return [text[i : i + TEXT_LIMIT] for i in range(0, len(text), TEXT_LIMIT)]

    def _trim_caption(self, text: Optional[str]):
        if not text:
            return None, None
        if len(text) <= CAPTION_LIMIT:
            return text, None
        return text[:CAPTION_LIMIT], text[CAPTION_LIMIT:]

    async def _send_text(self, client, target_id, message):
        text = message.message
        if not text:
            return
        await client.send_message(
            target_id,
            text,
            formatting_entities=message.entities,
            link_preview=isinstance(message.media, MessageMediaWebPage),
            silent=message.silent,
        )

    async def _send_media(self, client, target_id, message):
        file_path = await message.download_media(file=TEMP_DIR)
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
            self.log_exception(f"send_file упал для сообщения {message.id}", exc)
            await client.forward_messages(
                entity=target_id,
                messages=message,
                from_peer=message.chat_id,
            )
        finally:
            if caption_rest:
                await client.send_message(
                    target_id,
                    caption_rest,
                    link_preview=False,
                    silent=message.silent,
                )
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as exc:
                    self.log_exception("Не удалось удалить temp-файл", exc)

    async def _send_album(self, client, target_id, messages):
        files = []
        for msg in sorted(messages, key=lambda m: m.id):
            path = await msg.download_media(file=TEMP_DIR)
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
            self.log_exception("send_file альбома упал", exc)
            await client.forward_messages(
                entity=target_id,
                messages=[msg.id for msg in messages],
                from_peer=messages[-1].chat_id,
            )
        if caption_rest and caption_msg:
            await client.send_message(
                target_id,
                caption_rest,
                link_preview=False,
                silent=caption_msg.silent,
            )
        for file_path in files:
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as exc:
                    self.log_exception("Не удалось удалить файл альбома", exc)

    async def _copy_message(self, client, message, target_id: int):
        if isinstance(message, MessageService):
            return
        if message.grouped_id:
            return  # альбомы обрабатываются отдельно
        meta = self._message_meta(message)
        self.log_state(f"Копируем сообщение: {meta}")
        if message.media:
            await self._send_media(client, target_id, message)
        else:
            await self._send_text(client, target_id, message)
        self.log_state(f"Сообщение {message.id} отправлено. Ждём 15 секунд для стабилизации.")
        await asyncio.sleep(15)

    async def _migrate_source(self, client, source_id: int, target_id: int):
        source_key = str(source_id)
        self.log_state(f"Начинаем миграцию из источника {source_id}")
        iterator_kwargs = {"reverse": True}
        if self.start_date:
            iterator_kwargs["offset_date"] = self.start_date
            self.log_state(f"Копируем с {self.start_date.strftime('%d.%m.%Y')}")
        else:
            last_id = self.progress.get(source_key, 0)
            iterator_kwargs["offset_id"] = last_id
            self.log_state(f"Продолжаем после ID {last_id}")

        current_group = None
        group_buffer = []

        async for message in client.iter_messages(source_id, **iterator_kwargs):
            if not self.is_running:
                break
            if isinstance(message, MessageService):
                self.log_state(f"Служебное сообщение пропущено: {message.id}")
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
                await self._copy_message(client, message, target_id)
                self._update_progress(source_key, message.id)
            except FloodWaitError as flood_exc:
                self.log_state(f"FloodWait ({flood_exc.seconds}s) — пауза.")
                await asyncio.sleep(flood_exc.seconds)
                continue
            except Exception as exc:
                self.log_exception(f"Ошибка копирования {message.id}", exc)
                self._update_progress(source_key, message.id)
                await asyncio.sleep(5)

        if group_buffer:
            await self._send_album(client, target_id, group_buffer)
            self._update_progress(source_key, max(m.id for m in group_buffer))

    async def _monitor_new_posts(self, client, target_id: int, source_ids: List[int]):
        self.log_state("Запущено отслеживание новых постов.")

        async def handler(event):
            if not self.is_running:
                return
            message = event.message
            if isinstance(message, MessageService):
                return
            if message.grouped_id:
                return
            try:
                await self._copy_message(client, message, target_id)
                chat_id = (
                    message.chat_id
                    if message.chat_id
                    else getattr(message.peer_id, "channel_id", None)
                )
                if chat_id:
                    self._update_progress(str(chat_id), message.id)
            except FloodWaitError as flood_exc:
                self.log_state(f"FloodWait при новом посте ({flood_exc.seconds}s)")
                await asyncio.sleep(flood_exc.seconds)
            except Exception as exc:
                self.log_exception(f"Ошибка нового поста {message.id}", exc)

        async def album_handler(event):
            if not self.is_running:
                return
            try:
                await self._send_album(client, target_id, event.messages)
                self._update_progress(str(event.chat_id), max(m.id for m in event.messages))
            except FloodWaitError as flood_exc:
                self.log_state(f"FloodWait альбома ({flood_exc.seconds}s)")
                await asyncio.sleep(flood_exc.seconds)
            except Exception as exc:
                self.log_exception("Ошибка альбома", exc)

        client.add_event_handler(handler, events.NewMessage(chats=source_ids))
        client.add_event_handler(album_handler, events.Album(chats=source_ids))

        try:
            while self.is_running:
                await asyncio.sleep(1)
        finally:
            client.remove_event_handler(handler)
            client.remove_event_handler(album_handler)
            self.log_state("Отслеживание новых постов остановлено.")

    async def _run(self, source_ids: List[int], target_id: int):
        client = TelegramClient(self.SESSION_NAME, self.api_id, self.api_hash)
        await client.connect()
        try:
            self.log_state("Подключение к Telegram установлено.")
            await self._authorize(client)
            for source in source_ids:
                if not self.is_running:
                    break
                await self._migrate_source(client, source, target_id)
            if self.is_running:
                await self._monitor_new_posts(client, target_id, source_ids)
        finally:
            await client.disconnect()
            self.log_state("Соединение закрыто.")

    def start_migration(self, source_ids: List[int], target_id: int):
        if not source_ids:
            raise ValueError("Нет источников.")
        self.is_running = True
        try:
            asyncio.run(self._run(source_ids, target_id))
        except Exception as exc:
            self.log_exception("Критическая ошибка", exc)
            raise
        finally:
            self.is_running = False

    def stop(self):
        self.is_running = False
