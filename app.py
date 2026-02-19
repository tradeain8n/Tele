import datetime
import json
import queue
import threading
from functools import wraps

from flask import Flask, render_template, request, jsonify

from telegram_logic import TelegramLogic

app = Flask(__name__, static_folder="static", template_folder="templates")

log_queue = queue.Queue()
logic_instance = None
logic_thread = None

session_state = {
    "authorized": False,
    "provided": {},
    "events": {},
    "waiting_for": "phone",
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
    session_state["waiting_for"] = step
    log(f"Требуется авторизация ({step}).")
    provided = session_state["provided"].pop(step, None)
    if provided is not None:
        return provided
    event = threading.Event()
    session_state["events"][step] = event
    event.wait()
    return session_state["provided"].pop(step, None)


def set_auth_data(step: str, value: str):
    session_state["provided"][step] = value
    event = session_state["events"].pop(step, None)
    if event:
        event.set()


def _start_logic(source_ids, target_id, start_date):
    global logic_instance
    logic_instance = TelegramLogic(
        api_id=source_ids["api_id"],
        api_hash=source_ids["api_hash"],
        log_callback=log,
        auth_callback=auth_callback,
        start_date=start_date,
    )
    try:
        logic_instance.start_migration(source_ids["ids"], target_id)
    except Exception as exc:
        log(f"Критическая ошибка: {exc}")
    finally:
        log("Логика завершена.")


@app.route("/")
def index():
    return render_template("index.html", authorized=session_state["authorized"])


@app.route("/logs")
@safe_response
def fetch_logs():
    items = []
    while not log_queue.empty():
        items.append(log_queue.get())
    return jsonify({"logs": items})


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
        sources = [int(item.strip()) for item in source_ids_text.split(",") if item.strip()]
    except ValueError:
        raise ValueError("ID каналов-источников должны быть числом.")
    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        raise ValueError("ID целевого канала должен быть числом.")

    selected_date = None
    if start_date:
        selected_date = datetime.datetime.strptime(start_date, "%Y-%m-%d")

    payload_for_logic = {
        "api_id": api_id,
        "api_hash": api_hash,
        "ids": sources,
    }

    log("Запуск миграции...")
    thread = threading.Thread(
        target=_start_logic, args=(payload_for_logic, target_id, selected_date), daemon=True
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
        raise ValueError("Требуется корректный шаг авторизации.")
    if not value:
        raise ValueError("Значение для авторизации не заполнено.")

    if step == "code":
        session_state["authorized"] = True
    set_auth_data(step, value)
    log(f"Получены данные для авторизации ({step}).")
    return jsonify({"status": "ok"})


@app.route("/logout", methods=["POST"])
@safe_response
def logout():
    session_state["authorized"] = False
    session_state["provided"].clear()
    session_state["events"].clear()
    log("Сессия сброшена. Требуется новый логин.")
    return jsonify({"status": "logged_out"})
