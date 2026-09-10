import logging
import asyncio
import re
import json
import sqlite3
import time as _t
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Union, List, Dict, Any

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, TelegramObject
)
from aiogram.filters import CommandStart, Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest

# ── LOGGING & CONFIG ──────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "BOT_TOKENINI_SHUYERGA_YOZING"
ADMINS = [123456789]  # Admin ID
CHANNEL_ID = -1001234567890

UZT_OFFSET = 5 * 3600

def get_now_ts() -> int:
    return int(_t.time())

# ── MA'LUMOTLAR BAZASI (SQLITE) ────────────────────────
DB_NAME = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        full_name TEXT,
        phone TEXT,
        created_at INTEGER
    )""")
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        order_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        items_json TEXT,
        total_price REAL,
        status TEXT,
        payment_method TEXT,
        created_at INTEGER
    )""")
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cashier (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER,
        amount REAL,
        paid_at INTEGER
    )""")
    
    conn.commit()
    conn.close()

init_db()

# ── MAHSULOTLAR MA'LUMOTI ──────────────────────────────
PRODUCTS = {
    "classic_hotdog": {
        "name": "Klassik Xot-dog",
        "price": 18000,
        "sizes": {"Standart": 18000, "Katta": 24000}
    },
    "cheese_hotdog": {
        "name": "Pishloqli Xot-dog",
        "price": 22000,
        "sizes": {"Standart": 22000, "Katta": 28000}
    }
}

DRINKS = {
    "coca_cola": {
        "name": "Coca-Cola",
        "price": 10000,
        "sizes": {"0.5L": 8000, "1.0L": 12000}
    }
}

# Xatolik to'g'rilandi: O'zgaruvchi e'lon qilindi
ALL_PRODUCTS = {**PRODUCTS, **DRINKS}
ALL_ALL_PRODUCTS = ALL_PRODUCTS  

# ── FSM STATES ─────────────────────────────────────────
class OrderState(StatesGroup):
    waiting_for_location = State()
    waiting_for_phone = State()
    waiting_for_payment = State()

class AdminState(StatesGroup):
    waiting_for_promo = State()

# ── YORDAMCHI FUNKSIYALAR ──────────────────────────────
def cart_text(cart: Dict[str, Any]) -> str:
    if not cart:
        return "🛒 Savatchangiz bo'sh."
    text = "🛒 **Sizning savatingiz:**\n\n"
    total = 0
    for key, item in cart.items():
        p_info = ALL_ALL_PRODUCTS.get(item['p_id'], {})
        p_name = p_info.get('name', 'Mahsulot')
        item_total = item['price'] * item['qty']
        total += item_total
        text += f"• **{p_name}** ({item['size']}) x {item['qty']} = {item_total:,.0f} so'm\n"
    text += f"\n💰 **Jami:** {total:,.0f} so'm"
    return text

def items_text(order_items: List[Dict[str, Any]]) -> str:
    text = ""
    total = 0
    for item in order_items:
        p_info = ALL_ALL_PRODUCTS.get(item['p_id'], {})
        p_name = p_info.get('name', 'Mahsulot')
        item_total = item['price'] * item['qty']
        total += item_total
        text += f"- {p_name} ({item['size']}) x {item['qty']} = {item_total:,.0f} so'm\n"
    text += f"\nJami: {total:,.0f} so'm"
    return text

def order_is_paid(order_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id FROM cashier WHERE order_id = ?", (order_id,))
    res = cur.fetchone()
    conn.close()
    return res is not None

def acc_to_input(y: int, mo: int, d: int) -> int:
    dt = datetime(int(y), int(mo), int(d), 23, 59, 59, tzinfo=timezone(timedelta(hours=5)))
    return int(dt.timestamp())
  # ── KLAVIATURALAR ──────────────────────────────────────
def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🍔 Menyu"), KeyboardButton(text="🛒 Savatcha")],
            [KeyboardButton(text="📞 Biz bilan aloqa")]
        ],
        resize_keyboard=True
    )

def phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def location_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Joylashuvni yuborish", request_location=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def payment_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💵 Naqd pul", callback_data="pay:cash")],
            [InlineKeyboardButton(text="💳 Payme / Click", callback_data="pay:card")]
        ]
    )

def cashier_kb():
    # Tuzatilgan funksiya: Endi chala qolmagan va tugmalar to'liq
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ To'lovni tasdiqlash", callback_data="c:confirm")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="c:cancel")],
            [InlineKeyboardButton(text="◀️ Orqaga", callback_data="a:back")]
        ]
    )

# ── ROUTER VA HANDLERLAR ──────────────────────────────
router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"Assalomu alaykum, {message.from_user.full_name}! Botimizga xush kelibsiz.",
        reply_markup=main_kb()
    )

@router.message(F.text == "🍔 Menyu")
async def show_menu(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for p_id, p_val in ALL_ALL_PRODUCTS.items():
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"{p_val['name']} - {p_val['price']} so'm", callback_data=f"p:{p_id}")
        ])
    await message.answer("Mahsulotni tanlang:", reply_markup=kb)

@router.callback_query(F.data.startswith("p:"))
async def choose_product(callback: CallbackQuery):
    p_id = callback.data.split(":")[1]
    p_info = ALL_ALL_PRODUCTS.get(p_id)
    if not p_info:
        await callback.answer("Mahsulot topilmadi!")
        return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for size, price in p_info["sizes"].items():
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"{size} - {price} so'm", callback_data=f"size:{p_id}:{size}")
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="back_to_menu")])
    
    await callback.message.edit_text(f"**{p_info['name']}** hajmini tanlang:", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu_handler(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for p_id, p_val in ALL_ALL_PRODUCTS.items():
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"{p_val['name']} - {p_val['price']} so'm", callback_data=f"p:{p_id}")
        ])
    await callback.message.edit_text("Mahsulotni tanlang:", reply_markup=kb)

@router.callback_query(F.data.startswith("size:"))
async def choose_size(callback: CallbackQuery, state: FSMContext):
    _, p_id, size = callback.data.split(":")
    p_info = ALL_ALL_PRODUCTS.get(p_id)
    price = p_info["sizes"][size]
    
    data = await state.get_data()
    cart = data.get("cart", {})
    
    item_key = f"{p_id}_{size}"
    if item_key in cart:
        cart[item_key]["qty"] += 1
    else:
        cart[item_key] = {"p_id": p_id, "size": size, "price": price, "qty": 1}
        
    await state.update_data(cart=cart)
    await callback.answer("Mahsulot savatchaga qo'shildi!")
    await callback.message.answer(f"✅ **{p_info['name']}** ({size}) savatchaga qo'shildi.", reply_markup=main_kb(), parse_mode="Markdown")

@router.message(F.text == "🛒 Savatcha")
async def show_cart(message: Message, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart", {})
    if not cart:
        await message.answer("🛒 Savatchangiz bo'sh.")
        return
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚖 Buyurtma berish", callback_data="checkout")],
        [InlineKeyboardButton(text="🗑 Savatni tozalash", callback_data="clear_cart")]
    ])
    await message.answer(cart_text(cart), reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "clear_cart")
async def clear_cart_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(cart={})
    await callback.answer("Savatcha tozalandi")
    await callback.message.edit_text("🛒 Savatchangiz bo'sh.")

@router.callback_query(F.data == "checkout")
async def start_checkout(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrderState.waiting_for_location)
    await callback.message.answer("Iltimos, yetkazib berish manzilingizni (geolokatsiya) yuboring:", reply_markup=location_kb())

@router.message(OrderState.waiting_for_location, F.location)
async def process_location(message: Message, state: FSMContext):
    await state.update_data(location={"lat": message.location.latitude, "lon": message.location.longitude})
    await state.set_state(OrderState.waiting_for_phone)
    await message.answer("Rahmat! Endi telefon raqamingizni yuboring:", reply_markup=phone_kb())

@router.message(OrderState.waiting_for_phone, F.contact)
async def process_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    await state.set_state(OrderState.waiting_for_payment)
    await message.answer("To'lov turini tanlang:", reply_markup=payment_kb())

@router.callback_query(OrderState.waiting_for_payment, F.data.startswith("pay:"))
async def process_payment(callback: CallbackQuery, state: FSMContext, bot: Bot):
    pay_method = callback.data.split(":")[1]
    data = await state.get_data()
    cart = data.get("cart", {})
    
    total_price = sum(item['price'] * item['qty'] for item in cart.values())
    items_json = json.dumps(list(cart.values()))
    
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO orders (user_id, items_json, total_price, status, payment_method, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (callback.from_user.id, items_json, total_price, "new", pay_method, get_now_ts())
    )
    order_id = cur.lastrowid
    conn.commit()
    conn.close()
    
    await state.clear()
    await callback.message.answer(f"✅ Buyurtmangiz qabul qilindi!\nOrder ID: #{order_id}", reply_markup=main_kb())
    
    # Kassa/Kanalga xabar yuborish
    admin_msg = f"🆕 **Yangi Buyurtma #{order_id}**\n\n"
    admin_msg += f"👤 Mijoz: {callback.from_user.full_name}\n"
    admin_msg += f"📞 Tel: {data.get('phone')}\n"
    admin_msg += f"💳 To'lov: {pay_method.upper()}\n\n"
    admin_msg += items_text(list(cart.values()))
    
    try:
        await bot.send_message(CHANNEL_ID, admin_msg, parse_mode="Markdown", reply_markup=cashier_kb())
    except Exception as e:
        logger.error(f"Kanalga yuborishda xatolik: {e}")

# ── BOTNI ISHGA TUSHRISH ──────────────────────────────
async def main():
    bot = Bot(token=TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    logger.info("Bot muvaffaqiyatli ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi!")
