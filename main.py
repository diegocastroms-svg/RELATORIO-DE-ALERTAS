# main.py — OURO ROTA DIÁRIA (V20.3 PROBABILIDADE + EXECUÇÃO IMEDIATA)
# Relatório diário automático: probabilidade + variação 24h + direção
# Executa automaticamente ao fazer deploy (sem esperar 20h)

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
TOP_N = 120
REQ_TIMEOUT = 10
VERSION = "OURO ROTA DIÁRIA V20.3 — PROBABILIDADE + EXECUÇÃO IMEDIATA"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} — relatório gerado automaticamente no deploy", 200

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

# ---------------- CÁLCULO DE PROBABILIDADE ----------------
def calc_prob(candles):
    try:
        closes = [float(k[4]) for k in candles]
        highs = [float(k[2]) for k in candles]
        lows = [float(k[3]) for k in candles]
        volumes = [float(k[5]) for k in candles]
        if len(closes) < 20:
            return 0

        # RSI simplificado (14 períodos)
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d for d in deltas if d > 0]
        losses = [-d for d in deltas if d < 0]
        avg_gain = sum(gains[-14:]) / 14 if len(gains) >= 14 else 0.001
        avg_loss = sum(losses[-14:]) / 14 if len(losses) >= 14 else 0.001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        # Direção de tendência (EMA9 vs EMA20)
        def ema(values, n):
            k = 2 / (n + 1)
            ema_val = values[0]
            for v in values[1:]:
                ema_val = v * k + ema_val * (1 - k)
            return ema_val

        ema9 = ema(closes[-20:], 9)
        ema20 = ema(closes[-20:], 20)
        tendencia = 1 if ema9 > ema20 else -1

        # Força de volume (última média x atual)
        vol_med = sum(volumes[-10:]) / 10
        vol_force = volumes[-1] / vol_med if vol_med > 0 else 1

        # Probabilidade composta (padrão OURO)
        prob = 0
        if rsi < 35 and tendencia == -1 and vol_force > 1.2:
            prob = 0.85  # reversão provável
        elif rsi > 55 and tendencia == 1 and vol_force > 1:
            prob = 0.75  # continuação provável
        elif 45 <= rsi <= 55:
            prob = 0.5  # neutro
        elif rsi < 25:
            prob = 0.9  # repique forte provável
        else:
            prob = 0.3  # baixa probabilidade

        return prob
    except Exception as e:
        print(f"[calc_prob ERRO] {e}")
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

# ---------------- RELATÓRIO ----------------
async def gerar_relatorio():
    async with aiohttp.ClientSession() as session:
        print(f"[{now_br()}] Iniciando geração do relatório...")
        pares = await get_top_usdt_symbols(session)
        resultados = []

        for s, vol, change in pares:
            kl = await get_klines(session, s)
            prob = calc_prob(kl)
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

        texto += f"\n📈 Total analisado: {len(resultados)} pares\n"
        texto += f"\n🟢 Relatório gerado automaticamente no deploy\n"
        await tg(session, texto)
        print(f"[{now_br()}] RELATÓRIO ENVIADO COM SUCESSO")

# ---------------- EXECUÇÃO IMEDIATA ----------------
async def agendar_execucao():
    print(f"[{now_br()}] OURO ROTA DIÁRIA ATIVO — gerando relatório imediato no deploy.")
    await gerar_relatorio()
    while True:
        await asyncio.sleep(3600)  # mantém o loop ativo sem repetir

# ---------------- MAIN ----------------
def start_bot():
    asyncio.run(agendar_execucao())

if __name__ == "__main__":
    import threading
    threading.Thread(target=start_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
