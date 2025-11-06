import json
import os
import re
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import List, Dict, Any

import requests  # nécessite: pip install requests

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

TOKEN = os.getenv("TOKEN")
ANNOUNCE_CHANNEL_ID = -1003199435152  # ID du canal annonces
DISCUSSION_CHAT_ID = -1003203628589   # ID du canal de discussion
TRADES_FILE = "trades.json"


# ---------- Stockage ----------

def load_trades() -> List[Dict[str, Any]]:
    if not os.path.exists(TRADES_FILE):
        return []
    with open(TRADES_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def save_trades(trades: List[Dict[str, Any]]) -> None:
    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2, default=str)


def add_trade(trade: Dict[str, Any]) -> None:
    trades = load_trades()
    trades.append(trade)
    save_trades(trades)


def clear_all_trades() -> None:
    """Supprime tous les trades."""
    save_trades([])


def clear_trades_by_symbol(symbol: str) -> int:
    """
    Supprime les trades dont la paire contient le symbole donné (ex: 'SOL').
    Renvoie le nombre de trades supprimés.
    """
    sym = symbol.upper()
    trades = load_trades()
    kept: List[Dict[str, Any]] = []
    removed = 0
    for t in trades:
        pair = str(t.get("pair", "")).upper()
        if sym in pair:
            removed += 1
        else:
            kept.append(t)
    save_trades(kept)
    return removed


def get_open_trades(limit: int = 10) -> List[Dict[str, Any]]:
    """
    Renvoie les trades enregistrés (on ne stocke que les trades ouverts).
    """
    trades = load_trades()
    return trades[-limit:]


# ---------- Utils parsing ----------

def extract_floats(line: str) -> List[float]:
    """Récupère tous les nombres (float) dans une ligne, peu importe les espaces/virgules."""
    txt = line.replace(",", ".")
    matches = re.findall(r"[-+]?\d*\.?\d+", txt)
    values: List[float] = []
    for m in matches:
        if m in ("", "+", "-", ".", "-."):
            continue
        try:
            values.append(float(m))
        except ValueError:
            pass
    return values


# ---------- API prix (CoinGecko) ----------

COINGECKO_SYMBOL_MAP: Dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "DOGE": "dogecoin",
    "PEPE": "pepe",
    "OP": "optimism",
    "ARB": "arbitrum",
    "LINK": "chainlink",
    "TON": "toncoin",
}

COINGECKO_DYNAMIC_MAP: Dict[str, str] = {}


def resolve_coingecko_id(symbol: str) -> str | None:
    """
    Résout un ticker (ex: 'BTC') vers un id CoinGecko (ex: 'bitcoin').
    1) map statique
    2) map dynamique
    3) API /search
    """
    sym = symbol.upper()

    if sym in COINGECKO_SYMBOL_MAP:
        return COINGECKO_SYMBOL_MAP[sym]

    if sym in COINGECKO_DYNAMIC_MAP:
        return COINGECKO_DYNAMIC_MAP[sym]

    try:
        url = "https://api.coingecko.com/api/v3/search"
        resp = requests.get(url, params={"query": sym}, timeout=5)
        data = resp.json()
        coins = data.get("coins", [])

        if not coins:
            print(f"[!] CoinGecko search: aucun résultat pour le symbole {sym}")
            return None

        best_id = None

        for c in coins:
            cg_sym = str(c.get("symbol", "")).upper()
            if cg_sym == sym:
                best_id = c.get("id")
                break

        if not best_id:
            best_id = coins[0].get("id")

        if not best_id:
            print(f"[!] CoinGecko search: impossible de déterminer un id pour {sym} -> {coins}")
            return None

        COINGECKO_DYNAMIC_MAP[sym] = best_id
        print(f"[+] CoinGecko mapping auto: {sym} -> {best_id}")
        return best_id

    except Exception as e:
        print(f"[!] Erreur CoinGecko search pour {sym} : {e}")
        return None


def get_price_for_pair(pair: str) -> float | None:
    """
    Récupère le prix du coin via CoinGecko.
    On suppose des paires du type 'BTC/USDT', 'SOL/USDT', etc.
    On récupère le prix en USD et on l'utilise comme prix USDT (approximation suffisante).
    """
    try:
        base = pair.split("/")[0].upper()  # 'BTC/USDT' -> 'BTC'
        cg_id = resolve_coingecko_id(base)
        if cg_id is None:
            print(f"[!] CoinGecko: impossible de résoudre un id pour {base}")
            return None

        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": cg_id, "vs_currencies": "usd"}
        resp = requests.get(url, params=params, timeout=5)
        data = resp.json()

        if cg_id not in data or "usd" not in data[cg_id]:
            print(f"[!] CoinGecko: pas de prix trouvé pour {pair} (id={cg_id}) -> {data}")
            return None

        price_usd = float(data[cg_id]["usd"])
        return price_usd  # 1 USDT ≈ 1 USD

    except Exception as e:
        print(f"[!] Erreur API CoinGecko pour {pair} : {e}")
        return None


def update_trade_with_price(trade: Dict[str, Any], price: float) -> tuple[bool, str | None]:
    """
    Met à jour un trade en fonction du prix courant.
    - coche les PE touchées
    - coche les TP touchés
    - Si TP1 touché pour la première fois, passe SL au BE automatiquement
    - renvoie (should_close, reason) -> si True, le trade est retiré de la liste
    """
    side = str(trade.get("side", "")).upper()
    entries: List[float] = trade.get("entries") or []
    tps: List[float] = trade.get("tps") or []
    sl = trade.get("sl", None)

    hit_tps: List[int] = trade.get("hit_tps") or []
    hit_entries: List[int] = trade.get("hit_entries") or []

    # --- PE touchées ---
    if side in ("LONG", "BUY"):
        for i, e in enumerate(entries):
            if i not in hit_entries and price <= e:
                hit_entries.append(i)
    elif side in ("SHORT", "SELL"):
        for i, e in enumerate(entries):
            if i not in hit_entries and price >= e:
                hit_entries.append(i)

    trade["hit_entries"] = hit_entries

    # --- TP touchées ---
    tp1_was_hit = 0 in hit_tps  # État avant update
    if side in ("LONG", "BUY"):
        for i, tp in enumerate(tps):
            if i not in hit_tps and price >= tp:
                hit_tps.append(i)
    elif side in ("SHORT", "SELL"):
        for i, tp in enumerate(tps):
            if i not in hit_tps and price <= tp:
                hit_tps.append(i)

    trade["hit_tps"] = hit_tps

    # --- Auto BE si TP1 touché pour la première fois ---
    tp1_now_hit = 0 in hit_tps
    if tp1_now_hit and not tp1_was_hit and sl is not None:
        # Calcul BE selon side
        be_price = None
        if entries:
            if side in ("LONG", "BUY"):
                be_price = max(entries)   # BE = entry la plus haute (long)
            elif side in ("SHORT", "SELL"):
                be_price = min(entries)   # BE = entry la plus basse (short)
        elif trade.get("entry") is not None:
            be_price = trade["entry"]

        if be_price is not None:
            trade["sl"] = be_price
            trade["sl_note"] = "BE"
            print(f"[*] SL auto passé au BE pour {trade.get('pair')} (TP1 touché, be={be_price})")

    # --- SL / full TP -> fermeture ---
    should_close = False
    reason: str | None = None

    if side in ("LONG", "BUY"):
        if sl is not None and price <= sl:
            should_close = True
            reason = f"SL touchée (prix {price} <= SL {sl})"
        elif tps:
            max_tp = max(tps)
            if price >= max_tp:
                should_close = True
                reason = f"Dernier TP atteint (prix {price} >= TP max {max_tp})"
    elif side in ("SHORT", "SELL"):
        if sl is not None and price >= sl:
            should_close = True
            reason = f"SL touchée (prix {price} >= SL {sl})"
        elif tps:
            min_tp = min(tps)
            if price <= min_tp:
                should_close = True
                reason = f"Dernier TP atteint (prix {price} <= TP min {min_tp})"

    return should_close, reason


async def job_check_prices(context: ContextTypes.DEFAULT_TYPE):
    """
    Job périodique : vérifie les prix, coche PE/TP, ferme les trades si SL ou TP final touché.
    """
    trades = load_trades()
    if not trades:
        return

    price_cache: Dict[str, float] = {}
    kept: List[Dict[str, Any]] = []

    print("\n📊 --- Vérification automatique des prix ---")

    for t in trades:
        pair = t.get("pair")
        side = t.get("side", "?")
        entries = t.get("entries") or [t.get("entry")]
        sl = t.get("sl")
        tps = t.get("tps") or []

        if not pair:
            kept.append(t)
            continue

        if pair not in price_cache:
            price = get_price_for_pair(pair)
            if price is None:
                print(f"[!] ❌ Impossible de récupérer le prix pour {pair}")
                kept.append(t)
                continue
            price_cache[pair] = price
        else:
            price = price_cache[pair]

        entries_str = ", ".join(str(e) for e in entries) if entries else "-"
        tps_str = ", ".join(str(tp) for tp in tps) if tps else "-"

        print(f"💰 {pair}: {price} | {side} | Entry(s): {entries_str} | SL: {sl} | TP(s): {tps_str}")

        should_close, reason = update_trade_with_price(t, price)
        if should_close:
            if reason:
                print(f"[-] Trade fermé par prix ({pair}) au prix {price} – {reason}")
            else:
                print(f"[-] Trade fermé par prix ({pair}) au prix {price}")
        else:
            kept.append(t)

    save_trades(kept)
    print("✅ Vérification terminée.")


# ---------- Mini serveur HTTP pour Render (healthcheck) ----------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    # On supprime le log HTTP bruyant
    def log_message(self, format, *args):
        return


def start_health_server():
    """Petit serveur HTTP juste pour Render, ne renvoie que 'OK'."""
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[health] HTTP server listening on port {port}")
    server.serve_forever()


# ---------- Parsing des calls ----------

def parse_trade_message(text: str, message_id: int) -> Dict[str, Any] | None:
    """
    Essaye de détecter un call de manière assez large.

    Exemples acceptés :
    🐰 LONG BTC/USDT
    LONG BTC/USDT
    XRP SHORT

    Puis plus bas :
    Entry: 95000
    PE: 2.451 3.125
    SL: 93500
    TP1: 97000
    TP2: 98000
    TP3: 99000
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return None

    first = lines[0]
    tokens = first.split()

    if not tokens:
        return None

    # Si le premier token est un emoji ou ne contient aucun caractère alphanumérique,
    # on l'enlève (cas 🐰 LONG BTC/USDT)
    if not any(ch.isalnum() for ch in tokens[0]) and len(tokens) > 1:
        tokens = tokens[1:]

    if len(tokens) < 2:
        return None

    sides = {"LONG", "SHORT", "BUY", "SELL"}
    side = None
    pair = None

    # Cas : LONG BTC/USDT
    if tokens[0].upper() in sides:
        side = tokens[0].upper()
        pair = tokens[1].upper()
    # Cas : XRP SHORT
    elif tokens[1].upper() in sides:
        side = tokens[1].upper()
        pair = tokens[0].upper()
    else:
        return None

    entries: List[float] | None = None
    sl = None
    tps: List[float] = []

    for line in lines[1:]:
        lower = line.lower()

        if ":" in line:
            after_colon = line.split(":", 1)[1]
        else:
            after_colon = line

        nums = extract_floats(after_colon)
        if not nums:
            continue

        if lower.startswith(("entry", "entrée", "entree", "pe")):
            if entries is None:
                entries = nums

        elif lower.startswith("sl"):
            if sl is None:
                sl = nums[0]

        elif lower.startswith("tp"):
            tps.extend(nums)

    if not entries or sl is None:
        return None

    entry_main = entries[0]

    trade = {
        "origin_message_id": message_id,
        "pair": pair,
        "side": side,
        "entry": entry_main,
        "entries": entries,
        "sl": sl,
        "tps": tps,
        "hit_tps": [],
        "hit_entries": [],
        "sl_note": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return trade


# ---------- Helpers droits ----------

async def user_is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Vérifie si l'utilisateur est admin du chat où il utilise la commande."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False

    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in ("administrator", "creator")


# ---------- Handlers Telegram ----------

async def handle_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reçoit les messages du canal d'annonces, détecte les nouveaux calls uniquement."""
    message = update.effective_message
    if message.chat_id != ANNOUNCE_CHANNEL_ID:
        return

    text = message.text or message.caption
    if not text:
        return

    # Vérification du smiley cerveau : si présent, on ignore pour ajouter un trade
    if '🧠' in text:
        print(f"[!] Message ignoré pour parsing trade (contient 🧠) : '{text[:50]}...'")
        return

    # On essaie de parser un nouveau call
    trade = parse_trade_message(text, message_id=message.message_id)
    if trade:
        add_trade(trade)
        print(
            f"[+] Nouveau trade enregistré : {trade['side']} {trade['pair']} | "
            f"entries={trade['entries']} | tps={trade['tps']}"
        )


async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche les trades ouverts, format joli (accessible à tout le monde)."""
    chat = update.effective_chat
    if not chat or chat.id != DISCUSSION_CHAT_ID:
        return  # 🔒 on ne répond qu'au canal discussion

    trades = get_open_trades(limit=20)
    if not trades:
        await update.message.reply_text(
            "📭 Aucun trade ouvert pour le moment.\n\n"
            "⚠️ Attention : les données affichées ici sont gérées par un bot. "
            "En cas de doute, référez-vous en priorité au canal annonces, car des erreurs sont possibles."
        )
        return

    blocks = []
    for idx, t in enumerate(trades, start=1):
        pair = t.get("pair", "?")
        side = t.get("side", "?")
        entries = t.get("entries") or [t.get("entry")]
        sl = t.get("sl")
        tps = t.get("tps") or []
        hit_tps: List[int] = t.get("hit_tps") or []
        hit_entries: List[int] = t.get("hit_entries") or []
        sl_note = t.get("sl_note", "")
        created = t.get("created_at")

        # Entrées (PE) avec ✅ si touchées
        if len(entries) == 1:
            mark = " ✅" if 0 in hit_entries else ""
            entries_line = f"🏁 Entry : {entries[0]}{mark}"
        else:
            parts = []
            for i, e in enumerate(entries, start=1):
                mark = " ✅" if (i - 1) in hit_entries else ""
                parts.append(f"PE{i}: {e}{mark}")
            entries_line = "🏁 " + " | ".join(parts)

        # SL
        if sl is not None:
            if sl_note:
                sl_line = f"🛡 SL : {sl} ({sl_note})"
            else:
                sl_line = f"🛡 SL : {sl}"
        else:
            sl_line = "🛡 SL : ?"

        # TP : on affiche TP1, TP2... avec ✅ si atteint
        if tps:
            tp_parts = []
            for i, value in enumerate(tps, start=1):
                mark = " ✅" if (i - 1) in hit_tps else ""
                tp_parts.append(f"TP{i}: {value}{mark}")
            tp_line = "🎯 " + " | ".join(tp_parts)
        else:
            tp_line = "🎯 TP : -"

        # Date (optionnel)
        created_line = ""
        if created:
            try:
                dt = created.split(".")[0].replace("T", " ")
                created_line = f"⏱ Créé : {dt} UTC"
            except Exception:
                pass

        block_lines = [
            f"📊 Trade #{idx}",
            f"{pair} – {side}",
            entries_line,
            sl_line,
            tp_line,
        ]
        if created_line:
            block_lines.append(created_line)

        blocks.append("\n".join(block_lines))

    text = "\n\n".join(blocks)
    text += (
        "\n\n⚠️ Attention : les données affichées ici sont gérées par un bot. "
        "En cas de doute, référez-vous en priorité au canal annonces, car des erreurs sont possibles."
    )
    await update.message.reply_text(text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Message d’accueil (admins seulement, dans le canal discussion)."""
    chat = update.effective_chat
    if not chat or chat.id != DISCUSSION_CHAT_ID:
        return  # 🔒 ne répond qu'au canal discussion

    if not await user_is_admin(update, context):
        await update.message.reply_text("⛔ Seuls les admins du chat peuvent utiliser /start.")
        return

    await update.message.reply_text(
        "Bot UlysseTif prêt à stocker les calls 📈\n"
        "Utilise /trades pour voir les trades ouverts."
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Vide les trades (admins seulement).
    - /clear           -> supprime tous les trades
    - /clear sol       -> supprime uniquement les trades dont la paire contient 'SOL'
    """
    chat = update.effective_chat
    if not chat or chat.id != DISCUSSION_CHAT_ID:
        return  # 🔒 ne répond qu'au canal discussion

    if not await user_is_admin(update, context):
        await update.message.reply_text("⛔ Seuls les admins du chat peuvent utiliser /clear.")
        return

    args = context.args
    if not args:
        clear_all_trades()
        await update.message.reply_text("🧹 Tous les trades ont été supprimés.")
        return

    symbol = args[0]
    removed = clear_trades_by_symbol(symbol)
    if removed == 0:
        await update.message.reply_text(f"ℹ️ Aucun trade trouvé pour '{symbol.upper()}'.")
    else:
        await update.message.reply_text(f"🧹 {removed} trade(s) supprimé(s) pour '{symbol.upper()}'.")    


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Édite un trade manuellement (admins seulement).
    Syntaxe :
      /edit BTC sl 102458
      /edit BTC tp1 106453

    - Cherche le DERNIER trade dont la paire contient 'BTC'
    - sl  : met à jour la SL (sans afficher 'edited'), et enlève éventuellement la note BE
    - tpN : met à jour TPN et reset les ✅ sur TP
    """
    chat = update.effective_chat
    if not chat or chat.id != DISCUSSION_CHAT_ID:
        return  # 🔒 ne répond qu'au canal discussion

    if not await user_is_admin(update, context):
        await update.message.reply_text("⛔ Seuls les admins du chat peuvent utiliser /edit.")
        return

    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "❓ Usage :\n"
            "/edit BTC sl 102458\n"
            "/edit BTC tp1 106453"
        )
        return

    symbol = args[0].upper()
    field = args[1].lower()
    value_raw = args[2]

    # Conversion en float
    try:
        new_value = float(value_raw.replace(",", "."))
    except ValueError:
        await update.message.reply_text(f"❌ Valeur invalide : {value_raw}")
        return

    trades = load_trades()
    target_index = None

    # On prend le DERNIER trade qui matche le symbole (comme "plus récent")
    for i in range(len(trades) - 1, -1, -1):
        pair = str(trades[i].get("pair", "")).upper()
        if symbol in pair:
            target_index = i
            break

    if target_index is None:
        await update.message.reply_text(f"ℹ️ Aucun trade trouvé pour '{symbol}'.")
        return

    trade = trades[target_index]
    pair_label = trade.get("pair", "?")

    # Édition de la SL
    if field == "sl":
        trade["sl"] = new_value
        # si une note existait (ex: 'BE'), on l'enlève quand on modifie la SL à la main
        trade["sl_note"] = ""
        trades[target_index] = trade
        save_trades(trades)
        await update.message.reply_text(
            f"✏️ SL mise à jour pour {pair_label} : {new_value}."
        )
        return

    # Édition d'un TP : tp1, tp2, ...
    if field.startswith("tp"):
        m = re.match(r"tp(\d+)", field)
        if not m:
            await update.message.reply_text(
                "❌ Champ non supporté. Utilise 'sl' ou 'tp1', 'tp2', ..."
            )
            return

        tp_index = int(m.group(1)) - 1  # tp1 -> index 0
        if tp_index < 0:
            await update.message.reply_text("❌ Index de TP invalide.")
            return

        tps: List[float] = trade.get("tps") or []

        # Étend la liste si besoin
        if tp_index < len(tps):
            tps[tp_index] = new_value
        else:
            # Si le TP demandé n'existe pas encore, on complète jusqu'à cet index
            while len(tps) < tp_index:
                tps.append(new_value)
            tps.append(new_value)

        trade["tps"] = tps
        # On reset les TP touchés pour que les ✅ soient recalculées sur les nouvelles valeurs
        trade["hit_tps"] = []

        trades[target_index] = trade
        save_trades(trades)

        await update.message.reply_text(
            f"✏️ TP{tp_index + 1} mis à jour pour {pair_label} : {new_value}.\n"
            f"✅ Les confirmations de TP seront recalculées sur cette nouvelle valeur."
        )
        return

    # Champ inconnu
    await update.message.reply_text(
        "❌ Champ non supporté. Utilise :\n"
        "- 'sl' pour la stop-loss\n"
        "- 'tp1', 'tp2', ... pour les take-profits"
    )


def main():
    app = Application.builder().token(TOKEN).build()

    # ✅ Job de vérification des prix toutes les 60 secondes (1 minute, commence après 10s)
    app.job_queue.run_repeating(job_check_prices, interval=60, first=10)

    # 🧠 On démarre le mini serveur HTTP pour Render dans un thread séparé
    threading.Thread(target=start_health_server, daemon=True).start()

    # Messages du canal d'annonces (uniquement ce canal, et pas les commandes)
    app.add_handler(
        MessageHandler(
            filters.Chat(ANNOUNCE_CHANNEL_ID) & (~filters.COMMAND),
            handle_announce
        )
    )

    # Commandes dans le canal de discussion uniquement
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("trades", cmd_trades))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("edit", cmd_edit))

    print("Bot lancé...")
    app.run_polling()


if __name__ == "__main__":
    main()
