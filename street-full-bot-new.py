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
        "INSERT INTO orders(ts,user_id,name,username,items,total,phone,lat,lon,pay,status,last_remind)"
        " VALUES(?,?,?,?,?,?,?,?,?,?, 'new', ?)",
        (int(time.time()), o["user_id"], o["name"], o["username"],
         json.dumps(o["items"], ensure_ascii=False), o["total"], o["phone"],
         o["lat"], o["lon"], o["pay"], int(time.time())))
    db.commit(); return cur.lastrowid

def get_order(oid):
    return db.execute(
        "SELECT id,ts,user_id,name,username,items,total,phone,lat,lon,pay,status,accepted_by,"
        "rating,comment,delivered_confirmed FROM orders WHERE id=?", (oid,)).fetchone()

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

def pending_orders():
    return db.execute("SELECT id,last_remind FROM orders WHERE status='new'").fetchall()

def touch_remind(oid):
    db.execute("UPDATE orders SET last_remind=? WHERE id=?", (int(time.time()),oid)); db.commit()


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
    "drink":{"name":"🥤 Salqin Ichimliklar","desc":"Coca-Cola, Fanta yoki Sprite.",
              "sizes":[("1.5 L",17000),("1 L",12000),("0.5 L",8000)]},
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

def products_kb():
    rows=[[InlineKeyboardButton(text=p["name"],callback_data=f"p:{pid}")] for pid,p in PRODUCTS.items()]
    rows.append([InlineKeyboardButton(text="🧺 Savatni ko'rish",callback_data="cart:show")])
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

def pay_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚚 Yetkazganda to'lash",callback_data="pay:cash")],
        [InlineKeyboardButton(text="💳 Online (karta)",callback_data="pay:online")]])

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
    phone = State(); location = State(); pay = State()

class AdminSt(StatesGroup):
    card_number = State(); card_holder = State(); new_admin = State()

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
    parts=["🌭 <b>STREET HOT DOG — MENYU</b>\n"]
    for p in PRODUCTS.values():
        parts.append(f"\n<b>{p['name']}</b>\n<i>{p['desc']}</i>")
        for s,pr in p["sizes"]: parts.append(f"• {s} — {money(pr)}")
    await m.answer("\n".join(parts), reply_markup=main_menu(m.from_user.id))

@router.message(F.text == "📞 Aloqa")
async def show_contact(m: Message):
    await m.answer(CONTACT, disable_web_page_preview=True, reply_markup=main_menu(m.from_user.id))

@router.message(F.text == "🚚 Yetkazib berish")
async def show_delivery(m: Message):
    await m.answer(DELIVERY, reply_markup=main_menu(m.from_user.id))

@router.message(F.text == "🛒 Buyurtma")
async def order_start(m: Message):
    await m.answer("🛒 Mahsulotni tanlang:", reply_markup=products_kb())

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
    await state.set_state(Order.phone)
    await c.message.answer("📱 Telefon raqamingizni yuboring:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Raqamni yuborish", request_contact=True)],
                      [KeyboardButton(text="◀️ Bekor qilish")]], resize_keyboard=True))
    await c.answer()

@router.message(Order.phone, F.contact)
async def phone_c(m: Message, state: FSMContext):
    await state.update_data(phone=m.contact.phone_number); await ask_location(m, state)

@router.message(Order.phone, F.text)
async def phone_t(m: Message, state: FSMContext):
    if m.text=="◀️ Bekor qilish":
        await state.clear(); await m.answer("Bekor qilindi.", reply_markup=main_menu(m.from_user.id)); return
    await state.update_data(phone=m.text); await ask_location(m, state)

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
    if m.text=="◀️ Bekor qilish":
        await state.clear(); await m.answer("Bekor qilindi.", reply_markup=main_menu(m.from_user.id)); return
    await m.answer("⚠️ Iltimos, «📍 Joylashuvni yuborish» tugmasini bosing (matn emas).")


# ── TO'LOV TANLANGANDAN SO'NG ZAKAZ QABUL QILINISHI ───
@router.callback_query(Order.pay, F.data.startswith("pay:"))
async def choose_pay(c: CallbackQuery, state: FSMContext, bot: Bot):
    await c.answer()
    try:
        pay = "Yetkazganda" if c.data == "pay:cash" else "Online (karta)"
        data = await state.get_data()
        cart = data.get("cart", [])

        if not cart:
            await c.message.answer("⚠️ Buyurtma topilmadi, iltimos qaytadan buyurtma bering.")
            await state.clear()
            return

        total = cart_total(cart)
        u = c.from_user
        uname = f"@{u.username}" if u.username else "—"

        oid = save_order({
            "user_id": u.id, "name": u.full_name, "username": uname,
            "items": cart, "total": total, "phone": data.get("phone"),
            "lat": data.get("lat"), "lon": data.get("lon"), "pay": pay
        })

        await state.clear()

        if c.data == "pay:online":
            cards = list_cards()
            ctext = "\n\n".join(f"💳 <code>{n}</code>\n👤 {h}" for _, n, h in cards) if cards else "💳 <code>xxxx xxxx xxxx xxxx</code>"
            await c.message.edit_text(
                f"✅ <b>Buyurtma #{oid} qabul qilindi!</b>\n\n"
                f"💳 <b>Online to'lov uchun karta:</b>\n\n{ctext}\n\n"
                f"💰 Summa: <b>{money(total)}</b>\n\n"
                f"To'lovni qilib, chekni operatorga yuboring: {PHONE}"
            )
        else:
            await c.message.edit_text(
                f"✅ <b>Buyurtma #{oid} qabul qilindi!</b>\n\n"
                f"💰 Summa: <b>{money(total)}</b>\n💳 Yetkazganda to'lash\n\n"
                f"Tez orada operator bog'lanadi: {PHONE}"
            )

        await c.message.answer("Bosh menyu 👇", reply_markup=main_menu(u.id))

        head = (f"🛒 <b>YANGI ZAKAZ #{oid}</b>\n\n{items_text(cart)}\n\n"
                f"💰 Jami: <b>{money(total)}</b>\n💳 To'lov: {pay}\n"
                f"📱 {data.get('phone')}\n\n"
                f"👤 {u.full_name} ({uname}) · 🆔 <code>{u.id}</code>")

        admin_ids = all_admin_ids()
        if not admin_ids:
            log.warning("Diqqat: hech qanday admin ID topilmadi — zakaz #%s haqida hech kimga xabar yuborilmadi.", oid)

        for aid in admin_ids:
            try:
                await bot.send_message(aid, head, reply_markup=new_order_kb(oid))
                if data.get("lat") and data.get("lon"):
                    await bot.send_location(aid, float(data["lat"]), float(data["lon"]))
            except Exception as e:
                log.error(f"Adminga xabar yuborishda xato ({aid}): {e}")

    except Exception:
        log.exception("choose_pay ichida kutilmagan xato")
        try:
            await c.message.answer(
                "⚠️ Buyurtmani rasmiylashtirishda texnik xatolik yuz berdi.\n"
                f"Iltimos, qaytadan urinib ko'ring yoki to'g'ridan-to'g'ri operatorga yozing: {PHONE}"
            )
        except Exception:
            pass
        await state.clear()


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
    rows=[[InlineKeyboardButton(text="📋 Zakazlar",callback_data="a:orders")],
          [InlineKeyboardButton(text="💳 Kartalar",callback_data="a:cards")]]
    if is_owner(uid):
        rows.append([InlineKeyboardButton(text="👤 Adminlar",callback_data="a:admins")])
        rows.append([InlineKeyboardButton(text="🗑 Tarixni tozalash",callback_data="a:clearhistory")])
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
        oid2, ts, uid, name, uname, items, total, phone, lat, lon, pay, status, acc, rating, comment, delivered_confirmed = row
        badge = STATUS_BADGE.get(status, "🆕")
        lines = [
            f"📋 <b>ZAKAZ #{oid2}</b> {badge}\n",
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
    oid,ts,uid,name,uname,items,total,phone,lat,lon,pay,status,acc,rating,comment,delivered_confirmed=row
    badge=STATUS_BADGE.get(status, "🆕")
    lines=[f"📋 <b>ZAKAZ #{oid}</b> {badge} ({status})\n",
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
