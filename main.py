# main.py — OURO ROTA DIÁRIA V22.2 (EMA9>MA20 Diário)
# Relatório diário com detecção de reversões e cruzamento EMA9>MA20 no 1D
# Execução automática no deploy

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
TOP_N = 120
REQ_TIMEOUT = 10
VERSION = "OURO ROTA DIÁRIA V22.2 (EMA9>MA20 Diário)"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} — Execução automática no deploy", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

async def tg(session, text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[TG] Token ou Chat ID ausente.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=REQ_TIMEOUT)
    except Exception as e:
        print(f"[TG ERRO] {e}")

def ema_series(values, period):
    ema = []
    k = 2 / (period + 1)
    for i, v in enumerate(values):
        if i == 0:
            ema.append(v)
        else:
            ema.append(v * k + ema[-1] * (1 - k))
    return ema

def ma_series(values, period):
    ma = []
    for i in range(len(values)):
        if i < period:
            ma.append(sum(values[:i+1]) / (i+1))
        else:
            ma.append(sum(values[i-period+1:i+1]) / period)
    return ma

def calc_prob(candles):
    try:
        closes = [float(k[4]) for k in candles]
        if len(closes) < 2:
            return 0
        diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        ups = sum(1 for d in diffs if d > 0)
        return ups / len(diffs)
    except:
        return 0

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval="1h", limit=48):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            return await r.json()
    except:
        return []

async def get_top_usdt_symbols(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with session.get(url, timeout=REQ_TIMEOUT) as r:
        data = await r.json()
    blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","EUR","BRL","PERP","TEST","USDE")
    pares = []
    for d in data:
        s = d.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        if any(x in s for x in blocked):
            continue
        qv = float(d.get("quoteVolume", 0) or 0)
        change = float(d.get("priceChangePercent", 0) or 0)
        pares.append((s, qv, change))
    pares.sort(key=lambda x: x[1], reverse=True)
    return pares[:TOP_N]

# ---------------- FILTRO TENDÊNCIA 1D ----------------
def tendencia_1d(candles):
    try:
        closes = [float(k[4]) for k in candles if len(k) >= 5]
        if len(closes) < 50:
            return False

        ema9 = ema_series(closes, 9)
        ma20 = ma_series(closes, 20)

        e9_prev2, e9_prev, e9_now = ema9[-3], ema9[-2], ema9[-1]
        m20_prev2, m20_prev, m20_now = ma20[-3], ma20[-2], ma20[-1]

        cruzamento = (e9_prev2 < m20_prev2) and (e9_prev > m20_prev) and (e9_now > m20_now)
        diferenca = (e9_now - m20_now) / m20_now

        if cruzamento and diferenca > 0.0015:
            return True
        return False
    except Exception as e:
        print(f"[tendencia_1d ERRO] {e}")
        return False

# ---------------- RELATÓRIO ----------------
async def gerar_relatorio():
    async with aiohttp.ClientSession() as session:
        print(f"[{now_br()}] Iniciando geração do relatório...")
        pares = await get_top_usdt_symbols(session)
        resultados = []
        tendencia_diaria = []

        for s, vol, change in pares:
            kl = await get_klines(session, s, "1h", 48)
            kl_1d = await get_klines(session, s, "1d", 100)

            prob = calc_prob(kl)
            if tendencia_1d(kl_1d):
                tendencia_diaria.append(s)

            resultados.append((s, prob, change))

        resultados.sort(key=lambda x: x[1], reverse=True)
        altas = resultados[:10]
        quedas = resultados[-10:]

        texto = "<b>📊 RELATÓRIO DIÁRIO — OURO ROTA DIÁRIA</b>\n"
        texto += f"⏰ {now_br()} BR\n\n"

        texto += "🔥 <b>Top 10 Probabilidades de Alta:</b>\n"
        for s, p, ch in altas:
            direcao = "⬆️" if ch >= 0 else "⚠️"
            texto += f"{direcao} {s}: {p*100:.1f}% | {ch:+.2f}% 24h\n"

        texto += "\n❄️ <b>Top 10 Probabilidades de Queda:</b>\n"
        for s, p, ch in quedas:
            direcao = "⬇️" if ch <= 0 else "⚠️"
            texto += f"{direcao} {s}: {p*100:.1f}% | {ch:+.2f}% 24h\n"

        texto += f"\n📈 <b>Moedas com tendência real no 1D</b> (EMA9>MA20 + 0.15% confirmada):\n"
        if tendencia_diaria:
            texto += ", ".join(tendencia_diaria)
        else:
            texto += "Nenhuma moeda com cruzamento confirmado."

        texto += f"\n\n📊 Total analisado: {len(resultados)} pares\n"
        texto += f"🟢 Relatório gerado automaticamente no deploy\n"

        await tg(session, texto)
        print(f"[{now_br()}] RELATÓRIO ENVIADO COM SUCESSO")

# ---------------- MAIN ----------------
def start_bot():
    asyncio.run(gerar_relatorio())

if __name__ == "__main__":
    import threading
    threading.Thread(target=start_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
