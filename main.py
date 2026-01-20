import os
import re
import time
import json
import sqlite3
import unicodedata
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify
from openpyxl import Workbook


# =========================
# CONFIG
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "").strip()

API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
DB = "alerts.db"

TZ_BR = ZoneInfo("America/Sao_Paulo")

app = Flask(__name__)


# =========================
# BASIC WEB
# =========================
@app.route("/")
def home():
    return "RELATORIO DE ALERTAS - UNIFICADO", 200

@app.route("/health")
def health():
    return "OK", 200


# =========================
# HELPERS
# =========================
def now_utc_iso():
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

def utc_iso_to_br_date_time(ts_iso):
    try:
        dt = datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_br = dt.astimezone(TZ_BR)
        return dt_br.strftime("%Y-%m-%d"), dt_br.strftime("%H:%M:%S")
    except:
        return "", ""

def strip_accents(s: str) -> str:
    s = s or ""
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

def norm_cmd(s: str) -> str:
    s = (s or "").strip()
    s = strip_accents(s).lower()
    return s

def tg_post(method, json_payload=None, data=None, files=None, timeout=60):
    url = f"{API}/{method}"
    return requests.post(url, json=json_payload, data=data, files=files, timeout=timeout)

def send_message(text, reply_markup=None):
    payload = {"chat_id": GROUP_CHAT_ID, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg_post("sendMessage", json_payload=payload, timeout=30)

def edit_message(message_id, text, reply_markup=None):
    payload = {"chat_id": GROUP_CHAT_ID, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg_post("editMessageText", json_payload=payload, timeout=30)

def answer_callback(callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text
    tg_post("answerCallbackQuery", json_payload=payload, timeout=20)

def send_excel(filepath, filename, caption):
    with open(filepath, "rb") as f:
        tg_post(
            "sendDocument",
            data={"chat_id": GROUP_CHAT_ID, "caption": caption},
            files={"document": (filename, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            timeout=90
        )

def clean_alert_time(alert_time):
    if not alert_time:
        return ""
    s = alert_time.strip()
    s = s.replace(" BR", "").replace("BR", "").strip()
    return s


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
            alert_time TEXT,
            raw TEXT
        )
    """)
    conn.commit()
    conn.close()


# =========================
# PARSING (do texto do alerta)
# =========================
def norm_tf(tf):
    if not tf:
        return None
    t = tf.strip().upper().replace(" ", "")
    t = t.replace("MIN", "M").replace("HRS", "H").replace("HR", "H")
    return t

def classify_alert(first_line, full_text):
    t = (first_line or "").upper().strip()

    if t.startswith("CRUZAMENTO MA200") or ("CRUZAMENTO" in t and "MA200" in t):
        return "CRUZAMENTO"

    if t.startswith("RSI"):
        return "RSI"

    if "TENDÊNCIA LONGA" in t or "TENDENCIA LONGA" in t:
        return "TENDENCIA"

    body = (full_text or "").upper()
    if "CRUZAMENTO" in body and "MA200" in body:
        return "CRUZAMENTO"
    if body.startswith("RSI") or "\nRSI" in body:
        return "RSI"
    if "TENDÊNCIA LONGA" in body or "TENDENCIA LONGA" in body:
        return "TENDENCIA"

    return "OUTROS"

def extract_timeframe(first_line, full_text):
    first_line = first_line or ""
    full_text = full_text or ""

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
    if "RSI" in (first_line or "").upper() and "<" in (first_line or ""):
        try:
            v = first_line.split("<", 1)[1].strip()
            rsi = float(v.replace(",", "."))
        except:
            pass

    # Ex: "RSI: 27.66" ou "RSI (1D): 25.53"
    if rsi is None:
        for l in lines:
            ll = l.strip()
            if "RSI" in ll and ":" in ll:
                try:
                    rhs = ll.split(":", 1)[1].strip().replace(",", ".")
                    m = re.search(r"[-+]?\d*\.?\d+", rhs)
                    if m:
                        rsi = float(m.group(0))
                        break
                except:
                    pass

    return rsi

def extract_symbol(lines):
    # pega primeira linha "limpa" que pareça MOEDA
    # (evita pegar RSI/MA200/HORA/PRECO etc)
    blacklist = {"RSI", "MA200", "HORA", "PRECO", "PREÇO", "STOP", "ALVO", "ALVOS", "ENTRADA"}
    for l in lines:
        ll = l.strip()
        if not ll:
            continue
        lu = ll.upper()
        if " " in lu:
            continue
        if len(lu) < 2 or len(lu) > 12:
            continue
        if not re.fullmatch(r"[A-Z0-9]+", lu):
            continue
        if lu in blacklist:
            continue
        # tem que ter pelo menos 1 letra (evita "1234")
        if not re.search(r"[A-Z]", lu):
            continue
        return lu
    return None

def parse_alert_text(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return None

    alert_type = lines[0].strip()
    alert_key = classify_alert(alert_type, text)
    timeframe = extract_timeframe(alert_type, text)
    alert_time = extract_hour(lines)
    rsi = extract_rsi(alert_type, lines)
    symbol = extract_symbol(lines)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "alert_key": alert_key,
        "rsi": rsi,
        "alert_time": alert_time,
        "raw": text
    }


# =========================
# STORE + (optional) SEND
# =========================
def store_alert_from_text(text):
    parsed = parse_alert_text(text)
    if not parsed:
        return False

    # só salva alertas que tenham chave válida (RSI/CRUZAMENTO/TENDENCIA/OUTROS)
    # se quiser ignorar OUTROS, descomenta:
    # if parsed["alert_key"] == "OUTROS":
    #     return False

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO alerts (ts_utc, symbol, timeframe, alert_key, rsi, alert_time, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        now_utc_iso(),
        parsed.get("symbol"),
        parsed.get("timeframe"),
        parsed.get("alert_key"),
        parsed.get("rsi"),
        parsed.get("alert_time"),
        text
    ))
    conn.commit()
    conn.close()
    return True

def record_and_send_alert(text):
    # usado quando ESTE bot gerar o alerta
    store_alert_from_text(text)
    send_message(text)


# =========================
# INGEST ENDPOINT (para unificar de vez)
# =========================
@app.route("/ingest", methods=["POST"])
def ingest():
    # Espera JSON:
    # { "text": "...alerta...", "send_to_group": true/false }
    try:
        data = request.get_json(force=True, silent=True) or {}
    except:
        data = {}

    text = data.get("text", "") or ""
    send_to_group = bool(data.get("send_to_group", False))

    if not text.strip():
        return jsonify({"ok": False, "error": "missing text"}), 400

    ok = store_alert_from_text(text)

    if send_to_group:
        send_message(text)

    return jsonify({"ok": True, "stored": bool(ok)}), 200


# =========================
# EXCEL REPORT
# =========================
def build_report_excel(days=1, alert_key=None, tf_filter=None):
    since_dt = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(days=days)
    since = since_dt.isoformat()

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    where = ["ts_utc >= ?"]
    params = [since]

    if alert_key and alert_key != "ALL":
        where.append("alert_key = ?")
        params.append(alert_key)

    if tf_filter and tf_filter != "ALL":
        where.append("timeframe = ?")
        params.append(tf_filter)

    where_sql = " AND ".join(where)

    cur.execute(f"""
        SELECT ts_utc, symbol, timeframe, rsi, alert_time
        FROM alerts
        WHERE {where_sql}
        ORDER BY id DESC
    """, tuple(params))
    rows = cur.fetchall()
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "RELATORIO"

    ws.append(["DATA", "HORA", "MOEDA", "TIMEFRAME", "RSI"])

    for ts_iso, sym, tf, r, a_time in rows:
        data_br, hora_br = utc_iso_to_br_date_time(ts_iso)

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
# INLINE MENU
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
# TEXT COMMAND PARSER
# =========================
def parse_relatorio_cmd(text):
    parts_raw = (text or "").strip().split()
    parts = [strip_accents(p).lower() for p in parts_raw]

    days = 1
    tf = None
    key = None

    # remove "relatorio" ou "/relatorio" ou "relatório"
    if parts:
        parts = parts[1:]

    for p in parts:
        if p == "hoje":
            days = 1
            continue

        if p.endswith("d"):
            try:
                days = int(p[:-1])
            except:
                pass
            continue

        # timeframe
        if re.fullmatch(r"\d{1,2}(m|h|d)", p):
            tf = norm_tf(p)
            continue

        if p in ["rsi"]:
            key = "RSI"
            continue

        if p in ["cruzamento", "ma200"]:
            key = "CRUZAMENTO"
            continue

        if p in ["tendencia", "tendencia_longa", "tendencia-longa", "tl", "longa"]:
            key = "TENDENCIA"
            continue

        if p in ["tudo", "all"]:
            key = "ALL"
            continue

    return days, key, tf


# =========================
# TELEGRAM POLLING
# =========================
def telegram_listener():
    offset = None
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={
                "timeout": 30,
                "offset": offset
            }, timeout=35).json()
        except:
            time.sleep(2)
            continue

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

            # MESSAGE (comandos digitados)
            msg = u.get("message", {}) or {}
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "") or ""

            if str(chat_id) != str(GROUP_CHAT_ID):
                continue

            cmd = norm_cmd(text)

            # MENU (com ou sem /)
            if cmd == "/menu" or cmd == "menu":
                send_message("ESCOLHA O TIPO DO RELATÓRIO:", reply_markup=keyboard_types())
                continue

            # RELATORIO (com ou sem /, com ou sem acento)
            if cmd.startswith("/relatorio") or cmd.startswith("relatorio") or cmd.startswith("/relatorio") or cmd.startswith("relatorio"):
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

        time.sleep(1)


# =========================
# START
# =========================
def run_flask():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not GROUP_CHAT_ID:
        print("FALTANDO ENV: TELEGRAM_TOKEN e/ou GROUP_CHAT_ID")
    else:
        db_init()

        t = threading.Thread(target=run_flask, daemon=True)
        t.start()

        telegram_listener()
