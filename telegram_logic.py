import asyncio
import datetime
import json
import os
from telethon import TelegramClient, events
from telethon.errors.rpcerrorlist import (
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import MessageService


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
        if phone:
            await client.send_code_request(phone)
        code = await client.loop.run_in_executor(None, self.auth_callback, "code")
        if code:
            await client.sign_in(phone=phone, code=code)
        else:
            raise Exception("Код не введён.")
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            password = await client.loop.run_in_executor(None, self.auth_callback, "password")
            if not password:
                raise Exception("Пароль 2FA не введён.")
            await client.sign_in(password=password)
        except PhoneCodeInvalidError as exc:
            raise Exception(f"Неверный код: {exc}")
        self.log("Авторизация прошла успешно.")

    async def _migrate_source(self, client, source_id, target_id):
        self.log(f"Начинаем миграцию из {source_id}.")
        iterator_kwargs = {"reverse": True}
        if self.start_date:
            iterator_kwargs["offset_date"] = self.start_date
            self.log(f"Фильтр: с {self.start_date.strftime('%d.%m.%Y')}.")
        else:
            last_id = self.progress.get(str(source_id), 0)
            iterator_kwargs["offset_id"] = last_id
            self.log(f"Продолжаем после ID: {last_id}.")

        async for message in client.iter_messages(source_id, **iterator_kwargs):
            if not self.is_running:
                break
            if isinstance(message, MessageService):
                self._update_progress(source_id, message.id)
                continue
            try:
                await client.forward_messages(
                    entity=target_id,
                    messages=message,
                    from_peer=source_id,
                    as_copy=True,
                )
                self._update_progress(source_id, message.id)
                self.log(f"Копия {message.id} из {source_id} отправлена.")
                await asyncio.sleep(2)
            except FloodWaitError as exc:
                self.log(f"FloodWait: ждём {exc.seconds}s.")
                await asyncio.sleep(exc.seconds)
            except Exception as exc:
                self.log(f"Ошибка при копировании {message.id}: {exc}")
                self._update_progress(source_id, message.id)
                await asyncio.sleep(5)

    async def _monitor(self, client, target_id, source_ids):
        self.log("Отслеживание новых постов запущено.")
        async def handler(event):
            if not self.is_running:
                return
            msg = event.message
            if isinstance(msg, MessageService):
                return
            try:
                await client.forward_messages(
                    entity=target_id,
                    messages=msg,
                    from_peer=event.chat_id,
                    as_copy=True,
                )
                self._update_progress(event.chat_id, msg.id)
                self.log(f"Новый пост {msg.id} с {event.chat_id} копирован.")
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
