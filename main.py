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
    conn.close()


def send(text):
    requests.post(
        f"{API}/sendMessage",
        json={"chat_id": GROUP_CHAT_ID, "text": text},
        timeout=10
    )


def parse_and_store(text):
    lines = text.splitlines()
    if not lines:
        return

    alert_type = lines[0].strip()
    symbol = None
    timeframe = None
    price = None

    if "(" in alert_type and ")" in alert_type:
        timeframe = alert_type.split("(")[-1].replace(")", "").strip()

    for l in lines:
        if l.isupper() and len(l) <= 10:
            symbol = l.strip()
        if "Preço" in l or "Preco" in l:
            try:
                price = float(l.split(":")[1].strip())
            except:
                pass

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO alerts (ts, symbol, timeframe, alert_type, price, raw)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        symbol,
        timeframe,
        alert_type,
        price,
        text
    ))
    conn.commit()
    conn.close()


def build_report(days=1):
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM alerts WHERE ts >= ?", (since,))
    total = cur.fetchone()[0]

    cur.execute("""
        SELECT timeframe, COUNT(*)
        FROM alerts
        WHERE ts >= ?
        GROUP BY timeframe
        ORDER BY COUNT(*) DESC
    """, (since,))
    by_tf = cur.fetchall()

    cur.execute("""
        SELECT symbol, COUNT(*)
        FROM alerts
        WHERE ts >= ?
        GROUP BY symbol
        ORDER BY COUNT(*) DESC
        LIMIT 10
    """, (since,))
    top = cur.fetchall()

    conn.close()

    msg = [f"📊 RELATÓRIO ({days}d)", f"Total: {total}"]

    if by_tf:
        msg.append("\n⏱ Timeframes:")
        for tf, c in by_tf:
            msg.append(f"- {tf}: {c}")

    if top:
        msg.append("\n🏷 Top moedas:")
        for s, c in top:
            msg.append(f"- {s}: {c}")

    return "\n".join(msg)


def listener():
    offset = None
    while True:
        r = requests.get(
            f"{API}/getUpdates",
            params={"timeout": 30, "offset": offset},
            timeout=35
        ).json()

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

            if "CRUZAMENTO" in text or "RSI" in text or "TENDÊNCIA" in text:
                parse_and_store(text)

        time.sleep(1)


if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not GROUP_CHAT_ID:
        print("FALTANDO ENV: TELEGRAM_TOKEN e/ou GROUP_CHAT_ID")
    else:
        db_init()
        listener()
