"""
Test (quiz) bot — aiogram v3 + aiosqlite. Interface language: Uzbek.

Run:  python bot.py
"""
import asyncio
import html
import json
import logging
import os
import random
import re
import string
import time
from dataclasses import dataclass, field
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
DB_PATH = os.getenv("DB_PATH", "quiz.db")
LETTERS = "ABCDEF"
BOT_USERNAME = ""

BTN_LIST = "📚 Testlar ro'yxati"
BTN_RESULTS = "📊 Natijalarim"
BTN_HELP = "ℹ️ Yordam"

esc = html.escape


# --------------------------------------------------------------------------
# DATABASE LAYER
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id   INTEGER PRIMARY KEY,
    full_name     TEXT,
    username      TEXT,
    registered_at TEXT
);
CREATE TABLE IF NOT EXISTS tests (
    test_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT UNIQUE NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT DEFAULT '',
    creator_id    INTEGER,
    passing_score INTEGER DEFAULT 60,      -- foiz (%)
    time_limit    INTEGER DEFAULT 0,       -- daqiqa, 0 = cheksiz
    is_published  INTEGER DEFAULT 1,
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS questions (
    question_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id              INTEGER NOT NULL,
    position             INTEGER,
    question_text        TEXT NOT NULL,
    options_json         TEXT NOT NULL,
    correct_option_index INTEGER NOT NULL,
    explanation          TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS results (
    result_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id     INTEGER,
    test_id         INTEGER,
    score           INTEGER,               -- to'g'ri javoblar soni
    total_questions INTEGER,
    passed_status   INTEGER,
    completed_at    TEXT
);
"""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def fetch_all(sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()


async def fetch_one(sql: str, params: tuple = ()):
    rows = await fetch_all(sql, params)
    return rows[0] if rows else None


async def execute(sql: str, params: tuple = ()) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid


async def upsert_user(user) -> None:
    await execute(
        """INSERT INTO users (telegram_id, full_name, username, registered_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(telegram_id) DO UPDATE SET full_name=excluded.full_name,
                                                  username=excluded.username""",
        (user.id, user.full_name, user.username, now_str()),
    )


async def generate_code() -> str:
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if not await fetch_one("SELECT 1 FROM tests WHERE code=?", (code,)):
            return code


async def create_test(creator_id: int, data: dict) -> str:
    """Saves the test together with all questions; returns its code."""
    code = await generate_code()
    test_id = await execute(
        """INSERT INTO tests (code, title, description, creator_id, passing_score,
                              time_limit, is_published, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (code, data["title"], data["description"], creator_id,
         data["passing"], data["time_limit"], now_str()),
    )
    for pos, q in enumerate(data["questions"], start=1):
        await execute(
            """INSERT INTO questions (test_id, position, question_text, options_json,
                                      correct_option_index, explanation)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (test_id, pos, q["text"], json.dumps(q["options"], ensure_ascii=False),
             q["correct"], q["explanation"]),
        )
    return code


async def delete_test(test_id: int) -> None:
    await execute("DELETE FROM questions WHERE test_id=?", (test_id,))
    await execute("DELETE FROM results WHERE test_id=?", (test_id,))
    await execute("DELETE FROM tests WHERE test_id=?", (test_id,))


# --------------------------------------------------------------------------
# ROUTERS
# --------------------------------------------------------------------------
common = Router()
admin = Router()
user = Router()

# Faqat ADMIN_IDS dagilar admin routerga kira oladi
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_LIST)],
                  [KeyboardButton(text=BTN_RESULTS), KeyboardButton(text=BTN_HELP)]],
        resize_keyboard=True,
    )


def fmt_time(minutes: int) -> str:
    return f"{minutes} daqiqa" if minutes else "cheklanmagan"


# --------------------------------------------------------------------------
# COMMON: /cancel
# --------------------------------------------------------------------------
@common.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Bekor qilindi.", reply_markup=main_menu())


# --------------------------------------------------------------------------
# ADMIN PANEL
# --------------------------------------------------------------------------
class NewTest(StatesGroup):
    title = State()
    description = State()
    time_limit = State()
    passing = State()
    q_text = State()
    q_options = State()
    q_correct = State()
    q_expl = State()
    q_more = State()


def admin_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Yangi test yaratish", callback_data="adm:new")
    kb.button(text="📋 Mening testlarim", callback_data="adm:list")
    kb.button(text="📈 Statistika", callback_data="adm:stats")
    kb.adjust(1)
    return kb.as_markup()


@admin.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛠 <b>Admin panel</b>\nKerakli bo'limni tanlang:", reply_markup=admin_menu_kb())


@admin.callback_query(F.data == "adm:menu")
async def adm_menu(cb: CallbackQuery):
    await cb.message.edit_text("🛠 <b>Admin panel</b>\nKerakli bo'limni tanlang:", reply_markup=admin_menu_kb())
    await cb.answer()


# ---- Test yaratish (FSM) --------------------------------------------------
@admin.callback_query(F.data == "adm:new")
async def adm_new(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(NewTest.title)
    await cb.message.answer("📝 Test nomini kiriting:\n\n(Bekor qilish: /cancel)")
    await cb.answer()


@admin.message(NewTest.title, F.text)
async def nt_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip(), questions=[])
    await state.set_state(NewTest.description)
    await message.answer("📄 Test tavsifini kiriting (o'tkazib yuborish: /skip):")


@admin.message(NewTest.description, F.text)
async def nt_desc(message: Message, state: FSMContext):
    desc = "" if message.text.strip() == "/skip" else message.text.strip()
    await state.update_data(description=desc)
    await state.set_state(NewTest.time_limit)
    await message.answer("⏱ Vaqt chegarasi (daqiqada). Cheklovsiz bo'lsa <b>0</b> yozing:")


@admin.message(NewTest.time_limit, F.text)
async def nt_time(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        return await message.answer("⚠️ Iltimos, butun son kiriting (masalan: 15 yoki 0).")
    await state.update_data(time_limit=int(message.text.strip()))
    await state.set_state(NewTest.passing)
    await message.answer("🎯 O'tish balli (foizda, 1–100). Masalan: <b>60</b>")


@admin.message(NewTest.passing, F.text)
async def nt_passing(message: Message, state: FSMContext):
    t = message.text.strip()
    if not t.isdigit() or not 1 <= int(t) <= 100:
        return await message.answer("⚠️ 1 dan 100 gacha son kiriting.")
    await state.update_data(passing=int(t))
    await state.set_state(NewTest.q_text)
    await message.answer("❓ <b>1-savol</b> matnini kiriting:")


@admin.message(NewTest.q_text, F.text)
async def nt_qtext(message: Message, state: FSMContext):
    await state.update_data(cur_text=message.text.strip())
    await state.set_state(NewTest.q_options)
    await message.answer(
        "🔤 Javob variantlarini kiriting — <b>har bir variant yangi qatorda</b> (2 dan 6 gacha).\n\n"
        "Masalan:\n<code>Toshkent\nSamarqand\nBuxoro\nXiva</code>"
    )


@admin.message(NewTest.q_options, F.text)
async def nt_options(message: Message, state: FSMContext):
    opts = [l.strip() for l in message.text.split("\n") if l.strip()]
    if not 2 <= len(opts) <= 6:
        return await message.answer("⚠️ Variantlar soni 2 dan 6 gacha bo'lishi kerak. Qayta yuboring.")
    await state.update_data(cur_options=opts)
    await state.set_state(NewTest.q_correct)
    kb = InlineKeyboardBuilder()
    for i in range(len(opts)):
        kb.button(text=LETTERS[i], callback_data=f"cor:{i}")
    kb.adjust(len(opts))
    listing = "\n".join(f"<b>{LETTERS[i]})</b> {esc(o)}" for i, o in enumerate(opts))
    await message.answer(f"{listing}\n\n✅ To'g'ri javobni tanlang:", reply_markup=kb.as_markup())


@admin.callback_query(NewTest.q_correct, F.data.startswith("cor:"))
async def nt_correct(cb: CallbackQuery, state: FSMContext):
    await state.update_data(cur_correct=int(cb.data.split(":")[1]))
    await state.set_state(NewTest.q_expl)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("💡 Javob izohini kiriting (o'tkazib yuborish: /skip):")
    await cb.answer()


@admin.message(NewTest.q_expl, F.text)
async def nt_expl(message: Message, state: FSMContext):
    expl = "" if message.text.strip() == "/skip" else message.text.strip()
    data = await state.get_data()
    questions = data["questions"]
    questions.append({
        "text": data["cur_text"],
        "options": data["cur_options"],
        "correct": data["cur_correct"],
        "explanation": expl,
    })
    await state.update_data(questions=questions)
    await state.set_state(NewTest.q_more)
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Yana savol qo'shish", callback_data="more:yes")
    kb.button(text="✅ Yakunlash va e'lon qilish", callback_data="more:no")
    kb.adjust(1)
    await message.answer(f"✅ Savol qo'shildi. Jami: <b>{len(questions)}</b> ta.", reply_markup=kb.as_markup())


@admin.callback_query(NewTest.q_more, F.data == "more:yes")
async def nt_more(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.set_state(NewTest.q_text)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"❓ <b>{len(data['questions']) + 1}-savol</b> matnini kiriting:")
    await cb.answer()


@admin.callback_query(NewTest.q_more, F.data == "more:no")
async def nt_publish(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    code = await create_test(cb.from_user.id, data)
    await state.clear()
    await cb.message.edit_reply_markup(reply_markup=None)
    link = f"https://t.me/{BOT_USERNAME}?start={code}"
    await cb.message.answer(
        "🎉 <b>Test e'lon qilindi!</b>\n\n"
        f"📌 Nomi: <b>{esc(data['title'])}</b>\n"
        f"❓ Savollar: {len(data['questions'])} ta\n"
        f"⏱ Vaqt: {fmt_time(data['time_limit'])}\n"
        f"🎯 O'tish balli: {data['passing']}%\n\n"
        f"🔑 Test kodi: <code>{code}</code>\n"
        f"🔗 Havola: {link}"
    )
    await cb.answer()


# ---- Mening testlarim -----------------------------------------------------
@admin.callback_query(F.data == "adm:list")
async def adm_list(cb: CallbackQuery):
    rows = await fetch_all(
        "SELECT test_id, title, is_published FROM tests WHERE creator_id=? ORDER BY test_id DESC LIMIT 30",
        (cb.from_user.id,),
    )
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"{'🟢' if r['is_published'] else '⚪️'} {r['title'][:40]}", callback_data=f"adm:t:{r['test_id']}")
    kb.button(text="⬅️ Orqaga", callback_data="adm:menu")
    kb.adjust(1)
    text = "📋 <b>Sizning testlaringiz:</b>" if rows else "Hali test yaratmagansiz."
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()


async def test_detail(cb: CallbackQuery, test_id: int):
    t = await fetch_one("SELECT * FROM tests WHERE test_id=?", (test_id,))
    if not t:
        return await cb.answer("Test topilmadi", show_alert=True)
    qn = (await fetch_one("SELECT COUNT(*) c FROM questions WHERE test_id=?", (test_id,)))["c"]
    st = await fetch_one(
        "SELECT COUNT(*) c, AVG(score*100.0/total_questions) a, SUM(passed_status) p FROM results WHERE test_id=?",
        (test_id,),
    )
    avg = f"{st['a']:.1f}%" if st["a"] is not None else "—"
    kb = InlineKeyboardBuilder()
    kb.button(text="🔴 Yashirish" if t["is_published"] else "🟢 E'lon qilish", callback_data=f"adm:pub:{test_id}")
    kb.button(text="🗑 O'chirish", callback_data=f"adm:del:{test_id}")
    kb.button(text="⬅️ Orqaga", callback_data="adm:list")
    kb.adjust(2, 1)
    await cb.message.edit_text(
        f"📌 <b>{esc(t['title'])}</b>\n"
        f"🔑 Kod: <code>{t['code']}</code>\n"
        f"❓ Savollar: {qn} | ⏱ {fmt_time(t['time_limit'])} | 🎯 {t['passing_score']}%\n"
        f"👥 Topshirganlar: {st['c']} | O'tganlar: {st['p'] or 0} | O'rtacha: {avg}\n"
        f"Holat: {'🟢 e`lon qilingan' if t['is_published'] else '⚪️ yashirin'}",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@admin.callback_query(F.data.startswith("adm:t:"))
async def adm_test(cb: CallbackQuery):
    await test_detail(cb, int(cb.data.split(":")[2]))


@admin.callback_query(F.data.startswith("adm:pub:"))
async def adm_toggle(cb: CallbackQuery):
    tid = int(cb.data.split(":")[2])
    await execute("UPDATE tests SET is_published = 1 - is_published WHERE test_id=?", (tid,))
    await test_detail(cb, tid)


@admin.callback_query(F.data.startswith("adm:del:"))
async def adm_delete_ask(cb: CallbackQuery):
    tid = int(cb.data.split(":")[2])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha, o'chirish", callback_data=f"adm:delok:{tid}")
    kb.button(text="❌ Yo'q", callback_data=f"adm:t:{tid}")
    kb.adjust(2)
    await cb.message.edit_text("⚠️ Testni va uning barcha natijalarini o'chirmoqchimisiz?", reply_markup=kb.as_markup())
    await cb.answer()


@admin.callback_query(F.data.startswith("adm:delok:"))
async def adm_delete(cb: CallbackQuery):
    await delete_test(int(cb.data.split(":")[2]))
    await cb.message.edit_text("🗑 Test o'chirildi.", reply_markup=admin_menu_kb())
    await cb.answer()


@admin.callback_query(F.data == "adm:stats")
async def adm_stats(cb: CallbackQuery):
    u = (await fetch_one("SELECT COUNT(*) c FROM users"))["c"]
    t = (await fetch_one("SELECT COUNT(*) c FROM tests"))["c"]
    r = await fetch_one("SELECT COUNT(*) c, SUM(passed_status) p FROM results")
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Orqaga", callback_data="adm:menu")
    await cb.message.edit_text(
        f"📈 <b>Statistika</b>\n\n👤 Foydalanuvchilar: {u}\n📚 Testlar: {t}\n"
        f"📝 Topshirilgan testlar: {r['c']}\n✅ Muvaffaqiyatli: {r['p'] or 0}",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


# --------------------------------------------------------------------------
# USER: TEST SESSION
# --------------------------------------------------------------------------
@dataclass
class Session:
    chat_id: int
    user_id: int
    full_name: str
    test: aiosqlite.Row
    questions: list
    idx: int = 0
    correct: int = 0
    answered: bool = False
    message_id: int = 0
    started: float = field(default_factory=time.time)
    deadline: float | None = None
    timer: asyncio.Task | None = None


SESSIONS: dict[int, Session] = {}  # user_id -> Session (xotirada saqlanadi)


def render_question(s: Session, chosen: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    q = s.questions[s.idx]
    opts = json.loads(q["options_json"])
    correct = q["correct_option_index"]
    lines = [f"❓ <b>Savol {s.idx + 1}/{len(s.questions)}</b>"]
    if s.deadline:
        left = max(0, int(s.deadline - time.time()))
        lines.append(f"⏳ Qolgan vaqt: ~{left // 60} daq {left % 60} son")
    lines += ["", esc(q["question_text"]), ""]
    for j, o in enumerate(opts):
        mark = ""
        if chosen is not None:
            mark = "✅ " if j == correct else ("❌ " if j == chosen else "")
        lines.append(f"{mark}<b>{LETTERS[j]})</b> {esc(o)}")
    kb = InlineKeyboardBuilder()
    if chosen is None:
        for j in range(len(opts)):
            kb.button(text=LETTERS[j], callback_data=f"ans:{s.idx}:{j}")
        kb.adjust(len(opts))
    else:
        if q["explanation"]:
            lines += ["", f"💡 {esc(q['explanation'])}"]
        last = s.idx + 1 >= len(s.questions)
        kb.button(text="🏁 Natijani ko'rish" if last else "Keyingi ➡️", callback_data="next")
    return "\n".join(lines), kb.as_markup()


async def run_timer(bot: Bot, s: Session) -> None:
    await asyncio.sleep(s.test["time_limit"] * 60)
    if SESSIONS.get(s.user_id) is s:
        await finish_test(bot, s, reason="⏰ <b>Vaqt tugadi!</b>")


def certificate_html(s: Session, percent: float, passed: bool, wrong: int) -> str:
    status = "O'TDI ✅" if passed else "O'TMADI ❌"
    color = "#1b8a3a" if passed else "#b3261e"
    heading = "SERTIFIKAT" if passed else "NATIJA VARAQASI"
    return f"""<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8">
<title>{heading}</title>
<style>
body{{font-family:Georgia,serif;background:#f4f1ea;padding:30px}}
.card{{max-width:720px;margin:auto;background:#fff;border:8px double {color};padding:40px;text-align:center}}
h1{{letter-spacing:4px;color:{color}}} .name{{font-size:30px;margin:10px 0}}
table{{margin:20px auto;border-collapse:collapse}} td{{padding:6px 18px;border-bottom:1px solid #ddd;text-align:left}}
.status{{font-size:26px;font-weight:bold;color:{color};margin-top:20px}}
</style></head><body><div class="card">
<h1>{heading}</h1>
<p>Ushbu hujjat quyidagi shaxsga beriladi:</p>
<div class="name"><b>{esc(s.full_name)}</b></div>
<p>«{esc(s.test['title'])}» testi bo'yicha</p>
<table>
<tr><td>To'g'ri javoblar</td><td><b>{s.correct}</b></td></tr>
<tr><td>Noto'g'ri javoblar</td><td><b>{wrong}</b></td></tr>
<tr><td>Foiz</td><td><b>{percent}%</b></td></tr>
<tr><td>O'tish balli</td><td>{s.test['passing_score']}%</td></tr>
<tr><td>Sana</td><td>{now_str()}</td></tr>
</table>
<div class="status">{status}</div>
<p style="color:#888;font-size:12px">Bu hujjat namunaviy (simulyatsiya) hisoblanadi.</p>
</div></body></html>"""


async def finish_test(bot: Bot, s: Session, reason: str = "") -> None:
    if SESSIONS.get(s.user_id) is not s:
        return
    SESSIONS.pop(s.user_id, None)
    if s.timer and s.timer is not asyncio.current_task():
        s.timer.cancel()

    total = len(s.questions)
    wrong = total - s.correct
    percent = round(s.correct / total * 100, 1) if total else 0.0
    passed = percent >= s.test["passing_score"]
    await execute(
        """INSERT INTO results (telegram_id, test_id, score, total_questions, passed_status, completed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (s.user_id, s.test["test_id"], s.correct, total, int(passed), now_str()),
    )
    # Eski savoldagi tugmalarni olib tashlaymiz
    try:
        await bot.edit_message_reply_markup(s.chat_id, s.message_id, reply_markup=None)
    except Exception:
        pass

    spent = int(time.time() - s.started)
    text = (
        (reason + "\n\n" if reason else "")
        + f"🏁 <b>Test yakunlandi: {esc(s.test['title'])}</b>\n\n"
        f"✅ To'g'ri: <b>{s.correct}</b>\n"
        f"❌ Noto'g'ri: <b>{wrong}</b>\n"
        f"📊 Natija: <b>{percent}%</b> (o'tish balli: {s.test['passing_score']}%)\n"
        f"⏱ Sarflangan vaqt: {spent // 60} daq {spent % 60} son\n\n"
        + ("🎉 <b>Tabriklaymiz, siz testdan o'tdingiz!</b>" if passed else "😔 <b>Afsuski, testdan o'tmadingiz.</b>")
    )
    await bot.send_message(s.chat_id, text, reply_markup=main_menu())
    doc = BufferedInputFile(
        certificate_html(s, percent, passed, wrong).encode("utf-8"),
        filename=f"{'sertifikat' if passed else 'natija'}_{s.test['code']}.html",
    )
    await bot.send_document(
        s.chat_id, doc,
        caption="🎓 Sertifikatingiz" if passed else "📄 Natija varaqasi (brauzerda oching)",
    )


async def show_test_info(message: Message, t: aiosqlite.Row) -> None:
    qn = (await fetch_one("SELECT COUNT(*) c FROM questions WHERE test_id=?", (t["test_id"],)))["c"]
    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Boshlash", callback_data=f"go:{t['test_id']}")
    await message.answer(
        f"📌 <b>{esc(t['title'])}</b>\n"
        + (f"{esc(t['description'])}\n" if t["description"] else "")
        + f"\n❓ Savollar: {qn} ta\n⏱ Vaqt: {fmt_time(t['time_limit'])}\n🎯 O'tish balli: {t['passing_score']}%",
        reply_markup=kb.as_markup(),
    )


async def open_by_code(message: Message, code: str) -> bool:
    t = await fetch_one("SELECT * FROM tests WHERE code=? AND is_published=1", (code.upper(),))
    if not t:
        return False
    await show_test_info(message, t)
    return True


# ---- Handlers -------------------------------------------------------------
@user.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    await upsert_user(message.from_user)
    await message.answer(
        f"👋 Assalomu alaykum, <b>{esc(message.from_user.full_name)}</b>!\n\n"
        "Bu bot orqali testlar yechishingiz mumkin.\n"
        "🔑 Test <b>kodini</b> yuboring yoki ro'yxatdan tanlang.",
        reply_markup=main_menu(),
    )
    if command.args and not await open_by_code(message, command.args.strip()):
        await message.answer("❌ Bunday kodli test topilmadi.")


@user.message(Command("admin"))
async def cmd_admin_denied(message: Message):
    await message.answer("⛔️ Sizda admin huquqi yo'q.")


@user.message(F.text == BTN_HELP)
@user.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ <b>Yordam</b>\n\n"
        "• Test kodini (6 belgi) yuboring — test ochiladi.\n"
        f"• «{BTN_LIST}» — barcha ochiq testlar.\n"
        "• /stop — joriy testni tugatish.\n"
        "• /cancel — amalni bekor qilish.\n"
        "• Adminlar uchun: /admin"
    )


@user.message(F.text == BTN_LIST)
async def list_tests(message: Message):
    rows = await fetch_all("SELECT test_id, title FROM tests WHERE is_published=1 ORDER BY test_id DESC LIMIT 30")
    if not rows:
        return await message.answer("Hozircha testlar yo'q.")
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=r["title"][:50], callback_data=f"open:{r['test_id']}")
    kb.adjust(1)
    await message.answer("📚 <b>Mavjud testlar:</b>", reply_markup=kb.as_markup())


@user.callback_query(F.data.startswith("open:"))
async def cb_open(cb: CallbackQuery):
    t = await fetch_one("SELECT * FROM tests WHERE test_id=? AND is_published=1", (int(cb.data.split(":")[1]),))
    if not t:
        return await cb.answer("Test topilmadi", show_alert=True)
    await show_test_info(cb.message, t)
    await cb.answer()


@user.message(F.text == BTN_RESULTS)
async def my_results(message: Message):
    rows = await fetch_all(
        """SELECT r.*, t.title FROM results r JOIN tests t ON t.test_id=r.test_id
           WHERE r.telegram_id=? ORDER BY r.result_id DESC LIMIT 10""",
        (message.from_user.id,),
    )
    if not rows:
        return await message.answer("Siz hali test topshirmagansiz.")
    lines = ["📊 <b>So'nggi natijalaringiz:</b>\n"]
    for r in rows:
        pct = round(r["score"] / r["total_questions"] * 100, 1) if r["total_questions"] else 0
        lines.append(f"{'✅' if r['passed_status'] else '❌'} {esc(r['title'])} — {r['score']}/{r['total_questions']} ({pct}%)\n    🕒 {r['completed_at']}")
    await message.answer("\n".join(lines))


@user.callback_query(F.data.startswith("go:"))
async def cb_go(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    if uid in SESSIONS:
        return await cb.answer("Sizda tugallanmagan test bor. /stop bilan to'xtating.", show_alert=True)
    t = await fetch_one("SELECT * FROM tests WHERE test_id=? AND is_published=1", (int(cb.data.split(":")[1]),))
    if not t:
        return await cb.answer("Test topilmadi", show_alert=True)
    questions = list(await fetch_all("SELECT * FROM questions WHERE test_id=? ORDER BY position", (t["test_id"],)))
    if not questions:
        return await cb.answer("Bu testda savollar yo'q.", show_alert=True)
    await upsert_user(cb.from_user)

    s = Session(chat_id=cb.message.chat.id, user_id=uid, full_name=cb.from_user.full_name, test=t, questions=questions)
    if t["time_limit"]:
        s.deadline = time.time() + t["time_limit"] * 60
        s.timer = asyncio.create_task(run_timer(bot, s))
    SESSIONS[uid] = s

    await cb.message.edit_reply_markup(reply_markup=None)
    text, kb = render_question(s)
    msg = await cb.message.answer(text, reply_markup=kb)
    s.message_id = msg.message_id
    await cb.answer("Omad! 🍀")


@user.callback_query(F.data.startswith("ans:"))
async def cb_answer(cb: CallbackQuery, bot: Bot):
    s = SESSIONS.get(cb.from_user.id)
    _, qidx, opt = cb.data.split(":")
    if not s or int(qidx) != s.idx or s.answered:
        return await cb.answer("Bu savol allaqachon yopilgan.")
    if s.deadline and time.time() > s.deadline:
        await cb.answer()
        return await finish_test(bot, s, reason="⏰ <b>Vaqt tugadi!</b>")
    s.answered = True
    chosen = int(opt)
    if chosen == s.questions[s.idx]["correct_option_index"]:
        s.correct += 1
        await cb.answer("✅ To'g'ri!")
    else:
        await cb.answer("❌ Noto'g'ri")
    text, kb = render_question(s, chosen=chosen)
    await cb.message.edit_text(text, reply_markup=kb)


@user.callback_query(F.data == "next")
async def cb_next(cb: CallbackQuery, bot: Bot):
    s = SESSIONS.get(cb.from_user.id)
    if not s or not s.answered:
        return await cb.answer()
    await cb.answer()
    if s.idx + 1 >= len(s.questions):
        return await finish_test(bot, s)
    s.idx += 1
    s.answered = False
    text, kb = render_question(s)
    await cb.message.edit_text(text, reply_markup=kb)


@user.message(Command("stop"))
async def cmd_stop(message: Message, bot: Bot):
    s = SESSIONS.get(message.from_user.id)
    if not s:
        return await message.answer("Faol test yo'q.")
    await finish_test(bot, s, reason="🛑 Test to'xtatildi.")


@user.message(F.text)
async def any_text(message: Message):
    """Boshqa har qanday matn — test kodi sifatida qabul qilinadi."""
    code = message.text.strip()
    if re.fullmatch(r"[A-Za-z0-9]{6}", code) and await open_by_code(message, code):
        return
    await message.answer("❌ Test topilmadi. Kodni tekshiring yoki «📚 Testlar ro'yxati» ni oching.")


# --------------------------------------------------------------------------
# ENTRY POINT
# --------------------------------------------------------------------------
async def main() -> None:
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN .env faylida ko'rsatilmagan!")
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS bo'sh — hech kim test yarata olmaydi.")
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher()
    dp.include_routers(common, admin, user)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
