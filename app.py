import datetime
import os
import queue
import threading
from functools import wraps

from flask import Flask, render_template, request, jsonify

try:
    from telegram_logic import TelegramLogic
except Exception as exc:
    raise RuntimeError(f"Не удалось импортировать TelegramLogic: {exc}")

SESSION_BASE = "cloner_session"
SESSION_FILES = [f"{SESSION_BASE}.session", f"{SESSION_BASE}.session-journal"]

app = Flask(__name__, static_folder="static", template_folder="templates")

log_queue = queue.Queue()
logic_instance = None

session_state = {
    "authorized": False,
    "current_phone": None,
    "auth_step": "idle",
    "provided": {},
    "events": {},
}


def log(message: str):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_queue.put(f"[{timestamp}] {message}")


def safe_response(handler):
    @wraps(handler)
    def wrapper(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except Exception as exc:
            log(f"Ошибка API: {exc}")
            return jsonify({"error": str(exc)}), 500

    return wrapper


def auth_callback(step: str):
    session_state["auth_step"] = step
    log(f"Требуется авторизация ({step}).")
    stored = session_state["provided"].pop(step, None)
    if stored is not None:
        return stored
    event = threading.Event()
    session_state["events"][step] = event
    event.wait()
    return session_state["provided"].pop(step, None)


def set_auth_data(step: str, value: str):
    session_state["provided"][step] = value
    event = session_state["events"].pop(step, None)
    if event:
        event.set()


def cleanup_session_files():
    for path in SESSION_FILES:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def start_logic(api_id, api_hash, sources, target_id, start_date):
    global logic_instance
    logic_instance = TelegramLogic(
        api_id=api_id,
        api_hash=api_hash,
        log_callback=log,
        auth_callback=auth_callback,
        start_date=start_date,
    )
    try:
        logic_instance.start_migration(sources, target_id)
    except Exception as exc:
        log(f"Критическая ошибка логики: {exc}")
    finally:
        log("Фоновой поток завершён.")
        session_state["auth_step"] = "idle"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/logs")
@safe_response
def fetch_logs():
    lines = []
    while not log_queue.empty():
        lines.append(log_queue.get())
    return jsonify({"logs": lines})


@app.route("/status")
@safe_response
def status():
    return jsonify(
        {
            "authorized": session_state["authorized"],
            "current_phone": session_state["current_phone"],
            "waiting_for": session_state["auth_step"],
        }
    )


@app.route("/start", methods=["POST"])
@safe_response
def start():
    payload = request.json or {}
    api_id = payload.get("api_id")
    api_hash = payload.get("api_hash")
    source_ids_text = payload.get("source_ids", "")
    target_id = payload.get("target_id")
    start_date = payload.get("start_date")

    if not api_id or not api_hash:
        raise ValueError("Укажите API ID и API Hash.")
    if not source_ids_text:
        raise ValueError("Укажите хотя бы один канал-источник.")

    try:
        sources = [int(part.strip()) for part in source_ids_text.split(",") if part.strip()]
    except ValueError:
        raise ValueError("ID источников должны быть числами.")
    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        raise ValueError("ID целевого канала должен быть числом.")

    parsed_date = None
    if start_date:
        parsed_date = datetime.datetime.strptime(start_date, "%Y-%m-%d")

    log("Запуск миграции...")
    thread = threading.Thread(
        target=start_logic,
        args=(api_id, api_hash, sources, target_id, parsed_date),
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "started"})


@app.route("/stop", methods=["POST"])
@safe_response
def stop():
    if logic_instance and logic_instance.is_running:
        logic_instance.stop()
        log("Остановка миграции по запросу.")
    return jsonify({"status": "stopping"})


@app.route("/auth", methods=["POST"])
@safe_response
def auth_step():
    payload = request.json or {}
    step = payload.get("step")
    value = payload.get("value")

    if step not in {"phone", "code", "password"}:
        raise ValueError("Неверный шаг авторизации.")
    if not value:
        raise ValueError("Значение обязательно.")

    if step == "phone":
        session_state["current_phone"] = value
        session_state["authorized"] = False
    elif step == "password":
        session_state["authorized"] = True

    set_auth_data(step, value)
    log(f"Получены данные для {step}.")
    return jsonify({"status": "ok", "waiting_for": session_state["auth_step"]})


@app.route("/logout", methods=["POST"])
@safe_response
def logout():
    if logic_instance and logic_instance.is_running:
        logic_instance.stop()
    session_state["authorized"] = False
    session_state["current_phone"] = None
    session_state["provided"].clear()
    session_state["events"].clear()
    session_state["auth_step"] = "idle"
    cleanup_session_files()
    log("Сессия сброшена.")
    return jsonify({"status": "logged_out"})
