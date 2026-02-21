import asyncio
import datetime
import json
import os
from typing import Dict, List, Optional

import telethon
from telethon import TelegramClient, events
from telethon.errors.rpcerrorlist import (
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import MessageService

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


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
        self.start_date = start_date
        self.log_callback = log_callback
        self.auth_callback = auth_callback
        self.progress: Dict[str, int] = self._load_progress()
        self.is_running = False
        os.makedirs("temp_media", exist_ok=True)
        self.log_state(f"Telethon {telethon.__version__}")

    def log(self, message: str):
        if self.log_callback:
            self.log_callback(message)

    def log_state(self, message: str):
        self.log(f"[state] {message}")

    def log_exception(self, text: str, exc: Exception):
        self.log(f"[error] {text} ({type(exc).__name__}: {exc})")

    def _load_progress(self) -> Dict[str, int]:
        if not os.path.exists(self.PROGRESS_FILE):
            return {}
        try:
            with open(self.PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            self.log_exception("Не удалось прочитать прогресс", exc)
            return {}

    def _save_progress(self):
        try:
            with open(self.PROGRESS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.progress, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.log_exception("Не удалось сохранить прогресс", exc)

    def _update_progress(self, source_id: str, message_id: int):
        self.progress[source_id] = message_id
        self._save_progress()

    def _message_meta(self, message):
        text = message.message or "<пустой текст>"
        snippet = text[:80].replace("\n", " ")
        entities = len(message.entities or [])
        media_name = type(message.media).__name__ if message.media else "нет медиа"
        return f"ID={message.id}, len={len(text)}, entities={entities}, media={media_name}, snippet={snippet!r}"

    async def _authorize(self, client: TelegramClient):
        if await client.is_user_authorized():
            self.log_state("Уже авторизованы.")
            return

        self.log_state("Требуется авторизация.")
        phone = await client.loop.run_in_executor(None, self.auth_callback, "phone")
        if not phone:
            raise Exception("Телефон не получен.")
        await client.send_code_request(phone)

        try:
            code = await client.loop.run_in_executor(None, self.auth_callback, "code")
            if not code:
                raise Exception("Код не введён.")
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            self.log_state("Требуется пароль 2FA.")
            password = await client.loop.run_in_executor(None, self.auth_callback, "password")
            if not password:
                raise Exception("Пароль 2FA не введён.")
            await client.sign_in(password=password)
        except PhoneCodeInvalidError as exc:
            raise Exception(f"Неверный код: {exc}")
        self.log_state("Авторизация завершена.")

    async def _copy_message(self, client, message, target_id: int):
        if isinstance(message, MessageService):
            return
        meta = self._message_meta(message)
        self.log_state(f"Копируем сообщение: {meta}")
        try:
            await client.forward_messages(
                entity=target_id,
                messages=message,
                from_peer=message.chat_id,
                as_copy=True,
            )
            self.log_state(f"[ok] forward_messages as_copy ({meta})")
            return
        except TypeError:
            self.log("[warn] as_copy не поддерживается, пробуем без него.")
        except Exception as exc:
            self.log_exception("copy_message/as_copy падает", exc)

        try:
            await client.forward_messages(
                entity=target_id,
                messages=message,
                from_peer=message.chat_id,
            )
            self.log_state(f"[ok] forward_messages без as_copy ({meta})")
        except Exception as exc:
            self.log_exception("forward_messages тоже упал", exc)
            raise

    async def _migrate_source(self, client, source_id: int, target_id: int):
        source_key = str(source_id)
        self.log_state(f"Начинаем миграцию из источника {source_id}")
        iterator_kwargs = {"reverse": True}
        if self.start_date:
            iterator_kwargs["offset_date"] = self.start_date
            self.log_state(f"Фильтр: с {self.start_date.strftime('%d.%m.%Y')}")
        else:
            last_id = self.progress.get(source_key, 0)
            iterator_kwargs["offset_id"] = last_id
            self.log_state(f"Продолжаем с сообщения после ID {last_id}")
        self.log_state(f"iterator(kwargs)={iterator_kwargs}")

        async for message in client.iter_messages(source_id, **iterator_kwargs):
            if not self.is_running:
                break
            if isinstance(message, MessageService):
                self.log(f"Пропущено служебное сообщение {message.id}")
                self._update_progress(source_key, message.id)
                continue
            try:
                await self._copy_message(client, message, target_id)
                self._update_progress(source_key, message.id)
                await asyncio.sleep(15)
            except FloodWaitError as flood_exc:
                self.log_state(f"FloodWait: ждём {flood_exc.seconds} сек.")
                await asyncio.sleep(flood_exc.seconds)
            except Exception as exc:
                self.log_exception(f"Ошибка копирования {message.id}", exc)
                self._update_progress(source_key, message.id)
                await asyncio.sleep(5)

    async def _monitor_new_posts(self, client, target_id: int, source_ids: List[int]):
        self.log_state("Запущено отслеживание новых публикаций.")

        async def handler(event):
            if not self.is_running:
                return
            message = event.message
            if isinstance(message, MessageService):
                return
            meta = self._message_meta(message)
            self.log_state(f"Новый пост: {meta}")
            try:
                await self._copy_message(client, message, target_id)
                chat_id = message.chat_id or getattr(message.peer_id, "channel_id", None)
                if chat_id:
                    self._update_progress(str(chat_id), message.id)
            except FloodWaitError as flood_exc:
                self.log_state(f"FloodWait при новом посте: {flood_exc.seconds}s")
                await asyncio.sleep(flood_exc.seconds)
            except Exception as exc:
                self.log_exception(f"Новый пост {message.id} упал", exc)

        handler = client.add_event_handler(handler, events.NewMessage(chats=source_ids))
        try:
            while self.is_running:
                await asyncio.sleep(1)
        finally:
            client.remove_event_handler(handler)
            self.log_state("Отслеживание закрыто.")

    async def _run(self, source_ids: List[int], target_id: int):
        client = TelegramClient(self.SESSION_NAME, self.api_id, self.api_hash)
        await client.connect()
        try:
            self.log_state("Подключились к Telegram.")
            await self._authorize(client)
            for source in source_ids:
                if not self.is_running:
                    break
                await self._migrate_source(client, source, target_id)
            if self.is_running:
                await self._monitor_new_posts(client, target_id, source_ids)
        finally:
            await client.disconnect()
            self.log_state("Отключились от Telegram.")

    def start_migration(self, source_ids: List[int], target_id: int):
        if not source_ids:
            raise ValueError("Не указано ни одного источника.")
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
