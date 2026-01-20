import os
import requests
import sqlite3
import time
import re
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
            alert_key TEXT,
            alert_type TEXT,
            price REAL,
            raw TEXT
        )
    """)
    conn.commit()
    conn.close()

def send(text):
    requests.post(f"{API}/sendMessage", json={
        "chat_id": GROUP_CHAT_ID,
        "text": text
    }, timeout=10)

def norm_tf(tf):
    if not tf:
        return None
    t = tf.strip().upper().replace(" ", "")
    t = t.replace("MIN", "M").replace("HRS", "H").replace("HR", "H")
    return t

def extract_timeframe(text_first_line, full_text):
    # 1) se tiver (15M) no título
    if "(" in text_first_line and ")" in text_first_line:
        tf = text_first_line.split("(")[-1].replace(")", "").strip()
        return norm_tf(tf)

    # 2) procurar "1H", "4H", "12H", "1D", "15M" no título/linha
    m = re.search(r"\b(\d{1,2})(M|H|D)\b", text_first_line.upper())
    if m:
        return norm_tf(m.group(1) + m.group(2))

    # 3) procurar no texto todo (ex: "RSI (1D)" etc)
    m2 = re.search(r"\b(\d{1,2})(M|H|D)\b", full_text.upper())
    if m2:
        return norm_tf(m2.group(1) + m2.group(2))

    return None

def classify_alert(text_first_line, full_text):
    t = text_first_line.upper().strip()

    if t.startswith("CRUZAMENTO MA200"):
        return "CRUZAMENTO_MA200"

    if t.startswith("RSI"):
        return "RSI"

    if "TENDÊNCIA LONGA" in t or "TENDENCIA LONGA" in t:
        return "TENDENCIA_LONGA"

    # fallback: se tiver palavras-chave no corpo
    body = full_text.upper()
    if "CRUZAMENTO" in body and "MA200" in body:
        return "CRUZAMENTO_MA200"
    if body.startswith("RSI") or "\nRSI" in body:
        return "RSI"
    if "TENDÊNCIA LONGA" in body or "TENDENCIA LONGA" in body:
        return "TENDENCIA_LONGA"

    return "OUTROS"

def parse_and_store(text):
    lines = text.splitlines()
    if not lines:
        return

    alert_type = lines[0].strip()
    symbol = None
    timeframe = None
    price = None

    alert_key = classify_alert(alert_type, text)
    timeframe = extract_timeframe(alert_type, text)

    for l in lines:
        if l.isupper() and len(l) <= 12 and l.isalpha():
            symbol = l.strip()
        if "Preço" in l or "Preco" in l:
            try:
                price = float(l.split(":")[1].strip())
            except:
                pass

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO alerts (ts, symbol, timeframe, alert_key, alert_type, price, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        symbol,
        timeframe,
        alert_key,
        alert_type,
        price,
        text
    ))
    conn.commit()
    conn.close()

def build_report(days=1, alert_key=None, timeframe=None):
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    where = ["ts >= ?"]
    params = [since]

    if alert_key:
        where.append("alert_key = ?")
        params.append(alert_key)

    if timeframe:
        where.append("timeframe = ?")
        params.append(timeframe)

    where_sql = " AND ".join(where)

    cur.execute(f"SELECT COUNT(*) FROM alerts WHERE {where_sql}", tuple(params))
    total = cur.fetchone()[0]

    cur.execute(f"""
        SELECT timeframe, COUNT(*)
        FROM alerts
        WHERE {where_sql}
        GROUP BY timeframe
        ORDER BY COUNT(*) DESC
    """, tuple(params))
    by_tf = cur.fetchall()

    cur.execute(f"""
        SELECT symbol, COUNT(*)
        FROM alerts
        WHERE {where_sql}
        GROUP BY symbol
        ORDER BY COUNT(*) DESC
        LIMIT 10
    """, tuple(params))
    top = cur.fetchall()

    conn.close()

    filtro = []
    if alert_key:
        filtro.append(alert_key)
    if timeframe:
        filtro.append(timeframe)
    filtro_txt = " | ".join(filtro) if filtro else "GERAL"

    msg = [f"📊 RELATÓRIO ({days}d) — {filtro_txt}", f"Total: {total}"]

    if by_tf:
        msg.append("\n⏱ Timeframes:")
        for tf, c in by_tf:
            msg.append(f"- {tf}: {c}")

    if top:
        msg.append("\n🏷 Top moedas:")
        for s, c in top:
            msg.append(f"- {s}: {c}")

    return "\n".join(msg)

def parse_relatorio_cmd(text):
    # formatos:
    # /relatorio
    # /relatorio cruzamento 15m 7d
    # /relatorio rsi 1h hoje
    # /relatorio tendencia 1d 2d
    parts = text.strip().split()
    days = 1
    alert_key = None
    tf = None

    if len(parts) >= 2:
        t = parts[1].lower().strip()
        if t in ["cruzamento", "ma200", "cruzamento_ma200"]:
            alert_key = "CRUZAMENTO_MA200"
        elif t in ["rsi"]:
            alert_key = "RSI"
        elif t in ["tendencia", "tendência", "longa", "tendencia_longa", "tendência_longa", "tl"]:
            alert_key = "TENDENCIA_LONGA"
        elif t in ["outros"]:
            alert_key = "OUTROS"

    # pegar possíveis argumentos restantes (tf e dias)
    for p in parts[2:]:
        pl = p.lower().strip()

        # hoje = 1 dia
        if pl == "hoje":
            days = 1
            continue

        # 7d, 2d etc
        if pl.endswith("d"):
            try:
                days = int(pl[:-1])
            except:
                pass
            continue

        # timeframe: 15m, 1h, 4h, 12h, 1d etc
        if re.match(r"^\d{1,2}(m|h|d)$", pl):
            tf = norm_tf(pl)
            continue

    return days, alert_key, tf

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
                days, alert_key, tf = parse_relatorio_cmd(text)
                send(build_report(days, alert_key=alert_key, timeframe=tf))
                continue

            # salva todos os alertas que chegarem (não só CRUZAMENTO/RSI)
            # mas você pode deixar restrito se quiser depois
            if text:
                parse_and_store(text)

        time.sleep(1)

if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not GROUP_CHAT_ID:
        print("FALTANDO ENV: TELEGRAM_TOKEN e/ou GROUP_CHAT_ID")
    else:
        db_init()
        listener()
