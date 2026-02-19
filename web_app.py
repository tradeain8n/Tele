import datetime
import queue
import threading
from collections import deque
from typing import Optional

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from telegram_logic import TelegramLogic

app = FastAPI()
log_queue = queue.Queue()
log_buffer = deque(maxlen=500)

logic = None
thread = None

auth_state = {"type": None, "value": None, "event": threading.Event()}


def log(message: str):
    log_queue.put(message)


def drain_logs():
    while not log_queue.empty():
        log_buffer.append(log_queue.get())


def auth_callback(auth_type: str):
    auth_state["type"] = auth_type
    auth_state["value"] = None
    auth_state["event"].clear()
    auth_state["event"].wait()
    return auth_state["value"]


def start_logic(api_id, api_hash, source_ids, target_id, start_date):
    global logic
    logic = TelegramLogic(
        api_id=api_id,
        api_hash=api_hash,
        log_callback=log,
        auth_callback=auth_callback,
        start_date=start_date,
    )
    logic.start_migration(source_ids, target_id)


@app.get("/", response_class=HTMLResponse)
def index():
    drain_logs()
    auth_block = ""
    if auth_state["type"]:
        auth_block = f"""
        <form action="/auth/submit" method="post">
            <h3>Требуется {auth_state["type"]}</h3>
            <input name="value" />
            <button type="submit">Отправить</button>
        </form>
        """

    logs_html = "<br>".join(log_buffer)
    return f"""
    <html>
    <body>
      <h1>Telegram Cloner Web</h1>
      {auth_block}
      <form action="/start" method="post">
        <input name="api_id" placeholder="API ID"><br>
        <input name="api_hash" placeholder="API Hash"><br>
        <input name="source_ids" placeholder="-100111,-100222"><br>
        <input name="target_id" placeholder="-100333"><br>
        <input name="start_date" placeholder="dd.mm.yyyy (необязательно)"><br>
        <button type="submit">Запустить</button>
      </form>
      <form action="/stop" method="post">
        <button type="submit">Остановить</button>
      </form>
      <h3>Логи:</h3>
      <div style="background:#111;color:#0f0;padding:10px;height:300px;overflow:auto;">
        {logs_html}
      </div>
    </body>
    </html>
    """


@app.post("/start")
def start(
    api_id: str = Form(...),
    api_hash: str = Form(...),
    source_ids: str = Form(...),
    target_id: str = Form(...),
    start_date: Optional[str] = Form(None),
):
    global thread
    if thread and thread.is_alive():
        return RedirectResponse("/", status_code=302)

    source_list = [int(x.strip()) for x in source_ids.split(",") if x.strip()]
    target_id_int = int(target_id)

    dt = None
    if start_date:
        dt = datetime.datetime.strptime(start_date, "%d.%m.%Y")

    thread = threading.Thread(
        target=start_logic,
        args=(api_id, api_hash, source_list, target_id_int, dt),
        daemon=True,
    )
    thread.start()
    return RedirectResponse("/", status_code=302)


@app.post("/stop")
def stop():
    global logic
    if logic:
        logic.stop()
    return RedirectResponse("/", status_code=302)


@app.post("/auth/submit")
def auth_submit(value: str = Form(...)):
    auth_state["value"] = value
    auth_state["event"].set()
    auth_state["type"] = None
    return RedirectResponse("/", status_code=302)