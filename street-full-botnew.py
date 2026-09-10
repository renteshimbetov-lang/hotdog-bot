"""
STREET HOT DOG — Telegram bot (Tuzatilgan + Yangi funksiyalar)
============================================================
Savatli buyurtma + lokatsiya + to'lov + adminga xabar va tasdiqlash
+ Bekor qilish / Yetkazildi + mijoz tasdig'i + yulduzcha baho + izoh
+ Mijoz uchun "Mening buyurtmalarim" (order history)

YANGI FUNKSIYALAR:
  - Admin zakazni "❌ Bekor qilish" qila oladi (yangi yoki qabul qilingan holatda)
  - Admin "🚚 Yetkazildi" tugmasini bosadi
  - Mijozga "Yetkazildimi?" so'raladi — ✅ Ha / ❌ Yo'q tugmalari bilan
  - "Ha" bo'lsa — 1-5 yulduzcha baho so'raladi
  - Baholagandan keyin — "💬 Izoh qoldirish" (ixtiyoriy, matn yozadi) yoki
    "➡️ O'tkazib yuborish" tugmalari chiqadi. Izoh yozish MAJBURIY EMAS.
  - "Yo'q" bo'lsa — adminlarga ogohlantirish yuboriladi.
  - 📦 "Mening buyurtmalarim" — mijoz o'zining oxirgi buyurtmalarini,
    ularning holati va bahosi bilan ko'ra oladi. Har bir buyurtma tagida
    (agar lokatsiya saqlangan bo'lsa) "📍 Manzilni ko'rish" tugmasi bor.

OLDINGI TUZATISHLAR:
  - ensure_schema() — eski baza faylida yetishmayotgan ustunlarni qo'shadi.
  - choose_pay, accept va boshqa admin handlerlari try/except bilan himoyalangan.
  - Global xato ushlagich (@router.errors) — hech qanday xato yashirinmaydi.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

# ── SOZLAMALAR ────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))
PHONE = "+998 97 027 87 70"
TG_CONTACT = "@eshmbetov"
SITE = "https://street-hotdog-uz.vercel.app"
REMIND_SEC = 120          # eslatma oralig'i (2 daqiqa)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# O'zbekiston vaqti: UTC+5
UZT_OFFSET = 5 * 3600  # 5 soat sekundlarda

def uzt_time(ts: int, fmt: str) -> str:
    """Unix timestamp ni O'zbekiston vaqtiga (UTC+5) o'tkazadi."""
    import time as _time
    return _time.strftime(fmt, _time.gmtime(ts + UZT_OFFSET))
log = logging.getLogger(__name__)
router = Router()
bot_ref = {"bot": None}

# ── BAZA (Volume) ─────────────────────────────────────
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "hotdog.db"))
db = sqlite3.connect(DB_PATH, check_same_thread=False)

db.execute("CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, name TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS cards(id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, holder TEXT)")
db.execute("""CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, user_id INTEGER, name TEXT, username TEXT,
    items TEXT, total INTEGER, phone TEXT,
    lat REAL, lon REAL, pay TEXT,
    status TEXT DEFAULT 'new',
    accepted_by INTEGER, last_remind INTEGER DEFAULT 0,
    rating INTEGER, comment TEXT, delivered_confirmed TEXT)""")
# Sozlamalar (kalit-qiymat)
db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
# Aksiyalar
db.execute("""CREATE TABLE IF NOT EXISTS promos(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT,
    title TEXT,
    description TEXT,
    data TEXT,
    active INTEGER DEFAULT 1,
    created_ts INTEGER)""")
# Kassa to'lovlari
db.execute("""CREATE TABLE IF NOT EXISTS cashier(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    ts INTEGER,
    amount INTEGER,
    pay_type TEXT,
    order_type TEXT,
    confirmed_by INTEGER)""")
db.commit()


def ensure_schema():
    """Eski (oldingi versiyadagi) baza faylida yetishmayotgan ustunlarni qo'shadi."""
    cols = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
    needed = {
        "lat": "REAL",
        "lon": "REAL",
        "pay": "TEXT",
        "status": "TEXT DEFAULT 'new'",
        "accepted_by": "INTEGER",
        "last_remind": "INTEGER DEFAULT 0",
        "rating": "INTEGER",
        "comment": "TEXT",
        "delivered_confirmed": "TEXT",
        "order_type": "TEXT DEFAULT 'delivery'",
        "receipt_file_id": "TEXT",
    }
    for col, coltype in needed.items():
        if col not in cols:
            log.warning("Bazada '%s' ustuni yo'q edi — qo'shilmoqda...", col)
            db.execute(f"ALTER TABLE orders ADD COLUMN {col} {coltype}")
    db.commit()

ensure_schema()

def is_admin(uid): 
    return uid == OWNER_ID or db.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None

def is_owner(uid): return uid == OWNER_ID

def all_admin_ids():
    ids = {OWNER_ID}
    for (u,) in db.execute("SELECT user_id FROM admins"): ids.add(u)
    return [i for i in ids if i]

def add_admin(uid, name): db.execute("INSERT OR REPLACE INTO admins VALUES(?,?)", (uid,name)); db.commit()
def del_admin(uid): db.execute("DELETE FROM admins WHERE user_id=?", (uid,)); db.commit()
def list_admins(): return db.execute("SELECT user_id,name FROM admins").fetchall()
def add_card(n,h): db.execute("INSERT INTO cards(number,holder) VALUES(?,?)",(n,h)); db.commit()
def del_card(cid): db.execute("DELETE FROM cards WHERE id=?", (cid,)); db.commit()
def list_cards(): return db.execute("SELECT id,number,holder FROM cards ORDER BY id").fetchall()

def save_order(o):
    cur = db.execute(
        "INSERT INTO orders(ts,user_id,name,username,items,total,phone,lat,lon,pay,status,last_remind,order_type,receipt_file_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?, 'new', ?,?,?)",
        (int(time.time()), o["user_id"], o["name"], o["username"],
         json.dumps(o["items"], ensure_ascii=False), o["total"], o["phone"],
         o.get("lat"), o.get("lon"), o["pay"], int(time.time()),
         o.get("order_type", "delivery"), o.get("receipt_file_id")))
    db.commit(); return cur.lastrowid

def get_order(oid):
    return db.execute(
        "SELECT id,ts,user_id,name,username,items,total,phone,lat,lon,pay,status,accepted_by,"
        "rating,comment,delivered_confirmed,order_type,receipt_file_id FROM orders WHERE id=?", (oid,)).fetchone()

def accept_order(oid, uid):
    db.execute("UPDATE orders SET status='accepted', accepted_by=? WHERE id=?", (uid,oid)); db.commit()

def set_status(oid, status):
    db.execute("UPDATE orders SET status=? WHERE id=?", (status, oid)); db.commit()

def set_delivered_confirmed(oid, val):
    db.execute("UPDATE orders SET delivered_confirmed=? WHERE id=?", (val, oid)); db.commit()

def set_rating(oid, rating):
    db.execute("UPDATE orders SET rating=? WHERE id=?", (rating, oid)); db.commit()

def set_comment(oid, comment):
    db.execute("UPDATE orders SET comment=? WHERE id=?", (comment, oid)); db.commit()

def recent_orders(limit=20):
    return db.execute(
        "SELECT id,ts,name,username,total,phone,pay,status,rating FROM orders ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()

def delete_all_orders():
    db.execute("DELETE FROM orders"); db.commit()

def delete_order(oid):
    db.execute("DELETE FROM orders WHERE id=?", (oid,)); db.commit()

def accounting_stats(ts_from: int, ts_to: int):
    """ts_from dan ts_to gacha bo'lgan davrning hisobi."""
    rows = db.execute(
        "SELECT id, ts, total, pay, order_type, status FROM orders "
        "WHERE ts >= ? AND ts < ? ORDER BY id DESC",
        (ts_from, ts_to)).fetchall()
    return rows

def pending_orders():
    return db.execute("SELECT id,last_remind FROM orders WHERE status='new'").fetchall()

def touch_remind(oid):
    db.execute("UPDATE orders SET last_remind=? WHERE id=?", (int(time.time()),oid)); db.commit()

# ── SETTINGS ─────────────────────────────────────────
def get_setting(key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def set_setting(key, value):
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value))); db.commit()

def is_open() -> bool:
    """Hozir do'kon ochiqmi? Manual override → jadval → default open."""
    override = get_setting("shop_override")  # "open" | "closed" | None
    if override == "closed": return False
    if override == "open": return True
    # Avtomatik jadval bo'yicha
    open_h = int(get_setting("open_hour", 10))
    close_h = int(get_setting("close_hour", 23))
    now_uzt = time.gmtime(int(time.time()) + UZT_OFFSET)
    h = now_uzt.tm_hour
    return open_h <= h < close_h

def is_delivery_active() -> bool:
    v = get_setting("delivery_active", "1")
    return v == "1"

# ── AKSIYALAR (PROMOS) ───────────────────────────────
def add_promo(ptype, title, desc, data_json):
    db.execute("INSERT INTO promos(type,title,description,data,active,created_ts) VALUES(?,?,?,?,1,?)",
               (ptype, title, desc, data_json, int(time.time()))); db.commit()

def list_promos(active_only=True):
    q = "SELECT id,type,title,description,data,active FROM promos"
    if active_only: q += " WHERE active=1"
    q += " ORDER BY id DESC"
    return db.execute(q).fetchall()

def toggle_promo(pid):
    db.execute("UPDATE promos SET active = 1-active WHERE id=?", (pid,)); db.commit()

def delete_promo(pid):
    db.execute("DELETE FROM promos WHERE id=?", (pid,)); db.commit()

def get_promo(pid):
    return db.execute("SELECT id,type,title,description,data,active FROM promos WHERE id=?", (pid,)).fetchone()

# ── KASSA ────────────────────────────────────────────
def cashier_confirm(order_id, amount, pay_type, order_type, confirmed_by):
    db.execute("INSERT INTO cashier(order_id,ts,amount,pay_type,order_type,confirmed_by) VALUES(?,?,?,?,?,?)",
               (order_id, int(time.time()), amount, pay_type, order_type, confirmed_by)); db.commit()

def cashier_stats(ts_from, ts_to):
    return db.execute(
        "SELECT id,order_id,ts,amount,pay_type,order_type FROM cashier WHERE ts>=? AND ts<?  ORDER BY id DESC",
        (ts_from, ts_to)).fetchall()

def order_is_paid(order_id):
    return db.execute("SELECT 1 FROM cashier WHERE order_id=?", (order_id,)).fetchone() is not None


# ── MAHSULOTLAR ───────────────────────────────────────
PRODUCTS = {
    "qazili":{"name":"🌭 Qazili Hot-Dog","desc":"Mo'l-ko'l qazi, tuxum va maxsus sous bilan.",
              "sizes":[("2x-Katta",30000),("Katta",25000),("O'rtacha",20000),("Kichkina",15000)]},
    "salatli":{"name":"🌭 Salatli Hot-Dog","desc":"Yangi sabzavotlar, pomidor sousi va krem bilan.",
              "sizes":[("2x-Katta",37000),("Katta",25000),("O'rtacha",17000),("Kichkina",14000)]},
    "hotlet":{"name":"🥪 Hot-Let","desc":"Go'sht taxtacha, pishloq va salat bargi bilan.",
              "sizes":[("Katta",30000),("Kichkina",22000)]},
    "burger":{"name":"🍔 Gamburger","desc":"Sertane, mol go'shti va eritilgan pishloq bilan.",
              "sizes":[("Gamburger",22000),("Chizburger",25000),("Dabl burger",32000)]},
    "coffee":{"name":"☕️ Coffee","desc":"Issiq va tetiklantiruvchi.",
              "sizes":[("Katta",8000),("Kichkina",5000)]},
    "cola":   {"name":"🥤 Coca-Cola",    "desc":"Klassik Coca-Cola.",
              "sizes":[("1.5 L",14000),("1 L",10000),("0.5 L",6000)]},
    "pepsi":  {"name":"🥤 Pepsi",        "desc":"Yangilovchi Pepsi.",
              "sizes":[("1.5 L",13000),("1 L",9000),("0.5 L",5000)]},
    "fanta":  {"name":"🍊 Fanta",        "desc":"Apelsin ta'mli Fanta.",
              "sizes":[("1.5 L",13000),("1 L",9000),("0.5 L",5000)]},
    "sprite": {"name":"🥤 Sprite",       "desc":"Limon-laym ta'mli Sprite.",
              "sizes":[("1.5 L",13000),("1 L",9000),("0.5 L",5000)]},
    "fuzetea":{"name":"🍵 Fuze Tea",     "desc":"Choy asosidagi ichimlik.",
              "sizes":[("0.5 L",8000)]},
    "icetea": {"name":"🧊 Ice Tea",      "desc":"Sovuq choy ichimlik.",
              "sizes":[("0.5 L",7000)]},
    "water":  {"name":"💧 Oddiy suv",    "desc":"Toza ichimlik suv.",
              "sizes":[("1.5 L",5000),("0.5 L",3000)]},
}

def money(n): return f"{n:,}".replace(",", " ") + " so'm"

CONTACT = f"""📞 <b>Bog'lanish</b>

Telefon: <a href="tel:+998900968770">{PHONE}</a>
Telegram: {TG_CONTACT}
Sayt: {SITE}

🕙 Ish vaqti: har kuni 10:00 – 23:00"""

DELIVERY = """🚚 <b>Yetkazib berish</b>

• Toshkent bo'ylab yetkazib beramiz
• 🔥 30 daqiqada yetkaziladi
• Maxsus issiq quticha bilan

🕙 Har kuni 10:00 – 23:00"""

STATUS_BADGE = {"new": "🆕", "accepted": "✅", "delivered": "🚚", "cancelled": "❌"}
STATUS_TEXT = {"new": "🆕 Yangi", "accepted": "✅ Qabul qilindi",
               "delivered": "🚚 Yetkazildi", "cancelled": "❌ Bekor qilindi"}


def main_menu(uid):
    rows = [[KeyboardButton(text="🛒 Buyurtma"), KeyboardButton(text="🧺 Savat")],
            [KeyboardButton(text="🌭 Menyu"), KeyboardButton(text="📞 Aloqa")],
            [KeyboardButton(text="🚚 Yetkazib berish"), KeyboardButton(text="📦 Mening buyurtmalarim")]]
    if is_admin(uid): rows.append([KeyboardButton(text="🛠 Admin panel")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Menyudan tanlang")

def promos_banner() -> str:
    """Faol aksiyalar bannerini qaytaradi."""
    promos = list_promos(active_only=True)
    if not promos: return ""
    lines = ["🔥 <b>AKSIYALAR:</b>"]
    for pid, ptype, title, desc, data_raw, _ in promos:
        try:
            d = json.loads(data_raw)
        except Exception:
            d = {}
        if ptype == "discount":
            old = d.get("old_price", 0)
            new = d.get("new_price", 0)
            lines.append(f"🏷 <b>{title}</b> — {money(old)} → <b>{money(new)}</b>\n  <i>{desc}</i>")
        elif ptype == "combo":
            combo_price = d.get("price", 0)
            lines.append(f"🎁 <b>{title}</b> — atigi <b>{money(combo_price)}</b>\n  <i>{desc}</i>")
        else:
            lines.append(f"⭐️ <b>{title}</b>\n  <i>{desc}</i>")
    return "\n".join(lines) + "\n\n"

def products_kb():
    rows = [[InlineKeyboardButton(text=p["name"], callback_data=f"p:{pid}")] for pid, p in PRODUCTS.items()]
    # Faol combo aksiyalarni ham ko'rsatamiz
    combos = [r for r in list_promos(active_only=True) if r[1] == "combo"]
    for cid, _, title, _, data_raw, _ in combos:
        rows.append([InlineKeyboardButton(text=f"🎁 {title}", callback_data=f"combo:{cid}")])
    rows.append([InlineKeyboardButton(text="🧺 Savatni ko'rish", callback_data="cart:show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def sizes_kb(pid):
    rows=[[InlineKeyboardButton(text=f"{s} — {money(pr)}",callback_data=f"s:{pid}:{i}")]
          for i,(s,pr) in enumerate(PRODUCTS[pid]["sizes"])]
    rows.append([InlineKeyboardButton(text="◀️ Orqaga",callback_data="back:products")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def qty_kb(pid,si,qty):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➖",callback_data=f"q:{pid}:{si}:{qty-1}"),
         InlineKeyboardButton(text=f"{qty} ta",callback_data="noop"),
         InlineKeyboardButton(text="➕",callback_data=f"q:{pid}:{si}:{qty+1}")],
        [InlineKeyboardButton(text="✅ Savatga qo'shish",callback_data=f"add:{pid}:{si}:{qty}")],
        [InlineKeyboardButton(text="◀️ Orqaga",callback_data=f"back:sizes:{pid}")]])

def cart_kb(has):
    rows=[]
    if has:
        rows.append([InlineKeyboardButton(text="✅ Buyurtma berish",callback_data="cart:order")])
        rows.append([InlineKeyboardButton(text="🗑 Tozalash",callback_data="cart:clear")])
    rows.append([InlineKeyboardButton(text="➕ Yana qo'shish",callback_data="back:products")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def order_type_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚚 Yetkazib berish", callback_data="otype:delivery")],
        [InlineKeyboardButton(text="🏃 O'zim olib ketaman (Pickup)", callback_data="otype:pickup")]])

def pay_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚚 Yetkazganda to'lash",callback_data="pay:cash")],
        [InlineKeyboardButton(text="💳 Online (karta)",callback_data="pay:online")]])

def pickup_pay_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 Naqd (oldindan operator bilan)", callback_data="ppay:cash")],
        [InlineKeyboardButton(text="💳 Online (karta orqali oldindan)", callback_data="ppay:online")]])

def new_order_kb(oid):
    """Yangi zakaz uchun admin klaviaturasi: qabul qilish yoki bekor qilish."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Zakazni qabul qilish", callback_data=f"acc:{oid}")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"cancel:{oid}")],
        [InlineKeyboardButton(text="◀️ Zakazlar ro'yxati", callback_data="a:orders")]])

def accepted_order_kb(oid):
    """Qabul qilingan zakaz uchun admin klaviaturasi: yetkazildi yoki bekor qilish."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚚 Yetkazildi", callback_data=f"deliver:{oid}")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"cancel:{oid}")],
        [InlineKeyboardButton(text="◀️ Zakazlar ro'yxati", callback_data="a:orders")]])

def confirm_delivery_kb(oid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ha, yetkazildi", callback_data=f"confirmdlv:{oid}:yes"),
         InlineKeyboardButton(text="❌ Yo'q", callback_data=f"confirmdlv:{oid}:no")]])

def rating_kb(oid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{n}⭐️", callback_data=f"rate:{oid}:{n}") for n in range(1, 6)]])

def comment_kb(oid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Izoh qoldirish (+)", callback_data=f"comment:{oid}")],
        [InlineKeyboardButton(text="➡️ O'tkazib yuborish", callback_data=f"skipcomment:{oid}")]])

def my_order_kb(oid, has_location):
    """Mijozning 'Mening buyurtmalarim' ro'yxatidagi har bir buyurtma tagidagi tugma."""
    rows = []
    if has_location:
        rows.append([InlineKeyboardButton(text="📍 Manzilni ko'rish", callback_data=f"myloc:{oid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


class Order(StatesGroup):
    order_type = State()   # delivery | pickup
    phone = State()
    location = State()     # faqat delivery uchun
    pay = State()
    pickup_cash_confirm = State()  # pickup + naqd: operator tasdig'i
    pickup_receipt = State()       # pickup + online: chek yuborish

class AdminSt(StatesGroup):
    card_number = State(); card_holder = State(); new_admin = State()
    acc_from = State(); acc_to = State()
    schedule_open = State(); schedule_close = State()

class Feedback(StatesGroup):
    comment = State()


async def get_cart(state): return (await state.get_data()).get("cart", [])
async def set_cart(state,cart): await state.update_data(cart=cart)

def cart_text(cart):
    if not cart: return "🧺 <b>Savat bo'sh.</b>\n\n«🛒 Buyurtma» orqali mahsulot qo'shing."
    lines=["🧺 <b>Savatingiz:</b>\n"]; total=0
    for it in cart:
        p=PRODUCTS[it["pid"]]; s,pr=p["sizes"][it["si"]]; sub=pr*it["qty"]; total+=sub
        lines.append(f"• {p['name']} ({s}) × {it['qty']} = {money(sub)}")
    lines.append(f"\n💰 <b>Jami: {money(total)}</b>"); return "\n".join(lines)

def cart_total(cart): return sum(PRODUCTS[i["pid"]]["sizes"][i["si"]][1]*i["qty"] for i in cart)

def items_text(items):
    out=[]
    for it in items:
        p=PRODUCTS[it["pid"]]; s,pr=p["sizes"][it["si"]]
        out.append(f"• {p['name']} ({s}) × {it['qty']} = {money(pr*it['qty'])}")
    return "\n".join(out)


# ── /start ────────────────────────────────────────────
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🌭 <b>STREET HOT DOG</b> ga xush kelibsiz!\n\n"
                   "«Yeysiz, yana bormi? deysiz» 😋\n\n"
                   "Buyurtma berish uchun «🛒 Buyurtma» ni bosing:",
                   reply_markup=main_menu(m.from_user.id))

@router.message(F.text == "🌭 Menyu")
async def show_menu(m: Message):
    banner = promos_banner()
    parts = ["🌭 <b>STREET HOT DOG — MENYU</b>\n"]
    if banner: parts.insert(0, banner)
    for p in PRODUCTS.values():
        parts.append(f"\n<b>{p['name']}</b>\n<i>{p['desc']}</i>")
        for s, pr in p["sizes"]: parts.append(f"• {s} — {money(pr)}")
    await m.answer("\n".join(parts), reply_markup=main_menu(m.from_user.id))

@router.message(F.text == "📞 Aloqa")
async def show_contact(m: Message):
    await m.answer(CONTACT, disable_web_page_preview=True, reply_markup=main_menu(m.from_user.id))

@router.message(F.text == "🚚 Yetkazib berish")
async def show_delivery(m: Message):
    if not is_delivery_active():
        await m.answer(
            "🚫 <b>Yetkazib berish hozir ishlamayapti.</b>\n\n"
            "Siz esa pickup (olib ketish) orqali buyurtma qilishingiz mumkin.\n"
            f"Qo'shimcha ma'lumot: {PHONE}",
            reply_markup=main_menu(m.from_user.id))
        return
    oh = get_setting("open_hour", 10)
    ch = get_setting("close_hour", 23)
    await m.answer(
        f"🚚 <b>Yetkazib berish</b>\n\n"
        f"• Toshkent bo'ylab yetkazib beramiz\n"
        f"• 🔥 30 daqiqada yetkaziladi\n"
        f"• Maxsus issiq quticha bilan\n\n"
        f"🕙 Har kuni {oh}:00 – {ch}:00",
        reply_markup=main_menu(m.from_user.id))

@router.message(F.text == "🛒 Buyurtma")
async def order_start(m: Message):
    if not is_open():
        oh = get_setting("open_hour", 10)
        ch = get_setting("close_hour", 23)
        override = get_setting("shop_override")
        if override == "closed":
            msg = "🔴 <b>Biz hozir yopiqmiz.</b>\n\nTez orada ochamiz! Keyinroq urinib ko'ring."
        else:
            msg = (f"🔴 <b>Biz hozir yopiqmiz.</b>\n\n"
                   f"Ish vaqtimiz: <b>{oh}:00 – {ch}:00</b>\n"
                   f"Ertaga kuting yoki telefon qiling: {PHONE}")
        await m.answer(msg, reply_markup=main_menu(m.from_user.id)); return
    banner = promos_banner()
    text = "🛒 Mahsulotni tanlang:"
    if banner:
        text = banner + text
    await m.answer(text, reply_markup=products_kb())

@router.message(F.text == "🧺 Savat")
async def cart_msg(m: Message, state: FSMContext):
    cart=await get_cart(state); await m.answer(cart_text(cart), reply_markup=cart_kb(bool(cart)))

@router.callback_query(F.data=="noop")
async def noop(c): await c.answer()

@router.callback_query(F.data=="back:products")
async def back_products(c):
    await c.message.edit_text("🛒 Mahsulotni tanlang:", reply_markup=products_kb()); await c.answer()

@router.callback_query(F.data.startswith("p:"))
async def choose_product(c):
    pid=c.data.split(":",1)[1]; p=PRODUCTS[pid]
    await c.message.edit_text(f"<b>{p['name']}</b>\n<i>{p['desc']}</i>\n\nO'lchamni tanlang:",reply_markup=sizes_kb(pid)); await c.answer()

@router.callback_query(F.data.startswith("back:sizes:"))
async def back_sizes(c):
    pid=c.data.split(":")[2]; p=PRODUCTS[pid]
    await c.message.edit_text(f"<b>{p['name']}</b>\n<i>{p['desc']}</i>\n\nO'lchamni tanlang:",reply_markup=sizes_kb(pid)); await c.answer()

@router.callback_query(F.data.startswith("s:"))
async def choose_size(c):
    _,pid,si=c.data.split(":"); si=int(si); s,pr=PRODUCTS[pid]["sizes"][si]
    await c.message.edit_text(f"<b>{PRODUCTS[pid]['name']}</b> — {s}\nNarxi: {money(pr)}\n\nSonini tanlang:",reply_markup=qty_kb(pid,si,1)); await c.answer()

@router.callback_query(F.data.startswith("q:"))
async def change_qty(c):
    _,pid,si,qty=c.data.split(":"); si,qty=int(si),max(1,min(50,int(qty))); s,pr=PRODUCTS[pid]["sizes"][si]
    await c.message.edit_text(f"<b>{PRODUCTS[pid]['name']}</b> — {s}\nNarxi: {money(pr)} × {qty} = {money(pr*qty)}\n\nSonini tanlang:",reply_markup=qty_kb(pid,si,qty)); await c.answer()

@router.callback_query(F.data.startswith("add:"))
async def add_cart(c, state: FSMContext):
    _,pid,si,qty=c.data.split(":"); si,qty=int(si),int(qty); cart=await get_cart(state)
    for it in cart:
        if it["pid"]==pid and it["si"]==si: it["qty"]+=qty; break
    else: cart.append({"pid":pid,"si":si,"qty":qty})
    await set_cart(state,cart)
    await c.message.edit_text(cart_text(cart)+"\n\n✅ Qo'shildi!", reply_markup=cart_kb(True)); await c.answer("Qo'shildi")

@router.callback_query(F.data=="cart:show")
async def cart_show(c, state: FSMContext):
    cart=await get_cart(state); await c.message.edit_text(cart_text(cart), reply_markup=cart_kb(bool(cart))); await c.answer()

@router.callback_query(F.data=="cart:clear")
async def cart_clear(c, state: FSMContext):
    await set_cart(state,[]); await c.message.edit_text("🧺 Savat tozalandi.", reply_markup=cart_kb(False)); await c.answer("Tozalandi")


# ── BUYURTMA BOSQICHLARI ──────────────────────────────
@router.callback_query(F.data=="cart:order")
async def cart_order(c: CallbackQuery, state: FSMContext):
    if not await get_cart(state):
        await c.answer("Savat bo'sh.", show_alert=True); return
    if not is_open():
        await c.answer("🔴 Biz hozir yopiqmiz. Keyinroq urinib ko'ring.", show_alert=True); return
    await state.set_state(Order.order_type)
    await c.message.answer("🛵 <b>Buyurtma turini tanlang:</b>", reply_markup=order_type_kb())
    await c.answer()

@router.callback_query(Order.order_type, F.data.startswith("otype:"))
async def choose_order_type(c: CallbackQuery, state: FSMContext):
    otype = c.data.split(":")[1]
    if otype == "delivery" and not is_delivery_active():
        await c.answer("🚫 Yetkazib berish hozir ishlamayapti. Pickup tanlang.", show_alert=True); return
    await state.update_data(order_type=otype)
    await state.set_state(Order.phone)
    await c.message.answer(
        "📱 Telefon raqamingizni yuboring:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Raqamni yuborish", request_contact=True)],
                      [KeyboardButton(text="◀️ Bekor qilish")]], resize_keyboard=True))
    await c.answer()

@router.message(Order.phone, F.contact)
async def phone_c(m: Message, state: FSMContext):
    await state.update_data(phone=m.contact.phone_number)
    await after_phone(m, state)

@router.message(Order.phone, F.text)
async def phone_t(m: Message, state: FSMContext):
    if m.text == "◀️ Bekor qilish":
        await state.clear(); await m.answer("Bekor qilindi.", reply_markup=main_menu(m.from_user.id)); return
    await state.update_data(phone=m.text)
    await after_phone(m, state)

async def after_phone(m: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("order_type") == "pickup":
        # Pickup: to'lov turini so'raymiz
        await state.set_state(Order.pay)
        await m.answer(
            "💰 <b>To'lov turini tanlang:</b>\n\n"
            "⚠️ <i>Pickup (olib ketish) uchun oldindan to'lov talab qilinadi.</i>",
            reply_markup=ReplyKeyboardRemove())
        await m.answer("Quyidagidan tanlang:", reply_markup=pickup_pay_kb())
    else:
        # Delivery: lokatsiya so'raymiz
        await ask_location(m, state)

async def ask_location(m, state):
    await state.set_state(Order.location)
    await m.answer(
        "📍 <b>Yetkazish manzili</b>\n\n"
        "Pastdagi «📍 Joylashuvni yuborish» tugmasini bosing.\n"
        "<i>Telefon xaritadan aniq joyingizni yuboradi.</i>",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Joylashuvni yuborish", request_location=True)],
                      [KeyboardButton(text="◀️ Bekor qilish")]], resize_keyboard=True))

@router.message(Order.location, F.location)
async def got_location(m: Message, state: FSMContext):
    await state.update_data(lat=m.location.latitude, lon=m.location.longitude)
    await state.set_state(Order.pay)
    await m.answer("📍 Joylashuv qabul qilindi!", reply_markup=ReplyKeyboardRemove())
    await m.answer("💰 <b>To'lov turini tanlang:</b>", reply_markup=pay_kb())

@router.message(Order.location, F.text)
async def location_text(m: Message, state: FSMContext):
    if m.text == "◀️ Bekor qilish":
        await state.clear(); await m.answer("Bekor qilindi.", reply_markup=main_menu(m.from_user.id)); return
    await m.answer("⚠️ Iltimos, «📍 Joylashuvni yuborish» tugmasini bosing (matn emas).")


# ── DELIVERY: TO'LOV ─────────────────────────────────
@router.callback_query(Order.pay, F.data.startswith("pay:"))
async def choose_pay(c: CallbackQuery, state: FSMContext, bot: Bot):
    await c.answer()
    try:
        pay = "Yetkazganda naqd" if c.data == "pay:cash" else "Online (karta)"
        data = await state.get_data()
        cart = data.get("cart", [])
        if not cart:
            await c.message.answer("⚠️ Buyurtma topilmadi, iltimos qaytadan buyurtma bering.")
            await state.clear(); return

        total = cart_total(cart)
        u = c.from_user
        uname = f"@{u.username}" if u.username else "—"

        oid = save_order({
            "user_id": u.id, "name": u.full_name, "username": uname,
            "items": cart, "total": total, "phone": data.get("phone"),
            "lat": data.get("lat"), "lon": data.get("lon"),
            "pay": pay, "order_type": "delivery"
        })
        await state.clear()

        if c.data == "pay:online":
            cards = list_cards()
            ctext = "\n\n".join(f"💳 <code>{n}</code>\n👤 {h}" for _, n, h in cards) if cards else "💳 <code>xxxx xxxx xxxx xxxx</code>"
            await c.message.edit_text(
                f"✅ <b>Buyurtma #{oid} qabul qilindi!</b>\n\n"
                f"💳 <b>Online to'lov uchun karta:</b>\n\n{ctext}\n\n"
                f"💰 Summa: <b>{money(total)}</b>\n\n"
                f"To'lovni qilib, chekni operatorga yuboring: {PHONE}")
        else:
            await c.message.edit_text(
                f"✅ <b>Buyurtma #{oid} qabul qilindi!</b>\n\n"
                f"💰 Summa: <b>{money(total)}</b>\n💳 Yetkazganda to'lash\n\n"
                f"Tez orada operator bog'lanadi: {PHONE}")

        await c.message.answer("Bosh menyu 👇", reply_markup=main_menu(u.id))
        await _notify_admins_new_order(bot, oid, cart, total, pay, "delivery", data, u, uname)

    except Exception:
        log.exception("choose_pay ichida kutilmagan xato")
        try:
            await c.message.answer(
                f"⚠️ Texnik xatolik. Iltimos qaytadan urinib ko'ring yoki operatorga yozing: {PHONE}")
        except Exception: pass
        await state.clear()


# ── PICKUP: TO'LOV TANLASH ───────────────────────────
@router.callback_query(Order.pay, F.data.startswith("ppay:"))
async def pickup_pay_choose(c: CallbackQuery, state: FSMContext):
    await c.answer()
    ptype = c.data.split(":")[1]
    await state.update_data(pickup_pay=ptype)

    if ptype == "cash":
        # Naqd: operator bilan gaplashish kerak
        await state.set_state(Order.pickup_cash_confirm)
        await c.message.edit_text(
            f"💵 <b>Naqd to'lov — Pickup</b>\n\n"
            f"Oldindan operator bilan bog'laning va narxni tasdiqlang:\n\n"
            f"📞 {PHONE}\n"
            f"✉️ {TG_CONTACT}\n\n"
            f"Gaplashib bo'lgach, quyidagi tugmani bosing 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Operator bilan gaplashdim", callback_data="pickup:cash:confirmed")],
                [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="pickup:cancel")]
            ]))
    else:
        # Online: karta ma'lumotlarini ko'rsatib, chek so'raymiz
        cards = list_cards()
        ctext = "\n\n".join(f"💳 <code>{n}</code>\n👤 {h}" for _, n, h in cards) if cards else "💳 <code>xxxx xxxx xxxx xxxx</code>"
        data = await state.get_data()
        total = cart_total(data.get("cart", []))
        await state.set_state(Order.pickup_receipt)
        await c.message.edit_text(
            f"💳 <b>Online to'lov — Pickup</b>\n\n"
            f"Quyidagi kartaga to'lov qiling:\n\n{ctext}\n\n"
            f"💰 Summa: <b>{money(total)}</b>\n\n"
            f"To'lovdan so'ng <b>chekni (rasm yoki PDF)</b> shu yerga yuboring 👇")

@router.callback_query(Order.pickup_cash_confirm, F.data=="pickup:cash:confirmed")
async def pickup_cash_confirmed(c: CallbackQuery, state: FSMContext, bot: Bot):
    await c.answer()
    try:
        data = await state.get_data()
        cart = data.get("cart", [])
        if not cart:
            await c.message.answer("⚠️ Savat bo'sh."); await state.clear(); return

        total = cart_total(cart)
        u = c.from_user
        uname = f"@{u.username}" if u.username else "—"

        oid = save_order({
            "user_id": u.id, "name": u.full_name, "username": uname,
            "items": cart, "total": total, "phone": data.get("phone"),
            "pay": "Naqd (pickup — operator tasdiqladi)", "order_type": "pickup"
        })
        await state.clear()
        await c.message.edit_text(
            f"✅ <b>Pickup zakaz #{oid} qabul qilindi!</b>\n\n"
            f"💰 Summa: <b>{money(total)}</b>\n"
            f"💵 Naqd to'lov — punkt yonida to'lanadi\n\n"
            f"Manzilimiz va tayyor bo'lish vaqti uchun operator bog'lanadi: {PHONE}")
        await c.message.answer("Bosh menyu 👇", reply_markup=main_menu(u.id))
        await _notify_admins_new_order(bot, oid, cart, total, "Naqd (pickup)", "pickup", data, u, uname)
    except Exception:
        log.exception("pickup_cash_confirmed xato")
        await c.answer("Xatolik", show_alert=True)

@router.callback_query(F.data=="pickup:cancel")
async def pickup_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("❌ Buyurtma bekor qilindi.")
    await c.message.answer("Bosh menyu 👇", reply_markup=main_menu(c.from_user.id))
    await c.answer()

@router.message(Order.pickup_receipt)
async def pickup_receipt_received(m: Message, state: FSMContext, bot: Bot):
    # Rasm yoki hujjat (PDF) qabul qilinadi
    file_id = None
    file_type = None
    if m.photo:
        file_id = m.photo[-1].file_id
        file_type = "photo"
    elif m.document:
        file_id = m.document.file_id
        file_type = "document"
    else:
        await m.answer("⚠️ Iltimos, chekni <b>rasm</b> yoki <b>PDF fayl</b> sifatida yuboring.")
        return

    try:
        data = await state.get_data()
        cart = data.get("cart", [])
        if not cart:
            await m.answer("⚠️ Savat bo'sh."); await state.clear(); return

        total = cart_total(cart)
        u = m.from_user
        uname = f"@{u.username}" if u.username else "—"

        oid = save_order({
            "user_id": u.id, "name": u.full_name, "username": uname,
            "items": cart, "total": total, "phone": data.get("phone"),
            "pay": "Online karta (pickup)", "order_type": "pickup",
            "receipt_file_id": file_id
        })
        await state.clear()

        await m.answer(
            f"✅ <b>Pickup zakaz #{oid} qabul qilindi!</b>\n\n"
            f"💰 Summa: <b>{money(total)}</b>\n"
            f"💳 Online to'lov — chek tekshirilmoqda\n\n"
            f"Tasdiqlangach operator bog'lanadi: {PHONE}",
            reply_markup=main_menu(u.id))

        # Adminlarga chek bilan birga xabar
        head = (f"🏃 <b>YANGI PICKUP ZAKAZ #{oid}</b>\n\n"
                f"{items_text(cart)}\n\n"
                f"💰 Jami: <b>{money(total)}</b>\n💳 Online karta (pickup)\n"
                f"📱 {data.get('phone')}\n\n"
                f"👤 {u.full_name} ({uname}) · 🆔 <code>{u.id}</code>\n\n"
                f"⬇️ <b>To'lov cheki:</b>")
        for aid in all_admin_ids():
            try:
                await bot.send_message(aid, head, reply_markup=new_order_kb(oid))
                if file_type == "photo":
                    await bot.send_photo(aid, file_id)
                else:
                    await bot.send_document(aid, file_id)
            except Exception as e:
                log.error(f"Adminga pickup chek yuborishda xato ({aid}): {e}")

    except Exception:
        log.exception("pickup_receipt_received xato")
        await m.answer(f"⚠️ Texnik xatolik. Operatorga murojaat qiling: {PHONE}")
        await state.clear()


async def _notify_admins_new_order(bot, oid, cart, total, pay, order_type, data, u, uname):
    """Adminlarga yangi zakaz haqida xabar yuboradi."""
    type_badge = "🚚 YETKAZIB BERISH" if order_type == "delivery" else "🏃 PICKUP (OLIB KETISH)"
    head = (f"🛒 <b>YANGI ZAKAZ #{oid}</b> — {type_badge}\n\n"
            f"{items_text(cart)}\n\n"
            f"💰 Jami: <b>{money(total)}</b>\n💳 To'lov: {pay}\n"
            f"📱 {data.get('phone')}\n\n"
            f"👤 {u.full_name} ({uname}) · 🆔 <code>{u.id}</code>")
    for aid in all_admin_ids():
        try:
            await bot.send_message(aid, head, reply_markup=new_order_kb(oid))
            if data.get("lat") and data.get("lon"):
                await bot.send_location(aid, float(data["lat"]), float(data["lon"]))
        except Exception as e:
            log.error(f"Adminga xabar yuborishda xato ({aid}): {e}")


# ── ADMIN: ZAKAZNI QABUL QILISH (TASDIQLASH) ─────────
@router.callback_query(F.data.startswith("acc:"))
async def accept(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True); return

    try:
        oid = int(c.data.split(":")[1])
        row = get_order(oid)
        if not row:
            await c.answer("Zakaz topilmadi", show_alert=True); return

        status = row[11]
        if status == "accepted":
            await c.answer("Bu zakaz allaqachon qabul qilingan.", show_alert=True)
            try: await c.message.edit_reply_markup(reply_markup=accepted_order_kb(oid))
            except Exception: pass
            return
        if status in ("cancelled", "delivered"):
            await c.answer(f"Bu zakaz holati: {status}. Qabul qilib bo'lmaydi.", show_alert=True)
            return

        accept_order(oid, c.from_user.id)

        try:
            await c.message.edit_text(
                c.message.html_text + f"\n\n✅ <b>QABUL QILINDI</b> ({c.from_user.full_name})",
                reply_markup=accepted_order_kb(oid))
        except Exception:
            pass

        try:
            await bot.send_message(row[2], f"✅ Buyurtmangiz #{oid} qabul qilindi!\n"
                                           "Tayyorlanmoqda va tez orada yetkaziladi. Rahmat! 🌭")
        except Exception as e:
            log.error(f"Mijozga tasdiq xabarini yuborishda xato (user {row[2]}): {e}")

        await c.answer("Qabul qilindi ✅")

    except Exception:
        log.exception("accept handlerida kutilmagan xato")
        await c.answer("Xatolik yuz berdi, konsolni tekshiring.", show_alert=True)


# ── ADMIN: ZAKAZNI BEKOR QILISH ───────────────────────
@router.callback_query(F.data.startswith("cancel:"))
async def cancel_order(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True); return

    try:
        oid = int(c.data.split(":")[1])
        row = get_order(oid)
        if not row:
            await c.answer("Zakaz topilmadi", show_alert=True); return

        if row[11] in ("cancelled", "delivered"):
            await c.answer(f"Bu zakaz allaqachon yakunlangan ({row[11]}).", show_alert=True)
            return

        set_status(oid, "cancelled")

        try:
            await c.message.edit_text(
                c.message.html_text + f"\n\n❌ <b>BEKOR QILINDI</b> ({c.from_user.full_name})",
                reply_markup=None)
        except Exception:
            pass

        try:
            await bot.send_message(row[2], f"❌ Buyurtmangiz #{oid} bekor qilindi.\n"
                                           f"Savol bo'lsa bog'laning: {PHONE}")
        except Exception as e:
            log.error(f"Mijozga bekor qilish xabarini yuborishda xato: {e}")

        await c.answer("Bekor qilindi")

    except Exception:
        log.exception("cancel_order handlerida kutilmagan xato")
        await c.answer("Xatolik yuz berdi", show_alert=True)


# ── ADMIN: ZAKAZNI "YETKAZILDI" DEB BELGILASH ────────
@router.callback_query(F.data.startswith("deliver:"))
async def deliver_order(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Ruxsat yo'q", show_alert=True); return

    try:
        oid = int(c.data.split(":")[1])
        row = get_order(oid)
        if not row:
            await c.answer("Zakaz topilmadi", show_alert=True); return

        if row[11] != "accepted":
            await c.answer("Avval zakaz qabul qilingan bo'lishi kerak.", show_alert=True)
            return

        set_status(oid, "delivered")

        try:
            await c.message.edit_text(
                c.message.html_text + f"\n\n🚚 <b>YETKAZILDI</b> ({c.from_user.full_name})",
                reply_markup=None)
        except Exception:
            pass

        try:
            await bot.send_message(
                row[2],
                f"🚚 Buyurtmangiz #{oid} yetkazildi deb belgilandi.\n\n"
                "Buyurtmangiz haqiqatan yetib keldimi?",
                reply_markup=confirm_delivery_kb(oid))
        except Exception as e:
            log.error(f"Mijozga yetkazish so'rovini yuborishda xato: {e}")

        await c.answer("Belgilandi 🚚")

    except Exception:
        log.exception("deliver_order handlerida kutilmagan xato")
        await c.answer("Xatolik yuz berdi", show_alert=True)


# ── MIJOZ: YETKAZILDIMI? Ha / Yo'q ────────────────────
@router.callback_query(F.data.startswith("confirmdlv:"))
async def confirm_delivery(c: CallbackQuery, bot: Bot):
    try:
        _, oid, ans = c.data.split(":")
        oid = int(oid)
        row = get_order(oid)
        if not row:
            await c.answer("Zakaz topilmadi", show_alert=True); return
        if row[2] != c.from_user.id:
            await c.answer("Bu buyurtma sizga tegishli emas.", show_alert=True); return

        if ans == "yes":
            set_delivered_confirmed(oid, "ha")
            await c.message.edit_text(
                "✅ Rahmat! Buyurtmangizni qanday baholaysiz?",
                reply_markup=rating_kb(oid))
        else:
            set_delivered_confirmed(oid, "yoq")
            await c.message.edit_text(
                "Xabaringiz uchun rahmat. Operator tez orada siz bilan bog'lanadi.",
                reply_markup=None)
            for aid in all_admin_ids():
                try:
                    await bot.send_message(
                        aid,
                        f"⚠️ Mijoz #{oid}-buyurtma <b>hali yetkazilmagan</b> deb bildirdi!\n"
                        f"📱 {row[7]}\n👤 {row[3]}")
                except Exception:
                    pass
        await c.answer()
    except Exception:
        log.exception("confirm_delivery handlerida kutilmagan xato")
        await c.answer("Xatolik yuz berdi", show_alert=True)


# ── MIJOZ: YULDUZCHA BAHO ─────────────────────────────
@router.callback_query(F.data.startswith("rate:"))
async def rate_order(c: CallbackQuery):
    try:
        _, oid, n = c.data.split(":")
        oid, n = int(oid), int(n)
        row = get_order(oid)
        if not row or row[2] != c.from_user.id:
            await c.answer("Ruxsat yo'q", show_alert=True); return

        set_rating(oid, n)
        stars = "⭐️" * n
        await c.message.edit_text(
            f"{stars} ({n}/5) — bahoyingiz uchun rahmat!\n\n"
            "Izoh qoldirmoqchimisiz? (majburiy emas)",
            reply_markup=comment_kb(oid))
        await c.answer()
    except Exception:
        log.exception("rate_order handlerida kutilmagan xato")
        await c.answer("Xatolik yuz berdi", show_alert=True)


# ── MIJOZ: IZOH (IXTIYORIY) ───────────────────────────
@router.callback_query(F.data.startswith("comment:"))
async def ask_comment(c: CallbackQuery, state: FSMContext):
    try:
        oid = int(c.data.split(":")[1])
        row = get_order(oid)
        if not row or row[2] != c.from_user.id:
            await c.answer("Ruxsat yo'q", show_alert=True); return

        await state.set_state(Feedback.comment)
        await state.update_data(feedback_oid=oid)
        await c.message.edit_text("💬 Izohingizni yozib yuboring:")
        await c.answer()
    except Exception:
        log.exception("ask_comment handlerida kutilmagan xato")
        await c.answer("Xatolik yuz berdi", show_alert=True)

@router.callback_query(F.data.startswith("skipcomment:"))
async def skip_comment(c: CallbackQuery):
    try:
        await c.message.edit_text("Baholaganingiz uchun rahmat! 🌭")
        await c.answer()
    except Exception:
        log.exception("skip_comment handlerida kutilmagan xato")
        await c.answer()

@router.message(Feedback.comment, F.text)
async def save_comment_handler(m: Message, state: FSMContext, bot: Bot):
    try:
        data = await state.get_data()
        oid = data.get("feedback_oid")
        await state.clear()
        if not oid:
            await m.answer("Bosh menyu 👇", reply_markup=main_menu(m.from_user.id))
            return

        set_comment(oid, m.text)
        await m.answer("Izohingiz uchun rahmat! 🌭", reply_markup=main_menu(m.from_user.id))

        row = get_order(oid)
        rating = row[13] if row else None
        stars = ("⭐️" * rating) if rating else "-"
        for aid in all_admin_ids():
            try:
                await bot.send_message(
                    aid,
                    f"💬 <b>Mijoz izohi</b> (#{oid}):\n\n{m.text}\n\n{stars}")
            except Exception:
                pass
    except Exception:
        log.exception("save_comment_handler ichida kutilmagan xato")
        await state.clear()
        await m.answer("⚠️ Izohni saqlashda xatolik yuz berdi.", reply_markup=main_menu(m.from_user.id))


# ── BUYRUQLAR / MIJOZ BUYURTMALARI TARIXI ─────────────
def get_orders_by_user(uid, limit=10):
    return db.execute(
        "SELECT id,ts,items,total,pay,status,rating,lat,lon FROM orders "
        "WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (uid, limit)).fetchall()

@router.message(F.text == "📦 Mening buyurtmalarim")
async def my_orders(m: Message):
    rows = get_orders_by_user(m.from_user.id)
    if not rows:
        await m.answer("📦 Sizda hali buyurtmalar yo'q.", reply_markup=main_menu(m.from_user.id))
        return

    await m.answer("📦 <b>Sizning buyurtmalaringiz:</b>", reply_markup=main_menu(m.from_user.id))

    for oid, ts, items, total, pay, status, rating, lat, lon in rows:
        t = uzt_time(ts, "%d.%m.%Y %H:%M")
        status_text = STATUS_TEXT.get(status, status)
        stars = f"\n⭐️ Baho: {'⭐️'*rating} ({rating}/5)" if rating else ""
        text = (
            f"<b>Buyurtma #{oid}</b> · {t}\n\n"
            f"{items_text(json.loads(items))}\n\n"
            f"💰 Jami: <b>{money(total)}</b>\n💳 {pay}\n"
            f"📌 Holat: {status_text}{stars}"
        )
        await m.answer(text, reply_markup=my_order_kb(oid, lat is not None and lon is not None))

@router.callback_query(F.data.startswith("myloc:"))
async def my_order_location(c: CallbackQuery, bot: Bot):
    try:
        oid = int(c.data.split(":")[1])
        row = get_order(oid)
        if not row:
            await c.answer("Zakaz topilmadi", show_alert=True); return
        if row[2] != c.from_user.id:
            await c.answer("Bu buyurtma sizga tegishli emas.", show_alert=True); return
        lat, lon = row[8], row[9]
        if lat is None or lon is None:
            await c.answer("Bu buyurtma uchun manzil saqlanmagan.", show_alert=True); return
        await bot.send_location(c.from_user.id, float(lat), float(lon))
        await c.answer()
    except Exception:
        log.exception("my_order_location handlerida kutilmagan xato")
        await c.answer("Xatolik yuz berdi", show_alert=True)


# ── ADMIN PANEL ───────────────────────────────────────
def admin_kb(uid):
    open_icon = "🟢" if is_open() else "🔴"
    dlv_icon  = "🚚" if is_delivery_active() else "🚫"
    rows = [
        [InlineKeyboardButton(text=f"{open_icon} Do'kon holati", callback_data="a:shop")],
        [InlineKeyboardButton(text="🔥 Aksiyalar",              callback_data="a:promos")],
        [InlineKeyboardButton(text="🧾 Kassiya",                callback_data="a:cashier")],
        [InlineKeyboardButton(text="📋 Zakazlar",               callback_data="a:orders")],
        [InlineKeyboardButton(text="💰 Bugalteriya",            callback_data="a:acc")],
        [InlineKeyboardButton(text="💳 Kartalar",               callback_data="a:cards")],
    ]
    if is_owner(uid):
        rows.append([InlineKeyboardButton(text="👤 Adminlar",        callback_data="a:admins")])
        rows.append([InlineKeyboardButton(text="🗑 Tarixni tozalash", callback_data="a:clearhistory")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.message(F.text=="🛠 Admin panel")
async def admin_panel(m: Message):
    if not is_admin(m.from_user.id): return
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=admin_kb(m.from_user.id))

def orders_list_kb(rows):
    """Zakazlar ro'yxati — har biri bosiladigan inline tugma."""
    kb = []
    for oid, ts, name, uname, total, phone, pay, status, rating in rows:
        badge = STATUS_BADGE.get(status, "🆕")
        t = uzt_time(ts, "%d.%m %H:%M")
        label = f"{badge} #{oid} · {t} · {money(total)}"
        kb.append([InlineKeyboardButton(text=label, callback_data=f"aview:{oid}")])
    kb.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="a:back")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

@router.callback_query(F.data=="a:orders")
async def a_orders(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q",show_alert=True); return
    rows = recent_orders()
    if not rows:
        await c.message.edit_text("📋 Hali zakaz yo'q.", reply_markup=admin_kb(c.from_user.id))
        await c.answer(); return
    await c.message.edit_text(
        "📋 <b>So'nggi zakazlar</b>\n\nKo'rish uchun zakazni tanlang 👇",
        reply_markup=orders_list_kb(rows))
    await c.answer()

@router.callback_query(F.data.startswith("aview:"))
async def aview_order(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    try:
        oid = int(c.data.split(":")[1])
        row = get_order(oid)
        if not row:
            await c.answer("Zakaz topilmadi", show_alert=True); return
        oid2, ts, uid, name, uname, items, total, phone, lat, lon, pay, status, acc, rating, comment, delivered_confirmed, order_type, receipt_file_id = row
        badge = STATUS_BADGE.get(status, "🆕")
        type_label = "🏃 Pickup (olib ketish)" if order_type == "pickup" else "🚚 Yetkazib berish"
        lines = [
            f"📋 <b>ZAKAZ #{oid2}</b> {badge} · {type_label}\n",
            items_text(json.loads(items)),
            f"\n💰 Jami: <b>{money(total)}</b>\n💳 {pay}\n📱 {phone}",
            f"👤 {name} ({uname}) · 🆔 <code>{uid}</code>",
            f"🕙 {uzt_time(ts, '%d.%m.%Y %H:%M')} (UZT)",
        ]
        if delivered_confirmed:
            confirm_text = "✅ Ha" if delivered_confirmed == "ha" else "❌ Yo'q"
            lines.append(f"\n🚚 Mijoz tasdig'i: {confirm_text}")
        if rating:
            lines.append(f"⭐️ Baho: {'⭐️'*rating} ({rating}/5)")
        if comment:
            lines.append(f"💬 Izoh: {comment}")

        # Holat bo'yicha admin tugmalari
        if status == "new":
            action_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Qabul qilish", callback_data=f"acc:{oid2}"),
                 InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"cancel:{oid2}")],
                [InlineKeyboardButton(text="◀️ Zakazlar", callback_data="a:orders")],
            ])
        elif status == "accepted":
            action_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚚 Yetkazildi", callback_data=f"deliver:{oid2}"),
                 InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"cancel:{oid2}")],
                [InlineKeyboardButton(text="◀️ Zakazlar", callback_data="a:orders")],
            ])
        else:
            action_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Zakazlar", callback_data="a:orders")],
            ])

        await c.message.edit_text("\n".join(lines), reply_markup=action_kb)
        if lat is not None and lon is not None:
            await bot.send_location(c.from_user.id, float(lat), float(lon))
        if receipt_file_id:
            await bot.send_photo(c.from_user.id, receipt_file_id,
                caption=f"🧾 Zakaz #{oid2} — to'lov cheki")
        await c.answer()
    except Exception:
        log.exception("aview_order xatosi")
        await c.answer("Xatolik yuz berdi", show_alert=True)

@router.message(Command("zakaz"))
async def zakaz_detail(m: Message, bot: Bot):
    if not is_admin(m.from_user.id): return
    p=m.text.split()
    if len(p)<2 or not p[1].isdigit(): await m.answer("Format: /zakaz 5"); return
    row=get_order(int(p[1]))
    if not row: await m.answer("Bunday zakaz yo'q."); return
    oid,ts,uid,name,uname,items,total,phone,lat,lon,pay,status,acc,rating,comment,delivered_confirmed,order_type,receipt_file_id=row
    badge=STATUS_BADGE.get(status, "🆕")
    type_label = "🏃 Pickup" if order_type == "pickup" else "🚚 Yetkazib berish"
    lines=[f"📋 <b>ZAKAZ #{oid}</b> {badge} · {type_label}\n",
           items_text(json.loads(items)),
           f"\n💰 Jami: <b>{money(total)}</b>\n💳 {pay}\n📱 {phone}",
           f"👤 {name} ({uname}) · 🆔 <code>{uid}</code>",
           f"🕙 {uzt_time(ts, '%d.%m.%Y %H:%M')} (UZT)"]
    if delivered_confirmed:
        confirm_text = "✅ Ha" if delivered_confirmed == "ha" else "❌ Yo'q"
        lines.append(f"\n🚚 Mijoz tasdig'i: {confirm_text}")
    if rating:
        lines.append(f"⭐️ Baho: {'⭐️'*rating} ({rating}/5)")
    if comment:
        lines.append(f"💬 Izoh: {comment}")
    await m.answer("\n".join(lines))
    if lat is not None:
        await bot.send_location(m.chat.id, lat, lon)

def cards_kb(uid):
    rows=[[InlineKeyboardButton(text="➕ Karta qo'shish",callback_data="card:add")]]
    for cid,num,holder in list_cards():
        rows.append([InlineKeyboardButton(text=f"🗑 {num}",callback_data=f"card:del:{cid}")])
    rows.append([InlineKeyboardButton(text="◀️ Orqaga",callback_data="a:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data=="a:cards")
async def a_cards(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q",show_alert=True); return
    cards=list_cards()
    txt="💳 <b>Online to'lov kartalari</b>\n\n"+("\n".join(f"• <code>{n}</code> — {h}" for _,n,h in cards) if cards else "Hozircha karta yo'q.")
    await c.message.edit_text(txt, reply_markup=cards_kb(c.from_user.id)); await c.answer()

@router.callback_query(F.data=="card:add")
async def card_add(c, state: FSMContext):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q",show_alert=True); return
    await state.set_state(AdminSt.card_number)
    await c.message.answer("💳 Karta raqamini yuboring (16 xona):"); await c.answer()

@router.message(AdminSt.card_number, F.text)
async def card_num(m: Message, state: FSMContext):
    num=m.text.replace(" ","")
    if not (num.isdigit() and len(num)==16): await m.answer("⚠️ 16 xonali raqam kiriting:"); return
    await state.update_data(card_number=" ".join(num[i:i+4] for i in range(0,16,4)))
    await state.set_state(AdminSt.card_holder); await m.answer("👤 Karta egasi ismini yuboring:")

@router.message(AdminSt.card_holder, F.text)
async def card_hold(m: Message, state: FSMContext):
    d=await state.get_data(); add_card(d["card_number"], m.text.strip().upper())
    await state.clear(); await m.answer("✅ Karta qo'shildi.", reply_markup=main_menu(m.from_user.id))

@router.callback_query(F.data.startswith("card:del:"))
async def card_del(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q",show_alert=True); return
    del_card(int(c.data.split(":")[2])); cards=list_cards()
    txt="💳 <b>Online to'lov kartalari</b>\n\n"+("\n".join(f"• <code>{n}</code> — {h}" for _,n,h in cards) if cards else "Hozircha karta yo'q.")
    await c.message.edit_text(txt, reply_markup=cards_kb(c.from_user.id)); await c.answer("O'chirildi")

def admins_kb():
    rows=[[InlineKeyboardButton(text="➕ Admin qo'shish",callback_data="adm:add")]]
    for uid,name in list_admins():
        rows.append([InlineKeyboardButton(text=f"🗑 {name or uid}",callback_data=f"adm:del:{uid}")])
    rows.append([InlineKeyboardButton(text="◀️ Orqaga",callback_data="a:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data=="a:admins")
async def a_admins(c: CallbackQuery):
    if not is_owner(c.from_user.id): await c.answer("Faqat bosh admin",show_alert=True); return
    admins=list_admins()
    txt="👤 <b>Adminlar</b>\n\n"+("\n".join(f"• {name or uid} (<code>{uid}</code>)" for uid,name in admins) if admins else "Boshqa admin yo'q.")
    await c.message.edit_text(txt, reply_markup=admins_kb()); await c.answer()

@router.callback_query(F.data=="adm:add")
async def adm_add(c, state: FSMContext):
    if not is_owner(c.from_user.id): await c.answer("Faqat bosh admin",show_alert=True); return
    await state.set_state(AdminSt.new_admin)
    await c.message.answer("👤 Yangi admin Telegram <b>ID</b> raqamini yuboring.\n(U @userinfobot ga yozib ID olsin)"); await c.answer()

@router.message(AdminSt.new_admin, F.text)
async def adm_save(m: Message, state: FSMContext):
    await state.clear()
    if not m.text.strip().isdigit(): await m.answer("⚠️ Faqat raqam.", reply_markup=main_menu(m.from_user.id)); return
    uid=int(m.text.strip()); add_admin(uid, f"admin{uid}")
    await m.answer(f"✅ Admin qo'shildi: <code>{uid}</code>", reply_markup=main_menu(m.from_user.id))

@router.callback_query(F.data.startswith("adm:del:"))
async def adm_del(c: CallbackQuery):
    if not is_owner(c.from_user.id): await c.answer("Faqat bosh admin",show_alert=True); return
    del_admin(int(c.data.split(":")[2])); admins=list_admins()
    txt="👤 <b>Adminlar</b>\n\n"+("\n".join(f"• {name or uid} (<code>{uid}</code>)" for uid,name in admins) if admins else "Boshqa admin yo'q.")
    await c.message.edit_text(txt, reply_markup=admins_kb()); await c.answer("O'chirildi")

@router.callback_query(F.data=="a:clearhistory")
async def a_clearhistory(c: CallbackQuery):
    if not is_owner(c.from_user.id): await c.answer("Faqat bosh admin", show_alert=True); return
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ha, hammasini o'chir", callback_data="a:clearhistory:yes")],
        [InlineKeyboardButton(text="❌ Yo'q, bekor qilish", callback_data="a:back")],
    ])
    await c.message.edit_text(
        "⚠️ <b>Diqqat!</b>\n\n"
        "Barcha zakazlar tarixi <b>butunlay o'chiriladi</b>.\n"
        "Bu amalni qaytarib bo'lmaydi!\n\n"
        "Davom etasizmi?",
        reply_markup=confirm_kb)
    await c.answer()

@router.callback_query(F.data=="a:clearhistory:yes")
async def a_clearhistory_yes(c: CallbackQuery):
    if not is_owner(c.from_user.id): await c.answer("Faqat bosh admin", show_alert=True); return
    delete_all_orders()
    log.warning("Barcha zakazlar tarixi o'chirildi. Admin: %s (%s)", c.from_user.full_name, c.from_user.id)
    await c.message.edit_text(
        "✅ <b>Barcha zakazlar tarixi o'chirildi.</b>",
        reply_markup=admin_kb(c.from_user.id))
    await c.answer("O'chirildi ✅")


# ── BUGALTERIYA ───────────────────────────────────────
def acc_period_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Bugun",       callback_data="acc:period:today")],
        [InlineKeyboardButton(text="📅 Bu hafta",    callback_data="acc:period:week")],
        [InlineKeyboardButton(text="📅 Bu oy",       callback_data="acc:period:month")],
        [InlineKeyboardButton(text="🗓 Sana oralig'i", callback_data="acc:period:custom")],
        [InlineKeyboardButton(text="◀️ Orqaga",      callback_data="a:back")],
    ])

def _day_start_uzt(days_ago=0) -> int:
    """UZT bo'yicha N kun oldingi kunning boshini Unix timestamp sifatida qaytaradi."""
    import time as _t
    now_uzt = _t.gmtime(int(_t.time()) + UZT_OFFSET)
    midnight_uzt = int(_t.mktime((now_uzt.tm_year, now_uzt.tm_mon,
                                   now_uzt.tm_mday, 0, 0, 0, 0, 0, 0))) - UZT_OFFSET
    return midnight_uzt - days_ago * 86400

def _week_start_uzt() -> int:
    import time as _t
    now_uzt = _t.gmtime(int(_t.time()) + UZT_OFFSET)
    wday = now_uzt.tm_wday  # 0=dushanba
    return _day_start_uzt(wday)

def _month_start_uzt() -> int:
    import time as _t
    now_uzt = _t.gmtime(int(_t.time()) + UZT_OFFSET)
    midnight_uzt = int(_t.mktime((now_uzt.tm_year, now_uzt.tm_mon,
                                   1, 0, 0, 0, 0, 0, 0))) - UZT_OFFSET
    return midnight_uzt

def _build_acc_report(rows: list, period_label: str) -> str:
    """Hisobot matnini tuzadi."""
    if not rows:
        return f"📊 <b>Bugalteriya — {period_label}</b>\n\nBu davrda zakaz yo'q."

    total_all = 0
    total_done = 0        # delivered
    count_all = len(rows)
    count_done = 0
    count_cancelled = 0
    cash_sum = 0
    online_sum = 0
    delivery_sum = 0
    pickup_sum = 0

    detail_lines = []
    for oid, ts, total, pay, order_type, status in rows:
        t = uzt_time(ts, "%d.%m %H:%M")
        badge = STATUS_BADGE.get(status, "🆕")
        otype = "🏃" if order_type == "pickup" else "🚚"
        detail_lines.append(f"{badge}{otype} <b>#{oid}</b> · {t} · {money(total)} · {pay}")

        total_all += total
        if status == "delivered":
            total_done += total
            count_done += 1
        if status == "cancelled":
            count_cancelled += 1
        if "naqd" in pay.lower() or "yetkazganda" in pay.lower():
            cash_sum += total
        else:
            online_sum += total
        if order_type == "pickup":
            pickup_sum += total
        else:
            delivery_sum += total

    lines = [
        f"📊 <b>Bugalteriya — {period_label}</b>\n",
        f"📦 Jami zakazlar: <b>{count_all} ta</b>",
        f"✅ Yetkazildi: <b>{count_done} ta</b> — {money(total_done)}",
        f"❌ Bekor qilindi: <b>{count_cancelled} ta</b>",
        f"\n💰 <b>Umumiy summa: {money(total_all)}</b>",
        f"  💵 Naqd: {money(cash_sum)}",
        f"  💳 Online: {money(online_sum)}",
        f"  🚚 Yetkazib berish: {money(delivery_sum)}",
        f"  🏃 Pickup: {money(pickup_sum)}",
        f"\n<i>─────────────────</i>",
        f"<b>Batafsil ro'yxat:</b>\n",
    ]
    lines += detail_lines
    return "\n".join(lines)

@router.callback_query(F.data=="a:acc")
async def a_acc(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    await c.message.edit_text(
        "💰 <b>Bugalteriya</b>\n\nDavrni tanlang:",
        reply_markup=acc_period_kb())
    await c.answer()

@router.callback_query(F.data.startswith("acc:period:"))
async def acc_period(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    period = c.data.split(":")[2]
    now = int(time.time()) + 86400  # kelajakdagi zakazlar ham kiritilsin

    if period == "today":
        ts_from = _day_start_uzt()
        label = f"Bugun ({uzt_time(ts_from, '%d.%m.%Y')})"
        rows = accounting_stats(ts_from, now)
        report = _build_acc_report(rows, label)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Orqaga", callback_data="a:acc")]
        ])
        await c.message.edit_text(report, reply_markup=kb)

    elif period == "week":
        ts_from = _week_start_uzt()
        label = f"Bu hafta ({uzt_time(ts_from, '%d.%m')} — bugun)"
        rows = accounting_stats(ts_from, now)
        report = _build_acc_report(rows, label)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Orqaga", callback_data="a:acc")]
        ])
        await c.message.edit_text(report, reply_markup=kb)

    elif period == "month":
        ts_from = _month_start_uzt()
        label = f"Bu oy ({uzt_time(ts_from, '%d.%m.%Y')} — bugun)"
        rows = accounting_stats(ts_from, now)
        report = _build_acc_report(rows, label)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Orqaga", callback_data="a:acc")]
        ])
        await c.message.edit_text(report, reply_markup=kb)

    elif period == "custom":
        await state.set_state(AdminSt.acc_from)
        await c.message.edit_text(
            "🗓 <b>Sana oralig'i</b>\n\n"
            "Boshlanish sanasini kiriting:\n"
            "<code>DD.MM.YYYY</code> formatda, masalan: <code>01.06.2025</code>")
    await c.answer()

@router.message(AdminSt.acc_from, F.text)
async def acc_from_input(m: Message, state: FSMContext):
    txt = m.text.strip()
    try:
        import time as _t
        d, mo, y = txt.split(".")
        ts_from = int(_t.mktime((int(y), int(mo), int(d), 0, 0, 0, 0, 0, 0))) - UZT_OFFSET
    except Exception:
        await m.answer("⚠️ Noto'g'ri format. Qaytadan kiriting (DD.MM.YYYY):"); return
    await state.update_data(acc_from=ts_from, acc_from_label=txt)
    await state.set_state(AdminSt.acc_to)
    await m.answer("Tugash sanasini kiriting (DD.MM.YYYY):")

@router.message(AdminSt.acc_to, F.text)
async def acc_to_input(m: Message, state: FSMContext):
    txt = m.text.strip()
    try:
        import time as _t
        d, mo, y = txt.split(".")
        ts_to = int(_t.mktime((int(y), int(mo), int(d), 23, 59, 59, 0, 0, 0))) - UZT_OFFSET
    except Exception:
        await m.answer("⚠️ Noto'g'ri format. Qaytadan kiriting (DD.MM.YYYY):"); return
    data = await state.get_data()
    ts_from = data["acc_from"]
    from_label = data["acc_from_label"]
    await state.clear()

    rows = accounting_stats(ts_from, ts_to + 1)
    label = f"{from_label} — {txt}"
    report = _build_acc_report(rows, label)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Orqaga", callback_data="a:acc")]
    ])
    await m.answer(report, reply_markup=kb)



# ── DO'KON HOLATI (ISH VAQTI) ────────────────────────
def shop_kb():
    override = get_setting("shop_override", "auto")
    oh = get_setting("open_hour", "10")
    ch = get_setting("close_hour", "23")
    dlv = is_delivery_active()
    now_icon = "🟢 Ochiq" if is_open() else "🔴 Yopiq"
    rows = [
        [InlineKeyboardButton(text=f"Hozir: {now_icon}", callback_data="noop")],
        [InlineKeyboardButton(text="🟢 Ochiq deb belgilash",  callback_data="shop:open"),
         InlineKeyboardButton(text="🔴 Yopiq deb belgilash", callback_data="shop:close")],
        [InlineKeyboardButton(text="🔄 Avtomatik (jadval bo'yicha)", callback_data="shop:auto")],
        [InlineKeyboardButton(text=f"🕙 Jadval: {oh}:00 – {ch}:00  (o'zgartirish)", callback_data="shop:schedule")],
        [InlineKeyboardButton(text=f"{'✅' if dlv else '🚫'} Yetkazib berish: {'Faol' if dlv else 'O\'chiq'}  (almashtirish)", callback_data="shop:dlv_toggle")],
        [InlineKeyboardButton(text="◀️ Orqaga", callback_data="a:back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data=="a:shop")
async def a_shop(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    override = get_setting("shop_override", "auto")
    oh = get_setting("open_hour", "10"); ch = get_setting("close_hour", "23")
    mode = {"open": "🟢 Qo'lda: Ochiq", "closed": "🔴 Qo'lda: Yopiq"}.get(override, f"🔄 Avtomatik ({oh}:00–{ch}:00)")
    await c.message.edit_text(
        f"🏪 <b>Do'kon holati</b>\n\nJoriy rejim: <b>{mode}</b>\n\nQuyidan o'zgartiring:",
        reply_markup=shop_kb())
    await c.answer()

@router.callback_query(F.data.in_({"shop:open","shop:close","shop:auto"}))
async def shop_toggle(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    val = {"shop:open":"open","shop:close":"closed","shop:auto":"auto"}[c.data]
    set_setting("shop_override", val if val != "auto" else "")
    label = {"open":"🟢 Do'kon OCHIQ deb belgilandi","closed":"🔴 Do'kon YOPIQ deb belgilandi","auto":"🔄 Avtomatik rejimga o'tildi"}[val]
    await c.answer(label, show_alert=True)
    override = get_setting("shop_override", "auto")
    oh = get_setting("open_hour", "10"); ch = get_setting("close_hour", "23")
    mode = {"open":"🟢 Qo'lda: Ochiq","closed":"🔴 Qo'lda: Yopiq"}.get(override, f"🔄 Avtomatik ({oh}:00–{ch}:00)")
    await c.message.edit_text(f"🏪 <b>Do'kon holati</b>\n\nJoriy rejim: <b>{mode}</b>", reply_markup=shop_kb())

@router.callback_query(F.data=="shop:dlv_toggle")
async def shop_dlv_toggle(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    new_val = "0" if is_delivery_active() else "1"
    set_setting("delivery_active", new_val)
    label = "✅ Yetkazib berish yoqildi" if new_val=="1" else "🚫 Yetkazib berish o'chirildi"
    await c.answer(label, show_alert=True)
    await c.message.edit_reply_markup(reply_markup=shop_kb())

@router.callback_query(F.data=="shop:schedule")
async def shop_schedule(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    await state.set_state(AdminSt.schedule_open)
    await c.message.answer("🕙 Ochilish soatini kiriting (masalan: <code>10</code>):")
    await c.answer()

@router.message(AdminSt.schedule_open, F.text)
async def schedule_open_input(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or not 0<=int(m.text.strip())<=23:
        await m.answer("⚠️ 0–23 orasida son kiriting:"); return
    await state.update_data(open_h=m.text.strip())
    await state.set_state(AdminSt.schedule_close)
    await m.answer("🕙 Yopilish soatini kiriting (masalan: <code>23</code>):")

@router.message(AdminSt.schedule_close, F.text)
async def schedule_close_input(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or not 0<=int(m.text.strip())<=23:
        await m.answer("⚠️ 0–23 orasida son kiriting:"); return
    d = await state.get_data()
    set_setting("open_hour", d["open_h"])
    set_setting("close_hour", m.text.strip())
    await state.clear()
    await m.answer(f"✅ Jadval yangilandi: <b>{d['open_h']}:00 – {m.text.strip()}:00</b>",
                   reply_markup=main_menu(m.from_user.id))


# ── AKSIYALAR ─────────────────────────────────────────
def promos_admin_kb():
    rows = [[InlineKeyboardButton(text="➕ Chegirma qo'shish", callback_data="promo:add:discount")],
            [InlineKeyboardButton(text="➕ Combo qo'shish",    callback_data="promo:add:combo")]]
    for pid, ptype, title, desc, _, active in list_promos(active_only=False):
        icon = "✅" if active else "⏸"
        picon = "🏷" if ptype=="discount" else "🎁"
        rows.append([
            InlineKeyboardButton(text=f"{icon}{picon} {title}", callback_data=f"promo:view:{pid}"),
        ])
    rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="a:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def promo_detail_kb(pid, active):
    toggle_text = "⏸ O'chirish" if active else "▶️ Yoqish"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text,    callback_data=f"promo:toggle:{pid}"),
         InlineKeyboardButton(text="🗑 O'chirish",  callback_data=f"promo:delete:{pid}")],
        [InlineKeyboardButton(text="◀️ Orqaga",    callback_data="a:promos")],
    ])

@router.callback_query(F.data=="a:promos")
async def a_promos(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    await c.message.edit_text("🔥 <b>Aksiyalar</b>\n\nMavjud aksiyalarni boshqaring yoki yangi qo'shing:",
                               reply_markup=promos_admin_kb())
    await c.answer()

@router.callback_query(F.data.startswith("promo:view:"))
async def promo_view(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    pid = int(c.data.split(":")[2])
    row = get_promo(pid)
    if not row: await c.answer("Topilmadi", show_alert=True); return
    _, ptype, title, desc, data_raw, active = row
    try: d = json.loads(data_raw)
    except: d = {}
    lines = [f"{'🏷' if ptype=='discount' else '🎁'} <b>{title}</b>",
             f"<i>{desc}</i>", ""]
    if ptype == "discount":
        lines.append(f"Eski narx: {money(d.get('old_price',0))}")
        lines.append(f"Yangi narx: <b>{money(d.get('new_price',0))}</b>")
        lines.append(f"Mahsulot: {d.get('product','')}")
    elif ptype == "combo":
        lines.append(f"Combo narxi: <b>{money(d.get('price',0))}</b>")
        lines.append(f"Tarkibi: {d.get('items','')}")
    holat_txt = '✅ Faol' if active else "⏸ To'xtatilgan"
    lines.append(f"\nHolat: {holat_txt}")
    await c.message.edit_text("\n".join(lines), reply_markup=promo_detail_kb(pid, active))
    await c.answer()

@router.callback_query(F.data.startswith("promo:toggle:"))
async def promo_toggle(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    pid = int(c.data.split(":")[2])
    toggle_promo(pid)
    row = get_promo(pid)
    if not row: await c.answer("Topilmadi"); return
    _, ptype, title, desc, data_raw, active = row
    try: d = json.loads(data_raw)
    except: d = {}
    lines = [f"{'🏷' if ptype=='discount' else '🎁'} <b>{title}</b>", f"<i>{desc}</i>", ""]
    if ptype == "discount":
        lines += [f"Eski narx: {money(d.get('old_price',0))}", f"Yangi narx: <b>{money(d.get('new_price',0))}</b>", f"Mahsulot: {d.get('product','')}"]
    elif ptype == "combo":
        lines += [f"Combo narxi: <b>{money(d.get('price',0))}</b>", f"Tarkibi: {d.get('items','')}"]
    holat_txt = '✅ Faol' if active else "⏸ To'xtatilgan"
    lines.append(f"\nHolat: {holat_txt}")
    await c.message.edit_text("\n".join(lines), reply_markup=promo_detail_kb(pid, active))
    await c.answer("✅ Yangilandi")

@router.callback_query(F.data.startswith("promo:delete:"))
async def promo_delete(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    pid = int(c.data.split(":")[2])
    delete_promo(pid)
    await c.answer("🗑 O'chirildi")
    await c.message.edit_text("🔥 <b>Aksiyalar</b>", reply_markup=promos_admin_kb())

@router.callback_query(F.data.startswith("promo:add:"))
async def promo_add_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    ptype = c.data.split(":")[2]
    await state.update_data(promo_type=ptype, combo_items=[])
    if ptype == "discount":
        # 1-qadam: mahsulot tanlash
        await c.message.edit_text(
            "🏷 <b>Chegirma — 1-qadam</b>\n\nQaysi mahsulotga chegirma?",
            reply_markup=_promo_products_kb("disc_prod"))
    else:
        # Combo: mahsulotlarni tanlash
        await c.message.edit_text(
            "🎁 <b>Combo — mahsulotlar</b>\n\nCombo tarkibiga mahsulot qo'shing:\n\n<i>Hali tanlanmadi</i>",
            reply_markup=_promo_combo_products_kb([]))
    await c.answer()

def _promo_products_kb(prefix):
    """Barcha mahsulot va o'lchamlarni tugma sifatida chiqaradi."""
    rows = []
    for pid, p in PRODUCTS.items():
        for si, (sname, price) in enumerate(p["sizes"]):
            rows.append([InlineKeyboardButton(
                text=f"{p['name']} · {sname} ({money(price)})",
                callback_data=f"{prefix}:{pid}:{si}")])
    rows.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="a:promos")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _promo_combo_products_kb(selected: list):
    """Combo uchun: mahsulot tanlash + tayyor tugmasi."""
    rows = []
    for pid, p in PRODUCTS.items():
        for si, (sname, price) in enumerate(p["sizes"]):
            rows.append([InlineKeyboardButton(
                text=f"➕ {p['name']} · {sname} ({money(price)})",
                callback_data=f"combo_add:{pid}:{si}")])
    done_label = f"✅ Tayyor ({len(selected)} ta tanlandi)" if selected else "✅ Tayyor"
    rows.append([InlineKeyboardButton(text=done_label, callback_data="combo_done")])
    rows.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="a:promos")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _discount_pct_kb(pid, si):
    """Chegirma foizlarini tugma sifatida ko'rsatadi."""
    p = PRODUCTS[pid]; sname, orig_price = p["sizes"][si]
    rows = []
    for pct in [5, 10, 15, 20, 25, 30, 40, 50]:
        new_price = int(orig_price * (100 - pct) / 100 / 500) * 500  # 500 so'mga yaxlitlash
        rows.append([InlineKeyboardButton(
            text=f"-{pct}%  →  {money(new_price)}",
            callback_data=f"disc_pct:{pid}:{si}:{pct}")])
    rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="promo:add:discount")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _combo_price_kb(items: list):
    """Combo uchun narx tavsiya tugmalari."""
    total = sum(PRODUCTS[it["pid"]]["sizes"][it["si"]][1] for it in items)
    rows = []
    for pct in [5, 10, 15, 20]:
        discounted = int(total * (100 - pct) / 100 / 500) * 500
        rows.append([InlineKeyboardButton(
            text=f"-{pct}% chegirma  →  {money(discounted)}",
            callback_data=f"combo_price:{discounted}")])
    # Yaxlit narxlar taklif
    for rnd in [500, 1000, 2000]:
        rounded = int(total / rnd) * rnd
        if rounded not in [int(total*(100-p)/100/500)*500 for p in [5,10,15,20]]:
            rows.append([InlineKeyboardButton(
                text=f"Yaxlit: {money(rounded)}",
                callback_data=f"combo_price:{rounded}")])
    rows.append([InlineKeyboardButton(text="◀️ Qayta tanlash", callback_data="promo:add:combo")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# Chegirma: mahsulot+o'lcham tanlandi
@router.callback_query(F.data.startswith("disc_prod:"))
async def disc_prod_chosen(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    _, pid, si = c.data.split(":")
    si = int(si)
    p = PRODUCTS[pid]; sname, orig_price = p["sizes"][si]
    await state.update_data(disc_pid=pid, disc_si=si)
    await c.message.edit_text(
        f"🏷 <b>Chegirma — 2-qadam</b>\n\n"
        f"Mahsulot: <b>{p['name']} · {sname}</b>\n"
        f"Asl narx: <b>{money(orig_price)}</b>\n\n"
        f"Chegirma foizini tanlang:",
        reply_markup=_discount_pct_kb(pid, si))
    await c.answer()

# Chegirma foiz tanlandi → aksiya saqlash
@router.callback_query(F.data.startswith("disc_pct:"))
async def disc_pct_chosen(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    _, pid, si, pct = c.data.split(":")
    si = int(si); pct = int(pct)
    p = PRODUCTS[pid]; sname, orig_price = p["sizes"][si]
    new_price = int(orig_price * (100 - pct) / 100 / 500) * 500
    title = f"{p['name']} · {sname} — {pct}% chegirma"
    desc = f"Asl narx: {money(orig_price)} → Aksiya narxi: {money(new_price)}"
    data_json = json.dumps({"product": f"{p['name']} ({sname})",
                            "old_price": orig_price, "new_price": new_price,
                            "pid": pid, "si": si}, ensure_ascii=False)
    add_promo("discount", title, desc, data_json)
    await state.clear()
    await c.message.edit_text(
        f"✅ <b>Aksiya qo'shildi!</b>\n\n"
        f"🏷 {title}\n"
        f"{money(orig_price)} → <b>{money(new_price)}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Aksiyalarga", callback_data="a:promos")]]))
    await c.answer()

# Combo: mahsulot qo'shish
@router.callback_query(F.data.startswith("combo_add:"))
async def combo_add_item(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    _, pid, si = c.data.split(":")
    si = int(si)
    d = await state.get_data()
    items = d.get("combo_items", [])
    items.append({"pid": pid, "si": si})
    await state.update_data(combo_items=items)
    # Ko'rinish yangilash
    selected_text = "\n".join(
        f"• {PRODUCTS[it['pid']]['name']} · {PRODUCTS[it['pid']]['sizes'][it['si']][0]} ({money(PRODUCTS[it['pid']]['sizes'][it['si']][1])})"
        for it in items)
    total = sum(PRODUCTS[it["pid"]]["sizes"][it["si"]][1] for it in items)
    await c.message.edit_text(
        f"🎁 <b>Combo — mahsulotlar</b>\n\n"
        f"<b>Tanlandi:</b>\n{selected_text}\n\n"
        f"Jami (asl): {money(total)}\n\n"
        f"Yana qo'shing yoki «✅ Tayyor» bosing:",
        reply_markup=_promo_combo_products_kb(items))
    await c.answer()

# Combo: tayyor — narx tanlash
@router.callback_query(F.data=="combo_done")
async def combo_done(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    d = await state.get_data()
    items = d.get("combo_items", [])
    if not items:
        await c.answer("⚠️ Hech narsa tanlanmadi!", show_alert=True); return
    total = sum(PRODUCTS[it["pid"]]["sizes"][it["si"]][1] for it in items)
    selected_text = "\n".join(
        f"• {PRODUCTS[it['pid']]['name']} · {PRODUCTS[it['pid']]['sizes'][it['si']][0]}"
        for it in items)
    await c.message.edit_text(
        f"🎁 <b>Combo narxi</b>\n\n"
        f"Tarkibi:\n{selected_text}\n\n"
        f"Asl jami: <b>{money(total)}</b>\n\n"
        f"Combo narxini tanlang:",
        reply_markup=_combo_price_kb(items))
    await c.answer()

# Combo: narx tanlandi → saqlash
@router.callback_query(F.data.startswith("combo_price:"))
async def combo_price_chosen(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    price = int(c.data.split(":")[1])
    d = await state.get_data()
    items = d.get("combo_items", [])
    orig_total = sum(PRODUCTS[it["pid"]]["sizes"][it["si"]][1] for it in items)
    items_label = " + ".join(
        f"{PRODUCTS[it['pid']]['name']} ({PRODUCTS[it['pid']]['sizes'][it['si']][0]})"
        for it in items)
    title = f"Combo: {items_label}"
    desc = f"Asl narx: {money(orig_total)} → Combo narxi: {money(price)}"
    data_json = json.dumps({"items": items_label, "price": price,
                            "products": items, "orig_total": orig_total}, ensure_ascii=False)
    add_promo("combo", title, desc, data_json)
    await state.clear()
    await c.message.edit_text(
        f"✅ <b>Combo qo'shildi!</b>\n\n"
        f"🎁 {items_label}\n"
        f"{money(orig_total)} → <b>{money(price)}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Aksiyalarga", callback_data="a:promos")]]))
    await state.clear()
    await c.answer()


# ── KASSIYA ───────────────────────────────────────────
def cashier_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Bugungi kassa",   callback_data="cash:today")],
        [InlineKeyboardButton(text="📅 Bu hafta",        callback_data="cash:week")],
        [InlineKeyboardButton(text="📅 Bu oy",           callback_data="cash:month")],
        [InlineKeyboardButton(text="💳 To'lovlarni tasdiqlash", callback_data="cash:confirm_list")],
        [InlineKeyboardButton(text="◀️ Orqaga",          callback_data="a:back")],
    ])

def cashier_stats(ts_from, ts_to):
    """Kassiya jadvalidagi tasdiqlangan to'lovlar."""
    return db.execute(
        "SELECT id,order_id,ts,amount,pay_type,order_type FROM cashier WHERE ts>=? AND ts<?  ORDER BY id DESC",
        (ts_from, ts_to)).fetchall()

def delivered_orders_stats(ts_from, ts_to):
    """Yetkazilgan barcha zakazlar (kassiyada tasdiqlanganmi yo'qmi — ikkalasi ham)."""
    return db.execute(
        "SELECT o.id, o.ts, o.total, o.pay, o.order_type, "
        "  CASE WHEN c.id IS NOT NULL THEN 1 ELSE 0 END as confirmed "
        "FROM orders o LEFT JOIN cashier c ON c.order_id = o.id "
        "WHERE o.status='delivered' AND o.ts>=? AND o.ts<? "
        "ORDER BY o.id DESC",
        (ts_from, ts_to)).fetchall()

def _cashier_report(rows_confirmed, rows_delivered, label):
    """
    rows_confirmed: kassiya jadvalidagi yozuvlar (tasdiqlangan)
    rows_delivered: yetkazilgan barcha zakazlar (tasdiqlanmagan ham bor bo'lishi mumkin)
    """
    if not rows_delivered:
        return f"🧾 <b>Kassiya — {label}</b>\n\nBu davrda yetkazilgan zakaz yo'q."

    total = sum(r[2] for r in rows_delivered)
    confirmed_total = sum(r[2] for r in rows_delivered if r[5] == 1)
    unconfirmed_total = total - confirmed_total

    naqd = sum(r[2] for r in rows_delivered
               if "naqd" in (r[3] or "").lower() or "yetkazganda" in (r[3] or "").lower())
    online = total - naqd
    dlv_sum = sum(r[2] for r in rows_delivered if (r[4] or "delivery") == "delivery")
    pickup_sum = total - dlv_sum

    lines = [
        f"🧾 <b>Kassiya — {label}</b>\n",
        f"✅ Tasdiqlangan: <b>{money(confirmed_total)}</b>",
        f"⏳ Tasdiqlanmagan: <b>{money(unconfirmed_total)}</b>",
        f"💰 Jami yetkazilgan: <b>{money(total)}</b>",
        f"  💵 Naqd: {money(naqd)}",
        f"  💳 Online: {money(online)}",
        f"  🚚 Yetkazib berish: {money(dlv_sum)}",
        f"  🏃 Pickup: {money(pickup_sum)}",
        f"\n📋 <b>Batafsil ({len(rows_delivered)} ta zakaz):</b>\n",
    ]
    for oid, ts, amount, pay_type, otype, confirmed in rows_delivered:
        t = uzt_time(ts, "%d.%m %H:%M")
        oi = "🏃" if otype == "pickup" else "🚚"
        tick = "✅" if confirmed else "⏳"
        lines.append(f"{tick}{oi} #{oid} · {t} · {money(amount)}")
    return "\n".join(lines)

@router.callback_query(F.data=="a:cashier")
async def a_cashier(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    await c.message.edit_text("🧾 <b>Kassiya</b>\n\nDavrni tanlang:", reply_markup=cashier_kb())
    await c.answer()

@router.callback_query(F.data.in_({"cash:today","cash:week","cash:month"}))
async def cash_period(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    key = c.data.split(":")[1]
    now = int(time.time()) + 86400
    if key=="today":
        ts_from=_day_start_uzt(); label=f"Bugun ({uzt_time(ts_from,'%d.%m.%Y')})"
    elif key=="week":
        ts_from=_week_start_uzt(); label=f"Bu hafta ({uzt_time(ts_from,'%d.%m')} — bugun)"
    else:
        ts_from=_month_start_uzt(); label=f"Bu oy ({uzt_time(ts_from,'%d.%m.%Y')} — bugun)"
    rows_confirmed = cashier_stats(ts_from, now)
    rows_delivered = delivered_orders_stats(ts_from, now)
    report = _cashier_report(rows_confirmed, rows_delivered, label)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Tasdiqlanmaganlar", callback_data="cash:confirm_list")],
        [InlineKeyboardButton(text="◀️ Orqaga", callback_data="a:cashier")]])
    await c.message.edit_text(report, reply_markup=kb)
    await c.answer()

@router.callback_query(F.data=="cash:confirm_list")
async def cash_confirm_list(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    # Yetkazilgan YOKI qabul qilingan (accepted) — kassada hali tasdiqlanmagan
    rows = db.execute(
        "SELECT id,ts,total,pay,order_type,status FROM orders "
        "WHERE status IN ('delivered','accepted') "
        "  AND id NOT IN (SELECT order_id FROM cashier) "
        "ORDER BY id DESC LIMIT 30").fetchall()
    if not rows:
        await c.answer("✅ Tasdiqlanmagan to'lov yo'q!", show_alert=True); return
    kb_rows = []
    for oid, ts, total, pay, otype, status in rows:
        t = uzt_time(ts, "%d.%m %H:%M")
        oi = "🏃" if otype == "pickup" else "🚚"
        badge = STATUS_BADGE.get(status, "")
        kb_rows.append([InlineKeyboardButton(
            text=f"{badge}{oi} #{oid} · {t} · {money(total)}",
            callback_data=f"cash:confirm:{oid}")])
    kb_rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="a:cashier")])
    await c.message.edit_text(
        "💳 <b>Tasdiqlanmagan to'lovlar</b>\n\n"
        "Zakazni bosing → kassada tasdiqlandi deb belgilanadi:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await c.answer()

@router.callback_query(F.data.startswith("cash:confirm:"))
async def cash_confirm_order(c: CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer("Ruxsat yo'q", show_alert=True); return
    oid = int(c.data.split(":")[2])
    row = get_order(oid)
    if not row: await c.answer("Topilmadi", show_alert=True); return
    if order_is_paid(oid): await c.answer("Bu zakaz allaqachon tasdiqlangan!", show_alert=True); return
    total = row[6]; pay = row[10]; order_type = row[16]
    cashier_confirm(oid, total, pay, order_type or "delivery", c.from_user.id)
    await c.answer(f"✅ #{oid} — {money(total)} tasdiqlandi!", show_alert=True)
    # Ro'yxatni yangilash
    rows = db.execute(
        "SELECT id,ts,total,pay,order_type,status FROM orders "
        "WHERE status IN ('delivered','accepted') "
        "  AND id NOT IN (SELECT order_id FROM cashier) "
        "ORDER BY id DESC LIMIT 30").fetchall()
    if not rows:
        await c.message.edit_text(
            "✅ <b>Barcha zakazlar tasdiqlangan!</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Orqaga", callback_data="a:cashier")]]))
        return
    kb_rows = []
    for oid2, ts2, total2, pay2, otype2, status2 in rows:
        t = uzt_time(ts2, "%d.%m %H:%M")
        oi = "🏃" if otype2 == "pickup" else "🚚"
        badge = STATUS_BADGE.get(status2, "")
        kb_rows.append([InlineKeyboardButton(
            text=f"{badge}{oi} #{oid2} · {t} · {money(total2)}",
            callback_data=f"cash:confirm:{oid2}")])
    kb_rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="a:cashier")])
    await c.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data=="a:back")
async def a_back(c: CallbackQuery):
    await c.message.edit_text("🛠 <b>Admin panel</b>", reply_markup=admin_kb(c.from_user.id)); await c.answer()

@router.message(F.text)
async def fallback(m: Message):
    await m.answer("Pastdagi menyudan tanlang 👇", reply_markup=main_menu(m.from_user.id))


# ── GLOBAL XATO USHLAGICH ─────────────────────────────
@router.errors()
async def global_error_handler(event: ErrorEvent):
    log.exception("Ushlanmagan xato: %s | update: %s", event.exception, event.update)
    return True


# ── ESLATMA TSRIKLI ────────────────────────────────────
async def reminder_loop():
    await asyncio.sleep(10)
    while True:
        try:
            bot = bot_ref["bot"]
            now = int(time.time())
            for oid, last in pending_orders():
                if now - (last or 0) >= REMIND_SEC:
                    row = get_order(oid)
                    if not row or row[11] != "new":
                        continue
                    total = row[6]
                    for aid in all_admin_ids():
                        try:
                            await bot.send_message(
                                aid,
                                f"⏰ <b>ESLATMA!</b>\n\nZakaz #{oid} ({money(total)}) hali "
                                "qabul qilinmadi. Iltimos, ko'rib chiqing 👇",
                                reply_markup=new_order_kb(oid))
                        except Exception: pass
                    touch_remind(oid)
        except Exception as e:
            log.warning("reminder: %s", e)
        await asyncio.sleep(30)

# ── ISHGA TUSHIRISH ───────────────────────────────────
async def set_commands(bot: Bot):
    from aiogram.types import BotCommand
    await bot.set_my_commands([BotCommand(command="start", description="Boshlash")])

async def main():
    if not TOKEN: raise SystemExit("BOT_TOKEN o'rnatilmagan.")
    if OWNER_ID == 0: log.warning("ADMIN_ID sozlanmagan — hech kim admin sifatida zakaz xabarlarini olmaydi!")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot_ref["bot"] = bot
    dp = Dispatcher(); dp.include_router(router)
    await set_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(reminder_loop())
    log.info("Baza: %s", DB_PATH); log.info("Bot ishga tushdi")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): log.info("To'xtatildi")
