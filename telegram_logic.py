import asyncio
import datetime
import json
import os
from typing import Dict, List, Optional

from telethon import TelegramClient, events
from telethon.errors.rpcerrorlist import (
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import MessageService


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
        self.progress: Dict[str, int] = self._load_progress()
        self.is_running = False

    def log(self, message: str):
        if self.log_callback:
            self.log_callback(message)

    def _load_progress(self) -> Dict[str, int]:
        if not os.path.exists(self.PROGRESS_FILE):
            return {}
        try:
            with open(self.PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_progress(self):
        try:
            with open(self.PROGRESS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.progress, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.log(f"Не удалось сохранить прогресс: {exc}")

    def _update_progress(self, source_id: str, message_id: int):
        self.progress[source_id] = message_id
        self._save_progress()

    @staticmethod
    def _message_meta(message) -> str:
        text = message.message or "<пустой текст>"
        text_snippet = text[:120].replace("\n", " ")
        entities = len(message.entities or [])
        media_type = type(message.media).__name__ if message.media else "нет медиа"
        return (
            f"ID={message.id}, текст_len={len(text)}, "
            f"entities={entities}, медиа={media_type}, сниппет={text_snippet!r}"
        )

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
            password = await client.loop.run_in_executor(None, self.auth_callback, "password")
            if not password:
                raise Exception("Авторизация отменена (пароль).")
            await client.sign_in(password=password)
        except PhoneCodeInvalidError as exc:
            raise Exception(f"Неверный код: {exc}")
        self.log("Авторизация успешна!")

    async def _copy_message(self, client, message, target_id: int):
        meta = self._message_meta(message)
        self.log(f"[prep] Перед копированием: источник={message.chat_id}, {meta}")

        try:
            await client.copy_message(entity=target_id, message=message)
            self.log(f"[ok] copy_message -> Успех ({meta})")
        except AttributeError:
            self.log(f"[warn] copy_message недоступен, пытаем forward_messages (as_copy)")
            await client.forward_messages(
                entity=target_id,
                messages=message,
                from_peer=message.chat_id,
                as_copy=True,
            )
            self.log(f"[ok] forward_messages(as_copy=True) -> Успех ({meta})")
        except TypeError:
            self.log(f"[warn] as_copy не поддерживается, forward_messages стандартно")
            await client.forward_messages(
                entity=target_id,
                messages=message,
                from_peer=message.chat_id,
            )
            self.log(f"[ok] forward_messages -> Успех ({meta})")

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

        self.log(f"Итератор настроен: {iterator_kwargs}")

        async for message in client.iter_messages(source_id, **iterator_kwargs):
            if not self.is_running:
                break

            if isinstance(message, MessageService):
                self.log(f"Пропуск служебного сообщения {message.id}")
                self._update_progress(source_key, message.id)
                continue

            try:
                await self._copy_message(client, message, target_id)
                self._update_progress(source_key, message.id)
                self.log(f"Скопировано сообщение {message.id} из {source_id}")
                await asyncio.sleep(2)
            except FloodWaitError as flood_exc:
                self.log(f"FloodWait: ждем {flood_exc.seconds} сек.")
                await asyncio.sleep(flood_exc.seconds)
            except Exception as exc:
                self.log(f"Ошибка при копировании сообщения {message.id}: {exc}")
                self._update_progress(source_key, message.id)
                await asyncio.sleep(5)

    async def _monitor_new_posts(self, client, target_id: int, source_ids: List[int]):
        self.log("Отслеживание новых постов запущено.")

        async def new_message_handler(event):
            if not self.is_running:
                return

            message = event.message
            if isinstance(message, MessageService):
                return

            meta = self._message_meta(message)
            self.log(f"Новый пост {message.id} из {event.chat_id}: {meta}")

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
                self.log(f"FloodWait при новом посте: {flood_exc.seconds} сек.")
                await asyncio.sleep(flood_exc.seconds)
            except Exception as exc:
                self.log(f"Ошибка нового поста {message.id}: {exc}")

        handler = client.add_event_handler(
            new_message_handler, events.NewMessage(chats=source_ids)
        )

        try:
            while self.is_running:
                await asyncio.sleep(1)
        finally:
            client.remove_event_handler(new_message_handler)
            self.log("Отслеживание новых постов остановлено.")

    async def _run(self, source_ids: List[int], target_id: int):
        client = TelegramClient(self.SESSION_NAME, self.api_id, self.api_hash)
        await client.connect()
        try:
            self.log("Подключение к Telegram выполнено.")
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
