import datetime
import json
import queue
import threading
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

@app.route("/")
def index():
    return render_template("index.html", authorized=session_state["authorized"])


@app.route("/logs")
def fetch_logs():
    items = []
    while not log_queue.empty():
        items.append(log_queue.get())
    return jsonify({"logs": items})


@app.route("/start", methods=["POST"])
def start():
    global logic_instance, logic_thread

    payload = request.json
    api_id = payload.get("api_id")
    api_hash = payload.get("api_hash")
    source_ids_text = payload.get("source_ids", "")
    target_id = payload.get("target_id")
    start_date = payload.get("start_date")

    if not api_id or not api_hash:
        return jsonify({"error": "Укажите API ID и API Hash"}), 400
    if not source_ids_text:
        return jsonify({"error": "Укажите хотя бы один канал-источник"}), 400
    try:
        source_ids = [int(item.strip()) for item in source_ids_text.split(",") if item.strip()]
    except ValueError:
        return jsonify({"error": "ID каналов-источников должны быть числами"}), 400
    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        return jsonify({"error": "ID целевого канала должен быть числом"}), 400

    selected_date = None
    if start_date:
        try:
            selected_date = datetime.datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "Неверный формат даты. Используйте yyyy-mm-dd"}), 400

    logic_instance = TelegramLogic(
        api_id=api_id,
        api_hash=api_hash,
        log_callback=log,
        auth_callback=auth_callback,
        start_date=selected_date,
    )

    def runner():
        try:
            logic_instance.start_migration(source_ids, target_id)
        except Exception as exc:
            log(f"Критическая ошибка: {exc}")

    logic_thread = threading.Thread(target=runner, daemon=True)
    logic_thread.start()
    log("Запуск миграции...")
    return jsonify({"status": "started"})


@app.route("/stop", methods=["POST"])
def stop():
    if logic_instance:
        logic_instance.stop()
        log("Остановка миграции по запросу.")
    return jsonify({"status": "stopping"})


@app.route("/auth", methods=["POST"])
def auth_step():
    payload = request.json
    step = payload.get("step")
    value = payload.get("value")
    if step not in {"phone", "code", "password"}:
        return jsonify({"error": "Неверный шаг авторизации"}), 400
    set_auth_data(step, value)
    if step == "code":
        session_state["authorized"] = True
    log(f"Получены данные для авторизации ({step}).")
    return jsonify({"status": "ok"})


@app.route("/logout", methods=["POST"])
def logout():
    session_state["authorized"] = False
    session_state["phone"] = None
    session_state["code"] = None
    session_state["password"] = None
    log("Сессия сброшена. Авторизация требуется заново.")
    return jsonify({"status": "logged_out"})

