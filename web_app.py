import asyncio
import datetime
import json
import os
import queue
import threading
from flask import Flask, render_template, request, jsonify

from telegram_logic import TelegramLogic

app = Flask(__name__, static_folder="static", template_folder="templates")

log_queue = queue.Queue()
logic_instance = None
logic_thread = None
session_state = {"authorized": False, "phone_hint": "+<код><номер>"}


def log(message: str):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    log_queue.put(entry)


def threadsafe_auth(dialog_type):
    """Эмуляция диалога авторизации через Web."""
    if dialog_type == "phone":
        session_state["waiting"] = "phone"
        return None
    elif dialog_type == "code":
        session_state["waiting"] = "code"
        return None
    elif dialog_type == "password":
        session_state["waiting"] = "password"
        return None
    return None


@app.route("/")
def index():
    return render_template("index.html", authorized=session_state["authorized"])


@app.route("/logs")
def logs():
    messages = []
    while not log_queue.empty():
        messages.append(log_queue.get())
    return jsonify({"logs": messages})


@app.route("/start", methods=["POST"])
def start():
    global logic_instance, logic_thread

    data = request.json
    api_id = data["api_id"]
    api_hash = data["api_hash"]
    target_id = int(data["target_id"])
    source_ids = [int(x.strip()) for x in data["source_ids"].split(",") if x.strip()]
    date_filter = data.get("start_date")
    start_date = (
        datetime.datetime.strptime(date_filter, "%Y-%m-%d")
        if date_filter
        else None
    )

    logic_instance = TelegramLogic(
        api_id=api_id,
        api_hash=api_hash,
        log_callback=log,
        auth_callback=threadsafe_auth,
        start_date=start_date,
    )

    def runner():
        try:
            logic_instance.start_migration(source_ids, target_id)
        except Exception as exc:
            log(f"Критическая ошибка: {exc}")

    logic_thread = threading.Thread(target=runner, daemon=True)
    logic_thread.start()
    return jsonify({"status": "started"})


@app.route("/stop", methods=["POST"])
def stop():
    if logic_instance:
        logic_instance.stop()
        log("Получен запрос на остановку.")
    return jsonify({"status": "stopping"})


@app.route("/auth", methods=["POST"])
def auth_step():
    payload = request.json
    step = payload["step"]
    value = payload["value"]
    if step == "phone":
        session_state["phone"] = value
        session_state["authorized"] = False
        log(f"Введён номер {value}. Ждём код.")
        return jsonify({"status": "code_required"})
    if step == "code":
        session_state["code"] = value
        session_state["authorized"] = True
        log("Код принят, сессия авторизована.")
        return jsonify({"status": "authorized"})
    if step == "password":
        session_state["password"] = value
        log("Пароль 2FA введён.")
        return jsonify({"status": "password_saved"})
    return jsonify({"status": "ok"})


@app.route("/logout", methods=["POST"])
def logout():
    session_state["authorized"] = False
    session_state.pop("phone", None)
    session_state.pop("code", None)
    session_state.pop("password", None)
    log("Сессия сброшена. Авторизация требуется заново.")
    return jsonify({"status": "logged_out"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
