"""
NexosTrade Webhook — reçoit signaux TradingView → poste dans Joe Trade avec boutons Telegram
"""
import os, json, logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx

logging.basicConfig(format="%(asctime)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN        = os.getenv("TELEGRAM_TOKEN", "")
JOETRADE_ID      = int(os.getenv("JOETRADE_GROUP_ID", "-1003942074689"))
JOETRADE_THREAD  = int(os.getenv("JOETRADE_THREAD_TRADING", "0"))  # topic "Signaux" si défini
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "nexos2026")
TGAPI            = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI()

# ── Mémoire en attente (signal reçu, pas encore confirmé) ───────────────────
_pending: dict = {}   # message_id → signal data


async def tg_post(endpoint: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{TGAPI}/{endpoint}", json=payload)
        result = r.json()
        if not result.get("ok"):
            log.error(f"Telegram error [{endpoint}]: {result}")
        else:
            log.info(f"Telegram OK [{endpoint}]")
        return result


def build_signal_text(s: dict) -> str:
    direction = s.get("type", "?").upper()
    symbol    = s.get("symbol", "?")
    entry     = s.get("entry",  "?")
    tp1       = s.get("tp1",    "?")
    tp2       = s.get("tp2",    "?")
    tp3       = s.get("tp3",    "?")
    sl        = s.get("sl",     "?")
    tf        = s.get("tf",     "5m")
    em        = "📈" if direction == "BUY" else "📉"
    col       = "🟢" if direction == "BUY" else "🔴"

    return (
        f"{em} *SIGNAL NEXOSTRADE — {direction}*\n"
        f"{col} *{symbol}* | TF : `{tf}`\n\n"
        f"💠 *Entrée :* `{entry}`\n"
        f"🎯 *TP1 :* `{tp1}`\n"
        f"🎯 *TP2 :* `{tp2}`\n"
        f"🎯 *TP3 :* `{tp3}`\n"
        f"🛑 *SL :*  `{sl}`\n\n"
        f"_John Hoarau — NexosTrade_"
    )


@app.post("/webhook/nexos")
async def receive_signal(request: Request):
    # Vérifie secret optionnel
    secret = request.headers.get("X-Webhook-Secret", "")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Secret invalide")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON invalide")

    log.info(f"Signal reçu: {body}")
    sig_type = body.get("type", "").upper()

    # Signaux TP/SL (notification simple, pas de bouton)
    if sig_type in ("TP1", "TP2", "TP3", "SL"):
        symbol = body.get("symbol", "?")
        price  = body.get("price", "?")
        em = "✅" if sig_type.startswith("TP") else "🛑"
        text = f"{em} *{sig_type} atteint — {symbol}*\nPrix : `{price}`\n\n_NexosTrade_"
        payload = {
            "chat_id":    JOETRADE_ID,
            "text":       text,
            "parse_mode": "Markdown",
        }
        if JOETRADE_THREAD:
            payload["message_thread_id"] = JOETRADE_THREAD
        await tg_post("sendMessage", payload)
        return {"ok": True}

    # Signal BUY / SELL → message avec boutons
    if sig_type not in ("BUY", "SELL"):
        return {"ok": True, "skipped": True}

    # Stocker signal avec ID court (max 64 bytes callback_data)
    import time as _time
    sig_id = str(int(_time.time() * 1000))[-10:]
    _pending[sig_id] = body

    text = build_signal_text(body)
    payload = {
        "chat_id":      JOETRADE_ID,
        "text":         text,
        "parse_mode":   "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Je prends", "callback_data": f"take:{sig_id}"},
                {"text": "❌ Je passe",  "callback_data": "skip"},
            ]]
        }
    }
    if JOETRADE_THREAD:
        payload["message_thread_id"] = JOETRADE_THREAD

    result = await tg_post("sendMessage", payload)
    msg_id = result.get("result", {}).get("message_id")
    log.info(f"Message envoyé → Joe Trade, msg_id={msg_id}, sig_id={sig_id}")
    return {"ok": True}


@app.post("/telegram/callback")
async def telegram_callback(request: Request):
    """Reçoit les updates Telegram (webhook PTB ou polling via proxy)."""
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": True})

    cq = update.get("callback_query")
    if not cq:
        return JSONResponse({"ok": True})

    cq_id   = cq["id"]
    data    = cq.get("data", "")
    user    = cq["from"]
    name    = user.get("first_name", "") + " " + user.get("last_name", "")
    name    = name.strip() or user.get("username", "Membre")

    if data == "skip":
        await tg_post("answerCallbackQuery", {"callback_query_id": cq_id, "text": "OK, tu passes 👍"})
        return JSONResponse({"ok": True})

    if data.startswith("take:"):
        sig_id = data[5:]
        sig = _pending.get(sig_id)
        if not sig:
            await tg_post("answerCallbackQuery", {"callback_query_id": cq_id, "text": "Signal expiré"})
            return JSONResponse({"ok": True})

        direction = sig.get("type", "?").upper()
        symbol    = sig.get("symbol", "?")
        entry     = sig.get("entry", "?")
        tp1       = sig.get("tp1", "?")
        tp2       = sig.get("tp2", "?")
        tp3       = sig.get("tp3", "?")
        sl        = sig.get("sl", "?")
        em        = "📈" if direction == "BUY" else "📉"

        confirm_text = (
            f"{em} *{name} prend le trade !*\n\n"
            f"*{direction} {symbol}*\n"
            f"💠 Entrée : `{entry}`\n"
            f"🎯 TP1 : `{tp1}` | TP2 : `{tp2}` | TP3 : `{tp3}`\n"
            f"🛑 SL : `{sl}`\n\n"
            f"_Bonne chance ! 🚀_"
        )
        payload = {
            "chat_id":    JOETRADE_ID,
            "text":       confirm_text,
            "parse_mode": "Markdown",
        }
        if JOETRADE_THREAD:
            payload["message_thread_id"] = JOETRADE_THREAD

        await tg_post("sendMessage", payload)
        await tg_post("answerCallbackQuery", {
            "callback_query_id": cq_id,
            "text": f"✅ Trade enregistré — {direction} {symbol} @ {entry}"
        })

    return JSONResponse({"ok": True})


@app.get("/health")
async def health():
    return {"status": "ok", "service": "nexos-webhook"}
