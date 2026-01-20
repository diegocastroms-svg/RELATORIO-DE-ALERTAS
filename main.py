import os
import re
import time
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from openpyxl import Workbook

# =========================
# CONFIG
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "").strip()

API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
DB = "alerts.db"
TZ_BR = ZoneInfo("America/Sao_Paulo")

# =========================
# HELPERS
# =========================
def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")

def norm_cmd(s):
    return strip_accents(s).lower().strip()

def tg_post(method, json=None, files=None):
    return requests.post(f"{API}/{method}", json=json, files=files, timeout=60)

def send_message(text, reply_markup=None):
    payload = {"chat_id": GROUP_CHAT_ID, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg_post("sendMessage", json=payload)

def send_excel(path, caption):
    with open(path, "rb") as f:
        tg_post(
            "sendDocument",
            files={"document": f},
            json={"chat_id": GROUP_CHAT_ID, "caption": caption}
        )

def now_utc():
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

# =========================
# DB
# =========================
def db_init():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT,
            symbol TEXT,
            timeframe TEXT,
            alert_key TEXT,
            rsi REAL,
            raw TEXT
        )
    """)
    conn.commit()
    conn.close()

# =========================
# PARSING
# =========================
def extract_symbol(lines):
    for l in lines:
        if re.fullmatch(r"[A-Z0-9]{3,12}", l):
            return l
    return None

def extract_timeframe(text):
    m = re.search(r"\b(\d{1,2})(M|H|D)\b", text.upper())
    return f"{m.group(1)}{m.group(2)}" if m else None

def extract_rsi(text):
    m = re.search(r"RSI[^0-9]*([0-9]+(\.[0-9]+)?)", text.upper())
    return float(m.group(1)) if m else None

def classify(text):
    t = text.upper()
    if "CRUZAMENTO" in t:
        return "CRUZAMENTO"
    if "RSI" in t:
        return "RSI"
    if "TENDENCIA" in t or "TENDÊNCIA" in t:
        return "TENDENCIA"
    return "OUTROS"

def store_alert(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO alerts (ts_utc, symbol, timeframe, alert_key, rsi, raw)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        now_utc(),
        extract_symbol(lines),
        extract_timeframe(text),
        classify(text),
        extract_rsi(text),
        text
    ))
    conn.commit()
    conn.close()

# =========================
# EXCEL
# =========================
def build_excel(days=1, key=None, tf=None):
    since = (datetime.utcnow() - timedelta(days=days)).replace(tzinfo=timezone.utc).isoformat()

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    where = ["ts_utc >= ?"]
    params = [since]

    if key:
        where.append("alert_key = ?")
        params.append(key)

    if tf:
        where.append("timeframe = ?")
        params.append(tf)

    cur.execute(f"""
        SELECT ts_utc, symbol, timeframe, rsi
        FROM alerts
        WHERE {" AND ".join(where)}
        ORDER BY id DESC
    """, params)

    rows = cur.fetchall()
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.append(["DATA", "HORA", "MOEDA", "TIMEFRAME", "RSI"])

    for ts, sym, tf, rsi in rows:
        dt = datetime.fromisoformat(ts).astimezone(TZ_BR)
        ws.append([
            dt.strftime("%Y-%m-%d"),
            dt.strftime("%H:%M:%S"),
            sym or "",
            tf or "",
            rsi or ""
        ])

    name = f"relatorio_{days}d.xlsx"
    wb.save(name)
    return name

# =========================
# TELEGRAM LOOP
# =========================
def listener():
    offset = None
    while True:
        r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 30}).json()
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message", {})
            chat = msg.get("chat", {}).get("id")
            text = msg.get("text", "")

            if str(chat) != str(GROUP_CHAT_ID):
                continue

            cmd = norm_cmd(text)

            if cmd.startswith("menu"):
                send_message("Use /relatorio [rsi|cruzamento|tendencia] [tf] [dias]")
                continue

            if cmd.startswith("relatorio") or cmd.startswith("/relatorio"):
                parts = cmd.split()
                days = 1
                key = None
                tf = None

                for p in parts:
                    if p.endswith("d"):
                        days = int(p[:-1])
                    if p in ["rsi", "cruzamento", "tendencia"]:
                        key = p.upper()
                    if re.fullmatch(r"\d{1,2}[mhd]", p):
                        tf = p.upper()

                file = build_excel(days, key, tf)
                send_excel(file, "📎 RELATÓRIO")
                continue

            # 🔥 ESSA É A LINHA QUE RESOLVE TUDO
            if text and not text.startswith("/"):
                store_alert(text)

        time.sleep(1)

# =========================
# START
# =========================
if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not GROUP_CHAT_ID:
        print("ENV faltando")
    else:
        db_init()
        listener()
