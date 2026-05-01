from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber
import re
import io
import os
import uuid
from datetime import datetime, date, timedelta
from typing import Optional, Tuple
from supabase import create_client, Client
import uvicorn
from dotenv import load_dotenv
from pathlib import Path
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import threading

load_dotenv()

# env_path = Path(__file__).parent / ".env.local"
# load_dotenv(dotenv_path=env_path, override=True)

app = FastAPI(title="PSX Profit Tracker API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_CHANNELS = {
    "intraday": os.getenv("DISCORD_INTRADAY_CHANNEL"),
    "swing": os.getenv("DISCORD_SWING_CHANNEL"),
}

def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def verify_token(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid token")
    return authorization.replace("Bearer ", "")

# ─── DISCORD MESSAGING ───────────────────────
def send_discord_message(channel_id: str, embed_data: dict = None, plain_text: str = None):
    """Send message to Discord channel using bot token"""
    url = f"https://discordapp.com/api/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    
    data = {}
    if embed_data:
        data["embeds"] = [embed_data]
    if plain_text:
        data["content"] = plain_text
    
    try:
        response = requests.post(url, json=data, headers=headers, timeout=10)
        if response.status_code in [200, 204]:
            return True, "Message sent"
        else:
            return False, f"Discord error: {response.status_code} - {response.text}"
    except Exception as e:
        return False, f"Error sending to Discord: {str(e)}"

def create_embed(trade_data: dict) -> dict:
    """Create a Discord embed for trade data"""
    return {
        "title": f"📊 {trade_data['symbol']} Trade Alert",
        "description": f"Trade scheduled for {trade_data['trade_date']} at {trade_data['trade_time']}",
        "color": 3066993 if trade_data.get('result', '').lower() == 'win' else 15158332,
        "fields": [
            {"name": "Symbol", "value": trade_data['symbol'], "inline": True},
            {"name": "Buy Price", "value": f"Rs. {trade_data['buy_price']}", "inline": True},
            {"name": "Sell Price", "value": f"Rs. {trade_data['sell_price']}", "inline": True},
            {"name": "Stop Loss", "value": f"Rs. {trade_data['stop_loss']}", "inline": True},
            {"name": "Difference", "value": f"Rs. {trade_data.get('difference', 'N/A')}", "inline": True},
            {"name": "Result", "value": trade_data.get('result', 'Pending'), "inline": True},
        ],
        "timestamp": datetime.utcnow().isoformat()
    }

def create_plain_text(trade_data: dict) -> str:
    """Create plain text message for Discord"""
    return f"""
📊 **{trade_data['symbol']} Trade Alert**
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Date: {trade_data['trade_date']} at {trade_data['trade_time']}
Buy Price: Rs. {trade_data['buy_price']}
Sell Price: Rs. {trade_data['sell_price']}
Stop Loss: Rs. {trade_data['stop_loss']}
Difference: Rs. {trade_data.get('difference', 'N/A')}
Result: {trade_data.get('result', 'Pending')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """

def send_scheduled_messages():
    """Background job: check and send scheduled Discord messages"""
    supabase = get_supabase()
    now = datetime.utcnow()
    
    try:
        # Get messages that should be sent now
        messages = supabase.table("discord_messages").select("*").eq(
            "status", "scheduled"
        ).lte("scheduled_at", now.isoformat()).execute().data
        
        for msg in messages:
            # Prepare trade data
            trade_data = {
                "symbol": msg["symbol"],
                "trade_date": msg["trade_date"].isoformat() if isinstance(msg["trade_date"], date) else msg["trade_date"],
                "trade_time": msg["trade_time"],
                "buy_price": msg["buy_price"],
                "sell_price": msg["sell_price"],
                "stop_loss": msg["stop_loss"],
                "difference": msg.get("difference"),
                "result": msg.get("result"),
            }
            
            # Send message
            embed = create_embed(trade_data) if msg["message_format"] == "embed" else None
            plain = create_plain_text(trade_data) if msg["message_format"] == "plain_text" else None
            
            success, error = send_discord_message(msg["channel_id"], embed, plain)
            
            # Update status
            update_data = {
                "status": "sent" if success else "failed",
                "sent_at": datetime.utcnow().isoformat(),
            }
            if not success:
                update_data["error_message"] = error
            
            supabase.table("discord_messages").update(update_data).eq("id", msg["id"]).execute()
            
            status_txt = "✓ sent" if success else f"✗ failed: {error}"
            print(f"  Discord: {msg['symbol']} to {msg['channel_name']} {status_txt}")
    
    except Exception as e:
        print(f"  Discord scheduler error: {str(e)}")

def validate_pdf(text: str) -> Tuple[bool, str]:
    tl = text.lower()
    if "transaction statement" not in tl:
        return False, "Not a Transaction Statement PDF"
    if "munirkhanani" not in tl:
        return False, "Not a Munir Khanani Securities document"
    if "settlement" not in tl:
        return False, "Missing Settlement information"
    if not any(kw in text for kw in ["BUY", "SELL", "T+1REG", "T+0REG"]):
        return False, "No BUY/SELL trade records found"
    return True, "Valid"

STOCK_HEADER_RE = re.compile(
    r'^([A-Z][A-Z0-9\s&\.\-]+?)\s{2,}([A-Z]{2,10})$'
    r'|^([A-Z][A-Z0-9\s&\.\-]+)\s+([A-Z]{2,6})$'
)

SKIP_PREFIXES = (
    'T+', 'AVG', 'CLIENT', 'USER', 'PAGE', 'DATE', 'TIME',
    'SETTLEMENT', 'TRADE', 'SR', 'FROM', 'TO', 'WEB',
    'EMAIL', 'PHONE', 'FED', 'OFFICE', 'TREC', 'PSX'
)

def detect_stock_header(line: str):
    line = line.strip()
    if not line or not line[0].isupper():
        return None
    if any(line.upper().startswith(p) for p in SKIP_PREFIXES):
        return None
    if re.search(r'\d', line):
        return None
    m = STOCK_HEADER_RE.match(line)
    if m:
        company = (m.group(1) or m.group(3) or '').strip()
        symbol  = (m.group(2) or m.group(4) or '').strip()
        if 2 <= len(symbol) <= 10 and len(company) >= 3:
            return company, symbol
    return None

TRADE_LINE_RE = re.compile(
    r'(T\+\d+REG)\s+'
    r'(?:[PB]\s*)?(SELL|BUY|PSELL|PBUY|BBUY|BSELL)\s+'
    r'([\d,]+)\s+'
    r'([\d.]+)\s+'
    r'([\d.]+)\s+'
    r'([\d.]+)\s+'
    r'([\d.]+)\s+'
    r'([\d.]+)\s+'
    r'([\d.]+)\s+'
    r'([\d.]+)\s+'
    r'([\d.]+)\s+'
    r'([\d.]+)\s+'
    r'(-?[\d,]+\.?\d*)'
)

def parse_trade_line(line, symbol, company):
    m = TRADE_LINE_RE.search(line)
    if not m:
        return None
    raw = m.group(2).upper().replace(' ', '')
    trade_type = 'SELL' if 'SELL' in raw else 'BUY'
    qty      = int(m.group(3).replace(',', ''))
    rate     = float(m.group(4))
    comm_raw = float(m.group(5))
    sst      = float(m.group(6))
    cdc      = float(m.group(7))
    cvt_wht  = float(m.group(8))
    others   = float(m.group(9))
    laga     = float(m.group(10))
    secp     = float(m.group(11))
    ncs      = float(m.group(12))
    amount   = float(m.group(13).replace(',', ''))
    commission = round(qty * rate * comm_raw / 100, 2) if comm_raw < 1 else round(comm_raw, 2)
    total_charges = round(commission + sst + cdc + cvt_wht + others + laga + secp + ncs, 2)
    print(f"  ✓ {symbol} {trade_type} qty={qty} @ {rate} | net={amount}")
    return {
        "company_name": company or symbol,
        "symbol": symbol,
        "settlement_type": m.group(1),
        "trade_type": trade_type,
        "quantity": qty,
        "rate": rate,
        "commission": commission,
        "sst": sst, "cdc": cdc, "cvt_wht": cvt_wht, "others": others,
        "laga": laga, "secp": secp, "ncs": ncs,
        "total_charges": total_charges,
        "gross_amount": abs(amount),
        "net_amount": amount,
        "is_short_sell": False, "matched": False, "pair_id": None
    }

def parse_munir_khanani_pdf(pdf_bytes: bytes) -> dict:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    ok, msg = validate_pdf(full_text)
    if not ok:
        raise ValueError(msg)

    sr_m   = re.search(r'\bSR(\d+)\b', full_text)
    dt_m   = re.search(r'Date\s*:(\d{2}-\d{2}-\d{4})', full_text)
    set_m  = re.search(r'Settlement\s*:\s*(\d{2}-\d{2}-\d{4})', full_text)
    cdc_m  = re.search(r'CDC\s*ID\s*:(\d+)', full_text)
    cli_m  = re.search(r'SR\d+\s+([A-Z][A-Z\s]+?)(?:\s+CDC|\s*$)', full_text, re.MULTILINE)

    statement_id = f"SR{sr_m.group(1)}" if sr_m else "UNKNOWN"
    client_name  = re.sub(r'[\s@+]+$', '', cli_m.group(1)).strip() if cli_m else "Unknown"

    trade_date = date.today().isoformat()
    if dt_m:
        try:
            trade_date = datetime.strptime(dt_m.group(1), "%d-%m-%Y").date().isoformat()
        except:
            pass

    trades = []
    current_company = current_symbol = None

    for line in full_text.split('\n'):
        s = line.strip()
        if not s:
            continue
        h = detect_stock_header(s)
        if h:
            current_company, current_symbol = h
            print(f"  → Stock: {current_symbol} ({current_company})")
            continue
        if current_symbol and re.search(r'T\+\d+REG', s) and re.search(r'SELL|BUY', s):
            t = parse_trade_line(s, current_symbol, current_company)
            if t:
                trades.append(t)

    symbols_in_pdf = sorted(set(t["symbol"] for t in trades))
    symbols_str    = "_".join(symbols_in_pdf) if symbols_in_pdf else "UNKNOWN"
    unique_id      = f"{statement_id}_{trade_date}_{symbols_str}"

    print(f"DEBUG: {statement_id} {trade_date} → {len(trades)} trades | unique_id: {unique_id}")

    return {
        "statement_id":         statement_id,
        "unique_statement_id":  unique_id,
        "trade_date":           trade_date,
        "settlement_date":      set_m.group(1) if set_m else None,
        "client_name":          client_name,
        "cdc_id":               cdc_m.group(1) if cdc_m else None,
        "trades":               trades,
    }


def match_trades(user_id, supabase):
    """
    Greedy quantity-based matching across all unmatched trades per symbol.

    PAEL example:
      BUYs  (sorted by date): [698 @ Apr2, 302 @ Apr2]   → total 1000
      SELLs (sorted by date): [1000 @ Apr9, 500 @ Apr9]  → total 1500

    Iteration 1:
      running_buy=0, running_sell=0
      Add buy[698]  → buy=698, sell=0   → buy > sell, add sell[1000]
      Add sell[1000]→ buy=698, sell=1000 → sell > buy, add buy[302]
      Add buy[302]  → buy=1000, sell=1000 → BALANCED ✓
      → Pair: buys[698,302] + sells[1000], pair_id_1

    Iteration 2:
      remaining buys=[], remaining sells=[500]
      No buys → loop ends, sell[500] stays unmatched ✓
    """
    rows = (
        supabase.table("trades")
        .select("*")
        .eq("user_id", user_id)
        .eq("matched", False)
        .order("trade_date")
        .execute()
        .data
    )

    by_sym = {}
    for t in rows:
        by_sym.setdefault(t["symbol"], {"BUY": [], "SELL": []})
        by_sym[t["symbol"]][t["trade_type"]].append(t)

    for sym, sides in by_sym.items():
        buys  = list(sides["BUY"])   # ordered by trade_date (from DB query)
        sells = list(sides["SELL"])

        bi = si = 0  # current pointers

        while bi < len(buys) and si < len(sells):
            running_buy  = 0
            running_sell = 0
            tmp_bi = bi
            tmp_si = si

            # Accumulate rows until both sides have equal total qty
            while True:
                if running_buy == running_sell and running_buy > 0:
                    break  # perfectly balanced — done with this pair

                # If unbalanced, add from whichever side is behind
                if running_buy <= running_sell:
                    if tmp_bi >= len(buys):
                        break  # ran out of buys without balancing
                    running_buy += buys[tmp_bi]["quantity"]
                    tmp_bi += 1
                else:
                    if tmp_si >= len(sells):
                        break  # ran out of sells without balancing
                    running_sell += sells[tmp_si]["quantity"]
                    tmp_si += 1

            # Only commit if we achieved a balance
            if running_buy != running_sell or running_buy == 0:
                break

            matched_buys  = buys[bi:tmp_bi]
            matched_sells = sells[si:tmp_si]

            pair_id  = str(uuid.uuid4())
            gross_pl = round(
                sum(s["gross_amount"]  for s in matched_sells) -
                sum(b["gross_amount"]  for b in matched_buys), 2
            )
            net_pl = round(
                gross_pl - sum(t["total_charges"] for t in matched_buys + matched_sells), 2
            )

            all_ids = [t["id"] for t in matched_buys + matched_sells]
            supabase.table("trades").update({
                "matched":  True,
                "pair_id":  pair_id,
                "gross_pl": gross_pl,
                "net_pl":   net_pl,
            }).in_("id", all_ids).execute()

            print(f"  ✓ {sym}: {len(matched_buys)}×BUY + {len(matched_sells)}×SELL "
                  f"qty={running_buy} net_pl={net_pl}")

            bi = tmp_bi
            si = tmp_si


def aggregate_unmatched_trades(trades: list) -> list:
    matched   = [t for t in trades if t.get("matched")]
    unmatched = [t for t in trades if not t.get("matched")]

    groups: dict = {}
    for t in unmatched:
        key = (t["symbol"], t["trade_type"])
        groups.setdefault(key, []).append(t)

    aggregated = []
    for (symbol, trade_type), group in groups.items():
        if len(group) == 1:
            aggregated.append({
                **group[0],
                "trade_dates":      [group[0]["trade_date"]],
                "aggregated":       False,
                "aggregated_count": 1,
            })
            continue

        total_qty   = sum(g["quantity"]     for g in group)
        total_gross = sum(g["gross_amount"] for g in group)
        avg_rate    = round(total_gross / total_qty, 2) if total_qty else 0
        dates       = sorted(set(g["trade_date"] for g in group))

        aggregated.append({
            **group[0],
            "id":               [g["id"] for g in group],
            "quantity":         total_qty,
            "rate":             avg_rate,
            "gross_amount":     round(total_gross, 2),
            "net_amount":       round(sum(g["net_amount"]    for g in group), 2),
            "commission":       round(sum(g["commission"]    for g in group), 2),
            "sst":              round(sum(g["sst"]           for g in group), 2),
            "cdc":              round(sum(g["cdc"]           for g in group), 2),
            "cvt_wht":          round(sum(g["cvt_wht"]       for g in group), 2),
            "others":           round(sum(g["others"]        for g in group), 2),
            "laga":             round(sum(g["laga"]          for g in group), 2),
            "secp":             round(sum(g["secp"]          for g in group), 2),
            "ncs":              round(sum(g["ncs"]           for g in group), 2),
            "total_charges":    round(sum(g["total_charges"] for g in group), 2),
            "trade_date":       dates[0],
            "trade_dates":      dates,
            "aggregated":       True,
            "aggregated_count": len(group),
        })

    aggregated.sort(key=lambda t: t["trade_date"], reverse=True)
    return matched + aggregated


# ─────────────────────────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "PSX Profit Tracker API is running"}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "PSX Profit Tracker API"}

@app.post("/api/upload-pdf")
async def upload_pdf(file: UploadFile = File(...), token: str = Depends(verify_token)):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files accepted")

    pdf_bytes = await file.read()

    try:
        parsed = parse_munir_khanani_pdf(pdf_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Parse error: {e}")

    if not parsed["trades"]:
        raise HTTPException(status_code=400, detail="No trades found in this PDF.")

    supabase = get_supabase()
    user_id  = supabase.auth.get_user(token).user.id

    if supabase.table("statements").select("id").eq(
        "unique_statement_id", parsed["unique_statement_id"]
    ).eq("user_id", user_id).execute().data:
        raise HTTPException(
            status_code=409,
            detail=f"Statement {parsed['statement_id']} with these stocks from {parsed['trade_date']} already imported."
        )

    stmt = supabase.table("statements").insert({
        "user_id":             user_id,
        "statement_id":        parsed["statement_id"],
        "unique_statement_id": parsed["unique_statement_id"],
        "trade_date":          parsed["trade_date"],
        "settlement_date":     parsed["settlement_date"],
        "client_name":         parsed["client_name"],
        "cdc_id":              parsed["cdc_id"],
    }).execute()

    db_id = stmt.data[0]["id"]

    rows = [{
        "user_id":             user_id,
        "statement_db_id":     db_id,
        "statement_id":        parsed["statement_id"],
        "unique_statement_id": parsed["unique_statement_id"],
        "trade_date":          parsed["trade_date"],
        "symbol":              t["symbol"],
        "company_name":        t["company_name"],
        "trade_type":          t["trade_type"],
        "settlement_type":     t["settlement_type"],
        "quantity":            t["quantity"],
        "rate":                t["rate"],
        "commission":          t["commission"],
        "sst":                 t["sst"],
        "cdc":                 t["cdc"],
        "cvt_wht":             t["cvt_wht"],
        "others":              t["others"],
        "laga":                t["laga"],
        "secp":                t["secp"],
        "ncs":                 t["ncs"],
        "total_charges":       t["total_charges"],
        "gross_amount":        t["gross_amount"],
        "net_amount":          t["net_amount"],
        "is_short_sell":       False,
        "matched":             False,
        "pair_id":             None,
    } for t in parsed["trades"]]

    supabase.table("trades").insert(rows).execute()
    match_trades(user_id, supabase)

    return {
        "success":         True,
        "statement_id":    parsed["statement_id"],
        "trade_date":      parsed["trade_date"],
        "trades_imported": len(parsed["trades"]),
        "message": f"Imported {len(parsed['trades'])} trade(s) from {parsed['statement_id']} ({parsed['trade_date']})"
    }


@app.get("/api/trades")
async def get_trades(
    month:               Optional[int] = None,
    year:                Optional[int] = None,
    aggregate_unmatched: bool          = True,
    token: str = Depends(verify_token)
):
    supabase = get_supabase()
    user_id  = supabase.auth.get_user(token).user.id
    q = supabase.table("trades").select("*").eq("user_id", user_id)
    if month and year:
        s  = f"{year}-{month:02d}-01"
        em = month + 1 if month < 12 else 1
        ey = year      if month < 12 else year + 1
        q  = q.gte("trade_date", s).lt("trade_date", f"{ey}-{em:02d}-01")
    trades = q.order("trade_date", desc=True).execute().data

    if aggregate_unmatched:
        trades = aggregate_unmatched_trades(trades)

    return {"trades": trades}


@app.get("/api/summary")
async def get_summary(token: str=Depends(verify_token)):
    supabase = get_supabase()
    user_id  = supabase.auth.get_user(token).user.id
    trades   = supabase.table("trades").select("*").eq("user_id", user_id).eq("matched", True).not_.is_("net_pl","null").execute().data
    seen = set(); pairs = []
    for t in trades:
        if t["pair_id"] and t["pair_id"] not in seen:
            seen.add(t["pair_id"]); pairs.append(t)
    today = date.today().isoformat()
    ws = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    ms    = date.today().replace(day=1).isoformat()
    def pl(lst): return round(sum(t["net_pl"] for t in lst if t.get("net_pl")), 2)
    best  = max(pairs, key=lambda t: t.get("net_pl", 0), default=None)
    worst = min(pairs, key=lambda t: t.get("net_pl", 0), default=None)
    return {
        "today_pl":      pl([t for t in pairs if t["trade_date"] == today]),
        "week_pl":       pl([t for t in pairs if t["trade_date"] >= ws]),
        "month_pl":      pl([t for t in pairs if t["trade_date"] >= ms]),
        "total_charges": round(sum(t["total_charges"] for t in trades), 2),
        "win_rate":      round(len([t for t in pairs if t.get("net_pl",0) > 0]) / len(pairs) * 100, 1) if pairs else 0,
        "total_trades":  len(pairs),
        "best_trade":    {"symbol": best["symbol"],  "pl": best["net_pl"]}  if best  else None,
        "worst_trade":   {"symbol": worst["symbol"], "pl": worst["net_pl"]} if worst else None,
    }

@app.get("/api/calendar")
async def get_calendar(month: int, year: int, token: str=Depends(verify_token)):
    supabase = get_supabase()
    user_id  = supabase.auth.get_user(token).user.id
    s  = f"{year}-{month:02d}-01"
    em = month+1 if month < 12 else 1
    ey = year   if month < 12 else year+1
    rows = supabase.table("trades").select("*").eq("user_id", user_id).gte(
        "trade_date", s
    ).lt("trade_date", f"{ey}-{em:02d}-01").execute().data
    by_date = {}; seen = set()
    for t in rows:
        d = t["trade_date"]
        if d not in by_date:
            by_date[d] = {"pl": 0, "charges": 0, "trades": 0, "symbols": []}
        by_date[d]["charges"] += t["total_charges"]
        by_date[d]["trades"]  += 1
        if t.get("symbol") and t["symbol"] not in by_date[d]["symbols"]:
            by_date[d]["symbols"].append(t["symbol"])
        if t.get("pair_id") and t["pair_id"] not in seen and t.get("net_pl"):
            seen.add(t["pair_id"])
            by_date[d]["pl"] += t["net_pl"]
    for d in by_date:
        by_date[d]["pl"]      = round(by_date[d]["pl"], 2)
        by_date[d]["charges"] = round(by_date[d]["charges"], 2)
    return {
        "daily":           by_date,
        "monthly_pl":      round(sum(v["pl"]      for v in by_date.values()), 2),
        "monthly_charges": round(sum(v["charges"] for v in by_date.values()), 2),
        "month":           month,
        "year":            year,
    }

@app.delete("/api/statement/{unique_statement_id}")
async def delete_statement(unique_statement_id: str, token: str=Depends(verify_token)):
    supabase = get_supabase()
    user_id  = supabase.auth.get_user(token).user.id
    supabase.table("trades").delete().eq("unique_statement_id", unique_statement_id).eq("user_id", user_id).execute()
    supabase.table("statements").delete().eq("unique_statement_id", unique_statement_id).eq("user_id", user_id).execute()
    return {"success": True}

# ─────────────────────────────────────────────────────────────
# DISCORD ADMIN ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.post("/api/discord/schedule")
async def schedule_discord_message(
    trade_data: dict,
    token: str = Depends(verify_token)
):
    """Schedule a Discord message to be sent at specific date/time"""
    supabase = get_supabase()
    user_id  = supabase.auth.get_user(token).user.id
    
    # For now, only allow specific admin (you)
    # In production, check against admin list
    
    channel_id = trade_data.get("channel_id", "1498347963532054768")
    if channel_id not in DISCORD_CHANNELS.values():
        raise HTTPException(status_code=400, detail="Invalid channel")
    
    channel_name = "INTRADAY" if channel_id == DISCORD_CHANNELS["intraday"] else "SWING"
    
    # Parse scheduled time
    try:
        scheduled_datetime = datetime.fromisoformat(trade_data["scheduled_at"])
        scheduled_datetime = scheduled_datetime - timedelta(hours=5)
    except:
        raise HTTPException(status_code=400, detail="Invalid scheduled_at format. Use ISO format: 2026-04-27T14:30:00")
    
    # Store in database
    msg_record = {
        "admin_user_id": user_id,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "message_format": trade_data.get("message_format", "embed"),
        "trade_date": trade_data["trade_date"],
        "trade_time": trade_data["trade_time"],
        "symbol": trade_data["symbol"],
        "buy_price": float(trade_data["buy_price"]),
        "sell_price": float(trade_data["sell_price"]),
        "stop_loss": float(trade_data["stop_loss"]),
        "difference": float(trade_data.get("difference", 0)) if trade_data.get("difference") else None,
        "result": trade_data.get("result"),
        "scheduled_at": scheduled_datetime.isoformat(),
    }
    
    result = supabase.table("discord_messages").insert(msg_record).execute()
    
    return {
        "success": True,
        "message_id": result.data[0]["id"],
        "scheduled_for": scheduled_datetime.isoformat(),
        "channel": channel_name,
    }

@app.get("/api/discord/messages")
async def get_discord_messages(
    status: str = "scheduled",
    token: str = Depends(verify_token)
):
    """Get admin's scheduled Discord messages"""
    supabase = get_supabase()
    user_id  = supabase.auth.get_user(token).user.id
    
    q = supabase.table("discord_messages").select("*").eq("admin_user_id", user_id)
    if status:
        q = q.eq("status", status)
    
    messages = q.order("scheduled_at", desc=True).execute().data
    return {"messages": messages}

@app.delete("/api/discord/messages/{message_id}")
async def cancel_discord_message(
    message_id: str,
    token: str = Depends(verify_token)
):
    """Cancel a scheduled Discord message"""
    supabase = get_supabase()
    user_id  = supabase.auth.get_user(token).user.id
    
    # Verify ownership
    msg = supabase.table("discord_messages").select("*").eq("id", message_id).execute().data
    if not msg or msg[0]["admin_user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    # Can only delete scheduled messages
    if msg[0]["status"] != "scheduled":
        raise HTTPException(status_code=400, detail="Can only cancel scheduled messages")
    
    supabase.table("discord_messages").delete().eq("id", message_id).execute()
    return {"success": True}

# ─────────────────────────────────────────────────────────────
# SCHEDULER STARTUP
# ─────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.add_job(
    send_scheduled_messages,
    IntervalTrigger(seconds=30),  # Check every 30 seconds
    id="discord_scheduler",
    name="Discord Message Scheduler",
    replace_existing=True
)

@app.on_event("startup")
async def start_scheduler():
    """Start background scheduler when app starts"""
    if not scheduler.running:
        scheduler.start()
        print("✓ Discord scheduler started")

@app.on_event("shutdown")
async def stop_scheduler():
    """Stop scheduler when app shuts down"""
    if scheduler.running:
        scheduler.shutdown()
        print("✓ Discord scheduler stopped")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)