import os, re, time, sqlite3, unicodedata
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
    return strip_accents((s or "").lower().strip())

def tg_post(method, json=None, files=None, timeout=60):
    return requests.post(f"{API}/{method}", json=json, files=files, timeout=timeout)

def send_message(text, reply_markup=None):
    payload = {"chat_id": GROUP_CHAT_ID, "text": text}
    if reply_markup: payload["reply_markup"] = reply_markup
    tg_post("sendMessage", json=payload)

def edit_message(message_id, text, reply_markup=None):
    payload = {"chat_id": GROUP_CHAT_ID, "message_id": message_id, "text": text}
    if reply_markup: payload["reply_markup"] = reply_markup
    tg_post("editMessageText", json=payload)

def answer_callback(cq_id, text=None):
    payload = {"callback_query_id": cq_id}
    if text: payload["text"] = text
    tg_post("answerCallbackQuery", json=payload)

def send_excel(path, caption):
    with open(path, "rb") as f:
        tg_post("sendDocument", json={"chat_id": GROUP_CHAT_ID, "caption": caption}, files={"document": f})

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
    conn.commit(); conn.close()

# =========================
# PARSING
# =========================
def extract_symbol(lines):
    for l in lines:
        if re.fullmatch(r"[A-Z0-9]{2,12}", l): return l
    return None

def extract_timeframe(text):
    m = re.search(r"\b(\d{1,2})(M|H|D)\b", text.upper())
    return f"{m.group(1)}{m.group(2)}" if m else None

def extract_rsi(text):
    m = re.search(r"RSI[^0-9]*([0-9]+(\.[0-9]+)?)", text.upper())
    return float(m.group(1)) if m else None

def classify(text):
    t = text.upper()
    if "CRUZAMENTO" in t: return "CRUZAMENTO"
    if "RSI" in t: return "RSI"
    if "TENDENCIA" in t or "TENDÊNCIA" in t: return "TENDENCIA"
    return "OUTROS"

def store_alert(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines: return
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
    conn.commit(); conn.close()

# =========================
# EXCEL
# =========================
def build_excel(days=1, key=None, tf=None):
    since = (datetime.utcnow() - timedelta(days=days)).replace(tzinfo=timezone.utc).isoformat()
    conn = sqlite3.connect(DB); cur = conn.cursor()
    where = ["ts_utc >= ?"]; params = [since]
    if key: where.append("alert_key = ?"); params.append(key)
    if tf: where.append("timeframe = ?"); params.append(tf)
    cur.execute(f"""
        SELECT ts_utc, symbol, timeframe, rsi
        FROM alerts
        WHERE {" AND ".join(where)}
        ORDER BY id DESC
    """, params)
    rows = cur.fetchall(); conn.close()

    wb = Workbook(); ws = wb.active
    ws.append(["DATA","HORA","MOEDA","TIMEFRAME","RSI"])
    for ts, sym, tfv, rsi in rows:
        dt = datetime.fromisoformat(ts).astimezone(TZ_BR)
        ws.append([dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S"), sym or "", tfv or "", rsi or ""])
    name = f"relatorio_{days}d_{key or 'ALL'}_{tf or 'ALL'}.xlsx"
    wb.save(name); return name

# =========================
# BOTÕES
# =========================
def cb_pack(**kvs):
    return "|".join([f"{k}={v}" for k,v in kvs.items()])

def cb_parse(data):
    out={}
    for p in (data or "").split("|"):
        if "=" in p:
            a,b=p.split("=",1); out[a]=b
    return out

def kb_types():
    return {"inline_keyboard":[
        [{"text":"RSI","callback_data":cb_pack(K="RSI",S="TF")}],
        [{"text":"CRUZAMENTO","callback_data":cb_pack(K="CRUZAMENTO",S="TF")}],
        [{"text":"TENDÊNCIA","callback_data":cb_pack(K="TENDENCIA",S="TF")}],
        [{"text":"TUDO","callback_data":cb_pack(K="ALL",S="TF")}],
    ]}

def kb_tf(K):
    return {"inline_keyboard":[
        [{"text":"15M","callback_data":cb_pack(K=K,F="15M",S="D")},
         {"text":"1H","callback_data":cb_pack(K=K,F="1H",S="D")},
         {"text":"4H","callback_data":cb_pack(K=K,F="4H",S="D")}],
        [{"text":"1D","callback_data":cb_pack(K=K,F="1D",S="D")},
         {"text":"TODOS","callback_data":cb_pack(K=K,F="ALL",S="D")}],
        [{"text":"⬅️ VOLTAR","callback_data":cb_pack(S="BACK")}],
    ]}

def kb_days(K,F):
    return {"inline_keyboard":[
        [{"text":"HOJE","callback_data":cb_pack(K=K,F=F,D="1",S="GO")},
         {"text":"2D","callback_data":cb_pack(K=K,F=F,D="2",S="GO")},
         {"text":"5D","callback_data":cb_pack(K=K,F=F,D="5",S="GO")},
         {"text":"7D","callback_data":cb_pack(K=K,F=F,D="7",S="GO")}],
        [{"text":"⬅️ VOLTAR","callback_data":cb_pack(K=K,S="TF")}],
    ]}

# =========================
# MANUAL CMD
# =========================
def parse_manual(text):
    p = norm_cmd(text).split()
    days=1; key=None; tf=None
    for x in p:
        if x=="hoje": days=1
        elif x.endswith("d") and x[:-1].isdigit(): days=int(x[:-1])
        elif x in ["rsi","cruzamento","tendencia"]: key=x.upper()
        elif re.fullmatch(r"\d{1,2}[mhd]", x): tf=x.upper()
    return days, key, tf

# =========================
# LOOP
# =========================
def listener():
    offset=None
    while True:
        r = requests.get(f"{API}/getUpdates", params={"offset":offset,"timeout":30}).json()
        for u in r.get("result",[]):
            offset = u["update_id"]+1

            # CALLBACK
            if "callback_query" in u:
                cq=u["callback_query"]; data=cq.get("data","")
                msg=cq.get("message",{}); chat=msg.get("chat",{}).get("id")
                mid=msg.get("message_id")
                if str(chat)!=str(GROUP_CHAT_ID):
                    answer_callback(cq["id"]); continue
                p=cb_parse(data)
                if p.get("S")=="BACK":
                    edit_message(mid,"ESCOLHA O TIPO:",kb_types()); answer_callback(cq["id"]); continue
                if p.get("S")=="TF":
                    edit_message(mid,"ESCOLHA O TIMEFRAME:",kb_tf(p.get("K"))); answer_callback(cq["id"]); continue
                if p.get("S")=="D":
                    edit_message(mid,"ESCOLHA O PERÍODO:",kb_days(p.get("K"),p.get("F"))); answer_callback(cq["id"]); continue
                if p.get("S")=="GO":
                    days=int(p.get("D","1"))
                    key=None if p.get("K")=="ALL" else p.get("K")
                    tf=None if p.get("F")=="ALL" else p.get("F")
                    f=build_excel(days,key,tf)
                    send_excel(f,"📎 RELATÓRIO")
                    edit_message(mid,"ESCOLHA O TIPO:",kb_types())
                    answer_callback(cq["id"]); continue
                answer_callback(cq["id"]); continue

            # MESSAGE
            msg=u.get("message",{}); chat=msg.get("chat",{}).get("id"); text=msg.get("text","")
            if str(chat)!=str(GROUP_CHAT_ID): continue
            cmd=norm_cmd(text)

            if cmd=="menu":
                send_message("ESCOLHA O TIPO:",kb_types()); continue

            if cmd.startswith("/relatorio") or cmd.startswith("relatorio"):
                days,key,tf=parse_manual(text)
                f=build_excel(days,key,tf)
                send_excel(f,"📎 RELATÓRIO"); continue

            # 🔥 SALVA QUALQUER ALERTA (não comando)
            if text and not text.startswith("/"):
                store_alert(text)

        time.sleep(1)

# =========================
# START
# =========================
if __name__=="__main__":
    if not TELEGRAM_TOKEN or not GROUP_CHAT_ID:
        print("ENV faltando")
    else:
        db_init()
        listener()
