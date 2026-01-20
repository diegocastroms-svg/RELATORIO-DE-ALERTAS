import os
import requests
import sqlite3
import time
from datetime import datetime, timedelta

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "").strip()

API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
DB = "alerts.db"

def db_init():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            symbol TEXT,
            timeframe TEXT,
            alert_type TEXT,
            price REAL,
            raw TEXT
        )
    """)
    conn.commit()

    # ===== CORRECAO: adicionar coluna para hora do alerta =====
    try:
        cur.execute("ALTER TABLE alerts ADD COLUMN alert_time TEXT")
        conn.commit()
    except:
        pass

    # ===== CORRECAO: adicionar coluna para RSI =====
    try:
        cur.execute("ALTER TABLE alerts ADD COLUMN rsi REAL")
        conn.commit()
    except:
        pass

    conn.close()

def send(text):
    requests.post(f"{API}/sendMessage", json={
        "chat_id": GROUP_CHAT_ID,
        "text": text
    }, timeout=10)

def parse_and_store(text):
    lines = text.splitlines()
    if not lines:
        return

    alert_type = lines[0].strip()
    symbol = None
    timeframe = None
    price = None

    # ===== CORRECAO =====
    alert_time = None
    rsi = None

    # tenta timeframe por (15M) etc
    if "(" in alert_type and ")" in alert_type:
        timeframe = alert_type.split("(")[-1].replace(")", "").strip()

    # se for RSI (ex: "RSI 1H < 35") tenta pegar timeframe do titulo
    if timeframe is None and "RSI" in alert_type:
        for tf in ["15M", "1H", "4H", "12H", "1D"]:
            if tf in alert_type.upper():
                timeframe = tf
                break

    for l in lines:
        ll = l.strip()

        if ll.isupper() and len(ll) <= 10:
            symbol = ll

        if "Preço" in ll or "Preco" in ll:
            try:
                price = float(ll.split(":")[1].strip())
            except:
                pass

        # exemplos: "Hora: 11:16:48 BR"
        if ll.startswith("Hora:") or "Hora:" in ll:
            try:
                alert_time = ll.split("Hora:", 1)[1].strip()
            except:
                pass

        # exemplos: "RSI: 27.66" / "RSI (1D): 25.53"
        if "RSI" in ll and ":" in ll:
            try:
                val = ll.split(":", 1)[1].strip()
                rsi = float(val)
            except:
                pass

    # se ainda não pegou timeframe, tenta achar em qualquer linha
    if timeframe is None:
        for tf in ["15M", "1H", "4H", "12H", "1D"]:
            for l in lines:
                if tf in l.upper():
                    timeframe = tf
                    break
            if timeframe is not None:
                break

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO alerts (ts, symbol, timeframe, alert_type, price, raw, alert_time, rsi)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        symbol,
        timeframe,
        alert_type,
        price,
        text,
        alert_time,
        rsi
    ))
    conn.commit()
    conn.close()

def build_report(days=1):
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM alerts WHERE ts >= ?", (since,))
    total = cur.fetchone()[0]

    # ===== CORRECAO: ultimos alertas com moeda/timeframe/rsi/hora =====
    cur.execute("""
        SELECT symbol, timeframe, rsi, alert_time, ts, alert_type
        FROM alerts
        WHERE ts >= ?
        ORDER BY id DESC
        LIMIT 10
    """, (since,))
    last = cur.fetchall()

    conn.close()

    msg = [f"📊 RELATÓRIO ({days}d)", f"Total: {total}"]

    if last:
        msg.append("\n🧾 Últimos alertas:")
        for sym, tf, r, a_time, ts, a_type in last:
            moeda = sym if sym else "None"
            timeframe = tf if tf else "None"
            rsi_txt = f"{r:.2f}" if isinstance(r, (int, float)) else "None"
            hora = a_time if a_time else ts
            msg.append(f"- {hora} | {moeda} | {timeframe} | RSI: {rsi_txt} | {a_type}")

    return "\n".join(msg)

def listener():
    offset = None
    while True:
        r = requests.get(f"{API}/getUpdates", params={
            "timeout": 30,
            "offset": offset
        }, timeout=35).json()

        for u in r.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "")

            if str(chat_id) != str(GROUP_CHAT_ID):
                continue

            if text.startswith("/relatorio"):
                parts = text.split()
                if len(parts) == 1:
                    send(build_report(1))
                elif len(parts) >= 2 and parts[1] == "hoje":
                    send(build_report(1))
                elif len(parts) >= 2 and parts[1].endswith("d"):
                    try:
                        send(build_report(int(parts[1][:-1])))
                    except:
                        send("Uso: /relatorio | /relatorio hoje | /relatorio 7d")
                else:
                    send("Uso: /relatorio | /relatorio hoje | /relatorio 7d")
                continue

            if "CRUZAMENTO" in text or "RSI" in text:
                parse_and_store(text)

        time.sleep(1)

if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not GROUP_CHAT_ID:
        print("FALTANDO ENV: TELEGRAM_TOKEN e/ou GROUP_CHAT_ID")
    else:
        db_init()
        listener()
