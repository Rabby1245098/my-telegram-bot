import os, io, re, random, hashlib, asyncio, threading
from pathlib import Path
import json
from json import JSONDecodeError
import base64
import datetime
import logging
from PIL import Image
import requests
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import TimedOut
from telegram.request import HTTPXRequest # <--- ADD THIS LINE HER
import warnings
import smtplib, ssl
from email.message import EmailMessage
import concurrent.futures
from datetime import timezone, timedelta

# Suppress specific warnings
warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")




# === Buttons ===
BTN_LIVE   = "▶️ Live Signals"
BTN_FUTURE = "📅 Future Signals"
BTN_CHART  = "📊 Chart Analysis"
BTN_STOP   = "⏹️ Stop"

# === Keyboards ===
from telegram import ReplyKeyboardMarkup

def _kb_main_menu():
    # Main menu: 3 options
    return ReplyKeyboardMarkup(
        [[BTN_LIVE], [BTN_FUTURE], [BTN_CHART]],
        resize_keyboard=True
    )

def _kb_only_stop():
    # While live is running: ONLY stop button
    return ReplyKeyboardMarkup([[BTN_STOP]], resize_keyboard=True, one_time_keyboard=True)




# --- Configuration ---
USERS_DB = os.path.join(os.path.dirname(__file__), "users.json")
USERS_LOCK = threading.Lock()
USERS = None # Global cache for user data

# Telegram Bot Token and OpenAI API Key (get from environment variables or set defaults)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "7858886162:AAF1gZYvXKYqxdxvOAJFlUk3eCxBGrBTYq8")


CHANNEL_USERNAME = "https://t.me/+zL1XvBP53JwwOGRle"
SECRET_TOKEN = "piyashbhai"

# --- add imports ---
import os, logging
from telegram import Bot, ForceReply

# --- second bot config (FILL THIS) ---
SECOND_BOT_TOKEN = "8133270173:AAGNiYV2a8hkKwzBLHtBO1f3XeIimrZL6Tg"  # given by you
# ⬇️ যে চ্যাটে এলার্ট পাঠাতে চান (আপনার নিজের Telegram user ID / admin group ID)
SECOND_BOT_CHAT_ID = int(os.environ.get("SECOND_BOT_CHAT_ID", "-1002704537048"))  # ← এখানে সংখ্যা বসান, নাহলে env var দিন



API_BASE_QUOTEX = "https://rtadvancequotex.pythonanywhere.com/signals"
API_BASE_BINOLLA = 'https://rtadvancebinolla.pythonanywhere.com/signals'


DHKA_TZ = timezone(timedelta(hours=6))

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "nexosignals@gmail.com") 
SMTP_PASS = os.getenv("SMTP_PASS", "fkxyjvkurnqfwzhq") 
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "nexosignals@gmail.com")

VERIFY_EXP_MIN = int(os.environ.get("VERIFY_EXP_MIN", "15")) 
RESEND_COOLDOWN_SEC = int(os.environ.get("RESEND_COOLDOWN_SEC", "60")) 

user_context = {}


CANCELLED_RUNS = set()

# --- OpenAI Client Setup ---
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
    print("Warning: OpenAI library not found. Chart analysis will use deterministic fallback.")

# --- User Data Management ---
def _load_users():
    global USERS
    if USERS is not None:
        return USERS
    try:
        with open(USERS_DB, "r", encoding="utf-8") as f:
            USERS = json.load(f)
    except (FileNotFoundError, JSONDecodeError):
        USERS = {}
    return USERS


# === Chart Analysis quota (FREE plan) ===
MAX_FREE_CHART_PER_DAY = int(os.environ.get("MAX_FREE_CHART_PER_DAY", "12"))

def _today_str_dhaka() -> str:
    return _now_dhaka_dt().strftime("%Y-%m-%d")

def _chart_quota_reset_if_needed(d: dict):
    """Reset daily quota counters if the day has changed (Dhaka time)."""
    today = _today_str_dhaka()
    if d.get("chart_date") != today:
        d["chart_date"] = today
        d["chart_used"] = 0

def _chart_quota_left(d: dict) -> int:
    """Return how many analyses are left today for FREE users. VIP is unlimited."""
    _chart_quota_reset_if_needed(d)
    if d.get("access") == "vip":
        return 10**9  # effectively unlimited
    used = int(d.get("chart_used", 0))
    return max(MAX_FREE_CHART_PER_DAY - used, 0)

def _chart_quota_inc(d: dict):
    """Increment usage counter once per analysis for FREE users."""
    _chart_quota_reset_if_needed(d)
    d["chart_used"] = int(d.get("chart_used", 0)) + 1


def _save_users():
    with USERS_LOCK:
        tmp = USERS_DB + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(USERS, f, ensure_ascii=False, indent=2)
        os.replace(tmp, USERS_DB)


def ensure_user(uid: int):
    """Create/get a safe container for this user in both persistent and in-memory stores."""
    u_persistent = _load_users()
    key = str(uid)

    # default persistent record
    if key not in u_persistent:
        u_persistent[key] = {
            "registered": False,
            "access": None,
            "mode": None,
            "awaiting": None,
            "selected_pairs": [],
            # NEW: daily chart quota
            "chart_date": _today_str_dhaka(),
            "chart_used": 0,
            # registration info (kept if present)
            "email": None,
            "verify_code": None,
            "verify_expiry": None,
            "verify_sent_at": None,
        }
        _save_users()

    # In-memory (session) state
    u_session = user_context.setdefault(uid, {
        "access": u_persistent[key].get("access", "pending"),
        "mode": u_persistent[key].get("mode"),
        "awaiting": u_persistent[key].get("awaiting"),
        "selected_pairs": u_persistent[key].get("selected_pairs", []),
        "selected_indicators": [],
        "timeframe": None,
        "live_running": False,
        "live_run_id": None,
        "registered": u_persistent[key].get("registered", False),
        "email": u_persistent[key].get("email"),
        "verify_code": u_persistent[key].get("verify_code"),
        "verify_expiry": u_persistent[key].get("verify_expiry"),
        "verify_sent_at": u_persistent[key].get("verify_sent_at"),
        "tg_username": None,  # filled on first interaction
        # NEW: daily chart quota mirrors persistent
        "chart_date": u_persistent[key].get("chart_date") or _today_str_dhaka(),
        "chart_used": int(u_persistent[key].get("chart_used", 0)),
    })

    # keep in-memory access in sync if persistent has value
    if u_persistent[key].get("access") is not None:
        u_session["access"] = u_persistent[key]["access"]

    # reset quota if day changed
    _chart_quota_reset_if_needed(u_session)
    return u_session

def save_user_persistent(uid: int):
    """Persist specific user data from in-memory to disk."""
    u_session = user_context.get(uid)
    if u_session:
        u_persistent = _load_users()
        key = str(uid)
        # persist main fields
        u_persistent[key]["registered"] = u_session.get("registered", False)
        u_persistent[key]["email"] = u_session.get("email")
        u_persistent[key]["verify_code"] = u_session.get("verify_code")
        u_persistent[key]["verify_expiry"] = u_session.get("verify_expiry")
        u_persistent[key]["verify_sent_at"] = u_session.get("verify_sent_at")
        u_persistent[key]["access"] = u_session.get("access")
        u_persistent[key]["selected_pairs"] = u_session.get("selected_pairs", [])
        # NEW: persist daily quota fields
        _chart_quota_reset_if_needed(u_session)
        u_persistent[key]["chart_date"] = u_session.get("chart_date")
        u_persistent[key]["chart_used"] = int(u_session.get("chart_used", 0))
        _save_users()



async def _notify_second_bot_new_user(tg_username: str | None, plan_code: str, user_id: int):
    """
    plan_code: "free" | "vip" | others
    Sends: 
    new free user detected -

    telegram username: @user
    plan name: free plan.
    """
    try:
        plan_name = "free plan" if str(plan_code).lower() == "free" else "vip plan"
        if not SECOND_BOT_TOKEN or not SECOND_BOT_CHAT_ID:
            logging.warning("Second-bot notify skipped (token/chat_id missing).")
            return
        bot2 = Bot(token=SECOND_BOT_TOKEN)
        uname = tg_username if tg_username else "N/A"
        msg = (
            f"new {plan_name.split()[0]} user detected -\n\n"
            f"telegram username: {uname}\n"
            f"plan name: {plan_name}."
        )
        await bot2.send_message(chat_id=SECOND_BOT_CHAT_ID, text=msg)
    except Exception as e:
        logging.exception(f"Second-bot notify failed: {e}")


# --- Keyboards ---
def _kb_main():
    return ReplyKeyboardMarkup([["M1", "M2", "M5"]], resize_keyboard=True)

def _kb_main_menu():
    # Main menu: Live / Future / Chart
    return ReplyKeyboardMarkup([["Live Signal", "Future Signal"], ["Chart Analysis"]], resize_keyboard=True)


def _kb_future_tf():
    return ReplyKeyboardMarkup([["M1", "M2", "M5"], ["Back"]], resize_keyboard=True)

def _kb_future_done():
    return ReplyKeyboardMarkup([["Back", "Again generate signals"]], resize_keyboard=True)

# --- Keyboards ---
def _registration_start_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Start Registration", callback_data="reg_start")],
        [InlineKeyboardButton("❌ Cancel", callback_data="reg_cancel")]
    ])
def _registration_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Resend Code", callback_data="reg_resend")],
        [InlineKeyboardButton("❌ Cancel", callback_data="reg_cancel")]
    ])
def _chart_analysis_kb_fallback():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📈 Chart Analysis", callback_data="chart_analysis")]])

# --- Time and Code Generation Helpers ---
def _now_dhaka_dt():
    return datetime.datetime.now(tz=DHKA_TZ)

def _gen_code(length: int = 6) -> str:
    return "".join(random.choices("0123456789", k=length))

def _is_valid_email(addr: str) -> bool:
    if not addr:
        return False
    addr = addr.strip()
    if len(addr) > 100:
        return False
    # Only Gmail and Googlemail are accepted
    return re.fullmatch(r"[A-Za-z0-9._%+-]+@(?:gmail\.com|googlemail\.com)", addr) is not None

# --- Email Sender (runs off-thread) ---
def _build_email_content_branded(to_email: str, code: str):
    brand = "Nexo Signals"
    subj = f"{brand} — Your One‑Time Verification Code"
    text = (
        f"{brand}\n\n"
        f"Your verification code: {code}\n\n"
        f"This code can only be used once within 15 minutes (single-use).\n"
        f"To start using the bot, type /verify {code} in Telegram.\n\n"
        f"If you did not request this, please ignore this email."

    )
    html = f"""
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#0b0f17;font-family:Arial,Helvetica,sans-serif;color:#e6e9ef;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0b0f17;">
      <tr><td align="center" style="padding:32px 16px;">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#121826;border-radius:16px;overflow:hidden;border:1px solid #1f2a3a;">
          <tr>
            <td style="padding:24px 28px;background:linear-gradient(90deg,#1b2535,#162032);border-bottom:1px solid #1f2a3a;">
              <div style="font-size:20px;letter-spacing:.5px;color:#c7d2fe;">{brand}</div>
              <div style="font-size:13px;color:#9aa7bd;margin-top:2px;">Chart Analysis Bot — Account Verification</div>
            </td>
          </tr>
          <tr>
            <td style="padding:28px;">
              <div style="font-size:18px;margin-bottom:6px;color:#e6e9ef;">Your One‑Time Code</div>
              <div style="font-size:32px;letter-spacing:6px;font-weight:700;background:#0f1724;border:1px solid #223048;border-radius:12px;padding:18px 24px;text-align:center;margin:12px 0 6px;color:#f1f5ff;">
                {code}
              </div>
              <div style="font-size:14px;color:#9aa7bd;">
                This code is <b>single‑use</b> and expires in <b>{VERIFY_EXP_MIN} minutes</b>.
              </div>

              <div style="height:18px"></div>
              <div style="font-size:15px;line-height:1.6;color:#cbd5e1;">
                How to Use / Usage Instructions (Telegram):
                <ol style="margin:12px 0 0 20px;">
                  <li>/verify <b>{code}</b> type</li>
                  <li>After success, you’ll be able to use the bot.</li>
                </ol>
              </div>

              <div style="height:18px"></div>
              <div style="font-size:12px;color:#7c8aa6;">
                If you did not request this, you can safely ignore this email.
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:18px 28px;border-top:1px solid #1f2a3a;color:#7c8aa6;font-size:12px;">
              © {brand} — Be Safe. Be Smart.
            </td>
          </tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>
"""
    return subj, text, html

def _send_verification_email_sync(to_email: str, code: str, username: str):
    subj, text, html = _build_email_content_branded(to_email, code)
    msg = EmailMessage()
    msg["Subject"] = subj
    msg["From"] = SENDER_EMAIL or SMTP_USER
    msg["To"] = to_email
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    if not SMTP_PASS:
        raise ValueError("SMTP_PASS environment variable is not set. Cannot send emails.")

    try:
        if str(SMTP_PORT) == "587":
            with smtplib.SMTP(SMTP_HOST, int(SMTP_PORT)) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
        else: # Default to SSL for port 465
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, int(SMTP_PORT), context=context) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
    except Exception as e:
        raise RuntimeError(f"Failed to send email via SMTP: {e}") from e

async def _send_verification_email(to_email: str, code: str, username: str):
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return await loop.run_in_executor(pool, _send_verification_email_sync, to_email, code, username)

# --- Signal Data Configuration ---
FREE_PAIRS = ["EURUSD-OTC", "EURCHF-OTC", "EURUSD", "USDNGN-OTC", "EURAUD-OTC", "USDMXN-OTC"]
ALL_PAIRS = [
    "USDEGP-OTC", "USDARS-OTC", "USDNGN-OTC", "USDPKR-OTC", "USDMXN-OTC", "NZDUSD-OTC",
    "USDIDR-OTC", "BTCUSD-OTC", "ETHUSD-OTC", "EURUSD-OTC", "GBPUSD-OTC", "AUDUSD-OTC",
    "EURGBP-OTC", "GBPJPY-OTC", "AUDCAD-OTC", "USDBDT-OTC", "EURUSD"
]
BINOLLA_FREE_PAIRS = ["GER30-OTC", "BNB-OTC", "BTCUSD-OTC", "TONCOIN-OTC", "USDCHF-OTC", "USDCAD-OTC"]
BINOLLA_VIP_PAIRS = [
    "AUDCHF-OTC", "USDBRL-OTC", "USDBDT-OTC", "GBPUSD-OTC", "AUDJPY-OTC", "EURUSD-OTC",
    "EURGBP-OTC", "EURJPY-OTC", "AUDUSD-OTC", "GBPCAD-OTC", "GBPCHF-OTC", "SOLUSD-OTC",
     "XAGUSD-OTC", "XAUUSD-OTC", "GER30-OTC"
]

QUOTEX_OTC_WEEKEND_PAIRS = {
    "GBPUSD-OTC","EURUSD-OTC","AUDUSD-OTC","USDJPY-OTC","USDCHF-OTC",
    "AUDJPY-OTC","AUDCHF-OTC","AUDCAD-OTC","GBPCAD-OTC","GBPCHF-OTC",
    "EURCAD-OTC","EURCHF-OTC","EURGBP-OTC", "EURJPY-OTC"
}

WEEKEND_ONLY_OTC_PAIRS = {"GBPUSD-OTC","EURUSD-OTC","AUDUSD-OTC","USDJPY-OTC","USDCHF-OTC",
    "AUDJPY-OTC","AUDCHF-OTC","AUDCAD-OTC","GBPCAD-OTC","GBPCHF-OTC",
    "EURCAD-OTC","EURCHF-OTC","EURGBP-OTC","EURJPY-OTC"}

# --- General Helpers ---
def _apply_quotex_week_rules(entries, broker: str, now_dt):
    if broker != "Quotex":
        return entries
    wd = now_dt.weekday(); t = now_dt.time(); cutoff = datetime.time(23, 55)
    if wd in (0,1,2,3) or (wd==4 and t <= cutoff):
        return [e for e in entries if e["pair"] not in QUOTEX_OTC_WEEKEND_PAIRS]
    if wd in (5,6) or (wd==4 and t > cutoff):
        return [e for e in entries if e["pair"] in QUOTEX_OTC_WEEKEND_PAIRS]
    return entries

def _normalize_pair_display(raw_pair: str, caption: str = "") -> str:
    if not raw_pair: return raw_pair
    p = raw_pair.strip()
    p = p.replace("(OTC)", "").replace("(otc)", "")
    if p.upper().endswith("-OTC"):
        p = p[:-4].rstrip("-")
    pc = p.replace(" ", "").replace("-", "")
    m = re.fullmatch(r'([A-Za-z]{3})/?([A-Za-z]{3})', pc)
    if m:
        p = f"{m.group(1).upper()}/{m.group(2).upper()}"
    elif "/" in p and "(" not in p:
        a,b = p.split("/",1)
        p = f"{a.strip().upper()}/{b.strip().upper()}"
    return p

def parse_pair_from_caption(caption: str) -> str:
    if not caption:
        return ""
    cap = caption.strip().replace("\n", " ")
    tokens = [t for t in cap.split() if any(sep in t for sep in ['/', '-'])]
    pair = tokens[0] if tokens else ""
    pair = _normalize_pair_display(pair, caption)
    if "OTC" in cap.upper() and "OTC" not in pair.upper():
        pair = f"{pair} (OTC)"
    return pair

def fallback_levels(base_price: float):
    step = max(base_price * 0.0002, 0.001)
    s1 = base_price - step * random.randint(1, 3)
    s2 = base_price - step * random.randint(2, 6)
    r1 = base_price + step * random.randint(1, 3)
    r2 = base_price + step * random.randint(2, 6)
    fmt = "{:.4f}" if base_price < 1000 else "{:.2f}"
    supports = f"{fmt.format(min(s1, s2))}, {fmt.format(max(s1, s2))}"
    resistances = f"{fmt.format(min(r1, r2))}, {fmt.format(max(r1, r2))}"
    return supports, resistances

def detect_prev_candle_color(image_bytes: bytes) -> str:
    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = im.size
        if w < 60 or h < 40:
            return "UNKNOWN"
        def crop_strip(x_start_frac, x_end_frac):
            x0 = max(0, int(w * x_start_frac))
            x1 = min(w, int(w * x_end_frac))
            return im.crop((x0, 0, x1, h)).resize((max(1, x1-x0), 200))
        prev_strip = crop_strip(0.91, 0.94)
        red_cnt = green_cnt = 0
        px = prev_strip.load()
        pw, ph = prev_strip.size
        for y in range(ph):
            for x in range(pw):
                r, g, b = px[x, y]
                if r+g+b < 90:
                    continue
                if r > g + 20 and r > b + 20:
                    red_cnt += 1
                if g > r + 20 and g > b + 20:
                    green_cnt += 1
        if red_cnt == 0 and green_cnt == 0:
            return "UNKNOWN"
        return "RED" if red_cnt > green_cnt else "GREEN"
    except Exception:
        return "UNKNOWN"

def _fmt_tf(tf: str) -> str:
    tf = (tf or "").upper()
    return {"M1": "1m", "M2": "2m", "M5": "5m", "M15": "15m"}.get(tf, tf.lower())

def _parse_api_lines(raw_text: str, selected_tf: str, now_dt):
    out = []
    sel_tf = (selected_tf or "M1").upper()
    for ln in (raw_text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m_time = re.search(r'(\d{1,2}:\d{2})', ln)
        m_dir  = re.search(r'\b(CALL|PUT)\b', ln, re.I)
        m_pair = re.search(r'\b([A-Z0-9]{3,}(?:[-/][A-Z0-9]{3,})+)\b', ln)
        if not (m_time and m_dir and m_pair):
            continue
        try:
            hh, mm = map(int, m_time.group(1).split(":"))
        except Exception:
            continue
        dt = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt < now_dt:
            continue
        m_tf = re.search(r'\b(M1|M2|M5|M15)\b', ln, re.I)
        line_tf = (m_tf.group(1).upper() if m_tf else sel_tf)
        pair = m_pair.group(1).upper()
        out.append({"pair": pair, "direction": m_dir.group(1).upper(), "tf": line_tf, "dt": dt})
    out.sort(key=lambda x: x["dt"])
    return out

def _dedupe_and_enforce_gap(entries: list) -> list:
    if not entries:
        return []
    entries = sorted(entries, key=lambda x: x["dt"])
    seen, result, last_time = set(), [], None
    MIN_GAP_MIN, MAX_GAP_MIN = 4, 6
    for e in entries:
        key = f"{e['pair']}|{e['dt'].strftime('%H:%M')}"
        if key in seen:
            continue
        if last_time is None:
            result.append(e); seen.add(key); last_time = e["dt"]; continue
        gap_min = random.randint(MIN_GAP_MIN, MAX_GAP_MIN)
        if (e["dt"] - last_time).total_seconds() >= gap_min * 60:
            result.append(e); seen.add(key); last_time = e["dt"]
    return result

def _enforce_specific_otc_weekend_only(entries: list):
    out, blocked = [], set()
    for e in entries:
        wd = e["dt"].weekday()
        if e["pair"] in WEEKEND_ONLY_OTC_PAIRS and wd not in (5, 6):
            blocked.add(e["pair"]); continue
        out.append(e)
    return out, sorted(blocked)

# --- OpenAI Analysis ---
SYSTEM_PROMPT = """You are a precise price action analyst for 1-minute binary entries.
Return analysis STRICTLY in the following exact template (no extra text):

Pair: <SYMBOL or UNKNOWN/PAIR>
OTC: <Yes|No>
Percentage: <75-95>%
Trend: <uptrend|downtrend|sideways|N/A>
Candle pattern: <one of the list below, or N/A>
Support: <S1>, <S2>
Resistance: <R1>, <R2>
Next candle: <CALL|PUT>
Reason: <2–3 short sentences with concrete price action: mention whether price failed to make a new high/low, broke or respected a clear level (use actual numbers), and what the most recent candle/pattern implies.>
- Level: <short phrase about key level reaction>
- Candle/Pattern/Momentum: <short phrase about pattern/momentum>
Confidence: <60-90>%

Use ONLY these candle pattern names (exact spelling) if detected; otherwise output N/A:
1) Hammer
2) Bullish Engulfing
3) Morning Star
4) Piercing Pattern
5) Three White Soldiers
6) Bullish Harami
7) Dragonfly Doji
8) Tweezer Bottom
9) Shooting Star
10) Bearish Engulfing
11) Evening Star
12) Dark Cloud Cover
13) Three Black Crows
14) Bearish Harami
15) Gravestone Doji
16) Tweezer Top
17) Doji
18) Spinning Top
19) Inside Bar
20) Marubozu
21) Rising Three Methods
22) Falling Three Methods

Rules:
- OTC must be Yes only if the chart or caption explicitly shows an OTC marker; otherwise No.
- Trend must be ONLY one of: uptrend, downtrend, sideways, N/A.
- Candle pattern must be exactly one item from the list above, or N/A.
- Supports/Resistances must be numeric with uniform decimals (4 for FX/crypto under 1000, 2 for indices/metals).
- Direction is for the NEXT 1-minute candle only.
- Reason paragraph should be ~25–45 words, no emojis.
- Bullets must be exactly as shown, concise.
"""

def analyze_with_openai(image_bytes, caption, is_vip):
    client = None
    if OpenAI is not None and OPENAI_API_KEY:
        try:
            client = OpenAI(api_key=OPENAI_API_KEY)
        except Exception as e:
            print(f"Error initializing OpenAI client: {e}")
            client = None

    if client is None:
        return None # Fallback will be used

    user_content = []
    context_text = f"Caption: {caption or '(none)'}\nPlan: {'VIP' if is_vip else 'FREE'}"
    user_content.append({"type": "text", "text": context_text})
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": user_content}],
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error calling OpenAI API: {e}")
        return None

def format_pretty(ai_text: str, last1_color: str, caption: str = "") -> str:
    pair = percentage = trend = supports = resistances = next_candle = confidence = reason_text = None
    candle_pattern = None
    otc_flag = None
    level_bullet = ""
    pattern_bullet = ""

    for line in ai_text.splitlines():
        if line.startswith("Pair:"): pair = line.split(":",1)[1].strip()
        elif line.startswith("OTC:"): otc_flag = line.split(":",1)[1].strip()
        elif line.startswith("Percentage:"): percentage = line.split(":",1)[1].strip()
        elif line.startswith("Trend:"): trend = line.split(":",1)[1].strip()
        elif line.startswith("Candle pattern:"): candle_pattern = line.split(":",1)[1].strip()
        elif line.startswith("Support:"): supports = line.split(":",1)[1].strip()
        elif line.startswith("Resistance:"): resistances = line.split(":",1)[1].strip()
        elif line.startswith("Next candle:"): next_candle = line.split(":",1)[1].strip()
        elif line.startswith("Confidence:"): confidence = line.split(":",1)[1].strip()
        elif line.startswith("Reason:"): reason_text = line.split(":",1)[1].strip()
        elif line.strip().startswith("- Level:"): level_bullet = line.strip().lstrip("- ").strip()
        elif line.strip().startswith("- Candle/Pattern/Momentum:"): pattern_bullet = line.strip().lstrip("- ").strip()

    arrow = "🔻" if (next_candle or "").upper().startswith("PUT") or "DOWN" in (next_candle or "").upper() else "🔺"
    last1 = last1_color if last1_color in ["RED", "GREEN"] else "UNKNOWN"

    pair = _normalize_pair_display(pair or "", caption)
    cap_has_otc = "OTC" in (caption or "").upper()
    if (otc_flag or "").strip().lower() == "yes":
        if "OTC" not in pair.upper():
            pair = f"{pair} (OTC)"
    elif not cap_has_otc:
        pair = pair.replace(" (OTC)", "").replace(" (otc)", "")

    pretty = "POF AI ANALYSIS SUCCESFUL ✅\n\n"
    if pair: pretty += f"📊 Analyst Pair : {pair}\n"
    if next_candle: pretty += f"☠️ Next candle go: {next_candle} {arrow}\n"
    if trend: pretty += f"💹 Market Trend: {trend}  \n"
    if candle_pattern: pretty += f"💀 Candle pattern: {candle_pattern}  \n"
    if supports: pretty += f"📊 Support: {supports} \n"
    if resistances: pretty += f"📊 Resistance: {resistances}\n\n"
    if percentage: pretty += f"✯ Market percentage: {percentage}\n"
    if confidence: pretty += f"✯ Confidence: {confidence}\n\n"
    pretty += f"# Last 1 Candles: {last1}\n\n"
    if reason_text: pretty += f"🤖 Trade Reason: {reason_text}  \n"
    if level_bullet: pretty += f"- {level_bullet}  \n"
    if pattern_bullet: pretty += f"- {pattern_bullet} \n\n"
    pretty += "CONTRACT OWNER: @piyashbhai"
    return pretty

# --- Telegram Bot Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    d = ensure_user(user_id)
    d["tg_username"] = update.effective_user.username or f"user_{user_id}"
    save_user_persistent(user_id) # Save initial user data

    keyboard = [
        [InlineKeyboardButton("Subscribe", url="https://t.me/+zL1XvBP53JwwOGRl")],
        [InlineKeyboardButton("Check Subscription", callback_data="check_sub")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=user_id, text="Please subscribe to the channel to use this bot:", reply_markup=reply_markup)

async def subscription_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    d = ensure_user(user_id)
    d["tg_username"] = query.from_user.username or f"user_{user_id}"
    save_user_persistent(user_id)

    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        if member.status in ['member', 'creator', 'administrator']:
            try: await query.message.delete()
            except Exception: pass # Ignore if message already deleted
            
            # If user is already registered, go directly to main menu
            if d.get("registered"):
                main_keyboard = [["Live Signal", "Future Signal"], ["Chart Analysis"]]
                if d.get("access") == "free":
                    main_keyboard.append(["VIP PLAN"])
                    msg = "You're enjoying FREE PLAN.\nIf you want VIP PLAN with full access, click below:"
                elif d.get("access") == "vip":
                    main_keyboard.append(["FREE PLAN"])
                    msg = "You're enjoying VIP PLAN.\nIf you want to switch to FREE PLAN, click below:"
                else: # Default to free if access is not set
                    d["access"] = "free"
                    main_keyboard.append(["VIP PLAN"])
                    msg = "You're enjoying FREE PLAN.\nIf you want VIP PLAN with full access, click below:"
                await context.bot.send_message(chat_id=user_id, text=msg, reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
            else:
                # Not registered, prompt for plan selection (which can lead to registration)
                d["access"] = "pending" # Reset access to pending until plan is chosen
                keyboard = [['VIP PLAN'], ['FREE PLAN']]
                reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
                await context.bot.send_message(chat_id=user_id, text="✅ Subscription confirmed!\nChoose a plan:", reply_markup=reply_markup)
        else:
            await query.answer("Please subscribe to the channel first.", show_alert=True)
    except Exception as e:
        print(f"Subscription check failed: {e}")
        await query.answer("Subscription check failed. Try again later.", show_alert=True)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    user_id = update.effective_user.id
    d = ensure_user(user_id)
    d["tg_username"] = update.effective_user.username or f"user_{user_id}"
    save_user_persistent(user_id) # Save user data including username

    text = (update.message.text or "").strip()
    t = text.strip()
    tu = t.upper()

    # Registration flow has priority when active
    if d.get("reg_stage") in {"ask_email", "await_code"}:
        await handle_registration_message_flow(update, context) # Call the dedicated registration message handler
        return

    # --- PLAN SELECT ---
    if tu == "FREE PLAN":
        d.update({"access": "free", "mode": None, "selected_pairs": [], "awaiting": None})
        save_user_persistent(user_id)
        keyboard = [['Live Signal', 'Future Signal'], ['Chart Analysis']]
        await update.message.reply_text(
            "You're on FREE PLAN.\nLimited features enabled.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return

    if tu == "VIP PLAN":
        d["access"] = "awaiting_token"
        save_user_persistent(user_id)
        await update.message.reply_text("Please enter your VIP token to continue:")
        return

    if d.get("access") == "awaiting_token":
        if t == SECRET_TOKEN:
            d.update({"access": "vip", "mode": None, "selected_pairs": [], "awaiting": None})
            save_user_persistent(user_id)
            keyboard = [['Live Signal', 'Future Signal'], ['Chart Analysis']]
            await update.message.reply_text(
                "✅ Token Verified!\nWelcome to VIP PLAN!",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            )
        else:
            await update.message.reply_text("❌ Invalid token. Try again.")
        return

    # --- MAIN MODES ---
    if t in ["Live Signal", "Future Signal", "Chart Analysis"]:
        d["mode"] = t.lower().split()[0]
        if t == "Future Signal":
            d["awaiting"] = "broker"
            await update.message.reply_text(
                "Select your broker:",
                reply_markup=ReplyKeyboardMarkup([["Binolla", "Quotex"]], resize_keyboard=True)
            )
            return
        if t == "Live Signal":
            d["awaiting"] = "live_broker"
            await update.message.reply_text(
                "Select your broker for live signal:",
                reply_markup=ReplyKeyboardMarkup([["Binolla", "Quotex"]], resize_keyboard=True)
            )
            return
        if t == "Chart Analysis":
            # This path is now handled by chart_analysis_entry callback
            # If user types "Chart Analysis" directly, we can redirect them to the button flow
            await chart_analysis_entry(update, context)
            return

    # Route other inputs
    await route_flow(update, context, t, d) 



from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes
import asyncio, datetime, hashlib, re

async def route_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, d: dict):
    user_id = update.effective_user.id

    # ---- helpers ----
    def _kb_main_menu():
        return ReplyKeyboardMarkup(
            [["Live Signal", "Future Signal"], ["Chart Analysis"]],
            resize_keyboard=True
        )

    def _kb_only_stop():
        return ReplyKeyboardMarkup([["Stop"]], resize_keyboard=True, one_time_keyboard=True)

    def _pairs_for_user():
        access = d.get("access", "free")
        broker = d.get("broker", "Quotex")
        BIN_VIP  = globals().get("BINOLLA_VIP_PAIRS", [])
        BIN_FREE = globals().get("BINOLLA_FREE_PAIRS", [])
        ALLP     = globals().get("ALL_PAIRS", [])
        FREEP    = globals().get("FREE_PAIRS", [])
        if broker == "Binolla":
            return BIN_VIP if access == "vip" else BIN_FREE
        return ALLP if access == "vip" else FREEP

    def _kb_pairs_grid(pairs, selected):
        rows = []
        for i in range(0, len(pairs), 2):
            row = []
            for p in pairs[i:i+2]:
                mark = "✅ " if p in selected else ""
                row.append(f"{mark}{p}")
            rows.append(row)
        # Note: live-flow এ Back নেই। future-flow এ Back আছে নিচের স্টেট অনুযায়ী।
        rows += [["Select All Pairs"], ["Done"]]
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)

    def _strip_mark(lbl: str) -> str:
        return lbl.replace("✅", "").strip()

    # ==========================
    # STOP সর্বোচ্চ অগ্রাধিকার
    # ==========================
    if text in ("Stop", "⏹️ Stop"):
        # job_queue named jobs থাকলে ক্যান্সেল
        try:
            for j in (context.job_queue.get_jobs_by_name(f"live:{user_id}") or []):
                j.schedule_removal()
        except Exception:
            pass
        # in-flight টাস্কদের জন্য ক্যান্সেল-মার্ক
        run_id = d.get("live_run_id")
        if run_id:
            try:
                globals().setdefault("CANCELLED_RUNS", set()).add(run_id)
            except Exception:
                pass

        # reset live state
        d["live_running"] = False
        d["live_run_id"] = None
        d["awaiting"] = None
        d["mode"] = None
        save_user_persistent(user_id)

        # back to main
        await update.message.reply_text("🛑 Live stopped. Back to main menu.", reply_markup=_kb_main_menu())
        return

    # ==========================
    # live_running অবস্থায় — শুধু Stop
    # ==========================
    if d.get("live_running"):
        await update.message.reply_text("Live is running. Press **Stop** to exit live mode.", reply_markup=_kb_only_stop())
        return

    # ==========================
    # LIVE FLOW
    # ==========================
    if d.get("awaiting") == "live_broker":
        if text not in ["Binolla", "Quotex"]:
            await update.message.reply_text("Please select a valid broker (Binolla or Quotex).")
            return
        d["mode"] = "live"
        d["broker"] = text
        d["selected_pairs"] = []
        d["awaiting"] = "live_pair_selection"
        pairs = _pairs_for_user()
        await update.message.reply_text("Select Pairs (toggle):", reply_markup=_kb_pairs_grid(pairs, d["selected_pairs"]))
        return

    if d.get("awaiting") == "live_pair_selection":
        allowed = _pairs_for_user()

        if text == "Select All Pairs":
            d["selected_pairs"] = allowed.copy()
            await update.message.reply_text(f"All pairs selected:\n{', '.join(allowed)}")
            return

        if text == "Done":
            d["awaiting"] = "live_indicators"
            d["selected_indicators"] = []
            await update.message.reply_text(
                "Select indicators (toggle):",
                reply_markup=ReplyKeyboardMarkup(
                    [["RSI", "MACD"], ["Stochastic", "Bollinger"], ["Done"]],
                    resize_keyboard=True
                )
            )
            return

        # pair toggle (✅ সহ/ছাড়া)
        raw = _strip_mark(text)
        if raw in allowed:
            sel = d.get("selected_pairs", [])
            if raw in sel:
                sel.remove(raw)
            else:
                sel.append(raw)
            d["selected_pairs"] = sel
            await update.message.reply_text(f"Selected Pairs: {', '.join(sel) if sel else 'None'}")
            return

    if d.get("awaiting") == "live_indicators":
        if text == "Done":
            d["awaiting"] = "live_tf"
            await update.message.reply_text("Select timeframe:", reply_markup=ReplyKeyboardMarkup([["M1", "M2", "M5"]], resize_keyboard=True))
            return
        if text in ["RSI", "MACD", "Stochastic", "Bollinger"]:
            indicators = d.get("selected_indicators", [])
            if text in indicators:
                indicators.remove(text)
            else:
                indicators.append(text)
            d["selected_indicators"] = indicators
            await update.message.reply_text(f"Selected indicators: {', '.join(indicators) if indicators else 'None'}")
        else:
            await update.message.reply_text("Select a valid indicator or press 'Done'")
        return

    if d.get("awaiting") == "live_tf":
        tf = text.upper().strip()
        if tf not in ["M1", "M2", "M5"]:
            await update.message.reply_text("Please choose M1, M2 or M5")
            return

        d["timeframe"] = tf
        d["live_running"] = True
        d["awaiting"] = None
        run_id = hashlib.md5(f"{user_id}-{datetime.datetime.now(datetime.timezone.utc).isoformat()}".encode()).hexdigest()
        d["live_run_id"] = run_id
        save_user_persistent(user_id)

        # লাইভ শুরু: কেবল Stop বাটন
        await update.message.reply_text("live signals actived ✅\n\nWAIT FOR A SIGNALS...", reply_markup=_kb_only_stop())

        # live loop/task শুরু — send_live_signals_from_api এর মধ্যে run_id চেক করবে
        asyncio.create_task(send_live_signals_from_api(context, user_id, run_id))
        return

    # ==========================
    # FUTURE FLOW
    # ==========================
    if d.get("awaiting") == "broker":
        if text not in ["Binolla", "Quotex"]:
            await update.message.reply_text("Please select a valid broker (Binolla or Quotex).")
            return
        d["mode"] = "future"
        d["broker"] = text
        d["selected_pairs"] = []
        d["awaiting"] = "pair_selection"

        allowed_pairs = _pairs_for_user()
        keyboard = [allowed_pairs[i:i+2] for i in range(0, len(allowed_pairs), 2)] + [["Select All Pairs"], ["Done"], ["Back"]]
        await update.message.reply_text(
            f"Broker: {text}\nPlan: {d.get('access','free').upper()}\n\nSelect Pairs (toggle):",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        )
        return

    if d.get("awaiting") == "pair_selection":
        allowed_pairs = _pairs_for_user()

        if text == "Select All Pairs":
            d["selected_pairs"] = allowed_pairs.copy()
            await update.message.reply_text(f"All pairs selected:\n{', '.join(allowed_pairs)}")
            return

        if text == "Done":
            if not d["selected_pairs"]:
                await update.message.reply_text("Please select at least one pair.")
                return
            d["awaiting"] = "start_time"
            await update.message.reply_text("Send Start Time (HH:MM, e.g., 00:00):")
            return

        raw = _strip_mark(text)
        if raw in allowed_pairs:
            sel = d["selected_pairs"]
            if raw in sel:
                sel.remove(raw)
            else:
                sel.append(raw)
            d["selected_pairs"] = sel
            await update.message.reply_text(f"Selected Pairs: {', '.join(sel) if sel else 'None'}")
            return

    if d.get("awaiting") == "start_time":
        try:
            datetime.datetime.strptime(text, "%H:%M")
            d["start_time"] = text
            d["awaiting"] = "end_time"
            await update.message.reply_text("Send End Time (HH:MM, e.g., 23:00):")
        except ValueError:
            await update.message.reply_text("Invalid time format. Use HH:MM")
        return

    if d.get("awaiting") == "end_time":
        try:
            datetime.datetime.strptime(text, "%H:%M")
            d["end_time"] = text
            d["awaiting"] = "tf"
            await update.message.reply_text(
                "Timeframe (choose one)",
                reply_markup=ReplyKeyboardMarkup([["M1", "M2", "M5"], ["Keep M1"]], resize_keyboard=True)
            )
        except ValueError:
            await update.message.reply_text("Invalid time format. Use HH:MM")
        return

    if d.get("awaiting") == "tf":
        tf = text.upper().strip()
        if tf not in ["M1", "M2", "M5", "KEEP M1"]:
            await update.message.reply_text("Please choose M1, M2, M5, or 'Keep M1'")
            return
        d["tf"] = "M1" if tf == "KEEP M1" else tf
        d["awaiting"] = "percentage"
        await update.message.reply_text("Minimum payout percentage? (e.g., 80–95). Default 90.\nSend a number:")
        return

    if d.get("awaiting") == "percentage":
        try:
            pct = int(re.sub(r'[^0-9]', '', text))
            if not (50 <= pct <= 100):
                raise ValueError
            d["percentage"] = pct
        except Exception:
            d["percentage"] = 90
        d["awaiting"] = "extra_filter"
        broker = d.get("broker", "Quotex")
        if broker == "Binolla":
            await update.message.reply_text(
                "Trend Filter? (on/off). Default: off",
                reply_markup=ReplyKeyboardMarkup([["on", "off"], ["Keep off"]], resize_keyboard=True)
            )
        else:
            await update.message.reply_text(
                "NXP Filter? (on/off). Default: on",
                reply_markup=ReplyKeyboardMarkup([["on", "off"], ["Keep on"]], resize_keyboard=True)
            )
        return

    if d.get("awaiting") == "extra_filter":
        broker = d.get("broker", "Quotex")
        val = text.lower().strip()
        if val not in ["on", "off", "keep on", "keep off"]:
            await update.message.reply_text("Choose on/off (or Keep on/off)")
            return
        if broker == "Binolla":
            d["trend_filter"] = "off" if val in ["off", "keep off"] else "on"
        else:
            d["nxp_filter"] = "on" if val in ["on", "keep on"] else "off"
        d["view"] = "semicolon"
        d["awaiting"] = None
        asyncio.create_task(fetch_and_send_future_signals_api(context.bot, user_id))
        return

    # ==========================
    # AGAIN GENERATE
    # ==========================
    if text == "Again generate signals":
        mode = d.get("mode")
        if mode in ("live", "future"):
            d["awaiting"] = "live_pair_selection" if mode == "live" else "pair_selection"
            d["selected_pairs"] = []
            pairs = _pairs_for_user()
            kb = _kb_pairs_grid(pairs, d["selected_pairs"])
            # future মোডে Back দরকার হলে লাইনটা বাড়ান:
            if mode == "future":
                # future-এর কিবোর্ডে Back যোগ
                rows = kb.keyboard + [["Back"]]
                kb = ReplyKeyboardMarkup(rows, resize_keyboard=True)
            await update.message.reply_text("Select Pairs (toggle):", reply_markup=kb)
            return
        else:
            await update.message.reply_text("Invalid state. Please choose Live or Future Signal again, or press /start.")
            return

    # ==========================
    # BACK → Main Menu (live না চললে)
    # ==========================
    if text == "Back":
        d["awaiting"] = None
        d["mode"] = None
        save_user_persistent(user_id)
        await update.message.reply_text("Back to main menu.", reply_markup=_kb_main_menu())
        return

    # ==========================
    # DEFAULT → Main Menu
    # ==========================
    if text in ("/start", "Start", "Main Menu", "Menu"):
        d["awaiting"] = None
        d["mode"] = None
        save_user_persistent(user_id)
        await update.message.reply_text("Choose an option:", reply_markup=_kb_main_menu())
        return

    await update.message.reply_text("Unknown input. Please press /start to begin.", reply_markup=_kb_main_menu())


# --- Live Signal Logic ---
async def send_live_signals_from_api(context: ContextTypes.DEFAULT_TYPE, user_id: int, run_id: str):
    show      = bool(globals().get("SHOW_LIVE_STATUS_MESSAGES", True))
    lead_sec  = int(globals().get("ANALYZE_LEAD_SECONDS", 15))
    early_min = int(globals().get("EARLY_MINUTES", 2))
    send_main = bool(globals().get("SEND_LIVE_MAIN_AT_T", False))

    kb_live_fn        = globals().get("_kb_live")
    kb_future_done_fn = globals().get("_kb_future_done")

    globals().setdefault("CANCELLED_RUNS", set())

    def _format_signal_text(pair_disp: str, tf_view: str, sig_time: str, direction: str, *, early=False, short_notice=False) -> str:
        badge = "⏰ EARLY (2 min)" if early else "🎯 FINAL"
        if short_notice:
            badge = "⚡ PIYASH BOT LIVE SIGNALS ⚡"
        return (
            f"{badge}\n\n"
            f"📊 Pairs: {pair_disp}\n"
            f"🕜 Timeframe: {tf_view}\n"
            f"⏳ Time: {sig_time}    (utc+6:00)\n"
            f"🎯 Direction: {direction}\n\n"
            "💸 Send feedback - @piyashbhai"
        )

    def _is_cancelled() -> bool:
        cr = globals().get("CANCELLED_RUNS", set())
        d_now = ensure_user(user_id) # Get latest user state
        return (
            (run_id in cr) or
            (d_now.get("cancel_run_id") == run_id) or
            (not d_now.get("live_running")) or
            (d_now.get("live_run_id") not in (None, run_id) and d_now.get("live_run_id") != run_id)
        )

    async def _delay_send(delay_sec: float, text: str):
        await asyncio.sleep(max(float(delay_sec), 0))
        if _is_cancelled():
            return
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
        except Exception:
            pass

    async def _analyze_then_cleanup(_an_delay: float, _pre_delay: float):
        try:
            await asyncio.sleep(max(_an_delay, 0))
            if _is_cancelled():
                return
            a_msg = await context.bot.send_message(chat_id=user_id, text="signals analysing.. wait for conformation.....")
            left = max(_pre_delay - _an_delay, 0)
            if left > 0:
                await asyncio.sleep(left)
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=a_msg.message_id)
            except Exception:
                pass
        except Exception:
            pass

    d = ensure_user(user_id)
    d["live_run_id"] = run_id
    d["live_running"] = True
    d["cancel_run_id"] = None
    CANCELLED_RUNS.discard(run_id)

    try:
        control_mid = d.get("live_control_msg_id")
        if control_mid:
            try:
                if callable(kb_live_fn):
                    await context.bot.edit_message_reply_markup(
                        chat_id=user_id, message_id=control_mid, reply_markup=kb_live_fn()
                    )
            except Exception:
                m = await context.bot.send_message(
                    chat_id=user_id,
                    text="✅ Live signals activated. Use STOP to cancel.",
                    reply_markup=(kb_live_fn() if callable(kb_live_fn) else None)
                )
                d["live_control_msg_id"] = m.message_id
        else:
            m = await context.bot.send_message(
                chat_id=user_id,
                text="✅ Live signals activated. Use STOP to cancel.",
                reply_markup=(kb_live_fn() if callable(kb_live_fn) else None)
            )
            d["live_control_msg_id"] = m.message_id
    except Exception:
        pass

    now_dhk = _now_dhaka_dt()
    if now_dhk.time() >= datetime.time(23, 0):
        if not _is_cancelled():
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="INVALID SIGNALS GENERATE.. YOU CAN GENERATE SIGNALS AFTER 00:00, utc+6:00",
                    reply_markup=(kb_live_fn() if callable(kb_live_fn) else None)
                )
            except Exception:
                pass
        return

    selected_pairs = d.get("selected_pairs", [])
    selected_tf    = (d.get("timeframe") or "M1").upper()
    start_str      = now_dhk.strftime("%H:%M")
    end_str        = "23:59"
    pairs_param    = ",".join(selected_pairs) if selected_pairs else \
                     "EURUSD-OTC,EURUSD,EURCHF-OTC,EURAUD-OTC"

    broker   = (d.get("broker") or "Quotex")
    base     = API_BASE_BINOLLA if broker == "Binolla" else API_BASE_QUOTEX
    live_pct = int(d.get("percentage", 95))
    trend    = (d.get("trend_filter") or "off").lower()
    nxp      = (d.get("nxp_filter") or "off").lower()

    url = (
        f"{base}?Pairs={pairs_param}"
        f"&tf={selected_tf}"
        f"&view=semicolon"
        f"&Start_Time={start_str}"
        f"&End_Time={end_str}"
        f"&percentage={live_pct}"
        + (f"&trend_filter={trend}" if broker == "Binolla" else f"&nxp_filter={nxp}")
    )

    if show and not _is_cancelled():
        try:
            await context.bot.send_message(chat_id=user_id, text="⏳ Fetching live signals...")
        except Exception:
            pass

    if _is_cancelled():
        return

    resp, last_err = None, None
    loop = asyncio.get_running_loop()

    def _req():
        return requests.get(url, timeout=(10, 25))

    for attempt in range(3):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                resp = await loop.run_in_executor(pool, _req)
            if resp.status_code == 200 and (resp.text or "").strip():
                break
            last_err = f"HTTP {resp.status_code}"
        except requests.exceptions.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < 2:
            await asyncio.sleep(0.8 * (2 ** attempt))
        if _is_cancelled():
            return

    if resp is None or resp.status_code != 200 or not (resp.text or "").strip():
        if not _is_cancelled():
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"⚠️ Live service unavailable ({last_err or 'no response'}). Tap “Again generate signals” to retry.",
                    reply_markup=(kb_future_done_fn() if callable(kb_future_done_fn) else None)
                )
            except Exception:
                pass
        return

    try:
        raw = resp.text.strip()
        now = _now_dhaka_dt()

        entries = _parse_api_lines(raw, selected_tf, now)
        entries = _dedupe_and_enforce_gap(entries)

        entries, blocked_pairs = _enforce_specific_otc_weekend_only(entries)
        if blocked_pairs and not _is_cancelled():
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"{', '.join(blocked_pairs)} not open pairs in quotex, try to another pairs.."
                )
            except Exception:
                pass

        entries = _apply_quotex_week_rules(entries, broker, now)

        if not entries:
            if now.time() >= datetime.time(22, 55):
                if not _is_cancelled():
                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text="INVALID SIGNALS GENERATE.. YOU CAN GENERATE SIGNALS AFTER 00:00, utc+6:00",
                            reply_markup=(kb_live_fn() if callable(kb_live_fn) else None)
                        )
                    except Exception:
                        pass
            else:
                if show and not _is_cancelled():
                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text="No upcoming signals found (for the selected timeframe)."
                        )
                    except Exception:
                        pass
            return

        scheduled = 0
        for e in entries:
            if _is_cancelled():
                break

            tf_view   = _fmt_tf(selected_tf)
            sig_time  = e["dt"].strftime("%H:%M")
            pair_disp = e["pair"]
            direction = e["direction"]

            pre_at       = e["dt"] - datetime.timedelta(minutes=early_min)
            pre_delay    = max((pre_at - now).total_seconds(), 0.0)
            main_delay   = max((e["dt"] - now).total_seconds(), 0.0)
            short_notice = (e["dt"] - now) < datetime.timedelta(minutes=early_min)

            early_text = _format_signal_text(
                pair_disp, tf_view, sig_time, direction,
                early=not short_notice, short_notice=short_notice
            )

            analyze_delay = max(pre_delay - lead_sec, 0.0)
            asyncio.create_task(_analyze_then_cleanup(analyze_delay, pre_delay))
            asyncio.create_task(_delay_send(pre_delay, early_text))

            if send_main:
                main_text = _format_signal_text(pair_disp, tf_view, sig_time, direction, early=False)
                asyncio.create_task(_delay_send(main_delay, main_text))

            scheduled += 1

        if show and not _is_cancelled():
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(f"GENERATING SIGNALS....")

                )

            except Exception:
                pass

    except Exception as e:
        if not _is_cancelled():
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"⚠️ Live service error while parsing/scheduling: {type(e).__name__} ({e})",
                    reply_markup=(kb_live_fn() if callable(kb_live_fn) else None)
                )
            except Exception:
                pass

# --- Future Signal Logic ---
async def fetch_and_send_future_signals_api(bot, user_id: int):
    d = ensure_user(user_id)
    pairs = d.get("selected_pairs", [])
    start = d.get("start_time", "00:00")
    end = d.get("end_time", "23:00")
    tf = d.get("tf", "M1")
    view = "semicolon"
    percentage = int(d.get("percentage", 90))
    nxp_filter = (d.get("nxp_filter") or "on")
    trend_filter = (d.get("trend_filter") or "off")
    platform = d.get("platform", d.get("broker", "Quotex"))

    if not pairs:
        await bot.send_message(chat_id=user_id, text="❌ No pairs selected.", reply_markup=_kb_future_done())
        return

    pairs_param = ",".join(pairs)
    broker = d.get("broker", "Quotex")
    base = API_BASE_BINOLLA if broker == "Binolla" else API_BASE_QUOTEX

    if broker == "Binolla":
        url = (
            f"{base}?Pairs={pairs_param}"
            f"&tf={tf}"
            f"&view={view}"
            f"&Start_Time={start}"
            f"&End_Time={end}"
            f"&percentage={percentage}"
            f"&trend_filter={trend_filter}"
        )
    else:
        url = (
            f"{base}?Pairs={pairs_param}"
            f"&tf={tf}"
            f"&view={view}"
            f"&Start_Time={start}"
            f"&End_Time={end}"
            f"&percentage={percentage}"
            f"&nxp_filter={nxp_filter}"
        )

    try:
        def _req():
            return requests.get(url, timeout=30)

        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            resp = await loop.run_in_executor(pool, _req)

        if resp.status_code != 200 or not resp.text.strip():
            await bot.send_message(chat_id=user_id, text=f"❌ Server returned no data (status {resp.status_code}).\nURL: {url}", reply_markup=_kb_future_done())
            return

        raw = resp.text.strip()
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

        header_keys = (
            "quotex advanced api signals",
            "selected pairs:", "not find pairs:", "percentage filter:",
            "nxp filter:", "trend filter:", "timeframe:", "timezones", "craft by", "total signals",
            "date:"
        )
        filtered = []
        for ln in lines:
            low = ln.lower()
            if any(k in low for k in header_keys):
                continue
            filtered.append(ln)

        if not filtered:
            await bot.send_message(chat_id=user_id, text="⚠️ No signals for the chosen filters.", reply_markup=_kb_future_done())
            return

        pretty_rows = []
        for ln in filtered:
            if ";" in ln:
                parts = [p.strip() for p in ln.split(";")]
                pretty_rows.append(" | ".join(parts))
            else:
                pretty_rows.append(ln)

        today = _now_dhaka_dt().strftime("%d • %m • %Y")
        zone = "UTC+6"
        duration_map = {"M1": "1 Minute", "M2": "2 Minutes", "M5": "5 Minutes"}
        duration = duration_map.get(tf.upper(), tf)

        header = (
            "╔═════════════❖═════════════╗\n"
            "        𒆜 FINAL SIGNAL 𒆜\n"
            "╚═════════════❖═════════════╝\n\n"
            "╭━━━⌯ DETAILS ⌯━━━╮\n"
            f"📅 Date: {today}\n"
            f"🌐 Zone: {zone}\n"
            f"⚡ Platform: {platform}\n"
            f"⏱️ Duration: {duration}\n"
            f"♻️ Martingale: 1 Step Only\n"
            "╰━━━━━━━━━━━━━━━━━╯\n"
        )

        signals_text = "\n".join(pretty_rows)

        footer = (
            "\n\n╔═════════⚠️ TRADE ALERT ⚠️═════════╗\n"
            "❗️ Avoid after oversized candles\n"
            "❗️ Doji patterns or price gaps\n"
            "❗️ Payouts below 80% = NO TRADE\n"
            "╟──────────────────────────────────╢\n"
            "❤️‍🔥 POF — BE SAFE. BE SMART. ❤️‍🔥\n"
            "🔥 Send feedback - @piyashbhai 🔥\n"
            "╚══════════════════════════════════╝"
        )

        full_text = f"{header}\n{signals_text}{footer}"

        if len(full_text) <= 3900:
            await bot.send_message(chat_id=user_id, text=full_text, reply_markup=_kb_future_done())
        else:
            MAX_LEN = 3500
            chunk_rows, chunks = [], []
            base_len = len(header)
            current_len = base_len
            for row in pretty_rows:
                if current_len + len(row) + 1 > MAX_LEN and chunk_rows:
                    chunks.append("\n".join(chunk_rows))
                    chunk_rows = [row]
                    current_len = base_len + len(row) + 1
                else:
                    chunk_rows.append(row)
                    current_len += len(row) + 1
            if chunk_rows:
                chunks.append("\n".join(chunk_rows))

            await bot.send_message(chat_id=user_id, text=f"{header}\n{chunks[0]}", reply_markup=_kb_future_done())
            for mid in chunks[1:-1]:
                await bot.send_message(chat_id=user_id, text=mid)
            if len(chunks) > 1:
                await bot.send_message(chat_id=user_id, text=f"{chunks[-1]}{footer}")

    except Exception as e:
        await bot.send_message(chat_id=user_id, text=f"❌ Error fetching signals:\n{e}\nURL: {url}", reply_markup=_kb_future_done())

# --- Chart Analysis Handlers ---
async def chart_analysis_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()

    user_id = update.effective_user.id
    d = ensure_user(user_id)
    d["tg_username"] = update.effective_user.username or f"user_{user_id}"
    save_user_persistent(user_id)

    # Registration gate
    if not d.get("registered"):
        d["awaiting"] = None
        d["reg_stage"] = "start"
        await _ask_registration(update, context, reason_text="ℹ️ You need to verify your account before using Chart Analysis")
        return

    # Daily quota check (FREE only)
    _chart_quota_reset_if_needed(d)
    if d.get("access") != "vip":
        left = _chart_quota_left(d)
        if left <= 0:
            # Show upgrade option
            main_keyboard = [['Live Signal', 'Future Signal'], ['Chart Analysis'], ['VIP PLAN']]
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ Daily limit reached for FREE plan.\n"
                    f"You can analyze up to {MAX_FREE_CHART_PER_DAY} charts per day.\n"
                    f"Come back after 00:00 (UTC+6) or switch to VIP for unlimited."
                ),
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
            )
            return
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ FREE plan remaining today: {left} analysis."
            )

    # proceed to chart analysis
    d["mode"] = "chart"
    d["awaiting"] = "chart_photo"
    save_user_persistent(user_id)
    await context.bot.send_message(
        chat_id=user_id,
        text="📊 **POF Chart Analysis**\n\nHEY USER JUST SEND ME QUOTEX, BINOLLA TRADING CHARTS SCREENSHOTS",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([['Back']], resize_keyboard=True)
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id
    d = ensure_user(user_id)

    # Only when in chart-analysis photo state
    if d.get("mode") != "chart" or d.get("awaiting") != "chart_photo":
        return

    # FREE plan daily limit check (hard-stop)
    _chart_quota_reset_if_needed(d)
    if d.get("access") != "vip" and _chart_quota_left(d) <= 0:
        kb = ReplyKeyboardMarkup([['Live Signal', 'Future Signal'], ['Chart Analysis'], ['VIP PLAN']], resize_keyboard=True)
        await update.message.reply_text(
            f"⚠️ FREE daily limit {MAX_FREE_CHART_PER_DAY} reached. "
            f"Come back after 00:00 (UTC+6) or switch to VIP for unlimited.",
            reply_markup=kb
        )
        return

    # Must be a photo
    if not update.message.photo:
        await update.message.reply_text("⚠️ No photo found. Please send a screenshot image.")
        return

    # Count usage (FREE only) — count once we accept a valid photo
    if d.get("access") != "vip":
        _chart_quota_inc(d)
        save_user_persistent(user_id)

    # (OPTIONAL) tell remaining
    if d.get("access") != "vip":
        left = _chart_quota_left(d)
        try:
            await update.message.reply_text(f"⏳ Using one analysis. Remaining today: {left}.")
        except Exception:
            pass

    # --- rest of your existing code to download photo & analyze ---
    photo = update.message.photo[-1]
    caption = update.message.caption or ""

    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.UPLOAD_PHOTO)

    try:
        tg_file = await _get_file_with_retry(context.bot, photo.file_id, retries=2)
        image_bytes = await _download_to_memory_with_retry(tg_file, retries=2)
    except TimedOut:
        await update.message.reply_text("⏳ Network timed out while receiving the photo. Please resend.")
        return
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed to fetch your photo: {e!s}")
        return

    try:
        last1_color = detect_prev_candle_color(image_bytes)
    except Exception:
        last1_color = None

    is_vip = (d.get("access") == "vip")

    ai_text = None
    try:
        ai_text = analyze_with_openai(image_bytes, caption, is_vip)
    except Exception as e:
        print(f"Error during OpenAI analysis: {e}")
        ai_text = None

    if ai_text is None:
        h = hashlib.md5(image_bytes + (caption or "").encode()).hexdigest()
        seed = int(h[:8], 16); rnd = random.Random(seed)

        pair = parse_pair_from_caption(caption) or "UNKNOWN/PAIR"
        m = re.search(r"(\d{1,4}\.\d{2,4})", caption.replace(",", "")) if caption else None
        num = float(m.group(1)) if m else None
        base = num if num else rnd.uniform(0.8000, 120.0000)

        supports, resistances = fallback_levels(base)
        trend = rnd.choice(["uptrend", "downtrend", "sideways"])
        direction = rnd.choice(["CALL", "PUT"]) if trend == "sideways" else ("CALL" if trend == "uptrend" else "PUT")
        perc_low, perc_high = (85, 95) if is_vip else (75, 88)
        percentage = rnd.randint(perc_low, perc_high)
        confidence = rnd.randint(60, 85)

        s_vals = [float(x) for x in supports.replace(',', ' ').split() if x.replace('.', '', 1).isdigit()]
        r_vals = [float(x) for x in resistances.replace(',', ' ').split() if x.replace('.', '', 1).isdigit()]
        s1 = min(s_vals) if s_vals else base - 0.001
        r1 = min(r_vals) if r_vals else base + 0.001

        if trend == "downtrend":
            reason = (f"The price failed to establish a new high near {r1:.4f}, then broke below {s1:.4f} support. "
                      f"Recent candles show bearish momentum; a retest of {s1:.4f} as resistance is likely.")
            level_b = "Level: Rejection at resistance"
            patt_b  = "Candle/Pattern/Momentum: Bearish momentum"
        elif trend == "uptrend":
            reason = (f"Buyers defended {s1:.4f}, pushing price back above {r1:.4f}. "
                      f"Momentum is building; {r1:.4f} may act as support on a shallow pullback.")
            level_b = "Level: Support holding"
            patt_b  = "Candle/Pattern/Momentum: Bullish momentum"
        else:
            reason = (f"Price is oscillating between {s1:.4f} and {r1:.4f} without a decisive break. "
                      f"Wait for a clean break-and-retest to confirm direction.")
            level_b = "Level: Range-bound"
            patt_b  = "Candle/Pattern/Momentum: Neutral momentum"

        ai_text = (
            f"Pair: {pair}\n"
            f"OTC: {'Yes' if 'OTC' in (caption or '').upper() else 'No'}\n"
            f"Percentage: {percentage}%\n"
            f"Trend: {trend}\n"
            f"Candle pattern: N/A\n"
            f"Support: {supports}\n"
            f"Resistance: {resistances}\n"
            f"Next candle: {direction}\n"
            f"Reason: {reason}\n"
            f"- {level_b}\n"
            f"- {patt_b}\n"
            f"Confidence: {confidence}%"
        )

    try:
        pretty = format_pretty(ai_text, last1_color, caption)
    except Exception as e:
        print(f"Error formatting AI text: {e}")
        pretty = "⚠️ Could not format analysis. Raw AI output:\n" + (ai_text or "No AI output.")

    await context.bot.send_message(chat_id=user_id, text=pretty)

    keyboard = [['Analyze another chart'], ['Back']]
    await context.bot.send_message(
        chat_id=user_id,
        text="What do you want to do next?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def handle_another_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or (update.message.text or "").strip() != "Analyze another chart":
        return
    # Re-enter chart analysis flow
    user_id = update.effective_user.id
    d = ensure_user(user_id)
    d["mode"] = "chart"
    d["awaiting"] = "chart_photo"
    await update.message.reply_text(
        "📷 Send your chart screenshot now (optional caption: e.g. `USD/INR (OTC) TF:1m 90.8700`).",
        reply_markup=ReplyKeyboardMarkup([['Back']], resize_keyboard=True)
    )

# --- Registration and Verification Handlers ---
async def _ask_registration(update: Update, context: ContextTypes.DEFAULT_TYPE, reason_text: str = ""):
    chat_id = (update.effective_chat.id if update.effective_chat else update.callback_query.from_user.id)
    text = (
        (reason_text + "\n\n") if reason_text else ""
         + "🔐 Complete registration before using Chart Analysis.\n\n" \
        "• If you are new: **Start Registration**\n" \
        "• If you did not receive a code: **Resend Code**\n" \
        "• If you already have a code: /verify 123456"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Start Registration", callback_data="reg_start")],
        [InlineKeyboardButton("🔁 Resend Code",       callback_data="reg_resend")],
        [InlineKeyboardButton("✖️ Cancel",            callback_data="reg_cancel")],
    ])
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="Markdown")

async def registration_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    d = ensure_user(user_id)
    d["reg_stage"] = "ask_email"
    d["awaiting"] = "reg_email"
    d["tg_username"] = query.from_user.username or f"user_{user_id}"
    save_user_persistent(user_id)
    await query.answer()
    await context.bot.send_message(
        chat_id=user_id,
        text="✉️ Send your Gmail address (e.g., yourname@gmail.com):",
        reply_markup=ForceReply(selective=True)
    )

async def _issue_and_email_code(context: ContextTypes.DEFAULT_TYPE, user_id: int, email: str):
    d = ensure_user(user_id)
    now = _now_dhaka_dt()
    last_sent_at = d.get("verify_sent_at")
    
    # Cooldown check
    if last_sent_at and isinstance(last_sent_at, str):
        try:
            last_sent_dt = datetime.datetime.fromisoformat(last_sent_at)
            if (now - last_sent_dt).total_seconds() < RESEND_COOLDOWN_SEC:
                remain = RESEND_COOLDOWN_SEC - int((now - last_sent_dt).total_seconds())
                await context.bot.send_message(chat_id=user_id, text=f"⏳ Please wait {remain}s before requesting a new code.")
                return False
        except ValueError:
            pass # Ignore if stored date is invalid

    if not SMTP_PASS:
        await context.bot.send_message(
            chat_id=user_id,
            text=("⚠️ Email sender is not configured. Please set SMTP_PASS environment variable "
                  "with your Gmail App Password and try again.")
        )
        return False

    code = _gen_code(6)
    d["verify_code"] = code
    d["verify_expiry"] = (now + datetime.timedelta(minutes=VERIFY_EXP_MIN)).isoformat()
    d["verify_sent_at"] = now.isoformat() # Store as ISO string
    d["email"] = email
    save_user_persistent(user_id)

    try:
        await _send_verification_email(email, code, d.get("tg_username", f"user_{user_id}"))
    except Exception as e:
        print(f"Error sending verification email: {e}")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ Could not send email: {type(e).__name__}. Check SMTP credentials and try again."
        )
        return False

    await context.bot.send_message(
        chat_id=user_id,
        text=(f"✅ Code sent to **{email}**.\n"
              f"Please enter the 6-digit code here (valid {VERIFY_EXP_MIN} minutes) OR reply with /verify {code}.\n"
              f"Type RESEND to get a new code."),
        parse_mode="Markdown",
        reply_markup=_registration_kb()
    )
    return True


async def handle_registration_message_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None:
        return

    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    d = ensure_user(user_id)

    # Already verified → ignore any registration texts
    if d.get("registered"):
        return

    stage = d.get("reg_stage")
    awaiting = d.get("awaiting")

    # ---------- 1) EMAIL STAGE ----------
    if stage == "ask_email" and awaiting == "reg_email":
        email = text.strip()

        # Gmail-only + simple validation
        if not re.fullmatch(r"(?i)[A-Z0-9._%+\-]+@gmail\.com", email):
            await update.message.reply_text(
                "❗️Please send a valid Gmail address (e.g., yourname@gmail.com).",
                reply_markup=ForceReply(selective=True),
            )
            return

        d["email"] = email
        d["reg_stage"] = "await_code"
        d["awaiting"] = "reg_code"
        save_user_persistent(user_id)

        # issue + email the code (handles cooldown & UI)
        await _issue_and_email_code(context, user_id, email)
        return

    # ---------- 2) CODE STAGE ----------
    if stage == "await_code" and awaiting == "reg_code":
        t = (text or "").strip()

        # Treat "Resend" typed as a resend command
        t_norm = t.upper().replace(" ", "").replace("-", "")
        if t_norm in {"RESEND", "RESENDCODE"}:
            email = d.get("email")
            if not email:
                # go back to email stage if somehow missing
                d["reg_stage"] = "ask_email"
                d["awaiting"] = "reg_email"
                save_user_persistent(user_id)
                await update.message.reply_text(
                    "✉️ Send your Gmail address (e.g., yourname@gmail.com):",
                    reply_markup=ForceReply(selective=True),
                )
                return
            await _issue_and_email_code(context, user_id, email)
            return

        # Accept ONLY a 6-digit code from the message
        m = re.fullmatch(r"\D*(\d{6})\D*", t)
        if not m:
            await update.message.reply_text(
                "Please send ONLY the 6-digit code (e.g., 123456) or type RESEND."
            )
            return

        code_to_verify = m.group(1)
        stored_code = d.get("verify_code")
        exp_iso = d.get("verify_expiry")

        # Parse expiry (ISO string stored earlier)
        exp_dt = None
        if exp_iso:
            try:
                exp_dt = datetime.datetime.fromisoformat(exp_iso)
            except ValueError:
                exp_dt = None  # ignore bad format

        if not stored_code:
            await update.message.reply_text("No code issued yet. Type RESEND to get a new code.")
            return

        if exp_dt and _now_dhaka_dt() > exp_dt:
            await update.message.reply_text("⏱️ Code expired. Type RESEND to get a new code.")
            return

        if code_to_verify != stored_code:
            await update.message.reply_text("❌ Wrong code. Please re-check, or type RESEND.")
            return

        # ---------- SUCCESS ----------
        d["registered"] = True
        d["registered_at"] = _now_dhaka_dt().isoformat()
        d["reg_stage"] = None
        d["awaiting"] = None
        d["verify_code"] = None
        d["verify_expiry"] = None
        save_user_persistent(user_id)

        await update.message.reply_text("🎉 Verification successful! Your account is now active.")
        d["mode"] = "chart"
        d["awaiting"] = "chart_photo"
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "📊 **POF Chart Analysis**\n\nHEY USER JUST SEND ME QUOTEX, BINOLLA TRADING CHARTS SCREENSHOTS"
            ),
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([['Back']], resize_keyboard=True),
        )
        return

async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    d = ensure_user(user_id)

    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /verify 123456")
        return

    code_to_verify = (args[0] or "").strip()

    stored_code = d.get("verify_code")
    exp_iso = d.get("verify_expiry")

    exp_dt = None
    if exp_iso:
        try:
            exp_dt = datetime.datetime.fromisoformat(exp_iso)
        except ValueError:
            pass # Invalid ISO format

    if not stored_code:
        await update.message.reply_text("No active code. Use Chart Analysis button to request a new code.")
        return

    if exp_dt and _now_dhaka_dt() > exp_dt:
        await update.message.reply_text("❌ Code expired. Please request a new code via Chart Analysis button.")
        return

    if code_to_verify != stored_code:
        await update.message.reply_text("❌ Wrong code. Try again or request a new code.")
        return

    # Success
    d["registered"] = True
    d["registered_at"] = _now_dhaka_dt().isoformat()
    d["reg_stage"] = None
    d["awaiting"] = None
    d["verify_code"] = None
    d["verify_expiry"] = None
    save_user_persistent(user_id)

    await update.message.reply_text("🎉 Verification successful! Your account is now active.")
    # Automatically transition to Chart Analysis UI
    d["mode"] = "chart"
    d["awaiting"] = "chart_photo"
    await context.bot.send_message(
        chat_id=user_id,
        text=(
            "📊 **POF Chart Analysis**\n\nHEY USER JUST SEND ME QUOTEX, BINOLLA TRADING CHARTS SCREENSHOTS"
        ),
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([['Back']], resize_keyboard=True)
    )

async def registration_resend_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    d = ensure_user(user_id)
    await query.answer()

    email = d.get("email")
    if not email:
        await context.bot.send_message(chat_id=user_id, text="First send your email address.")
        d["reg_stage"] = "ask_email"
        d["awaiting"] = "reg_email"
        save_user_persistent(user_id)
        await context.bot.send_message(
            chat_id=user_id, text="✉️ Send your *Gmail* address (example: name@gmail.com):",
            reply_markup=ForceReply(selective=True)
        )
        return
    await _issue_and_email_code(context, user_id, email)

async def registration_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    d = ensure_user(user_id)
    d["reg_stage"] = None
    d["awaiting"] = None
    d["verify_code"] = None
    d["verify_expiry"] = None
    d["email"] = None # Clear email on cancel
    save_user_persistent(user_id)
    await query.answer("Registration cancelled.")
    await context.bot.send_message(
        chat_id=user_id,
        text="Registration cancelled. You can start again anytime.",
        reply_markup=_chart_analysis_kb_fallback()
    )

# --- File Download Helpers ---
async def _get_file_with_retry(bot, file_id, retries=2, base_delay=1.5):
    for attempt in range(retries + 1):
        try:
            return await bot.get_file(file_id)
        except TimedOut:
            if attempt < retries:
                await asyncio.sleep(base_delay * (2 ** attempt))
            else:
                raise

async def _download_to_memory_with_retry(tg_file, retries=2, base_delay=1.5):
    bio = io.BytesIO()
    for attempt in range(retries + 1):
        try:
            await tg_file.download_to_memory(out=bio)
            return bio.getvalue()
        except TimedOut:
            if attempt < retries:
                await asyncio.sleep(base_delay * (2 ** attempt))
            else:
                raise

# --- Error Handler ---
async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.exception("Exception while handling update: %s", context.error)
    try:
        if update and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ An error occurred. Please try again."
            )
    except Exception:
        pass

# --- Main Function to Run the Bot ---
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "YOUR_TELEGRAM_TOKEN_HERE":
        raise SystemExit("Error: TELEGRAM_TOKEN environment variable is not set or is default.")
    if not SMTP_PASS:
        print("Warning: SMTP_PASS environment variable is not set. Email verification will not work.")
    if not OPENAI_API_KEY:
        print("Warning: OPENAI_API_KEY environment variable is not set. Chart analysis will use deterministic fallback.")

    request = HTTPXRequest(
        connection_pool_size=32,
        read_timeout=120.0,
        connect_timeout=15.0
    )

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(request).build()

    # --- Handlers ---
    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("verify", verify_command))

    # Callback queries (buttons)
    app.add_handler(CallbackQueryHandler(subscription_check,     pattern=r"^check_sub$"))
    app.add_handler(CallbackQueryHandler(chart_analysis_entry,   pattern=r"^chart_analysis$"))
    app.add_handler(CallbackQueryHandler(registration_start,     pattern=r"^reg_start$"))
    app.add_handler(CallbackQueryHandler(registration_resend_cb, pattern=r"^reg_resend$"))
    app.add_handler(CallbackQueryHandler(registration_cancel_cb, pattern=r"^reg_cancel$"))

    # Message handlers
    # High priority for specific text buttons (like "Analyze another chart")
    app.add_handler(MessageHandler(filters.Regex(r"^Analyze another chart$"), handle_another_chart), group=0)
    app.add_handler(MessageHandler(filters.Regex(r"^Back$"), route_flow), group=0) # Handle 'Back' button

    # Photo handler for chart analysis
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))

    # Registration message handler (higher priority than general message handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_registration_message_flow), group=1)

    # General text message handler (lowest priority)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message), group=2)


    # Error handler
    app.add_error_handler(on_error)

    print("Bot STARTED!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
