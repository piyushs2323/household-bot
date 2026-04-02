#!/usr/bin/env python3
"""
Full household_bot.py — webhook-ready, includes admin commands,
approval system, persistent DB support + /exportdb backup.
"""

import os
import re
import csv
import ssl
import imaplib
import poplib
import email
import sqlite3
import shutil
from email.message import Message
from email.utils import parseaddr
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict

import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from flask import Flask, request

# ====== CONFIG ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

# ✅ PERSISTENT DB PATH
DB_FILE = os.getenv("DB_FILE", "/data/accounts.db")

CSV_BOOTSTRAP = os.getenv("CSV_BOOTSTRAP", "accounts.csv")
MAX_EMAILS_CHECK = int(os.getenv("MAX_EMAILS_CHECK", "20"))

if not BOT_TOKEN:
    raise SystemExit("Please set BOT_TOKEN in Render Environment")
if not ADMIN_ID:
    raise SystemExit("Please set ADMIN_ID in Render Environment")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ====== OTT patterns ======
OTT_SENDERS = [
    "info@account.netflix.com",
    "no-reply@account.netflix.com",
]

NETFLIX_LINK_PATTERNS = [
    re.compile(r"https://www\.netflix\.com/account/update-primary-location\?nftoken=[^\s\"'<>]+", re.I),
    re.compile(r"https://www\.netflix\.com/account/travel/verify\?nftoken=[^\s\"'<>]+", re.I),
]

# ====== DATA MODEL ======
@dataclass
class Account:
    email: str
    password: str
    protocol: str  # "imap" or "pop3"
    server: str
    port: int

# ====== DATABASE HELPERS ======
def init_db():
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            email TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            protocol TEXT NOT NULL CHECK (protocol IN ('imap','pop3')),
            server TEXT NOT NULL,
            port INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS approved_users (
            user_id INTEGER PRIMARY KEY
        )
    """)

    con.commit()
    con.close()

def upsert_account(acc: Account):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO accounts(email,password,protocol,server,port)
        VALUES(?,?,?,?,?)
        ON CONFLICT(email) DO UPDATE SET
          password=excluded.password,
          protocol=excluded.protocol,
          server=excluded.server,
          port=excluded.port
    """, (acc.email, acc.password, acc.protocol, acc.server, acc.port))
    con.commit()
    con.close()

def delete_account(email_addr: str):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM accounts WHERE email=?", (email_addr,))
    con.commit()
    con.close()

def get_account(email_addr: str) -> Optional[Account]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT email,password,protocol,server,port FROM accounts WHERE email=?", (email_addr,))
    row = cur.fetchone()
    con.close()

    if row:
        return Account(row[0], row[1], row[2], row[3], int(row[4]))
    return None

def list_accounts() -> List[Tuple[str,str,int]]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT email, server, port FROM accounts ORDER BY email")
    rows = cur.fetchall()
    con.close()
    return rows

# ====== APPROVAL HELPERS ======
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def is_approved(user_id: int) -> bool:
    if is_admin(user_id):
        return True

    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM approved_users WHERE user_id=?", (user_id,))
    ok = cur.fetchone() is not None
    con.close()
    return ok

def approve_user(user_id: int):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO approved_users(user_id) VALUES(?)", (user_id,))
    con.commit()
    con.close()

def unapprove_user(user_id: int):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DELETE FROM approved_users WHERE user_id=?", (user_id,))
    con.commit()
    con.close()

def list_approved() -> List[int]:
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("SELECT user_id FROM approved_users")
    rows = [r[0] for r in cur.fetchall()]
    con.close()
    return rows

# ====== EMAIL PARSING HELPERS ======
def normalize_link(u: str) -> str:
    u = (u or "").strip()
    if "<" in u:
        u = u.split("<",1)[0]
    return u.rstrip(').,;\'"')

def extract_links_from_text(text: str) -> List[str]:
    links: List[str] = []
    for pat in NETFLIX_LINK_PATTERNS:
        for m in pat.findall(text or ""):
            links.append(m)
    seen = set()
    clean = []
    for l in links:
        nl = normalize_link(l)
        if nl and nl not in seen:
            seen.add(nl)
            clean.append(nl)
    return clean

def message_from_bytes_safe(raw: bytes) -> Message:
    try:
        return email.message_from_bytes(raw)
    except Exception:
        return email.message_from_string(raw.decode("utf-8", "ignore"))

def get_text_from_message(msg: Message) -> str:
    try:
        if msg.is_multipart():
            parts = []
            for p in msg.walk():
                ctype = p.get_content_type()
                if ctype in ("text/plain", "text/html"):
                    try:
                        payload = p.get_payload(decode=True)
                        if payload is None:
                            continue
                        charset = p.get_content_charset() or "utf-8"
                        parts.append(payload.decode(charset, "ignore"))
                    except Exception:
                        try:
                            parts.append(p.get_payload(decode=True).decode("utf-8","ignore"))
                        except Exception:
                            pass
            return "\n".join(parts)
        else:
            payload = msg.get_payload(decode=True)
            if payload is None:
                return ""
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, "ignore")
    except Exception:
        return ""

# ====== FETCH via POP3/IMAP ======
def fetch_via_pop3(server: str, port: int, email_addr: str, password: str) -> List[List[str]]:
    out: List[List[str]] = []
    try:
        conn = poplib.POP3_SSL(server, port, timeout=30)
        conn.user(email_addr)
        conn.pass_(password)
        rsp = conn.list()
        num_messages = len(rsp[1])
        if num_messages == 0:
            conn.quit()
            return out
        start = max(1, num_messages - MAX_EMAILS_CHECK + 1)
        for i in range(start, num_messages + 1):
            try:
                lines = conn.retr(i)[1]
                raw_msg = b"\n".join(lines)
                msg = message_from_bytes_safe(raw_msg)
                frm = parseaddr(msg.get("From",""))[1].lower()
                if frm not in [s.lower() for s in OTT_SENDERS]:
                    continue
                text = get_text_from_message(msg)
                links = extract_links_from_text(text)
                if links:
                    out.append(links)
            except Exception:
                continue
        conn.quit()
    except Exception as e:
        raise e
    return out

def fetch_via_imap(server: str, port: int, email_addr: str, password: str) -> List[List[str]]:
    out: List[List[str]] = []
    try:
        m = imaplib.IMAP4_SSL(server, port)
        m.login(email_addr, password)
        m.select("INBOX", readonly=True)
        ids = set()
        for s in OTT_SENDERS:
            try:
                typ, data = m.search(None, f'(FROM "{s}")')
                if typ == "OK" and data and data[0]:
                    for i in data[0].split():
                        ids.add(i)
            except Exception:
                continue
        if not ids:
            m.logout()
            return out
        # sort and take latest N
        id_list = sorted(list(ids), key=lambda x: int(x), reverse=True)[:MAX_EMAILS_CHECK]
        for i in id_list:
            try:
                typ, msg_data = m.fetch(i, "(RFC822)")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                msg = message_from_bytes_safe(msg_data[0][1])
                frm = parseaddr(msg.get("From",""))[1].lower()
                if frm not in [s.lower() for s in OTT_SENDERS]:
                    continue
                text = get_text_from_message(msg)
                links = extract_links_from_text(text)
                if links:
                    out.append(links)
            except Exception:
                continue
        m.logout()
    except Exception as e:
        raise e
    return out

def fetch_household_info(acc: Account) -> List[List[str]]:
    if acc.protocol == "imap":
        return fetch_via_imap(acc.server, acc.port, acc.email, acc.password)
    else:
        return fetch_via_pop3(acc.server, acc.port, acc.email, acc.password)

# ====== BOT UI ======
user_state: Dict[int, str] = {}

def greet_text():
    return "Household bot ready.\nSend YES to continue or EXIT to cancel."

# ====== START ======
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    uid = message.from_user.id

    if not is_approved(uid):
        bot.reply_to(message, "❌ You are not approved.")
        return

    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(KeyboardButton("Yes"), KeyboardButton("Exit"))

    bot.reply_to(message, greet_text(), reply_markup=kb)
    user_state[uid] = "awaiting_yes"

# ====== ADMIN COMMANDS ======
def admin_only(message) -> bool:
    return message.from_user.id == ADMIN_ID

@bot.message_handler(commands=['add'])
def cmd_add(message):
    if not admin_only(message):
        return

    try:
        parts = message.text.split()
        if len(parts) != 6:
            raise ValueError
        _, email_addr, password, protocol, server, port = parts
        protocol = protocol.lower()
        if protocol not in ("imap","pop3"):
            raise ValueError("protocol must be imap or pop3")
        upsert_account(Account(email_addr, password, protocol, server, int(port)))
        bot.reply_to(message, f"✅ Saved {email_addr} ({protocol} {server}:{port})")
    except Exception:
        bot.reply_to(message, "Usage:\n/add <email> <password> <imap|pop3> <server> <port>")

@bot.message_handler(commands=['del'])
def cmd_del(message):
    if not admin_only(message):
        return
    try:
        _, email_addr = message.text.split()
        delete_account(email_addr)
        bot.reply_to(message, f"🗑️ Deleted {email_addr}")
    except Exception:
        bot.reply_to(message, "Usage:\n/del <email>")

@bot.message_handler(commands=['list'])
def cmd_list(message):
    if not admin_only(message):
        return
    rows = list_accounts()
    if not rows:
        bot.reply_to(message, "Database empty.")
    else:
        pretty = "\n".join([f"• {e} — {s}:{p}" for e,s,p in rows])
        bot.reply_to(message, "📋 Accounts:\n" + pretty)

@bot.message_handler(commands=['importcsv'])
def cmd_importcsv(message):
    if not admin_only(message):
        return
    added = 0
    if os.path.exists(CSV_BOOTSTRAP):
        try:
            with open(CSV_BOOTSTRAP, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    email_addr = (row.get("email") or "").strip()
                    password = (row.get("password") or "").strip()
                    protocol = (row.get("protocol") or "pop3").strip().lower() or "pop3"
                    server = (row.get("server") or "").strip()
                    try:
                        port = int(row.get("port") or 0)
                    except:
                        port = 0
                    if email_addr and password and server and port:
                        upsert_account(Account(email_addr, password, protocol, server, port))
                        added += 1
        except Exception:
            added = 0
    bot.reply_to(message, f"📥 Imported {added} account(s) from {CSV_BOOTSTRAP}")

@bot.message_handler(commands=['approve'])
def cmd_approve(message):
    if not admin_only(message):
        return
    try:
        _, uid_str = message.text.split()
        uid = int(uid_str)
        approve_user(uid)
        bot.reply_to(message, f"✅ Approved user {uid}")
    except Exception:
        bot.reply_to(message, "Usage: /approve <telegram_id>")

@bot.message_handler(commands=['unapprove'])
def cmd_unapprove(message):
    if not admin_only(message):
        return
    try:
        _, uid_str = message.text.split()
        uid = int(uid_str)
        unapprove_user(uid)
        bot.reply_to(message, f"🗑️ Unapproved user {uid}")
    except Exception:
        bot.reply_to(message, "Usage: /unapprove <telegram_id>")

@bot.message_handler(commands=['approved'])
def cmd_list_approved(message):
    if not admin_only(message):
        return
    rows = list_approved()
    if not rows:
        bot.reply_to(message, "No approved users yet.")
    else:
        pretty = "\n".join([f"• {uid}" for uid in rows])
        bot.reply_to(message, "✅ Approved users:\n" + pretty)

# ====== ✅ EXPORT DB (ADMIN ONLY) ======
@bot.message_handler(commands=['exportdb'])
def cmd_exportdb(message):
    if not admin_only(message):
        return

    try:
        if not os.path.exists(DB_FILE):
            bot.reply_to(message, "❌ DB not found.")
            return

        backup_path = "/tmp/accounts_backup.db"
        shutil.copy(DB_FILE, backup_path)

        with open(backup_path, "rb") as f:
            bot.send_document(message.chat.id, f, caption="✅ Database Backup")

        os.remove(backup_path)

    except Exception as e:
        bot.reply_to(message, f"⚠️ Export failed: {e}")

# ====== MESSAGE FLOW ======
@bot.message_handler(func=lambda m: True, content_types=['text'])
def text_router(message):
    uid = message.from_user.id
    txt = (message.text or "").strip()

    if not is_approved(uid):
        bot.reply_to(message, "❌ Not approved.")
        return

    # ✅ STEP 1: Waiting for YES
    if user_state.get(uid) == "awaiting_yes":
        if txt.lower() == "yes":
            bot.reply_to(message, "Enter the mail ID", reply_markup=ReplyKeyboardRemove())
            user_state[uid] = "awaiting_email"
        else:
            bot.reply_to(message, "Exited. Type /start again.", reply_markup=ReplyKeyboardRemove())
            user_state.pop(uid, None)
        return

    # ✅ STEP 2: Waiting for Email
    if user_state.get(uid) == "awaiting_email":
        email_addr = txt

        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_addr):
            bot.reply_to(message, "❌ Invalid email. Try again.")
            return

        acc = get_account(email_addr)
        if not acc:
            bot.reply_to(message, "❌ This email is not in the database.")
            user_state.pop(uid, None)
            return

        bot.send_chat_action(message.chat.id, "typing")
        try:
            results = fetch_household_info(acc)
        except Exception as e:
            bot.reply_to(message, f"⚠️ Couldn't read mailbox: {e}")
            user_state.pop(uid, None)
            return

        if not results:
            bot.reply_to(message, "❌ No household emails found recently. Try again later.")
            user_state.pop(uid, None)
            return

        reply_lines = [f"📬 Results for <b>{email_addr}</b>"]
        for links in results:
            for ln in links:
                reply_lines.append(f"🔗 <code>{ln}</code>")
            reply_lines.append("— — — — —")
        bot.reply_to(message, "\n".join(reply_lines))
        user_state.pop(uid, None)
        return

    # ✅ EXIT command
    if txt.lower() in ("exit","cancel"):
        bot.reply_to(message, "Exited.")
        user_state.pop(uid, None)
        return

    # default restart keywords
    if txt.lower() in ("yes","start"):
        kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.row(KeyboardButton("Yes"), KeyboardButton("Exit"))
        bot.reply_to(message, greet_text(), reply_markup=kb)
        user_state[uid] = "awaiting_yes"
        return

# ====== WEBHOOK ======
@app.route("/" + BOT_TOKEN, methods=['POST'])
def webhook_receive():
    json_str = request.stream.read().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/")
def webhook_set():
    # set webhook URL to your Render app domain + token
    render_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("RENDER_APP_URL") or os.getenv("RENDER_INTERNAL_URL")
    # fallback to an example domain (change if you host elsewhere)
    if not render_url:
        render_url = "https://household-bot.onrender.com"
    webhook_url = render_url.rstrip("/") + "/" + BOT_TOKEN
    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)
    return "Webhook set", 200

# ====== MAIN ======
if __name__ == "__main__":
    init_db()
    # bootstrap from CSV (if present)
    try:
        if os.path.exists(CSV_BOOTSTRAP):
            # optional: import on startup
            with open(CSV_BOOTSTRAP, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    email_addr = (row.get("email") or "").strip()
                    password = (row.get("password") or "").strip()
                    protocol = (row.get("protocol") or "pop3").strip().lower() or "pop3"
                    server = (row.get("server") or "").strip()
                    try:
                        port = int(row.get("port") or 0)
                    except:
                        port = 0
                    if email_addr and password and server and port:
                        upsert_account(Account(email_addr, password, protocol, server, port))
    except Exception:
        pass

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
