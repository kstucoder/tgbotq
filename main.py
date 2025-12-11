import os
import re
import logging
import asyncio
import tempfile
from datetime import datetime
from typing import Dict, List, Optional

from image_ai import inject_ai_images_into_content
from image_convert import replace_latex_with_images, url_to_data_img_src, url_to_img_tag


from dotenv import load_dotenv
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    FSInputFile,
)
from aiogram.filters import Command
from aiogram.filters.command import CommandObject

# ---------------- CONFIG ----------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID", "0"))
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
PAYMENT_CARD = os.getenv("PAYMENT_CARD", "9860 **** **** 1234")
DB_PATH = os.getenv("DB_PATH", "files_bot.db")

RAW_FRONTEND_URL = os.getenv("FRONTEND_URL", "https://nurali-print.vercel.app")
FRONTEND_URL = RAW_FRONTEND_URL.rstrip("/")  # hamma joyda shundan foydalanamiz

# WebApp -> Bot integratsiyasi uchun ichki API token va port
INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "nurali_print_super_secret_2025")

# Railway / Render kabi xostlarda odatda PORT env bo‘ladi, bo‘lmasa API_PORT
API_PORT = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Adminning foydalanuvchiga yuborish rejimi
ADMIN_SEND_TARGET: Dict[int, int] = {}
# Adminning broadcast (barcha foydalanuvchilarga) rejimi
ADMIN_BROADCAST_MODE: set[int] = set()
# Admin panel holati
ADMIN_PANEL_MODE: set[int] = set()
# Admin user_id kiritishini kutayotgan holat (✉️ Userga xabar uchun)
ADMIN_WAITING_TARGET_USER: set[int] = set()

# ----------------------------------------

# ---------- DB init ----------
CREATE_TABLES_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    category TEXT,
    tags TEXT,
    price INTEGER DEFAULT 0,
    description TEXT,
    file_id TEXT,
    file_unique_id TEXT,
    channel_message_id INTEGER,
    backup_channel_message_id INTEGER,
    caption TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    title, caption, tags, content='files', content_rowid='id'
);

-- Trigger: yangi fayl qo'shilganda FTS ga ham qo'shish
CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
  INSERT INTO files_fts(rowid, title, caption, tags) 
  VALUES (new.id, new.title, new.caption, new.tags);
END;

-- Trigger: fayl o'chirilganda FTS dan ham o'chirish
CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
  DELETE FROM files_fts WHERE rowid = old.id;
END;

-- Trigger: fayl yangilanganda FTS ni ham yangilash
CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
  UPDATE files_fts SET title = new.title, caption = new.caption, tags = new.tags 
  WHERE rowid = new.id;
END;

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    file_row_id INTEGER NOT NULL,
    status TEXT DEFAULT 'waiting_for_screenshot',
    screenshot_file_id TEXT,
    screenshot_file_unique_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (file_row_id) REFERENCES files (id)
);

CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders(user_id, status);
CREATE INDEX IF NOT EXISTS idx_files_channel_msg ON files(channel_message_id);

-- Foydalanuvchilar ro‘yxati (broadcast uchun)
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


CAPTION_KEY_PATTERN = re.compile(
    r"^\s*(TITLE|CATEGORY|TAGS|PRICE|DESCRIPTION)\s*:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


async def init_db():
    """Ma'lumotlar bazasini yaratish va sozlash"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executescript(CREATE_TABLES_SQL)
            await db.commit()
            log.info("Database initialized successfully")
    except Exception as e:
        log.error(f"Database initialization error: {e}")
        raise


# ---------- Caption parser ----------
def parse_caption(caption: str) -> Dict:
    """Caption dan metadata ajratib olish"""
    meta = {
        "title": "",
        "category": "",
        "tags": "",
        "price": 0,
        "description": "",
        "caption": caption or "",
    }

    if not caption:
        return meta

    matches = CAPTION_KEY_PATTERN.findall(caption)
    for key, val in matches:
        k = key.strip().upper()
        v = val.strip()

        if k == "TITLE":
            meta["title"] = v
        elif k == "CATEGORY":
            meta["category"] = v
        elif k == "TAGS":
            meta["tags"] = v
        elif k == "PRICE":
            try:
                meta["price"] = int(re.sub(r"\D", "", v))
            except Exception:
                meta["price"] = 0
        elif k == "DESCRIPTION":
            meta["description"] = v

    return meta


# ---------- Users ops ----------
async def register_user(user: types.User):
    """Foydalanuvchini users jadvalida ro‘yxatdan o‘tkazish / yangilash"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name
            """,
                (
                    user.id,
                    user.username,
                    user.first_name,
                    user.last_name,
                ),
            )
            await db.commit()
    except Exception as e:
        log.error(f"Error registering user {user.id}: {e}")


# ---------- DB operations ----------
async def insert_file_record(db, meta: Dict) -> int:
    """Yangi fayl yozuvini bazaga qo'shish"""
    try:
        query = """
        INSERT INTO files (title, category, tags, price, description, file_id, 
                          file_unique_id, channel_message_id, backup_channel_message_id, caption)
        VALUES (:title, :category, :tags, :price, :description, :file_id, 
                :file_unique_id, :channel_message_id, :backup_channel_message_id, :caption)
        """
        cur = await db.execute(query, meta)
        await db.commit()
        return cur.lastrowid
    except Exception as e:
        log.error(f"Error inserting file record: {e}")
        raise


async def search_files(db, query_text: str, limit: int = 10) -> List:
    """Fayllarni qidirish (FTS5 bilan)"""
    try:
        q = query_text.strip()

        sql = """
        SELECT f.id,
               f.title,
               f.category,
               f.tags,
               f.price,
               f.description,
               f.file_id,
               f.channel_message_id
        FROM files_fts
        JOIN files f ON f.id = files_fts.rowid
        WHERE files_fts MATCH ?
        ORDER BY bm25(files_fts)
        LIMIT ?
        """

        cur = await db.execute(sql, (q, limit))
        return await cur.fetchall()

    except Exception as e:
        log.error(f"Search error (FTS): {e}")
        # fallback LIKE qidiruv
        try:
            like_sql = """
                SELECT id, title, category, tags, price, description, file_id, channel_message_id
                FROM files
                WHERE title LIKE ? OR tags LIKE ? OR description LIKE ?
                LIMIT ?
            """
            pattern = f"%{query_text}%"
            cur = await db.execute(like_sql, (pattern, pattern, pattern, limit))
            return await cur.fetchall()
        except Exception as e2:
            log.error(f"Fallback search error: {e2}")
            return []


async def get_file_by_id(db, file_row_id: int) -> Optional[tuple]:
    """ID bo'yicha faylni olish"""
    try:
        cur = await db.execute("SELECT * FROM files WHERE id = ?", (file_row_id,))
        return await cur.fetchone()
    except Exception as e:
        log.error(f"Error getting file by ID {file_row_id}: {e}")
        return None


async def create_order(db, user_id: int, username: str, file_row_id: int) -> int:
    """Yangi buyurtma yaratish (kanaldagi fayllar uchun)"""
    try:
        cur = await db.execute(
            """
            INSERT INTO orders (user_id, username, file_row_id, status)
            VALUES (?, ?, ?, ?)
        """,
            (user_id, username, file_row_id, "waiting_for_screenshot"),
        )
        await db.commit()
        return cur.lastrowid
    except Exception as e:
        log.error(f"Error creating order: {e}")
        raise


async def attach_screenshot_to_order(db, order_id: int, file_id: str, file_unique_id: str):
    """Buyurtmaga screenshot biriktirish"""
    try:
        await db.execute(
            """
            UPDATE orders 
            SET screenshot_file_id = ?, screenshot_file_unique_id = ?, 
                status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """,
            (file_id, file_unique_id, "pending_admin", order_id),
        )
        await db.commit()
    except Exception as e:
        log.error(f"Error attaching screenshot: {e}")
        raise


async def get_order(db, order_id: int) -> Optional[tuple]:
    """ID bo'yicha buyurtmani olish"""
    try:
        cur = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        return await cur.fetchone()
    except Exception as e:
        log.error(f"Error getting order {order_id}: {e}")
        return None


async def set_order_status(db, order_id: int, status: str):
    """Buyurtma statusini o'zgartirish"""
    try:
        await db.execute(
            "UPDATE orders SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, order_id),
        )
        await db.commit()
    except Exception as e:
        log.error(f"Error setting order status: {e}")
        raise


async def get_pending_order_for_user(db, user_id: int) -> Optional[tuple]:
    """Foydalanuvchining aktiv buyurtmasini topish"""
    try:
        cur = await db.execute(
            """
            SELECT id FROM orders
            WHERE user_id = ? AND status = 'waiting_for_screenshot'
            ORDER BY created_at DESC LIMIT 1
        """,
            (user_id,),
        )
        return await cur.fetchone()
    except Exception as e:
        log.error(f"Error getting pending order: {e}")
        return None


# ---------- Keyboards ----------

def main_menu_kb() -> ReplyKeyboardMarkup:
    """Asosiy menyu klaviaturasi (pastdagi tugmalar)"""
    buttons = [
        [
            KeyboardButton(text="🔍 Qidirish"),
            KeyboardButton(text="📋 Mening buyurtmalarim"),
        ],
        [
            KeyboardButton(text="❓ Yordam"),
            KeyboardButton(text="📞 Admin bilan bog'lanish"),
        ],
    ]
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        input_field_placeholder="Qidiruv uchun fayl nomini yozing...",
    )


def cancel_kb() -> ReplyKeyboardMarkup:
    """Bekor qilish klaviaturasi"""
    buttons = [[KeyboardButton(text="❌ Bekor qilish")]]
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def admin_panel_kb() -> ReplyKeyboardMarkup:
    """Admin panel klaviaturasi"""
    buttons = [
        [
            KeyboardButton(text="📊 Statistika"),
            KeyboardButton(text="📢 Broadcast yuborish"),
        ],
        [
            KeyboardButton(text="✉️ Userga xabar"),
            KeyboardButton(text="🛠 Admin veb-panel"),
        ],
        [
            KeyboardButton(text="🔙 Admin paneldan chiqish"),
        ],
    ]
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        input_field_placeholder="Admin panel...",
    )


def files_list_kb(rows: List, prefix: str = "BUY") -> InlineKeyboardMarkup:
    """Fayllar ro'yxati klaviaturasi"""
    buttons = []
    for r in rows:
        rowid = r[0]
        title = r[1] or "Nomsiz fayl"
        price = r[4] or 0
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"📄 {title} — {price:,} so'm",
                    callback_data=f"{prefix}:{rowid}",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_order_kb(order_id: int) -> InlineKeyboardMarkup:
    """Admin uchun buyurtmani tasdiqlash klaviaturasi (kanaldagi fayllar uchun)"""
    buttons = [
        [
            InlineKeyboardButton(
                text="✅ Tasdiqlash", callback_data=f"ADMIN_APPROVE:{order_id}"
            ),
            InlineKeyboardButton(
                text="❌ Rad etish", callback_data=f"ADMIN_REJECT:{order_id}"
            ),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------- Handlers ----------


@dp.message(Command(commands=["start"]))
async def cmd_start(message: types.Message):
    """Start buyrug'ini qayta ishlash"""
    await register_user(message.from_user)
    if message.from_user.id in ADMIN_PANEL_MODE:
        ADMIN_PANEL_MODE.discard(message.from_user.id)

    welcome_text = (
        "👋 <b>Assalomu alaykum!</b>\n\n"
        "📚 Men fayllarni qidirish va sotib olish botiman.\n\n"
        "🔍 <b>Qanday foydalanish:</b>\n"
        "• <b>🔍 Qidirish</b> tugmasini bosing yoki fayl nomini yozing\n"
        "• Ro'yxatdan kerakli faylni tanlang\n"
        "• To'lovni amalga oshiring va screenshot yuboring\n\n"
        "💡 Masalan: <code>python dasturlash</code> yoki <code>matematika</code>\n\n"
        "📞 Savol bo'lsa <b>📞 Admin bilan bog'lanish</b> tugmasini bosing."
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=main_menu_kb())


@dp.message(Command(commands=["help"]))
async def cmd_help(message: types.Message):
    """Yordam buyrug'i"""
    await register_user(message.from_user)

    help_text = (
        "📖 <b>Yordam</b>\n\n"
        "🔍 <b>Qidirish:</b>\n"
        "Kerakli faylingiz nomini yozing yoki 🔍 Qidirish tugmasini bosing.\n\n"
        "💰 <b>Sotib olish:</b>\n"
        "1️⃣ Faylni tanlang\n"
        "2️⃣ Karta raqamiga to'lov qiling\n"
        "3️⃣ Screenshot yuboring\n"
        "4️⃣ Admin tasdiqlagandan keyin fayl sizga yuboriladi\n\n"
        "📝 <b>Tugmalar:</b>\n"
        "🔍 Qidirish - Fayllarni qidirish\n"
        "📋 Mening buyurtmalarim - Buyurtmalar tarixi\n"
        "❓ Yordam - Bu yordam\n"
        "📞 Admin - Admin bilan bog'lanish"
    )
    await message.answer(help_text, parse_mode="HTML", reply_markup=main_menu_kb())


@dp.message(Command(commands=["adm"]))
async def cmd_admin_stats(message: types.Message):
    """Admin statistikasi - faqat admin uchun"""
    if message.from_user.id != ADMIN_CHAT_ID and message.chat.id != ADMIN_CHAT_ID:
        await message.answer("❌ Bu buyruq faqat admin uchun!")
        return

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT COUNT(*) FROM files")
            total_files = (await cur.fetchone())[0]

            cur = await db.execute(
                """
                SELECT category, COUNT(*) as cnt 
                FROM files 
                WHERE category != '' 
                GROUP BY category 
                ORDER BY cnt DESC 
                LIMIT 5
            """
            )
            top_categories = await cur.fetchall()

            cur = await db.execute("SELECT COUNT(*) FROM orders")
            total_orders = (await cur.fetchone())[0]

            cur = await db.execute(
                """
                SELECT status, COUNT(*) as cnt 
                FROM orders 
                GROUP BY status
            """
            )
            orders_by_status = await cur.fetchall()

            cur = await db.execute(
                """
                SELECT COUNT(*), SUM(f.price)
                FROM orders o
                JOIN files f ON o.file_row_id = f.id
                WHERE o.status = 'approved'
            """
            )
            approved_stats = await cur.fetchone()
            approved_count = approved_stats[0] or 0
            total_revenue = approved_stats[1] or 0

            cur = await db.execute(
                """
                SELECT COUNT(*)
                FROM orders
                WHERE DATE(created_at) = DATE('now')
            """
            )
            today_orders = (await cur.fetchone())[0]

            cur = await db.execute(
                """
                SELECT COUNT(*), SUM(f.price)
                FROM orders o
                JOIN files f ON o.file_row_id = f.id
                WHERE o.status = 'approved' 
                AND DATE(o.created_at) = DATE('now')
            """
            )
            today_stats = await cur.fetchone()
            today_approved = today_stats[0] or 0
            today_revenue = today_stats[1] or 0

            cur = await db.execute(
                """
                SELECT f.title, f.price, COUNT(*) as sales
                FROM orders o
                JOIN files f ON o.file_row_id = f.id
                WHERE o.status = 'approved'
                GROUP BY f.id
                ORDER BY sales DESC
                LIMIT 5
            """
            )
            top_files = await cur.fetchall()

            cur = await db.execute(
                """
                SELECT COUNT(DISTINCT user_id)
                FROM orders
                WHERE status = 'approved'
            """
            )
            unique_customers = (await cur.fetchone())[0]
    except Exception as e:
        log.error(f"Admin stats error: {e}")
        await message.answer("❌ Statistikani olishda xatolik yuz berdi!")
        return

    stats_text = "📊 <b>ADMIN STATISTIKASI</b>\n" + "=" * 30 + "\n\n"

    stats_text += "📁 <b>FAYLLAR</b>\n"
    stats_text += f"├ Jami: <b>{total_files}</b> ta\n"
    if top_categories:
        stats_text += "├ Top kategoriyalar:\n"
        for cat, cnt in top_categories:
            stats_text += f"│  • {cat}: {cnt} ta\n"
    stats_text += "\n"

    stats_text += "🛒 <b>BUYURTMALAR</b>\n"
    stats_text += f"├ Jami: <b>{total_orders}</b> ta\n"
    stats_text += f"├ Bugun: <b>{today_orders}</b> ta\n"
    stats_text += "├ Status bo'yicha:\n"

    status_names = {
        "waiting_for_screenshot": "⏳ Screenshot kutilmoqda",
        "pending_admin": "🕐 Admin tekshiryapti",
        "approved": "✅ Tasdiqlangan",
        "rejected": "❌ Rad etilgan",
        "cancelled": "🚫 Bekor qilingan",
    }

    for status, cnt in orders_by_status:
        status_name = status_names.get(status, status)
        stats_text += f"│  • {status_name}: {cnt} ta\n"
    stats_text += "\n"

    stats_text += "💰 <b>MOLIYAVIY</b>\n"
    stats_text += f"├ Jami foyda: <b>{total_revenue:,}</b> so'm\n"
    stats_text += f"├ Bugungi foyda: <b>{today_revenue:,}</b> so'm\n"
    stats_text += f"├ Tasdiqlangan: <b>{approved_count}</b> ta\n"
    stats_text += f"├ Bugun tasdiqlangan: <b>{today_approved}</b> ta\n"
    avg_check = int(total_revenue / approved_count) if approved_count > 0 else 0
    stats_text += f"├ O'rtacha check: <b>{avg_check:,}</b> so'm\n\n"

    stats_text += "👥 <b>MIJOZLAR</b>\n"
    stats_text += f"├ Unique mijozlar: <b>{unique_customers}</b> ta\n"
    if unique_customers and approved_count:
        stats_text += (
            f"├ O'rtacha buyurtma/mijoz: <b>{approved_count / unique_customers:.1f}</b> ta\n"
        )
    stats_text += "\n"

    if top_files:
        stats_text += "🏆 <b>TOP 5 FAYLLAR</b>\n"
        for idx, (title, price, sales) in enumerate(top_files, 1):
            show_title = title if len(title) <= 30 else title[:30] + "..."
            stats_text += f"{idx}. {show_title}\n"
            stats_text += (
                f"   💵 {price:,} so'm × {sales} = {price * sales:,} so'm\n"
            )
        stats_text += "\n"

    stats_text += "=" * 30 + "\n"
    stats_text += f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    reply_kb = admin_panel_kb() if message.from_user.id in ADMIN_PANEL_MODE else main_menu_kb()
    await message.answer(stats_text, parse_mode="HTML", reply_markup=reply_kb)


@dp.message(Command(commands=["adm777"]))
async def cmd_admin_panel(message: types.Message):
    """Admin panelni ochish"""
    if message.from_user.id != ADMIN_CHAT_ID and message.chat.id != ADMIN_CHAT_ID:
        await message.answer("❌ Bu buyruq faqat admin uchun!")
        return

    ADMIN_PANEL_MODE.add(message.from_user.id)
    ADMIN_WAITING_TARGET_USER.discard(message.from_user.id)
    ADMIN_SEND_TARGET.pop(message.from_user.id, None)
    ADMIN_BROADCAST_MODE.discard(message.from_user.id)

    await message.answer(
        "🛠 <b>Admin paneliga xush kelibsiz!</b>\n\n"
        "Quyidagi tugmalar orqali admin funksiyalaridan foydalanishingiz mumkin.",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(),
    )


@dp.message(Command(commands=["myorders"]))
async def cmd_my_orders(message: types.Message):
    """Foydalanuvchi buyurtmalari"""
    await register_user(message.from_user)
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cur = await db.execute(
                """
                SELECT o.id, o.status, o.created_at, f.title, f.price
                FROM orders o
                JOIN files f ON o.file_row_id = f.id
                WHERE o.user_id = ?
                ORDER BY o.created_at DESC
                LIMIT 10
            """,
                (user_id,),
            )
            orders = await cur.fetchall()
        except Exception as e:
            log.error(f"Error fetching user orders: {e}")
            await message.answer(
                "❌ Xatolik yuz berdi. Keyinroq urinib ko'ring.",
                reply_markup=main_menu_kb(),
            )
            return

    if not orders:
        await message.answer(
            "📭 Sizda hali buyurtmalar yo'q.\n\n🔍 Qidirish tugmasini bosib, fayllarni ko'ring!",
            reply_markup=main_menu_kb(),
        )
        return

    status_emoji = {
        "waiting_for_screenshot": "⏳",
        "pending_admin": "🕐",
        "approved": "✅",
        "rejected": "❌",
    }

    status_text = {
        "waiting_for_screenshot": "Screenshot kutilmoqda",
        "pending_admin": "Admin tekshiryapti",
        "approved": "Tasdiqlangan",
        "rejected": "Rad etilgan",
    }

    text = "📋 <b>Sizning buyurtmalaringiz:</b>\n\n"
    for order in orders:
        order_id, status, created, title, price = order
        emoji = status_emoji.get(status, "❓")
        status_name = status_text.get(status, status)
        text += (
            f"{emoji} <b>Buyurtma #{order_id}</b>\n"
            f"📄 {title}\n"
            f"💵 {price:,} so'm\n"
            f"📊 Status: {status_name}\n"
            f"📅 {created[:16]}\n\n"
        )

    await message.answer(text, parse_mode="HTML", reply_markup=main_menu_kb())


# ---------- Reply Keyboard Handlers ----------


@dp.message(F.text == "🔍 Qidirish")
async def btn_search(message: types.Message):
    """Qidirish tugmasi"""
    await register_user(message.from_user)

    await message.answer(
        "🔍 <b>Qidirish</b>\n\n"
        "Qidirayotgan faylingiz nomini yozing.\n\n"
        "💡 Masalan:\n"
        "• <code>python dasturlash</code>\n"
        "• <code>matematika 9-sinf</code>\n"
        "• <code>ingliz tili</code>",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )


@dp.message(F.text == "📋 Mening buyurtmalarim")
async def btn_my_orders(message: types.Message):
    """Buyurtmalar tugmasi"""
    await cmd_my_orders(message)


@dp.message(F.text == "❓ Yordam")
async def btn_help(message: types.Message):
    """Yordam tugmasi"""
    await cmd_help(message)


@dp.message(F.text == "📞 Admin bilan bog'lanish")
async def btn_contact_admin(message: types.Message):
    """Admin bilan bog'lanish tugmasi"""
    await register_user(message.from_user)

    await message.answer(
        "📞 <b>Admin bilan bog'lanish</b>\n\n"
        "Savol yoki muammo bo'lsa, quyidagi ma'lumotlarni yuboring:\n\n"
        "• Muammo tavsifi\n"
        "• Buyurtma raqami (agar mavjud bo'lsa)\n"
        "• Screenshot (agar kerak bo'lsa)\n\n"
        "Admin tez orada javob beradi! ⏰",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


@dp.message(F.text == "❌ Bekor qilish")
async def btn_cancel(message: types.Message):
    """Bekor qilish tugmasi"""
    await register_user(message.from_user)
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        row = await get_pending_order_for_user(db, user_id)
        if row:
            order_id = row[0]
            await set_order_status(db, order_id, "cancelled")
            await message.answer(
                f"❌ Buyurtma #{order_id} bekor qilindi.\n\nAsosiy menyuga qaytdingiz.",
                reply_markup=main_menu_kb(),
            )
        else:
            await message.answer(
                "↩️ Asosiy menyuga qaytdingiz.", reply_markup=main_menu_kb()
            )


# ---------- Admin panel tugmalari ----------

@dp.message(F.from_user.id == ADMIN_CHAT_ID, F.text == "🔙 Admin paneldan chiqish")
async def admin_exit_panel(message: types.Message):
    """Admin paneldan chiqish, oddiy menyuga qaytish"""
    ADMIN_PANEL_MODE.discard(message.from_user.id)
    ADMIN_BROADCAST_MODE.discard(message.from_user.id)
    ADMIN_SEND_TARGET.pop(message.from_user.id, None)
    ADMIN_WAITING_TARGET_USER.discard(message.from_user.id)

    await message.answer(
        "↩️ Admin paneldan chiqdingiz. Oddiy foydalanuvchi menyusiga qaytdingiz.",
        reply_markup=main_menu_kb(),
    )


@dp.message(F.from_user.id == ADMIN_CHAT_ID, F.text == "📊 Statistika")
async def admin_panel_stats_btn(message: types.Message):
    """Admin paneldagi Statistika tugmasi"""
    await cmd_admin_stats(message)


@dp.message(F.from_user.id == ADMIN_CHAT_ID, F.text == "📢 Broadcast yuborish")
async def admin_panel_broadcast_btn(message: types.Message):
    """Admin paneldagi Broadcast tugmasi"""
    ADMIN_BROADCAST_MODE.add(message.from_user.id)
    await message.answer(
        "📢 <b>Broadcast rejimi yoqildi.</b>\n\n"
        "Endi <b>bitta xabar</b> yuboring (matn, rasm, rasm+caption, hujjat va hokazo) — "
        "u barcha ro‘yxatdagi foydalanuvchilarga yuboriladi.\n\n"
        "Bekor qilish uchun: <code>/cancel_broadcast</code> yoki admin paneldan chiqishingiz mumkin.",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(),
    )


@dp.message(F.from_user.id == ADMIN_CHAT_ID, F.text == "✉️ Userga xabar")
async def admin_panel_send_btn(message: types.Message):
    """Admin paneldagi 'Userga xabar' tugmasi"""
    ADMIN_WAITING_TARGET_USER.add(message.from_user.id)
    ADMIN_SEND_TARGET.pop(message.from_user.id, None)
    await message.answer(
        "✉️ Qaysi foydalanuvchiga xabar yubormoqchisiz?\n\n"
        "Iltimos, <b>User ID</b> ni raqam ko‘rinishida yuboring.\n"
        "Masalan: <code>123456789</code>\n\n"
        "Bekor qilish uchun: <code>/cancel_send</code> yoki '🔙 Admin paneldan chiqish' tugmasini bosing.",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(),
    )


@dp.message(F.from_user.id == ADMIN_CHAT_ID, F.text.regexp(r"^\d+$"))
async def admin_enter_user_id(message: types.Message):
    """Admin panel: User ID kiritish bosqichi"""
    admin_id = message.from_user.id
    if admin_id not in ADMIN_WAITING_TARGET_USER:
        return  # hozir user_id kutilmayapti

    target_user_id = int(message.text.strip())
    ADMIN_WAITING_TARGET_USER.discard(admin_id)
    ADMIN_SEND_TARGET[admin_id] = target_user_id

    await message.answer(
        f"✅ User ID qabul qilindi: <code>{target_user_id}</code>\n\n"
        "Endi <b>bitta xabar</b> yuboring (matn, rasm yoki rasm+caption) — "
        "u shu foydalanuvchiga yuboriladi.\n"
        "Bekor qilish uchun: <code>/cancel_send</code> yoki '🔙 Admin paneldan chiqish'.",
        parse_mode="HTML",
        reply_markup=admin_panel_kb(),
    )


@dp.message(F.from_user.id == ADMIN_CHAT_ID, F.text == "🛠 Admin veb-panel")
async def admin_open_web_panel(message: types.Message):
    """
    Admin uchun React admin panelni WebApp sifatida ochadigan tugma.
    Masalan: https://nurali-print.vercel.app/?adm=777
    """
    admin_url = f"{FRONTEND_URL}?adm=777"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 Admin veb-panelni ochish",
                    web_app=WebAppInfo(url=admin_url),
                )
            ]
        ]
    )

    await message.answer(
        "🛠 <b>Admin veb-panel</b>\n\n"
        "Quyidagi tugmani bosib, admin tizimni WebApp ko‘rinishida oching.",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ---------- Qidiruv uchun matn handler (ADMIN emas!) ----------

@dp.message(
    F.text
    & (F.from_user.id != ADMIN_CHAT_ID)
    & ~F.text.startswith("/")
    & ~F.text.in_(
        [
            "🔍 Qidirish",
            "📋 Mening buyurtmalarim",
            "❓ Yordam",
            "📞 Admin bilan bog'lanish",
            "❌ Bekor qilish",
            # Admin panel tugmalari ham qidiruvga tushmasin:
            "📊 Statistika",
            "📢 Broadcast yuborish",
            "✉️ Userga xabar",
            "🔙 Admin paneldan chiqish",
            "🛠 Admin veb-panel",
        ]
    )
)
async def text_search_handler(message: types.Message):
    """Matn orqali qidirish (faqat oddiy foydalanuvchi)"""
    await register_user(message.from_user)

    qtext = message.text.strip()

    if len(qtext) < 2:
        await message.reply(
            "⚠️ Qidiruv uchun kamida 2 ta belgi kiriting.",
            reply_markup=main_menu_kb(),
        )
        return

    search_msg = await message.answer("🔍 Qidiryapman...")

    async with aiosqlite.connect(DB_PATH) as db:
        rows = await search_files(db, qtext, limit=10)

    await search_msg.delete()

    # AI WEB-APP INTEGRATSIYA
    if not rows:
        tg_id = message.from_user.id
        tg_username = message.from_user.username or ""
        ai_url = f"{FRONTEND_URL}?tg_id={tg_id}&tg_username={tg_username}"

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🤖 AI bilan yaratish",
                        web_app=WebAppInfo(url=ai_url),
                    )
                ]
            ]
        )

        await message.reply(
            "😔 Afsuski, hech narsa topilmadi.\n\n"
            "🤖 Istasangiz, mavzu bo‘yicha AI yordamida tayyor referat yaratishingiz mumkin.\n"
            "Quyidagi tugma orqali web ilovani oching:",
            reply_markup=kb,
        )
        return

    kb = files_list_kb(rows)
    result_text = f"✅ <b>{len(rows)} ta fayl topildi!</b>\n\nKerakli faylni tanlang:"
    await message.reply(result_text, reply_markup=kb, parse_mode="HTML")


# ---------- BUY callback (kanaldagi faylni sotib olish) ----------

@dp.callback_query(F.data.startswith("BUY:"))
async def on_buy_callback(callback: types.CallbackQuery):
    """Sotib olish tugmasi bosilganda"""
    try:
        await register_user(callback.from_user)

        _, rowid_s = callback.data.split(":", 1)
        rowid = int(rowid_s)

        async with aiosqlite.connect(DB_PATH) as db:
            row = await get_file_by_id(db, rowid)

            if not row:
                await callback.answer("❌ Fayl topilmadi!", show_alert=True)
                return

            pending = await get_pending_order_for_user(db, callback.from_user.id)
            if pending:
                await callback.answer(
                    "⚠️ Sizda tugallanmagan buyurtma bor! "
                    "Avval uni yakunlang yoki admin bilan bog'laning.",
                    show_alert=True,
                )
                await callback.message.answer(
                    "Tugallanmagan buyurtmangiz bor. Screenshot yuboring yoki admin bilan bog'laning.",
                    reply_markup=main_menu_kb(),
                )
                return

            order_id = await create_order(
                db,
                callback.from_user.id,
                callback.from_user.username or callback.from_user.full_name,
                rowid,
            )

        price = row[4] or 0
        title = row[1] or "Nomsiz fayl"
        description = row[5] or "Tavsif yo'q"

        order_text = (
            f"🛒 <b>Buyurtma #{order_id}</b>\n\n"
            f"📄 <b>Fayl:</b> {title}\n"
            f"📝 <b>Tavsif:</b> {description}\n"
            f"💰 <b>Narxi:</b> {price:,} so'm\n\n"
            f"💳 <b>To'lov kartasi:</b> <code>{PAYMENT_CARD}</code>\n\n"
            f"📸 <b>Keyingi qadam:</b>\n"
            f"1. Yuqoridagi karta raqamiga {price:,} so'm o'tkazing\n"
            f"2. To'lov screenshotini shu chatga yuboring\n"
            f"3. Admin tekshirib, faylni yuboradi\n\n"
            f"⚠️ <i>Faqat to'g'ri screenshot yuboring!</i>"
        )

        await callback.message.answer(
            order_text,
            parse_mode="HTML",
            reply_markup=cancel_kb(),
        )
        await callback.answer()

    except Exception as e:
        log.error(f"Error in buy callback: {e}")
        await callback.answer("❌ Xatolik yuz berdi!", show_alert=True)


# ---------- Screenshot (FAQAT oddiy user, admin emas!) ----------

@dp.message(F.photo & (F.from_user.id != ADMIN_CHAT_ID))
async def photo_handler(message: types.Message):
    """Oddiy foydalanuvchidan kelgan to'lov screenshotini qabul qilish (kanaldagi fayl uchun)"""
    await register_user(message.from_user)
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            row = await get_pending_order_for_user(db, user_id)

            if not row:
                await message.reply(
                    "❌ Sizda faol buyurtma yo'q.\n\n"
                    "Avval faylni tanlang va buyurtma bering.",
                    reply_markup=main_menu_kb(),
                )
                return

            order_id = row[0]
            photo = message.photo[-1]

            await attach_screenshot_to_order(
                db, order_id, photo.file_id, photo.file_unique_id
            )

            order = await get_order(db, order_id)
            cur = await db.execute(
                "SELECT id, title, price, channel_message_id FROM files WHERE id = ?",
                (order[3],),
            )
            file_row = await cur.fetchone()
        except Exception as e:
            log.error(f"Error in photo handler: {e}")
            await message.reply(
                "❌ Xatolik yuz berdi. Keyinroq urinib ko'ring.",
                reply_markup=main_menu_kb(),
            )
            return

    username = message.from_user.username
    full_name = message.from_user.full_name
    user_link = f"@{username}" if username else full_name

    admin_caption = (
        f"🔔 <b>YANGI TO'LOV TALABI</b>\n\n"
        f"👤 <b>Buyurtmachi:</b> {user_link}\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"📄 <b>Fayl:</b> {file_row[1]}\n"
        f"🆔 <b>Fayl ID:</b> {file_row[0]}\n"
        f"💰 <b>Narxi:</b> {file_row[2]:,} so'm\n"
        f"📋 <b>Order ID:</b> #{order_id}\n\n"
        f"⏰ <b>Vaqt:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    try:
        await bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=photo.file_id,
            caption=admin_caption,
            reply_markup=admin_order_kb(order_id),
            parse_mode="HTML",
        )

        await message.reply(
            "✅ <b>Screenshot qabul qilindi!</b>\n\n"
            "🕐 Admin tez orada tekshiradi va fayl yuboriladi.\n"
            "📋 Buyurtmangizni kuzatish uchun <b>📋 Mening buyurtmalarim</b> tugmasini bosing.",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )

    except Exception as e:
        log.error(f"Error sending to admin: {e}", exc_info=True)
        await message.reply(
            "⚠️ Screenshot qabul qilindi, lekin adminga yuborishda xatolik.\n"
            "Iltimos admin bilan bog'laning.",
            reply_markup=main_menu_kb(),
        )


# ---------- Admin tasdiqlash / rad etish (kanaldagi fayllar oqimi) ----------

@dp.callback_query(F.data.startswith("ADMIN_APPROVE:"))
async def admin_approve_handler(callback: types.CallbackQuery):
    """Admin tomonidan tasdiqlash (kanaldagi fayl uchun buyurtma)"""
    try:
        _, order_id_s = callback.data.split(":", 1)
        order_id = int(order_id_s)

        async with aiosqlite.connect(DB_PATH) as db:
            order = await get_order(db, order_id)

            if not order:
                await callback.message.edit_caption(
                    caption="❌ Buyurtma topilmadi yoki allaqachon qayta ishlangan."
                )
                await callback.answer()
                return

            if order[4] != "pending_admin":
                await callback.answer(
                    "⚠️ Bu buyurtma allaqachon qayta ishlangan!", show_alert=True
                )
                return

            await set_order_status(db, order_id, "approved")

            cur = await db.execute(
                "SELECT channel_message_id, backup_channel_message_id, title FROM files WHERE id = ?",
                (order[3],),
            )
            file_row = await cur.fetchone()

        buyer_id = order[1]
        file_sent = False

        if file_row[0]:
            try:
                await bot.copy_message(
                    chat_id=buyer_id,
                    from_chat_id=CHANNEL_ID,
                    message_id=file_row[0],
                )
                file_sent = True
            except Exception as e:
                log.error(f"Error copying from main channel: {e}")

        if not file_sent and file_row[1]:
            try:
                await bot.copy_message(
                    chat_id=buyer_id,
                    from_chat_id=BACKUP_CHANNEL_ID,
                    message_id=file_row[1],
                )
                file_sent = True
            except Exception as e:
                log.error(f"Error copying from backup channel: {e}")

        if file_sent:
            await bot.send_message(
                chat_id=buyer_id,
                text=(
                    "🎉 <b>Tabriklaymiz!</b>\n\n"
                    "✅ To'lovingiz tasdiqlandi va fayl yuborildi.\n"
                    "📄 Yuqoridagi xabarda faylni topasiz.\n\n"
                    "🙏 Xaridingiz uchun rahmat!\n"
                    "🔄 Yana kerak bo'lsa, /start bosing."
                ),
                parse_mode="HTML",
            )

            updated_caption = (
                f"{callback.message.caption}\n\n"
                f"✅ <b>TASDIQLANDI</b>\n"
                f"👤 Admin: {callback.from_user.full_name}\n"
                f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await callback.message.edit_caption(
                caption=updated_caption, parse_mode="HTML"
            )
            await callback.answer(
                "✅ Buyurtma tasdiqlandi va fayl yuborildi!", show_alert=True
            )
        else:
            await callback.answer("❌ Faylni yuborishda xatolik!", show_alert=True)
            await bot.send_message(
                chat_id=buyer_id,
                text=(
                    "⚠️ To'lovingiz tasdiqlandi, lekin faylni yuborishda xatolik. "
                    "Admin bilan bog'laning."
                ),
            )

    except Exception as e:
        log.error(f"Error in admin approve: {e}")
        await callback.answer("❌ Xatolik yuz berdi!", show_alert=True)


@dp.callback_query(F.data.startswith("ADMIN_REJECT:"))
async def admin_reject_handler(callback: types.CallbackQuery):
    """Admin tomonidan rad etish (kanaldagi fayl oqimi)"""
    try:
        _, order_id_s = callback.data.split(":", 1)
        order_id = int(order_id_s)

        async with aiosqlite.connect(DB_PATH) as db:
            order = await get_order(db, order_id)

            if not order:
                await callback.message.edit_caption(caption="❌ Buyurtma topilmadi.")
                await callback.answer()
                return

            if order[4] in ["approved", "rejected"]:
                await callback.answer(
                    "⚠️ Bu buyurtma allaqachon qayta ishlangan!", show_alert=True
                )
                return

            await set_order_status(db, order_id, "rejected")

        await bot.send_message(
            chat_id=order[1],
            text=(
                "❌ <b>To'lovingiz rad etildi</b>\n\n"
                "Sababi: Screenshot noto'g'ri yoki to'lov summasi mos emas.\n\n"
                "📞 Iltimos admin bilan bog'laning:\n"
                "Muammo: to'lov tasdiqlanmadi\n"
                f"Order ID: #{order_id}"
            ),
            parse_mode="HTML",
        )

        updated_caption = (
            f"{callback.message.caption}\n\n"
            f"❌ <b>RAD ETILDI</b>\n"
            f"👤 Admin: {callback.from_user.full_name}\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await callback.message.edit_caption(
            caption=updated_caption, parse_mode="HTML"
        )
        await callback.answer("❌ Buyurtma rad etildi!", show_alert=True)

    except Exception as e:
        log.error(f"Error in admin reject: {e}")
        await callback.answer("❌ Xatolik yuz berdi!", show_alert=True)


# ---------- Channel post handler ----------

@dp.channel_post()
async def channel_post_handler(message: types.Message):
    """Kanalga yangi post qo'shilganda avtomatik indekslash"""
    if message.chat.id not in [CHANNEL_ID, BACKUP_CHANNEL_ID]:
        return

    caption = message.caption or ""
    meta = parse_caption(caption)

    file_id, file_unique_id = None, None

    if message.document:
        file_id = message.document.file_id
        file_unique_id = message.document.file_unique_id
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_unique_id = message.photo[-1].file_unique_id
    elif message.video:
        file_id = message.video.file_id
        file_unique_id = message.video.file_unique_id
    elif message.audio:
        file_id = message.audio.file_id
        file_unique_id = message.audio.file_unique_id
    else:
        log.info("Unsupported media type in channel post")
        return

    if message.chat.id == BACKUP_CHANNEL_ID:
        meta["backup_channel_message_id"] = message.message_id
        meta["channel_message_id"] = None
    else:
        meta["channel_message_id"] = message.message_id
        meta["backup_channel_message_id"] = None

    meta.update(
        {
            "file_id": file_id,
            "file_unique_id": file_unique_id,
            "caption": caption,
        }
    )

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            rowid = await insert_file_record(db, meta)

        log.info(
            f"✅ Indexed new file: ID={rowid}, "
            f"title='{meta.get('title', 'N/A')}', "
            f"channel={'BACKUP' if message.chat.id == BACKUP_CHANNEL_ID else 'MAIN'}"
        )

        if ADMIN_CHAT_ID and ADMIN_CHAT_ID != message.chat.id:
            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"✅ <b>Yangi fayl indekslandi</b>\n\n"
                        f"🆔 <b>ID:</b> {rowid}\n"
                        f"📄 <b>Sarlavha:</b> {meta.get('title') or 'Kiritilmagan'}\n"
                        f"📂 <b>Kategoriya:</b> {meta.get('category') or 'Yo‘q'}\n"
                        f"🏷 <b>Teglar:</b> {meta.get('tags') or 'Yo‘q'}\n"
                        f"💰 <b>Narx:</b> {meta.get('price', 0):,} so'm\n"
                        f"📡 <b>Kanal:</b> "
                        f"{'Backup' if message.chat.id == BACKUP_CHANNEL_ID else 'Asosiy'}"
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                log.error(f"Could not notify admin: {e}")

    except Exception as e:
        log.error(f"Error indexing file: {e}")


# ---------- ADMIN → FOYDALANUVCHI XABAR YUBORISH (komandalar) ----------

@dp.message(Command(commands=["send"]))
async def admin_send_command(message: types.Message, command: CommandObject):
    """Admin: /send <user_id> — keyingi xabarni o‘sha foydalanuvchiga yuborish"""
    if message.from_user.id != ADMIN_CHAT_ID and message.chat.id != ADMIN_CHAT_ID:
        await message.answer("❌ Bu buyruq faqat admin uchun!")
        return

    if not command.args:
        await message.answer(
            "ℹ️ Foydalanish: <code>/send 123456789</code>\n"
            "Yoki admin paneldagi '✉️ Userga xabar' tugmasidan foydalaning.\n\n"
            "Keyin esa yubormoqchi bo‘lgan xabaringizni (matn, rasm, rasm+caption) jo'nating.",
            parse_mode="HTML",
        )
        return

    args = command.args.strip().split()
    if not args[0].isdigit():
        await message.answer(
            "❗ Birinchi argument <b>user_id</b> bo‘lishi kerak.\n"
            "Masalan: <code>/send 123456789</code>",
            parse_mode="HTML",
        )
        return

    target_user_id = int(args[0])
    ADMIN_SEND_TARGET[message.from_user.id] = target_user_id
    ADMIN_WAITING_TARGET_USER.discard(message.from_user.id)

    reply_kb = admin_panel_kb() if message.from_user.id in ADMIN_PANEL_MODE else main_menu_kb()

    await message.answer(
        f"✅ Xabar yuborish rejimi yoqildi.\n"
        f"🎯 Foydalanuvchi ID: <code>{target_user_id}</code>\n\n"
        f"Endi <b>bitta xabar</b> yuboring (matn, rasm yoki rasm+caption) — men uni shu foydalanuvchiga yuboraman.\n"
        f"Bekor qilish uchun: <code>/cancel_send</code>",
        parse_mode="HTML",
        reply_markup=reply_kb,
    )


@dp.message(Command(commands=["cancel_send"]))
async def admin_cancel_send(message: types.Message):
    """Admin: yuborish rejimini bekor qilish"""
    if message.from_user.id != ADMIN_CHAT_ID and message.chat.id != ADMIN_CHAT_ID:
        await message.answer("❌ Bu buyruq faqat admin uchun!")
        return

    ADMIN_SEND_TARGET.pop(message.from_user.id, None)
    ADMIN_WAITING_TARGET_USER.discard(message.from_user.id)

    reply_kb = admin_panel_kb() if message.from_user.id in ADMIN_PANEL_MODE else main_menu_kb()
    await message.answer(
        "↩️ Foydalanuvchiga xabar yuborish rejimi bekor qilindi.",
        reply_markup=reply_kb,
    )


# ---------- ADMIN BROADCAST (barcha users) komandalar ----------

@dp.message(Command(commands=["broadcast"]))
async def admin_broadcast_command(message: types.Message):
    """Admin: /broadcast — keyingi xabarni barcha foydalanuvchilarga yuborish"""
    if message.from_user.id != ADMIN_CHAT_ID and message.chat.id != ADMIN_CHAT_ID:
        await message.answer("❌ Bu buyruq faqat admin uchun!")
        return

    ADMIN_BROADCAST_MODE.add(message.from_user.id)

    reply_kb = admin_panel_kb() if message.from_user.id in ADMIN_PANEL_MODE else main_menu_kb()

    await message.answer(
        "📢 <b>Broadcast rejimi yoqildi.</b>\n\n"
        "Endi <b>bitta xabar</b> yuboring (matn, rasm, rasm+caption, hujjat va hokazo) — "
        "u barcha ro‘yxatdagi foydalanuvchilarga yuboriladi.\n\n"
        "Bekor qilish uchun: <code>/cancel_broadcast</code>",
        parse_mode="HTML",
        reply_markup=reply_kb,
    )


@dp.message(Command(commands=["cancel_broadcast"]))
async def admin_cancel_broadcast(message: types.Message):
    """Admin: broadcast rejimini bekor qilish"""
    if message.from_user.id != ADMIN_CHAT_ID and message.chat.id != ADMIN_CHAT_ID:
        await message.answer("❌ Bu buyruq faqat admin uchun!")
        return

    ADMIN_BROADCAST_MODE.discard(message.from_user.id)

    reply_kb = admin_panel_kb() if message.from_user.id in ADMIN_PANEL_MODE else main_menu_kb()
    await message.answer(
        "↩️ Broadcast rejimi bekor qilindi.",
        reply_markup=reply_kb,
    )


# ---------- Admin xabarini foydalanuvchilarga forward qilish ----------

@dp.message(F.from_user.id == ADMIN_CHAT_ID)
async def admin_forward_message(message: types.Message):
    """
    Agar admin:
    - /send <user_id> yoki admin panel orqali user_id tanlagan bo‘lsa → keyingi xabar bitta foydalanuvchiga
    - /broadcast yoki admin panel orqali broadcast yoqqan bo‘lsa → keyingi xabar barcha users jadvalidagi userlarga
      yuboriladi.
    """
    admin_id = message.from_user.id

    if message.text and message.text.startswith("/"):
        return

    if admin_id not in ADMIN_SEND_TARGET and admin_id not in ADMIN_BROADCAST_MODE:
        return

    panel_buttons = {
        "📊 Statistika",
        "📢 Broadcast yuborish",
        "✉️ Userga xabar",
        "🔙 Admin paneldan chiqish",
        "🛠 Admin veb-panel",
    }
    if message.text and message.text in panel_buttons:
        return

    if message.text and message.text.isdigit():
        return

    if admin_id in ADMIN_SEND_TARGET:
        target_user_id = ADMIN_SEND_TARGET.pop(admin_id, None)
        if not target_user_id:
            return

        try:
            await bot.copy_message(
                chat_id=target_user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            reply_kb = admin_panel_kb() if admin_id in ADMIN_PANEL_MODE else main_menu_kb()
            await message.answer(
                f"✅ Xabar foydalanuvchiga yuborildi.\n🆔 User ID: <code>{target_user_id}</code>",
                parse_mode="HTML",
                reply_markup=reply_kb,
            )
        except Exception as e:
            log.error(f"Error sending admin message to user {target_user_id}: {e}")
            reply_kb = admin_panel_kb() if admin_id in ADMIN_PANEL_MODE else main_menu_kb()
            await message.answer(
                "❌ Xabarni foydalanuvchiga yuborishda xatolik yuz berdi.",
                reply_markup=reply_kb,
            )
        return

    if admin_id in ADMIN_BROADCAST_MODE:
        ADMIN_BROADCAST_MODE.discard(admin_id)

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT user_id FROM users")
                users = await cur.fetchall()
        except Exception as e:
            log.error(f"Error fetching users for broadcast: {e}")
            reply_kb = admin_panel_kb() if admin_id in ADMIN_PANEL_MODE else main_menu_kb()
            await message.answer(
                "❌ Broadcast uchun foydalanuvchilarni olishda xatolik.",
                reply_markup=reply_kb,
            )
            return

        total = len(users)
        sent = 0

        for (uid,) in users:
            try:
                await bot.copy_message(
                    chat_id=uid,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                sent += 1
            except Exception as e:
                log.error(f"Broadcast send error to {uid}: {e}")

        reply_kb = admin_panel_kb() if admin_id in ADMIN_PANEL_MODE else main_menu_kb()
        await message.answer(
            f"📢 Broadcast yakunlandi.\n"
            f"Jami foydalanuvchi: {total}\n"
            f"✅ Yuborildi: {sent}\n"
            f"❌ Xatolik bo‘lganlar: {total - sent}",
            reply_markup=reply_kb,
        )

        return

# === REFERAT / MUSTAQIL ISH UCHUN TITUL SHABLONI VA FORMAT FUNKSIYALARI ===

TITLE_TEMPLATE = {
    "top": "O‘ZBEKISTON RESPUBLIKASI OLIY TA’LIM, FAN VA INNOVATSIYALAR VAZIRLIGI",
}



def clean_ai_content(raw: str) -> str:
    """
    Firebase (AI) dan kelgan matnni biroz tozalab beradi:
    - oxiridagi 'Izoh:' blokini kesadi
    - '---' chiziqlarni olib tashlaydi
    - '### 1. Kirish' kabi heading belgilari (#) ni olib tashlaydi
    - ortiqcha bo'sh qatordan tozalaydi
    """
    text = raw or ""

    # Oxirida keladigan Izoh / Eslatma bloklari bo'lsa, kesib tashlaymiz
    text = re.sub(
        r"\n+\s*(Izoh|Eslatma)\s*:.*$",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
        # 👇👇👇 SHU YANGI QATORNI QO‘SHASIZ 👇👇👇
    # Maxsus marker-qatorni butunlay olib tashlash
    text = re.sub(
        r"^\s*\[FOYDALANILGAN ADABIYOTLAR YANGI SAHIFA\]\s*$",
        "",
        text,
        flags=re.MULTILINE,
    )

    # Markdown horizontal rule: --- qatorini o'chirish
    text = re.sub(r"^\s*---\s*$", "", text, flags=re.MULTILINE)

    # ### 1. Kirish -> 1. Kirish (heading belgilari (#) ni olib tashlash)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)

    # Juda ko'p bo'sh qatorlarni qisqartirish
    text = re.sub(r"\n{2,}", "\n\n", text)
    # text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)

    return text.strip()


def ai_content_to_html_paragraphs(content: str) -> str:
    """
    Firebase’dan kelgan matnni Word uchun HTML'ga aylantiradi:
    - **qalin** -> <strong>qalin</strong>
    - markdown jadval satrlarini | col1 | col2 | -> <table>...
    - 1. Kirish, 2. Asosiy qism, 3. Xulosa, 4. Foydalanilgan adabiyotlar sarlavhalarini alohida formatlaydi
    - 4. Foydalanilgan adabiyotlar bo'limi alohida sahifadan boshlanadi
    """
    if not content:
        return ""
    # LaTeX formulalarni img tegiga aylantiramiz
    content = replace_latex_with_images(content)

    # 1) Qalin shrift: **matn** -> <strong>matn</strong>
    content_processed = re.sub(
        r"\*\*(.+?)\*\*",
        r"<strong>\1</strong>",
        content,
        flags=re.DOTALL,
    )

    lines = content_processed.splitlines()
    html_blocks: list[str] = []
    table_buffer: list[str] = []

    def flush_table():
        nonlocal table_buffer, html_blocks
        if not table_buffer:
            return
        rows = table_buffer
        table_buffer = []

        # Markdown jadvalidagi separator (|---|---|) qatorlarini olib tashlash
        cleaned_rows = [
            r for r in rows
            if not re.match(r"^\s*\|?\s*-+\s*(\|\s*-+\s*)+\|?\s*$", r)
        ]
        if not cleaned_rows:
            return

        html = [
            '<table border="1" cellspacing="0" cellpadding="4" '
            'style="border-collapse:collapse;margin:8px 0;font-size:12pt; width:100%;">'
        ]
        for i, row in enumerate(cleaned_rows):
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            tag = "th" if i == 0 else "td"
            html.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>")
        html.append("</table>")
        html_blocks.append("\n".join(html))

    for line in lines:
        # Jadval qatori: | col1 | col2 |
        if re.match(r"^\s*\|.*\|\s*$", line):
            table_buffer.append(line)
            continue

        # Jadval tugagan bo‘lishi mumkin
        if table_buffer:
            flush_table()

        stripped = line.strip()
        if not stripped:
            continue

        # === Asosiy bo'lim sarlavhalari ===
        # 1. Kirish
        if re.match(r"^1\.\s*Kirish\s*$", stripped, flags=re.IGNORECASE):
            html_blocks.append(
                '<p class="section-title-main">1. Kirish</p>'
            )
            continue

        # 2. Asosiy qism
        if re.match(r"^2\.\s*Asosiy qism\s*$", stripped, flags=re.IGNORECASE):
            html_blocks.append(
                '<p class="section-title-main">2. Asosiy qism</p>'
            )
            continue

        # 3. Xulosa
        if re.match(r"^3\.\s*Xulosa\s*$", stripped, flags=re.IGNORECASE):
            html_blocks.append(
                '<p class="section-title-main">3. Xulosa</p>'
            )
            continue

        # 4. Foydalanilgan adabiyotlar — alohida sahifadan boshlansin
        if re.match(r"^4\.\s*Foydalanilgan adabiyotlar", stripped, flags=re.IGNORECASE):
            # Yangi sahifadan boshlash
            html_blocks.append(
                '<br style="page-break-before:always; mso-special-character:line-break;" />'
            )
            html_blocks.append(
                '<p class="section-title-main">4. Foydalanilgan adabiyotlar</p>'
            )
            continue

        # === Ichki bo'limlar: 2.1. ..., 2.2. ... va hokazo ===
        # Masalan: 2.1. Sun'iy intellekt tushunchasi
        if re.match(r"^\d+\.\d+\.\s+.+$", stripped):
            html_blocks.append(
                f'<p class="section-title-sub">{stripped}</p>'
            )
            continue

        # Agar satr allaqachon HTML tag bilan boshlansa (<div>, <table> va h.k.)
        if stripped.lstrip().startswith("<"):
            html_blocks.append(stripped)
        else:
            html_blocks.append(f"<p>{stripped}</p>")

    # Oxirgi jadval bo'lsa, uni ham flush qilamiz
    if table_buffer:
        flush_table()

    return "\n".join(html_blocks)


def build_title_page_html(topic: str, work_type_name: str, year: int | None = None) -> str:
    """
    Klassik titul:
    - hammasi o'rtaga tekislangan
    - interval ~1.5
    - tepa: vazirlik
    - 4 ta bo'sh qator
    - 2 qator oraliq bilan: UNIVERSITETI, FAKULTETI, KAFEDRASI, Mavzu qatori
    - 26 pt da ish turi (work_type_name, masalan: MUSTAQIL ISH yoki REFERAT)
    - sahifa oxirida faqat yil
    """
    if year is None:
        year = datetime.now().year

    t = TITLE_TEMPLATE

    return f"""
    <div class="title-page" style="width:100%; text-align:center;">


      <!-- Vazirlik nomi -->
      <p style="margin-top:40px; margin-bottom:0; text-align:center; text-indent:0; font-size:18pt; font-weight:bold; text-transform:uppercase;">
        {t["top"]}
      </p>

      <!-- 4 ta bo'sh qator -->
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>

      <!-- UNIVERSITETI -->
      <p style="margin-top:0; margin-bottom:16px; text-align:center; text-indent:0; font-size:18pt;">
        ___________________________________ UNIVERSITETI
      </p>

      <!-- FAKULTETI -->
      <p style="margin-top:0; margin-bottom:16px; text-align:center; text-indent:0; font-size:18pt;">
        ___________________________________ FAKULTETI
      </p>

      <!-- KAFEDRASI -->
      <p style="margin-top:0; margin-bottom:16px; text-align:center; text-indent:0; font-size:18pt;">
        ___________________________________ KAFEDRASI
      </p>

      <!-- Yana kichik bo'sh joy -->
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>

      <!-- Ish turi (26 pt) -->
      <p style="margin-top:0; margin-bottom:24px; text-align:center; text-indent:0; font-size:26pt; font-weight:bold;">
        {work_type_name.upper()}
      </p>
      <!-- Yana kichik bo'sh joy -->
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <!-- Mavzu qatori -->
      <p style="margin-top:0; margin-bottom:0; text-align:center; text-indent:0; font-size:14pt;">
        Mavzu: <span style="color:#c00000;">“{topic}”</span>
      </p>
      <!-- Yana kichik bo'sh joy -->
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <p style="margin:0; text-indent:0;">&nbsp;</p>
      <!-- Sahifa pastidagi yil -->
      <p style="margin-top:120px; margin-bottom:0; text-align:center; text-indent:0; font-size:16pt;">
        {year}
      </p>
    </div>

    <!-- Keyingi betdan asosiy matn boshlansin -->
    <br style="page-break-before:always; mso-special-character:line-break;" />
    """

# ---------- Referat uchun .doc fayl yasash (WebApp oqimi) ----------

def build_word_doc_file(topic: str, work_type_name: str, content: str) -> str:
    """
    WebApp orqali kelgan matndan TITUL + asosiy matnli .doc (Word) fayl yaratadi.
    1-bet: umumiy titul
    2-betdan: AI rasmlari qo‘shilgan matn
    """
    year = datetime.now().year
    safe_topic = re.sub(r"[^0-9A-Za-zА-Яа-яЎҚҒҲўқғҳ]+", "_", topic)[:40] or "referat"

    # 1) Firebase / Groq'dan kelgan matnni biroz tozalab olamiz
    cleaned = clean_ai_content(content)
    # 2) [RASM n: ...] markerlarini AI rasmlari bilan almashtiramiz (faqat backendda)
    with_images = inject_ai_images_into_content(cleaned)

    # 3) Titul sahifani HTML ko‘rinishida olamiz
    title_html = build_title_page_html(topic=topic, work_type_name=work_type_name, year=year)

    # 4) Asosiy matnni HTML paragraflarga/jadvallarga aylantiramiz
    body_html = ai_content_to_html_paragraphs(with_images)

    # 5) Umumiy Word HTML hujjat
    html = f"""
    <html xmlns:o='urn:schemas-microsoft-com:office:office'
          xmlns:w='urn:schemas-microsoft-com:office:word'
          xmlns='http://www.w3.org/TR/REC-html40'>
    <head>
      <meta charset="utf-8">
      <title>{work_type_name} - {topic}</title>
      <style>
        @page {{ size:A4; margin:2cm 2.5cm 2cm 3cm; }}
        body {{
          font-family:'Times New Roman';
          font-size:14pt;
          line-height:150%;
          text-align:justify;
          mso-line-height-rule:exactly;
        }}
        p {{
          text-indent:1.25cm;
          margin-top:0;
          margin-bottom:0;
          line-height:150%;
          mso-line-height-rule:exactly;  /* Word shuni ko‘radi */
        }}

        /* Titul sahifa uchun alohida qoidalar */
        .title-page p {{
          text-indent:0;
          text-align:center;
          line-height:100%;  /* Titulda 1.0, asosiy matnda 1.5 qoladi */
          mso-line-height-rule:exactly;
        }}

        table {{
          border-collapse:collapse;
        }}
        th {{
          font-weight:bold;
          text-align:center;
        }}
        td {{
          vertical-align:top;
        }}
        .image-container {{
          text-align:center;
          margin:0.7cm 0;
        }}
        .image-container p {{
          text-indent:0;
          margin:0;
          text-align:center;
        }}
        .section-title-main {{
          text-indent:0;
          font-weight:bold;
          text-align:center;
          margin-top:0.7cm;
          margin-bottom:0.4cm;
          font-size:16pt;
        }}
        .section-title-sub {{
          text-indent:0;
          font-weight:bold;
          margin-top:0.5cm;
          margin-bottom:0.2cm;
        }}
      </style>


    </head>
    <body>
      {title_html}
      {body_html}
    </body>
    </html>
    """

    fd, path = tempfile.mkstemp(suffix=".doc", prefix=f"{safe_topic}_")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


# CORS uchun ruxsat etilgan origin (frontend domeni bilan bir xil)
ALLOWED_ORIGIN = FRONTEND_URL


@web.middleware
async def cors_middleware(request: web.Request, handler):
    # Preflight (OPTIONS) bo'lsa, handler'ga umuman kirmasdan javob beramiz
    if request.method == "OPTIONS":
        resp = web.Response(status=200)
    else:
        resp = await handler(request)

    origin = request.headers.get("Origin")

    if origin == ALLOWED_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"

    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp

# ... (boshqa import'lar)

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

async def handle_admin_xabar(request: web.Request):
    """
    POST /api/admin_xabar
    Frontend dan yangi to'lov kelsa, admin'ga faqat Web App panel tugmasi bilan xabar yuboradi.
    """
    if request.content_type != 'application/json':
        return web.json_response({"ok": False, "error": "Content-Type must be application/json"}, status=400)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "JSON ma'lumot noto'g'ri"}, status=400)
    order_id = data.get("orderId")
    topic = data.get("topic")
    work_type_name = data.get("workTypeName")
    price = data.get("price")
    telegram_user_id = data.get("telegramUserId")
    telegram_username = data.get("telegramUsername")
    if not order_id or not topic or not work_type_name or not price:
        return web.json_response({"ok": False, "error": "orderId, topic, workTypeName, price majburiy"}, status=400)
    # Admin'ga xabar yuborish
    user_info = f"{telegram_user_id} (@{telegram_username})" if telegram_username else str(telegram_user_id) if telegram_user_id else "Noma'lum foydalanuvchi"
    text = (
        f"🔔 <b>YANGI TO'LOV TEKSHIRISH UCHUN!</b>\n"
        f"🆔 <b>Buyurtma:</b> <code>{order_id}</code>\n"
        f"📄 <b>Ish turi:</b> {work_type_name}\n"
        f"💰 <b>Summa:</b> {price:,} so'm\n"
        f"📝 <b>Mavzu:</b> {topic}\n"
        f"👤 <b>Foydalanuvchi:</b> {user_info}"
    )
    
    # Faqat Web App tugmasi
    admin_url = f"{FRONTEND_URL}?adm=777"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 Admin Panelni Ochish",
                    web_app=WebAppInfo(url=admin_url)
                )
            ]
        ]
    )
    
    try:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID, 
            text=text, 
            parse_mode="HTML",
            reply_markup=kb
        )
        return web.json_response({"ok": True, "detail": "Admin xabari yuborildi"})
    except Exception as e:
        log.error(f"/api/admin_xabar error: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": "Xabar yuborilmadi"}, status=500)
  

# ---------- HTTP API: WebApp'dan referat yuborish uchun ----------

async def handle_send_referat(request: web.Request):
    """
    POST /api/send_referat

    JSON body misol:
    {
      "token": "INTERNAL_API_TOKEN",
      "telegramUserId": "123456789",
      "telegramUsername": "username_agar_bolsa",
      "topic": "Sun'iy intellekt ...",
      "workTypeName": "Referat",
      "content": "butun referat matni..."
    }
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    token = data.get("token")
    if token != INTERNAL_API_TOKEN:
        return web.json_response({"ok": False, "error": "Unauthorized"}, status=403)

    telegram_user_id = data.get("telegramUserId") or data.get("telegram_user_id")
    topic = data.get("topic")
    work_type_name = data.get("workTypeName") or data.get("work_type_name") or "Referat"
    content = data.get("content") or data.get("contentFull")

    if not telegram_user_id or not topic or not content:
        return web.json_response(
            {"ok": False, "error": "telegramUserId, topic va content majburiy"},
            status=400,
        )

    try:
        chat_id = int(telegram_user_id)
    except Exception:
        return web.json_response({"ok": False, "error": "telegramUserId noto'g'ri"}, status=400)

    file_path = None
    try:
        file_path = build_word_doc_file(topic, work_type_name, content)
        file_name = os.path.basename(file_path)

        input_file = FSInputFile(file_path, filename=file_name)
        caption = f"{work_type_name} — {topic}"

        await bot.send_document(
            chat_id=chat_id,
            document=input_file,
            caption=caption[:1024],
        )

        return web.json_response({"ok": True, "detail": "File sent via bot"})

    except Exception as e:
        log.error(f"/api/send_referat error: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": "Server error"}, status=500)
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


# ---------- Startup & Shutdown ----------

async def on_startup():
    """Bot ishga tushganda"""
    log.info("=" * 50)
    log.info("🚀 Bot ishga tushmoqda...")
    log.info(f"📋 Bot token: {BOT_TOKEN[:10]}...")
    log.info(f"📡 Channel ID: {CHANNEL_ID}")
    log.info(f"📡 Backup Channel ID: {BACKUP_CHANNEL_ID}")
    log.info(f"👤 Admin Chat ID: {ADMIN_CHAT_ID}")
    log.info(f"💳 Payment Card: {PAYMENT_CARD}")
    log.info(f"🗄 Database: {DB_PATH}")
    log.info(f"🌐 FRONTEND_URL: {FRONTEND_URL}")
    log.info(f"🌐 ALLOWED_ORIGIN: {ALLOWED_ORIGIN}")
    log.info(f"🌐 API_PORT: {API_PORT}")

    await init_db()

    try:
        bot_info = await bot.get_me()
        log.info(f"✅ Bot muvaffaqiyatli ulandi: @{bot_info.username}")
        log.info(f"📝 Bot nomi: {bot_info.first_name}")
        log.info(f"🆔 Bot ID: {bot_info.id}")
    except Exception as e:
        log.error(f"❌ Bot ma'lumotlarini olishda xatolik: {e}")

    log.info("=" * 50)


async def on_shutdown():
    """Bot to'xtaganda"""
    log.info("🛑 Bot to'xtatilmoqda...")
    await bot.session.close()
    log.info("✅ Bot to'xtatildi")


async def main():
    """Asosiy funksiya: Telegram bot + HTTP API birgalikda"""
    runner = None
    try:
        await on_startup()  # on_startup ichida log va db sozlamalari

        # dp.errors.register(error_handler)  # hozircha ishlatmaymiz

        # HTTP API app (CORS bilan)
        app = web.Application(middlewares=[cors_middleware])
        app.add_routes([
            web.post("/api/send_referat", handle_send_referat),
            # ✅ Yangi API:
            web.post('/api/admin_xabar', handle_admin_xabar),  # Qo'shilayotgan API
            # OPTIONS uchun alohida handler shart emas, middleware 200 qaytaradi
        ])
        runner = web.AppRunner(app)
        await runner.setup()
        # Prod uchun 0.0.0.0 da tinglaymiz
        site = web.TCPSite(runner, "0.0.0.0", API_PORT)
        await site.start()
        log.info(f"🌐 HTTP API ishga tushdi: 0.0.0.0:{API_PORT} /api/send_referat, /api/admin_xabar")

        # Telegram bot polling
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

    except KeyboardInterrupt:
        log.info("⌨️ Keyboard interrupt - bot to'xtatilmoqda...")
    except Exception as e:
        log.error(f"❌ Fatal error: {e}", exc_info=True)
    finally:
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                pass
        await on_shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("👋 Bot yakunlandi")
