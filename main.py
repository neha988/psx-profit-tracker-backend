from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber
import re
import io
import os
import uuid
import hashlib
from datetime import datetime, date, timedelta
from typing import Optional, Tuple
from supabase import create_client, Client
from postgrest.exceptions import APIError
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
    if not any(kw in text for kw in ["BUY", "SELL", "T+1REG", "T+0REG", "FT2REG", "FT1REG", "FTREG"]):
        return False, "No BUY/SELL trade records found"
    return True, "Valid"

def detect_broker(text: str) -> str:
    tl = text.lower()
    if "akd securities" in tl:
        return "AKD"
    if "munirkhanani" in tl or "munir khanani" in tl:
        return "Munir Khanani"
    return "Unknown"

STOCK_HEADER_RE = re.compile(
    r'^([A-Z][A-Z0-9\s&\.\-\(\)]+?)\s{2,}([A-Z][A-Z0-9\-]{1,10})$'
    r'|^([A-Z][A-Z0-9\s&\.\-\(\)]+)\s+([A-Z][A-Z0-9\-]{1,9})$'
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

TRADE_LINE_RE = re.compile(r'\b(T\+\d+REG|FT\d*REG)\b')
ACTION_RE = re.compile(r'\b(PSELL|PBUY|BBUY|BSELL|SELL|BUY|S|B)\b')
NUMBER_RE = re.compile(r'\(?-?[\d,]+(?:\.\d+)?\)?')

def parse_pdf_number(value: str) -> float:
    value = value.strip()
    is_parenthesized = value.startswith("(") and value.endswith(")")
    cleaned = value.strip("()").replace(",", "")
    parsed = float(cleaned)
    return -abs(parsed) if is_parenthesized else parsed

def parse_trade_numbers(line: str, start: int):
    values = NUMBER_RE.findall(line[start:])
    return values if len(values) >= 11 else None

def parse_trade_line(line, symbol, company):
    settlement_m = TRADE_LINE_RE.search(line)
    if not settlement_m:
        return None

    action_m = ACTION_RE.search(line, settlement_m.end())
    values = parse_trade_numbers(line, action_m.end() if action_m else settlement_m.end())
    if not values:
        return None

    settlement_type = settlement_m.group(1)
    amount   = parse_pdf_number(values[-1])
    if action_m:
        raw = action_m.group(1).upper().replace(' ', '')
        trade_type = 'SELL' if 'SELL' in raw or raw == 'S' else 'BUY'
    else:
        trade_type = 'SELL' if amount < 0 else 'BUY'

    qty      = int(parse_pdf_number(values[0]))
    rate     = parse_pdf_number(values[1])
    comm_raw = parse_pdf_number(values[2])
    sst      = parse_pdf_number(values[3])
    cdc      = parse_pdf_number(values[4])
    cvt_wht  = parse_pdf_number(values[5])
    others   = parse_pdf_number(values[6])
    laga     = parse_pdf_number(values[7])
    secp     = parse_pdf_number(values[8])
    ncs      = parse_pdf_number(values[9])
    # Munir Khanani prints Comm. as a per-share rate, not as a percentage of value.
    commission = round(qty * comm_raw, 2)
    total_charges = round(commission + sst + cdc + cvt_wht + others + laga + secp + ncs, 2)
    
    # Detect if it's a futures contract
    is_futures = settlement_type.startswith("FT") or '-' in symbol and any(m in symbol.upper() for m in ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'])
    
    trade_label = f"Futures {trade_type}" if is_futures else f"{trade_type}"
    print(f"  OK {symbol} {trade_label} qty={qty} @ {rate} | net={amount}")
    return {
        "company_name": company or symbol,
        "symbol": symbol,
        "settlement_type": settlement_type,
        "trade_type": trade_type,
        "quantity": qty,
        "rate": rate,
        "commission": commission,
        "sst": sst, "cdc": cdc, "cvt_wht": cvt_wht, "others": others,
        "laga": laga, "secp": secp, "ncs": ncs,
        "total_charges": total_charges,
        "gross_amount": abs(amount),
        "net_amount": amount,
        "is_futures": is_futures,
        "is_short_sell": False, "matched": False, "pair_id": None
    }

def build_trade_signature(trades: list) -> str:
    return "_".join(
        f"{t['settlement_type']}:{t['symbol']}:{t['trade_type']}:{int(t['quantity'])}:{float(t['rate'])}"
        for t in sorted(trades, key=lambda item: (
            item["settlement_type"], item["symbol"], item["trade_type"], int(item["quantity"]), float(item["rate"])
        ))
    )

def stable_statement_suffix(*parts: Optional[str]) -> str:
    basis = "|".join(normalize_account_value(part) for part in parts if part)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10].upper()

def statement_trade_signature(supabase, user_id: str, statement_id: str) -> Tuple[str, int]:
    existing_trades = supabase.table("trades").select(
        "settlement_type,symbol,trade_type,quantity,rate"
    ).eq("user_id", user_id).eq("statement_db_id", statement_id).execute().data

    return build_trade_signature(existing_trades), len(existing_trades)

def delete_statement_by_id(supabase, user_id: str, statement_id: str):
    existing_trades = supabase.table("trades").select("id,pair_id").eq(
        "user_id", user_id
    ).eq("statement_db_id", statement_id).execute().data

    pair_ids = sorted({t["pair_id"] for t in existing_trades if t.get("pair_id")})
    if pair_ids:
        supabase.table("trades").update({
            "matched": False,
            "pair_id": None,
            "gross_pl": None,
            "net_pl": None,
        }).eq("user_id", user_id).in_("pair_id", pair_ids).execute()

    supabase.table("trades").delete().eq("statement_db_id", statement_id).eq("user_id", user_id).execute()
    supabase.table("statements").delete().eq("id", statement_id).eq("user_id", user_id).execute()

def user_scoped_statement_id(user_id: str, unique_statement_id: str) -> str:
    return f"{user_id}_{unique_statement_id}"

def find_user_statements_by_unique_ids(supabase, user_id: str, unique_ids: list) -> list:
    found = []
    seen = set()
    for unique_id in unique_ids:
        rows = supabase.table("statements").select("id,unique_statement_id").eq(
            "unique_statement_id", unique_id
        ).eq("user_id", user_id).execute().data or []
        for row in rows:
            if row["id"] not in seen:
                found.append(row)
                seen.add(row["id"])
    return found

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

    statement_id = f"SR{sr_m.group(1)}" if sr_m else None
    
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
            print(f"  Stock: {current_symbol} ({current_company})")
            continue
        if current_symbol and re.search(r'(T\+\d+REG|FT\d*REG)', s):
            t = parse_trade_line(s, current_symbol, current_company)
            if t:
                trades.append(t)

    symbols_in_pdf = sorted(set(t["symbol"] for t in trades))
    symbols_str    = "_".join(symbols_in_pdf) if symbols_in_pdf else "UNKNOWN"
    trade_signature = build_trade_signature(trades)

    if not statement_id:
        pdf_date = dt_m.group(1).replace("-", "") if dt_m else trade_date.replace("-", "")
        suffix = stable_statement_suffix(
            "MK",
            trade_date,
            set_m.group(1) if set_m else None,
            cdc_m.group(1) if cdc_m else client_name,
            trade_signature,
        )
        statement_id = f"MK-{pdf_date}-{suffix}"

    legacy_unique_id = f"{statement_id}_{trade_date}_{symbols_str}"
    unique_id      = f"{legacy_unique_id}_{trade_signature}" if trade_signature else legacy_unique_id

    print(f"DEBUG: {statement_id} {trade_date} -> {len(trades)} trades | unique_id: {unique_id}")

    return {
        "statement_id":         statement_id,
        "unique_statement_id":  unique_id,
        "legacy_unique_statement_id": legacy_unique_id,
        "trade_signature":      trade_signature,
        "trade_date":           trade_date,
        "settlement_date":      set_m.group(1) if set_m else None,
        "client_name":          client_name,
        "cdc_id":               cdc_m.group(1) if cdc_m else None,
        "trades":               trades,
    }

AKD_DATE_RE = re.compile(r'\bDate\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})')
AKD_MEMO_RE = re.compile(r'\bMemo\s*#\s*\n?\s*([A-Z0-9/\-]+)', re.IGNORECASE)
AKD_CLIENT_RE = re.compile(r'\bTo\s+(.+?)(?:\s+-\s+|\n)', re.IGNORECASE)
AKD_TRADE_RE = re.compile(
    r'^([A-Z][A-Z0-9\-]+)\s+'
    r'(Ready|Future|F-Mtm|F-MTM)\s+'
    r'([\d,]+)\s+'
    r'([\d.]+)\s+'
    r'([\d,]+(?:\.\d+)?)\s+'
    r'([\d,]+(?:\.\d+)?)\s+'
    r'([\d,]+(?:\.\d+)?)\s+'
    r'([\d,]+(?:\.\d+)?)\s+'
    r'([\d,]+(?:\.\d+)?)\s+'
    r'([\d,]+(?:\.\d+)?)\s+'
    r'([\d,]+(?:\.\d+)?)\s+'
    r'([\d,]+(?:\.\d+)?)\s+'
    r'([\d,]+(?:\.\d+)?)$',
    re.IGNORECASE
)

def parse_akd_pdf(pdf_bytes: bytes) -> dict:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    if detect_broker(full_text) != "AKD":
        raise ValueError("Not an AKD Securities document")

    date_m = AKD_DATE_RE.search(full_text)
    if not date_m:
        raise ValueError("Missing AKD trade date")
    trade_date = datetime.strptime(" ".join(date_m.groups()), "%B %d %Y").date().isoformat()

    memo_m = AKD_MEMO_RE.search(full_text)
    client_m = AKD_CLIENT_RE.search(full_text)
    
    statement_id = memo_m.group(1).strip() if memo_m else None
    
    client_name = client_m.group(1).strip() if client_m else "Unknown"

    trades = []
    current_side = None

    for raw_line in full_text.splitlines():
        line = re.sub(r'\s+', ' ', raw_line.strip())
        if not line:
            continue

        compact = line.replace(" ", "").upper()
        if compact == "PURCHASE":
            current_side = "BUY"
            continue
        if compact == "SALE":
            current_side = "SELL"
            continue
        if line.upper().startswith(("TOTAL :", "CLIENT TOTAL", "SUMMARY")):
            current_side = None
            continue
        if not current_side:
            continue

        m = AKD_TRADE_RE.match(line)
        if not m:
            continue

        symbol = m.group(1).upper()
        market = m.group(2).upper()
        qty = int(parse_pdf_number(m.group(3)))
        rate = parse_pdf_number(m.group(4))
        commission = parse_pdf_number(m.group(5))
        cvt_wht = parse_pdf_number(m.group(6))
        secp = parse_pdf_number(m.group(7))
        laga = parse_pdf_number(m.group(8))
        psx_laga = parse_pdf_number(m.group(9))
        sst = parse_pdf_number(m.group(10))
        ncs = parse_pdf_number(m.group(11))
        cdc = parse_pdf_number(m.group(12))
        amount = parse_pdf_number(m.group(13))
        total_charges = round(commission + cvt_wht + secp + laga + psx_laga + sst + ncs + cdc, 2)
        is_futures = market.startswith("F") or '-' in symbol and any(
            month in symbol for month in ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']
        )

        trades.append({
            "company_name": symbol,
            "symbol": symbol,
            "settlement_type": market,
            "trade_type": current_side,
            "quantity": qty,
            "rate": rate,
            "commission": commission,
            "sst": sst,
            "cdc": cdc,
            "cvt_wht": cvt_wht,
            "others": psx_laga,
            "laga": laga,
            "secp": secp,
            "ncs": ncs,
            "total_charges": total_charges,
            "gross_amount": abs(amount),
            "net_amount": -abs(amount) if current_side == "SELL" else abs(amount),
            "is_futures": is_futures,
            "is_short_sell": False,
            "matched": False,
            "pair_id": None,
        })
        print(f"  OK AKD {symbol} {current_side} qty={qty} @ {rate} | net={trades[-1]['net_amount']}")

    if not trades:
        raise ValueError("No AKD trade rows found")

    symbols_str = "_".join(sorted(set(t["symbol"] for t in trades)))
    trade_signature = build_trade_signature(trades)

    if not statement_id:
        suffix = stable_statement_suffix("AKD", trade_date, client_name, trade_signature)
        statement_id = f"AKD-{trade_date.replace('-', '')}-{suffix}"

    legacy_unique_id = f"AKD_{statement_id}_{trade_date}_{symbols_str}"
    unique_id = f"{legacy_unique_id}_{trade_signature}" if trade_signature else legacy_unique_id

    return {
        "broker": "AKD",
        "statement_id": statement_id,
        "unique_statement_id": unique_id,
        "legacy_unique_statement_id": legacy_unique_id,
        "trade_signature": trade_signature,
        "trade_date": trade_date,
        "settlement_date": None,
        "client_name": client_name,
        "cdc_id": None,
        "trades": trades,
    }

def parse_pdf_statement(pdf_bytes: bytes) -> dict:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        sample_text = "\n".join(page.extract_text() or "" for page in pdf.pages[:2])

    broker = detect_broker(sample_text)
    if broker == "AKD":
        return parse_akd_pdf(pdf_bytes)
    if broker == "Munir Khanani":
        parsed = parse_munir_khanani_pdf(pdf_bytes)
        parsed["broker"] = "Munir Khanani"
        return parsed
    raise ValueError("Broker format not supported yet")

def normalize_account_value(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().upper())

def broker_account_identity(row: dict) -> Tuple[str, str]:
    broker = row.get("broker") or "Unknown"
    cdc_id = normalize_account_value(row.get("cdc_id"))
    client_name = normalize_account_value(row.get("client_name"))
    if cdc_id and cdc_id != "UNKNOWN":
        return "CDC ID", cdc_id
    if client_name and client_name != "UNKNOWN":
        return "Client Name", client_name
    return "", ""

def enforce_single_broker_account(supabase, user_id: str, parsed: dict):
    broker = parsed.get("broker", "Unknown")
    id_label, id_value = broker_account_identity(parsed)
    if not id_value:
        raise HTTPException(
            status_code=400,
            detail=f"Could not identify the {broker} account in this PDF. Please upload a statement with a clear client name or CDC ID."
        )

    existing = supabase.table("statements").select("client_name,cdc_id").eq(
        "user_id", user_id
    ).eq("broker", broker).execute().data or []

    existing_accounts = {}
    for row in existing:
        existing_label, existing_value = broker_account_identity({**row, "broker": broker})
        if existing_value:
            existing_accounts[existing_value] = existing_label

    if not existing_accounts or id_value in existing_accounts:
        return

    existing_label, existing_value = next(iter(existing_accounts.items()))
    raise HTTPException(
        status_code=400,
        detail=(
            f"Only one {broker} account is allowed per user. "
            f"This PDF belongs to {id_label} {id_value}, but your tracker already has "
            f"{existing_label} {existing_value} for {broker}."
        )
    )

TRADE_MONEY_FIELDS = (
    "commission", "sst", "cdc", "cvt_wht", "others", "laga", "secp", "ncs",
    "total_charges", "gross_amount", "net_amount"
)

TRADE_COPY_FIELDS = (
    "user_id", "statement_db_id", "statement_id", "unique_statement_id", "broker",
    "trade_date", "symbol", "company_name", "trade_type", "settlement_type",
    "quantity", "rate", "commission", "sst", "cdc", "cvt_wht", "others",
    "laga", "secp", "ncs", "total_charges", "gross_amount", "net_amount",
    "is_futures", "is_short_sell"
)

def scaled_trade_values(trade: dict, quantity: int) -> dict:
    original_qty = int(trade.get("quantity") or 0)
    ratio = quantity / original_qty if original_qty else 0
    values = {"quantity": quantity}
    for field in TRADE_MONEY_FIELDS:
        values[field] = round(float(trade.get(field) or 0) * ratio, 4)
    return values

def split_trade_for_match(supabase, trade: dict, match_qty: int):
    original_qty = int(trade["quantity"])
    if match_qty >= original_qty:
        return {**trade}, None

    matched_values = scaled_trade_values(trade, match_qty)
    residual_qty = original_qty - match_qty
    residual_values = scaled_trade_values(trade, residual_qty)

    supabase.table("trades").update(matched_values).eq("id", trade["id"]).execute()

    residual_payload = {
        field: trade.get(field)
        for field in TRADE_COPY_FIELDS
        if field in trade
    }
    residual_payload.update(residual_values)
    residual_payload.update({
        "matched": False,
        "pair_id": None,
        "gross_pl": None,
        "net_pl": None,
    })
    inserted = supabase.table("trades").insert(residual_payload).execute().data[0]

    return {**trade, **matched_values}, inserted


def _match_trades_exact_quantity_legacy(user_id, supabase):
    """
    Greedy quantity-based matching across all unmatched trades per exact symbol.
    Futures are matched only within the same contract month because their symbol
    includes the month suffix, for example DGKC-APR and DGKC-MAY stay separate.

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
        key = (t.get("broker") or "Unknown", t["symbol"])
        by_sym.setdefault(key, {"BUY": [], "SELL": []})
        by_sym[key][t["trade_type"]].append(t)

    for (broker, sym), sides in by_sym.items():
        buys  = list(sides["BUY"])   # ordered by trade_date (from DB query)
        sells = list(sides["SELL"])

        bi = si = 0  # current pointers

        while bi < len(buys) and si < len(sells):
            buy = buys[bi]
            sell = sells[si]
            match_qty = min(int(buy["quantity"]), int(sell["quantity"]))

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

            print(f"  OK {broker} {sym}: {len(matched_buys)}xBUY + {len(matched_sells)}xSELL "
                  f"qty={running_buy} net_pl={net_pl}")

            bi = tmp_bi
            si = tmp_si


def clear_all_matched_trades(user_id, supabase):
    """
    Reset all matched trades for a user to unmatched state.
    This prepares for fresh re-matching on new uploads.
    """
    supabase.table("trades").update({
        "matched": False,
        "pair_id": None,
        "gross_pl": None,
        "net_pl": None,
    }).eq("user_id", user_id).execute()
    print(f"[REMATCH] Cleared all matched trades for user {user_id}")


def match_trades(user_id, supabase):
    """
    Match unmatched trades per broker + exact symbol:
    - Aggregate all BUYs and SELLs for a symbol
    - Match min(total_buy, total_sell) in ONE pair
    - Split trades as needed so matched trades show correct qty
    - Remainder becomes new unmatched trade
    
    Example: 2000 BUY + 8000 SELL → 1 pair(2000/2000) + 6000 SELL unmatched
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
        key = (t.get("broker") or "Unknown", t["symbol"])
        by_sym.setdefault(key, {"BUY": [], "SELL": []})
        by_sym[key][t["trade_type"]].append(t)

    for (broker, sym), sides in by_sym.items():
        buys = list(sides["BUY"])
        sells = list(sides["SELL"])
        
        if not buys or not sells:
            continue

        # Calculate totals
        total_buy_qty = sum(int(b["quantity"]) for b in buys)
        total_sell_qty = sum(int(s["quantity"]) for s in sells)
        match_qty = min(total_buy_qty, total_sell_qty)

        if match_qty <= 0:
            continue

        pair_id = str(uuid.uuid4())
        matched_ids = []  # Track all IDs that will be in this pair
        remaining_to_match_buy = match_qty
        remaining_to_match_sell = match_qty

        # Split BUY trades: keep taking until we reach match_qty
        for buy_trade in buys:
            if remaining_to_match_buy <= 0:
                break
            
            buy_qty = int(buy_trade["quantity"])
            split_qty = min(buy_qty, remaining_to_match_buy)
            
            if split_qty == buy_qty:
                # Entire trade matches, no split needed
                matched_ids.append(buy_trade["id"])
                remaining_to_match_buy -= buy_qty
            else:
                # Need to split: keep matched portion, create remainder
                matched_buy, remainder_buy = split_trade_for_match(supabase, buy_trade, split_qty)
                matched_ids.append(matched_buy["id"])
                remaining_to_match_buy -= split_qty

        # Split SELL trades: keep taking until we reach match_qty
        for sell_trade in sells:
            if remaining_to_match_sell <= 0:
                break
            
            sell_qty = int(sell_trade["quantity"])
            split_qty = min(sell_qty, remaining_to_match_sell)
            
            if split_qty == sell_qty:
                # Entire trade matches, no split needed
                matched_ids.append(sell_trade["id"])
                remaining_to_match_sell -= sell_qty
            else:
                # Need to split: keep matched portion, create remainder
                matched_sell, remainder_sell = split_trade_for_match(supabase, sell_trade, split_qty)
                matched_ids.append(matched_sell["id"])
                remaining_to_match_sell -= split_qty

        # Calculate P&L from matched trades
        matched_trades = supabase.table("trades").select("*").in_("id", matched_ids).execute().data
        buys_in_pair = [t for t in matched_trades if t["trade_type"] == "BUY"]
        sells_in_pair = [t for t in matched_trades if t["trade_type"] == "SELL"]
        
        total_buy_gross = sum(b["gross_amount"] for b in buys_in_pair)
        total_buy_charges = sum(b["total_charges"] for b in buys_in_pair)
        total_sell_gross = sum(s["gross_amount"] for s in sells_in_pair)
        total_sell_charges = sum(s["total_charges"] for s in sells_in_pair)
        
        gross_pl = round(total_sell_gross - total_buy_gross, 2)
        net_pl = round(gross_pl - total_buy_charges - total_sell_charges, 2)

        # Mark all matched trades with this pair_id and P&L
        supabase.table("trades").update({
            "matched": True,
            "pair_id": pair_id,
            "gross_pl": gross_pl,
            "net_pl": net_pl,
        }).in_("id", matched_ids).execute()

        print(f"  OK {broker} {sym}: BUY({sum(int(b['quantity']) for b in buys_in_pair)}) + "
              f"SELL({sum(int(s['quantity']) for s in sells_in_pair)}) → 1 pair, net_pl={net_pl}")


def aggregate_unmatched_trades(trades: list) -> list:
    matched   = [t for t in trades if t.get("matched")]
    unmatched = [t for t in trades if not t.get("matched")]

    groups: dict = {}
    for t in unmatched:
        key = (t.get("broker") or "Unknown", t["symbol"], t["trade_type"])
        groups.setdefault(key, []).append(t)

    aggregated = []
    for (broker, symbol, trade_type), group in groups.items():
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
            "id":               group[0]["id"],
            "quantity":         total_qty,
            "rate":             avg_rate,
            "gross_amount":     round(total_gross, 2),
            "net_amount":       round(sum((g.get("net_amount") or 0)    for g in group), 2),
            "commission":       round(sum((g.get("commission") or 0)    for g in group), 2),
            "sst":              round(sum((g.get("sst") or 0)           for g in group), 2),
            "cdc":              round(sum((g.get("cdc") or 0)           for g in group), 2),
            "cvt_wht":          round(sum((g.get("cvt_wht") or 0)       for g in group), 2),
            "others":           round(sum((g.get("others") or 0)        for g in group), 2),
            "laga":             round(sum((g.get("laga") or 0)          for g in group), 2),
            "secp":             round(sum((g.get("secp") or 0)          for g in group), 2),
            "ncs":              round(sum((g.get("ncs") or 0)           for g in group), 2),
            "total_charges":    round(sum((g.get("total_charges") or 0) for g in group), 2),
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
async def upload_pdf(file: UploadFile = File(...), token: str = Depends(verify_token), force: bool = False):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files accepted")

    pdf_bytes = await file.read()

    try:
        parsed = parse_pdf_statement(pdf_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Parse error: {e}")

    if not parsed["trades"]:
        raise HTTPException(status_code=400, detail="No trades found in this PDF.")

    supabase = get_supabase()
    user_id  = supabase.auth.get_user(token).user.id
    enforce_single_broker_account(supabase, user_id, parsed)
    raw_unique_id = parsed["unique_statement_id"]
    storage_unique_id = user_scoped_statement_id(user_id, raw_unique_id)
    
    print(f"[UPLOAD] force={force}, user_id={user_id}, raw_id={raw_unique_id}, storage_id={storage_unique_id}")

    # Search for existing statements with this unique_id
    existing = supabase.table("statements").select("id,unique_statement_id").eq(
        "unique_statement_id", storage_unique_id
    ).eq("user_id", user_id).execute().data or []
    
    print(f"[UPLOAD] Found {len(existing)} existing statements with storage_unique_id")

    duplicate = []
    stale_statement_ids = []

    for statement in existing:
        existing_signature, existing_count = statement_trade_signature(supabase, user_id, statement["id"])
        print(f"[UPLOAD] Statement {statement['id']}: {existing_count} trades, signature match: {existing_signature == parsed['trade_signature']}")
        
        if existing_count == 0:
            # Empty statement, always delete
            stale_statement_ids.append(statement["id"])
        elif existing_signature == parsed["trade_signature"]:
            if force:
                print(f"[UPLOAD] Force mode: exact duplicate - replacing old statement {statement['id']}")
                stale_statement_ids.append(statement["id"])
            else:
                print(f"[UPLOAD] Exact duplicate - returning conflict")
                duplicate = [statement]
                break
        else:
            # Different signature (different number of trades)
            if force:
                # User is forcing re-import, so delete the old one
                print(f"[UPLOAD] Force mode: signature mismatch ({existing_count} vs {len(parsed['trades'])}) - deleting old statement")
                stale_statement_ids.append(statement["id"])
            else:
                # Not forcing: this is a conflict situation
                print(f"[UPLOAD] Signature mismatch ({existing_count} vs {len(parsed['trades'])}) - showing user the conflict option")
                duplicate = [statement]
                break

    # Also check legacy format
    legacy_id = parsed.get("legacy_unique_statement_id")
    if not duplicate and legacy_id and legacy_id != raw_unique_id:
        print(f"[UPLOAD] Checking legacy format: {legacy_id}")
        legacy_storage_id = user_scoped_statement_id(user_id, legacy_id)
        legacy_existing = supabase.table("statements").select("id,unique_statement_id").eq(
            "unique_statement_id", legacy_storage_id
        ).eq("user_id", user_id).execute().data or []
        
        for legacy_statement in legacy_existing:
            existing_signature, existing_count = statement_trade_signature(supabase, user_id, legacy_statement["id"])
            if existing_count == 0:
                stale_statement_ids.append(legacy_statement["id"])
            elif existing_signature == parsed["trade_signature"]:
                if force:
                    print(f"[UPLOAD] Force mode: legacy exact duplicate - deleting old statement")
                    stale_statement_ids.append(legacy_statement["id"])
                else:
                    print(f"[UPLOAD] Legacy exact duplicate - returning conflict")
                    duplicate = [legacy_statement]
                    break
            else:
                if force:
                    print(f"[UPLOAD] Force mode: legacy signature mismatch - deleting old statement")
                    stale_statement_ids.append(legacy_statement["id"])
                else:
                    duplicate = [legacy_statement]
                    break

    if not duplicate and parsed.get("trade_signature"):
        broker = parsed.get("broker", "Unknown")
        print(f"[UPLOAD] Checking same-day statements by trade signature: broker={broker}, trade_date={parsed['trade_date']}")
        same_day_query = supabase.table("statements").select("id,unique_statement_id").eq(
            "user_id", user_id
        ).eq("trade_date", parsed["trade_date"])
        if broker:
            same_day_query = same_day_query.eq("broker", broker)
        same_day_statements = same_day_query.execute().data or []

        ignored_ids = {s["id"] for s in existing} | set(stale_statement_ids)
        for same_day_statement in same_day_statements:
            if same_day_statement["id"] in ignored_ids:
                continue
            existing_signature, existing_count = statement_trade_signature(supabase, user_id, same_day_statement["id"])
            if existing_count == 0:
                stale_statement_ids.append(same_day_statement["id"])
                continue
            if existing_signature == parsed["trade_signature"]:
                if force:
                    print(f"[UPLOAD] Force mode: old generated-id duplicate - deleting {same_day_statement['id']}")
                    stale_statement_ids.append(same_day_statement["id"])
                else:
                    print(f"[UPLOAD] Old generated-id duplicate - returning conflict")
                    duplicate = [same_day_statement]
                    break

    print(f"[UPLOAD] Deleting {len(stale_statement_ids)} stale/duplicate statements")
    for stale_id in stale_statement_ids:
        delete_statement_by_id(supabase, user_id, stale_id)
        print(f"  ✓ Removed stale/duplicate statement {stale_id}")

    if duplicate and not force:
        print(f"[UPLOAD] Returning 409 conflict (not forcing)")
        statement_identifier = parsed["statement_id"]
        raise HTTPException(
            status_code=409,
            detail=f"This statement already exists (ID: {statement_identifier}). Use force re-import if you believe a trade was missed."
        )

    try:
        stmt = supabase.table("statements").insert({
            "user_id":             user_id,
            "statement_id":        parsed["statement_id"],
            "unique_statement_id": storage_unique_id,
            "broker":              parsed.get("broker", "Unknown"),
            "trade_date":          parsed["trade_date"],
            "settlement_date":     parsed["settlement_date"],
            "client_name":         parsed["client_name"],
            "cdc_id":              parsed["cdc_id"],
        }).execute()
        print(f"[UPLOAD] Created new statement {stmt.data[0]['id']}")
    except APIError as e:
        if getattr(e, "code", None) == "23505" or "23505" in str(e):
            print(f"[UPLOAD] Unique constraint violation (duplicate insert)")
            statement_identifier = parsed["statement_id"]
            raise HTTPException(
                status_code=409,
                detail=f"Statement {statement_identifier} already exists. Try force re-import or contact support if the issue persists."
            )
        raise

    db_id = stmt.data[0]["id"]

    rows = [{
        "user_id":             user_id,
        "statement_db_id":     db_id,
        "statement_id":        parsed["statement_id"],
        "unique_statement_id": storage_unique_id,
        "broker":              parsed.get("broker", "Unknown"),
        "trade_date":          parsed["trade_date"],
        "symbol":              (t.get("symbol") or "").upper(),
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
        "is_futures":          t.get("is_futures", False),
        "is_short_sell":       False,
        "matched":             False,
        "pair_id":             None,
    } for t in parsed["trades"]]

    supabase.table("trades").insert(rows).execute()
    
    # Clear all previous matching and re-match everything
    # This ensures consistent output regardless of upload order
    clear_all_matched_trades(user_id, supabase)
    match_trades(user_id, supabase)

    return {
        "success":         True,
        "statement_id":    parsed["statement_id"],
        "broker":          parsed.get("broker", "Unknown"),
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

@app.post("/api/trades/rematch")
async def rematch_trades(token: str = Depends(verify_token)):
    supabase = get_supabase()
    user_id  = supabase.auth.get_user(token).user.id
    match_trades(user_id, supabase)
    return {"success": True}


@app.get("/api/summary")
async def get_summary(token: str=Depends(verify_token)):
    supabase = get_supabase()
    user_id  = supabase.auth.get_user(token).user.id
    matched_trades = supabase.table("trades").select("*").eq("user_id", user_id).eq("matched", True).not_.is_("net_pl","null").execute().data
    all_trades = supabase.table("trades").select("broker,total_charges").eq("user_id", user_id).execute().data
    seen = set(); pairs = []
    for t in matched_trades:
        if t["pair_id"] and t["pair_id"] not in seen:
            seen.add(t["pair_id"]); pairs.append(t)
    today = date.today().isoformat()
    ws = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    ms    = date.today().replace(day=1).isoformat()
    def pl(lst): return round(sum(t["net_pl"] for t in lst if t.get("net_pl")), 2)
    def win_rate(lst): return round(len([t for t in lst if t.get("net_pl",0) > 0]) / len(lst) * 100, 1) if lst else 0
    best  = max(pairs, key=lambda t: t.get("net_pl", 0), default=None)
    worst = min(pairs, key=lambda t: t.get("net_pl", 0), default=None)

    broker_names = sorted(set((t.get("broker") or "Unknown") for t in all_trades + pairs))
    brokers = []
    for broker in broker_names:
        broker_pairs = [t for t in pairs if (t.get("broker") or "Unknown") == broker]
        broker_rows = [t for t in all_trades if (t.get("broker") or "Unknown") == broker]
        brokers.append({
            "broker": broker,
            "pl": pl(broker_pairs),
            "trades": len(broker_pairs),
            "win_rate": win_rate(broker_pairs),
            "charges": round(sum(t.get("total_charges") or 0 for t in broker_rows), 2),
        })

    return {
        "today_pl":      pl([t for t in pairs if t["trade_date"] == today]),
        "week_pl":       pl([t for t in pairs if t["trade_date"] >= ws]),
        "month_pl":      pl([t for t in pairs if t["trade_date"] >= ms]),
        "total_charges": round(sum(t.get("total_charges") or 0 for t in all_trades), 2),
        "win_rate":      win_rate(pairs),
        "total_trades":  len(pairs),
        "best_trade":    {"symbol": best["symbol"],  "pl": best["net_pl"]}  if best  else None,
        "worst_trade":   {"symbol": worst["symbol"], "pl": worst["net_pl"]} if worst else None,
        "brokers":       brokers,
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
# TRADE MANAGEMENT (Add, Edit, Delete)
# ─────────────────────────────────────────────────────────────

@app.post("/api/trades/add")
async def add_trade(
    trade_data: dict,
    token: str = Depends(verify_token)
):
    """Add a new manual trade"""
    supabase = get_supabase()
    user_id = supabase.auth.get_user(token).user.id
    
    try:
        # Convert string values to numbers
        quantity = float(trade_data.get("quantity", 0))
        rate = float(trade_data.get("rate", 0))
        total_charges = float(trade_data.get("total_charges", 0))
        commission = float(trade_data.get("commission", 0))
        
        gross_amount = quantity * rate
        net_amount = gross_amount - total_charges
        
        trade_record = {
            "user_id": user_id,
            "symbol": (trade_data.get("symbol") or "").upper(),
            "company_name": trade_data.get("company_name", trade_data.get("symbol")),
            "trade_type": trade_data.get("trade_type"),  # BUY or SELL
            "trade_date": trade_data.get("trade_date"),
            "quantity": int(quantity),
            "rate": rate,
            "total_charges": total_charges,
            "commission": commission,
            "gross_amount": gross_amount,
            "net_amount": net_amount,
            "broker": trade_data.get("broker", "Manual"),
            "settlement_type": trade_data.get("settlement_type", "Ready"),
            "is_futures": trade_data.get("is_futures", False),
            "is_short_sell": False,
            "matched": False,
            "pair_id": None,
            "statement_db_id": None,
        }
        
        result = supabase.table("trades").insert(trade_record).execute()
        
        # Re-match all trades to pair the new trade with existing ones
        match_trades(user_id, supabase)
        
        return {"success": True, "trade_id": result.data[0]["id"], "message": "Trade added successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to add trade: {str(e)}")

@app.put("/api/trades/{trade_id}")
async def edit_trade(
    trade_id: str,
    trade_data: dict,
    token: str = Depends(verify_token)
):
    """Edit an existing trade"""
    supabase = get_supabase()
    user_id = supabase.auth.get_user(token).user.id
    
    try:
        # Verify ownership
        existing = supabase.table("trades").select("id").eq("id", trade_id).eq("user_id", user_id).execute().data
        if not existing:
            raise HTTPException(status_code=403, detail="Trade not found or unauthorized")
        
        # Convert string values to numbers
        quantity = float(trade_data.get("quantity", 0))
        rate = float(trade_data.get("rate", 0))
        total_charges = float(trade_data.get("total_charges", 0))
        commission = float(trade_data.get("commission", 0))
        
        gross_amount = quantity * rate
        net_amount = gross_amount - total_charges
        
        update_record = {
            "symbol": (trade_data.get("symbol") or "").upper(),
            "company_name": trade_data.get("company_name"),
            "trade_type": trade_data.get("trade_type"),
            "trade_date": trade_data.get("trade_date"),
            "quantity": int(quantity),
            "rate": rate,
            "total_charges": total_charges,
            "commission": commission,
            "gross_amount": gross_amount,
            "net_amount": net_amount,
            "broker": trade_data.get("broker"),
            "settlement_type": trade_data.get("settlement_type"),
            "is_futures": trade_data.get("is_futures", False),
        }
        
        supabase.table("trades").update(update_record).eq("id", trade_id).eq("user_id", user_id).execute()
        
        # Re-match all trades in case the edit affects pairing
        match_trades(user_id, supabase)
        
        return {"success": True, "message": "Trade updated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to edit trade: {str(e)}")

@app.delete("/api/trades/{trade_id}")
async def delete_trade(
    trade_id: str,
    token: str = Depends(verify_token)
):
    """Delete a trade"""
    supabase = get_supabase()
    user_id = supabase.auth.get_user(token).user.id
    
    try:
        # Verify ownership
        existing = supabase.table("trades").select("id,pair_id").eq("id", trade_id).eq("user_id", user_id).execute().data
        if not existing:
            raise HTTPException(status_code=403, detail="Trade not found or unauthorized")
        
        trade = existing[0]
        
        # If trade is part of a pair, unmatch it
        if trade.get("pair_id"):
            supabase.table("trades").update({
                "matched": False,
                "pair_id": None,
                "gross_pl": None,
                "net_pl": None,
            }).eq("pair_id", trade["pair_id"]).eq("user_id", user_id).execute()
        
        # Delete the trade
        supabase.table("trades").delete().eq("id", trade_id).eq("user_id", user_id).execute()
        
        # Re-match all trades in case deletion affects pairing of remaining trades
        match_trades(user_id, supabase)
        
        return {"success": True, "message": "Trade deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to delete trade: {str(e)}")

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
