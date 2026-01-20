import os
import requests
import sqlite3
import time
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from openpyxl import Workbook

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "").strip()

API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
DB = "alerts.db"

TZ_BR = ZoneInfo("America/Sao_Paulo")

# =========================
# TELEGRAM HELPERS
# =========================
def tg_post(method, json=None, data=None, files=None, timeout=60):
    url = f"{API}/{method}"
    return requests.post(url, json=json, data=data, files=files, timeout=timeout)

def send_message(text, reply_markup=None):
    payload = {"chat_id": GROUP_CHAT_ID, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg_post("sendMessage", json=payload, timeout=30)

def edit_message(message_id, text, reply_markup=None):
    payload = {"chat_id": GROUP_CHAT_ID, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg_post("editMessageText", json=payload, timeout=30)

def answer_callback(callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text
    tg_post("answerCallbackQuery", json=payload, timeout=20)

def send_excel(filepath, filename, caption):
    with open(filepath, "rb") as f:
        tg_post(
            "sendDocument",
            data={"chat_id": GROUP_CHAT_ID, "caption": caption},
            files={"document": (filename, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            timeout=90
        )

# =========================
# DB
# =========================
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

# =========================
# PARSING
# =========================
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

    # Ex: "RSI 4H < 38"
    if "RSI" in first_line.upper() and "<" in first_line:
        try:
            v = first_line.split("<", 1)[1].strip()
            rsi = float(v)
        except:
            pass

    # Ex: "RSI: 27.66"
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
        datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
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

# =========================
# EXCEL
# =========================
def ts_to_br_date_time(ts_iso):
    try:
        dt = datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_br = dt.astimezone(TZ_BR)
        return dt_br.strftime("%Y-%m-%d"), dt_br.strftime("%H:%M:%S")
    except:
        return "", ""

def clean_alert_time(alert_time):
    if not alert_time:
        return ""
    s = alert_time.strip()
    s = s.replace(" BR", "").replace("BR", "").strip()
    return s

def build_report_excel(days=1, alert_key=None, tf_filter=None):
    since_dt = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(days=days)
    since = since_dt.isoformat()

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    where = ["ts >= ?"]
    params = [since]

    if alert_key and alert_key != "ALL":
        where.append("alert_key = ?")
        params.append(alert_key)

    if tf_filter and tf_filter != "ALL":
        where.append("timeframe = ?")
        params.append(tf_filter)

    where_sql = " AND ".join(where)

    cur.execute(f"""
        SELECT ts, symbol, timeframe, rsi, alert_time
        FROM alerts
        WHERE {where_sql}
        ORDER BY id DESC
    """, tuple(params))
    rows = cur.fetchall()

    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "RELATORIO"

    # COLUNAS EM MAIUSCULO (como você pediu)
    ws.append(["DATA", "HORA", "MOEDA", "TIMEFRAME", "RSI"])

    for ts_iso, sym, tf, r, a_time in rows:
        data_br, hora_br = ts_to_br_date_time(ts_iso)

        hora_final = clean_alert_time(a_time) if a_time else hora_br
        moeda = sym if sym else ""
        timeframe = tf if tf else ""
        rsi_val = r if isinstance(r, (int, float)) else ""

        ws.append([data_br, hora_final, moeda, timeframe, rsi_val])

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    k = alert_key if alert_key else "ALL"
    f = tf_filter if tf_filter else "ALL"
    filename = f"relatorio_{days}d_{k}_{f}_{stamp}.xlsx"
    wb.save(filename)
    return filename

# =========================
# MENU PROFISSIONAL (INLINE)
# =========================
def cb_pack(k=None, f=None, d=None, s=None):
    parts = ["M"]
    if k is not None:
        parts.append(f"K={k}")
    if f is not None:
        parts.append(f"F={f}")
    if d is not None:
        parts.append(f"D={d}")
    if s is not None:
        parts.append(f"S={s}")
    return "|".join(parts)

def cb_parse(data):
    out = {"K": None, "F": None, "D": None, "S": None}
    if not data:
        return out
    pieces = data.split("|")
    for p in pieces:
        if "=" in p:
            a, b = p.split("=", 1)
            if a in out:
                out[a] = b
    return out

def keyboard_types():
    return {
        "inline_keyboard": [
            [{"text": "RSI", "callback_data": cb_pack(k="RSI", s="TF")}],
            [{"text": "CRUZAMENTO", "callback_data": cb_pack(k="CRUZAMENTO", s="TF")}],
            [{"text": "TENDÊNCIA LONGA", "callback_data": cb_pack(k="TENDENCIA", s="TF")}],
            [{"text": "TUDO", "callback_data": cb_pack(k="ALL", s="TF")}],
        ]
    }

def keyboard_timeframes(k):
    return {
        "inline_keyboard": [
            [
                {"text": "15M", "callback_data": cb_pack(k=k, f="15M", s="DAYS")},
                {"text": "1H",  "callback_data": cb_pack(k=k, f="1H",  s="DAYS")},
                {"text": "4H",  "callback_data": cb_pack(k=k, f="4H",  s="DAYS")},
            ],
            [
                {"text": "12H", "callback_data": cb_pack(k=k, f="12H", s="DAYS")},
                {"text": "1D",  "callback_data": cb_pack(k=k, f="1D",  s="DAYS")},
                {"text": "TODOS TF", "callback_data": cb_pack(k=k, f="ALL", s="DAYS")},
            ],
            [
                {"text": "⬅️ VOLTAR", "callback_data": cb_pack(s="BACK_TYPES")}
            ]
        ]
    }

def keyboard_days(k, f):
    return {
        "inline_keyboard": [
            [
                {"text": "HOJE", "callback_data": cb_pack(k=k, f=f, d="1", s="GO")},
                {"text": "2D",   "callback_data": cb_pack(k=k, f=f, d="2", s="GO")},
                {"text": "5D",   "callback_data": cb_pack(k=k, f=f, d="5", s="GO")},
                {"text": "7D",   "callback_data": cb_pack(k=k, f=f, d="7", s="GO")},
            ],
            [
                {"text": "⬅️ VOLTAR", "callback_data": cb_pack(k=k, s="BACK_TF")}
            ]
        ]
    }

# =========================
# TEXTO NORMAL (com ou sem /)
# Ex:
#   relatorio rsi 4h 2d
#   /relatorio cruzamento 15m hoje
#   menu /menu
# =========================
def parse_relatorio_cmd(text):
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

        if pl in ["tendencia", "tendência", "tendencia_longa", "tendência_longa", "longa", "tl"]:
            key = "TENDENCIA"
            continue

        if pl in ["tudo", "all"]:
            key = "ALL"
            continue

    return days, key, tf

# =========================
# LISTENER
# =========================
def listener():
    offset = None
    while True:
        r = requests.get(f"{API}/getUpdates", params={
            "timeout": 30,
            "offset": offset
        }, timeout=35).json()

        for u in r.get("result", []):
            offset = u["update_id"] + 1

            # CALLBACK (botões)
            if "callback_query" in u:
                cq = u.get("callback_query", {})
                cq_id = cq.get("id")
                data = cq.get("data", "")
                msg = cq.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                message_id = msg.get("message_id")

                if str(chat_id) != str(GROUP_CHAT_ID):
                    answer_callback(cq_id)
                    continue

                p = cb_parse(data)
                k = p.get("K")
                f = p.get("F")
                d = p.get("D")
                s = p.get("S")

                if s == "BACK_TYPES":
                    edit_message(message_id, "ESCOLHA O TIPO DO RELATÓRIO:", reply_markup=keyboard_types())
                    answer_callback(cq_id)
                    continue

                if s == "TF":
                    edit_message(message_id, "ESCOLHA O TIMEFRAME:", reply_markup=keyboard_timeframes(k))
                    answer_callback(cq_id)
                    continue

                if s == "BACK_TF":
                    edit_message(message_id, "ESCOLHA O TIMEFRAME:", reply_markup=keyboard_timeframes(k))
                    answer_callback(cq_id)
                    continue

                if s == "DAYS":
                    edit_message(message_id, "ESCOLHA O PERÍODO:", reply_markup=keyboard_days(k, f))
                    answer_callback(cq_id)
                    continue

                if s == "GO":
                    try:
                        days = int(d) if d else 1
                    except:
                        days = 1

                    key = k if k else "ALL"
                    tf = f if f else "ALL"

                    answer_callback(cq_id, text="GERANDO EXCEL...", show_alert=False)

                    filepath = build_report_excel(days=days, alert_key=key, tf_filter=tf)

                    caption = f"📎 RELATÓRIO ({days}D)"
                    if key and key != "ALL":
                        caption += f" — {key}"
                    if tf and tf != "ALL":
                        caption += f" — {tf}"

                    send_excel(filepath, filepath, caption)

                    edit_message(message_id, "ESCOLHA O TIPO DO RELATÓRIO:", reply_markup=keyboard_types())
                    continue

                answer_callback(cq_id)
                continue

            # MESSAGE (comandos + alertas)
            msg = u.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "")

            if str(chat_id) != str(GROUP_CHAT_ID):
                continue

            cmd = text.strip().lower()

            # MENU (com ou sem /)
            if cmd == "/menu" or cmd == "menu":
                send_message("ESCOLHA O TIPO DO RELATÓRIO:", reply_markup=keyboard_types())
                continue

            # RELATORIO (com ou sem /)
            if cmd.startswith("/relatorio") or cmd.startswith("relatorio"):
                days, key, tf = parse_relatorio_cmd(text)

                filepath = build_report_excel(
                    days=days,
                    alert_key=key if key else "ALL",
                    tf_filter=tf if tf else "ALL"
                )

                caption = f"📎 RELATÓRIO ({days}D)"
                if key and key != "ALL":
                    caption += f" — {key}"
                if tf and tf != "ALL":
                    caption += f" — {tf}"

                send_excel(filepath, filepath, caption)
                continue

            # SALVAR ALERTAS (tudo que não é comando)
            if text and not text.startswith("/"):
                parse_and_store(text)

        time.sleep(1)

if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not GROUP_CHAT_ID:
        print("FALTANDO ENV: TELEGRAM_TOKEN e/ou GROUP_CHAT_ID")
    else:
        db_init()
        listener()
