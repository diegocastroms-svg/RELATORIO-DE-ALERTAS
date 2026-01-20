import os
import requests
import sqlite3
import time
import re
from datetime import datetime, timedelta
from openpyxl import Workbook

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
            alert_key TEXT,
            alert_type TEXT,
            price REAL,
            raw TEXT
        )
    """)
    conn.commit()

    try:
        cur.execute("ALTER TABLE alerts ADD COLUMN alert_time TEXT")
        conn.commit()
    except:
        pass

    try:
        cur.execute("ALTER TABLE alerts ADD COLUMN rsi REAL")
        conn.commit()
    except:
        pass

    conn.close()

def send_excel(filepath, filename, caption):
    with open(filepath, "rb") as f:
        requests.post(
            f"{API}/sendDocument",
            data={
                "chat_id": GROUP_CHAT_ID,
                "caption": caption
            },
            files={
                "document": (filename, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            },
            timeout=60
        )

def norm_tf(tf):
    if not tf:
        return None
    t = tf.strip().upper().replace(" ", "")
    t = t.replace("MIN", "M").replace("HRS", "H").replace("HR", "H")
    return t

def extract_timeframe(first_line, full_text):
    if "(" in first_line and ")" in first_line:
        tf = first_line.split("(")[-1].replace(")", "").strip()
        return norm_tf(tf)

    m = re.search(r"\b(\d{1,2})(M|H|D)\b", first_line.upper())
    if m:
        return norm_tf(m.group(1) + m.group(2))

    m2 = re.search(r"\b(\d{1,2})(M|H|D)\b", full_text.upper())
    if m2:
        return norm_tf(m2.group(1) + m2.group(2))

    return None

def extract_hour(lines):
    for l in lines:
        ll = l.strip()
        if ll.startswith("Hora:") or "Hora:" in ll:
            try:
                return ll.split("Hora:", 1)[1].strip()
            except:
                return None
    return None

def extract_rsi(first_line, lines):
    rsi = None

    # exemplo: "RSI 1H < 35"
    if "RSI" in first_line.upper() and "<" in first_line:
        try:
            v = first_line.split("<", 1)[1].strip()
            rsi = float(v)
        except:
            pass

    # exemplo: "RSI: 27.66"
    if rsi is None:
        for l in lines:
            ll = l.strip()
            if "RSI" in ll and ":" in ll:
                try:
                    val = ll.split(":", 1)[1].strip()
                    rsi = float(val)
                    break
                except:
                    pass

    return rsi

def classify_alert(first_line, full_text):
    t = first_line.upper().strip()

    if t.startswith("CRUZAMENTO MA200"):
        return "CRUZAMENTO"

    if t.startswith("RSI"):
        return "RSI"

    if "TENDÊNCIA LONGA" in t or "TENDENCIA LONGA" in t:
        return "TENDENCIA"

    body = full_text.upper()
    if "CRUZAMENTO" in body and "MA200" in body:
        return "CRUZAMENTO"
    if body.startswith("RSI") or "\nRSI" in body:
        return "RSI"
    if "TENDÊNCIA LONGA" in body or "TENDENCIA LONGA" in body:
        return "TENDENCIA"

    return "OUTROS"

def parse_and_store(text):
    lines = text.splitlines()
    if not lines:
        return

    alert_type = lines[0].strip()

    alert_key = classify_alert(alert_type, text)
    timeframe = extract_timeframe(alert_type, text)
    alert_time = extract_hour(lines)
    rsi = extract_rsi(alert_type, lines)

    symbol = None
    price = None

    for l in lines:
        ll = l.strip()

        if ll.isupper() and len(ll) <= 12 and ll.isalpha():
            symbol = ll

        if "Preço" in ll or "Preco" in ll:
            try:
                price = float(ll.split(":")[1].strip())
            except:
                pass

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO alerts (ts, symbol, timeframe, alert_key, alert_type, price, raw, alert_time, rsi)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        symbol,
        timeframe,
        alert_key,
        alert_type,
        price,
        text,
        alert_time,
        rsi
    ))
    conn.commit()
    conn.close()

def build_report_excel(days=1, alert_key=None, tf_filter=None):
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    where = ["ts >= ?"]
    params = [since]

    if alert_key:
        where.append("alert_key = ?")
        params.append(alert_key)

    if tf_filter:
        where.append("timeframe = ?")
        params.append(tf_filter)

    where_sql = " AND ".join(where)

    cur.execute(f"""
        SELECT symbol, timeframe, rsi, alert_time, alert_key
        FROM alerts
        WHERE {where_sql}
        ORDER BY id DESC
    """, tuple(params))
    rows = cur.fetchall()

    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "Alertas"
    ws.append(["MOEDA", "TIMEFRAME", "RSI", "HORA", "TIPO"])

    for sym, tf, r, h, k in rows:
        moeda = sym if sym else ""
        timeframe = tf if tf else ""
        rsi_val = r if isinstance(r, (int, float)) else ""
        hora = h if h else ""
        tipo = k if k else ""
        ws.append([moeda, timeframe, rsi_val, hora, tipo])

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    name_key = alert_key if alert_key else "ALL"
    name_tf = tf_filter if tf_filter else "ALL"
    filename = f"relatorio_{days}d_{name_key}_{name_tf}_{stamp}.xlsx"
    wb.save(filename)
    return filename

def parse_relatorio_cmd(text):
    # Aceita:
    # /relatorio
    # /relatorio rsi
    # /relatorio rsi 4h 2d
    # /relatorio cruzamento 15m hoje
    # /relatorio tendencia 1d 7d
    parts = text.strip().split()
    days = 1
    tf = None
    key = None

    for p in parts[1:]:
        pl = p.lower().strip()

        if pl == "hoje":
            days = 1
            continue

        if pl.endswith("d"):
            try:
                days = int(pl[:-1])
            except:
                pass
            continue

        if re.match(r"^\d{1,2}(m|h|d)$", pl):
            tf = norm_tf(pl)
            continue

        if pl in ["rsi"]:
            key = "RSI"
            continue

        if pl in ["cruzamento", "ma200"]:
            key = "CRUZAMENTO"
            continue

        if pl in ["tendencia", "tendência", "longa", "tendencia_longa", "tendência_longa"]:
            key = "TENDENCIA"
            continue

    return days, key, tf

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
                days, key, tf = parse_relatorio_cmd(text)
                filepath = build_report_excel(days, alert_key=key, tf_filter=tf)

                caption = f"📎 Relatório ({days}d)"
                if key:
                    caption += f" — {key}"
                if tf:
                    caption += f" — {tf}"

                send_excel(filepath, filepath, caption)
                continue

            if text and not text.startswith("/"):
                parse_and_store(text)

        time.sleep(1)

if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not GROUP_CHAT_ID:
        print("FALTANDO ENV: TELEGRAM_TOKEN e/ou GROUP_CHAT_ID")
    else:
        db_init()
        listener()
