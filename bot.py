"""
Отжимайкин — Telegram-бот для тренировок отжиманий
Полный код с исправлениями:
- корректный user_id при нажатии "Начать" из уведомления
- часовые пояса (включая "New York")
- контекст диалога без повторов старых ответов
- правильный расчёт серии дней подряд (streak)
- исправлены отступы, вызывавшие IndentationError
"""

import asyncio
import logging
import sys
import json
import re
from datetime import datetime, date, time, timedelta, timezone
from typing import Union

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from sqlalchemy import (
    Column, BigInteger, String, Integer, Boolean,
    Date, Time, DateTime, ForeignKey, UniqueConstraint,
    func, select
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship

import aiohttp
from aiohttp import web
import pytz

import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PORT = int(os.getenv("PORT", "8080"))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ---------- DATABASE ----------
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    user_id = Column(BigInteger, primary_key=True)
    name = Column(String(100))
    max_reps = Column(Integer)
    timezone = Column(String(50), default="Europe/Moscow")
    reminder_time = Column(Time)                # UTC
    reminder_on = Column(Boolean, default=True)
    current_week = Column(Integer, default=1)
    current_reps_per_set = Column(Integer, default=10)
    rest_seconds = Column(Integer, default=90)
    pending_step = Column(Integer, default=0)
    last_reminder_date = Column(Date)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

    workouts = relationship("Workout", back_populates="user", lazy="selectin")
    achievements = relationship("Achievement", back_populates="user", lazy="selectin")

class Workout(Base):
    __tablename__ = "workouts"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    date = Column(Date, nullable=False)
    set1_reps = Column(Integer, default=0)
    set2_reps = Column(Integer, default=0)
    set3_reps = Column(Integer, default=0)
    completed = Column(Boolean, default=False)
    skipped = Column(Boolean, default=False)
    rest_day = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
    __table_args__ = (UniqueConstraint("user_id", "date", name="unique_user_date"),)
    user = relationship("User", back_populates="workouts")

class Achievement(Base):
    __tablename__ = "achievements"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(String(500))
    awarded_at = Column(DateTime, server_default=func.now())
    user = relationship("User", back_populates="achievements")

class DialogueHistory(Base):
    __tablename__ = "dialogue_history"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    role = Column(String(20), nullable=False)         # user / assistant
    message = Column(String(2000), nullable=False)
    timestamp = Column(DateTime, server_default=func.now())

# Правильный URL для asyncpg
_db_url = DATABASE_URL
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql+asyncpg://", 1)
elif _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(_db_url, echo=False, pool_size=5, max_overflow=10, pool_pre_ping=True, pool_recycle=3600)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized")

bot: Bot = None
dp: Dispatcher = None

# ---------- DEEPSEEK API ----------
SYSTEM_PROMPT = (
    "Ты — Отжимайкин, дружелюбный и мотивирующий фитнес-тренер для домашних отжиманий.\n"
    "Ты специализируешься на технике, восстановлении, питании для роста силы и мотивации.\n"
    "Твой стиль: лёгкий юмор, эмодзи, краткость (2-4 предложения).\n"
    "Если вопрос не по теме — мягко возвращаешь к тренировкам.\n"
    "Отвечай **только на последнее сообщение пользователя**. Не повторяй ответы из истории, "
    "если тебя об этом не просили. Используй эмодзи: 💪🔥🎉😊💥🏋️"
)

async def ask_deepseek(user_message: str, history: list[dict] = None, user_context: str = "") -> str:
    if not DEEPSEEK_API_KEY:
        return get_fallback_response(user_message)

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if user_context:
        messages.append({"role": "system", "content": user_context})
    if history:
        # Берём последние 4 сообщения, чтобы избежать старых зацикливаний
        messages.extend(history[-4:])
    messages.append({"role": "user", "content": user_message})

    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.9,
        "max_tokens": 300
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.deepseek.com/v1/chat/completions",
                json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")
    return get_fallback_response(user_message)

def get_fallback_response(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["боль", "болит"]):
        return "Мышечная боль — это нормально! 💪 Растяжка, тёплая ванна и белок помогут. Если боль острая — отдохни!"
    if "техник" in t:
        return "Держи тело прямым, руки на ширине плеч, локти прижимай к корпусу. Опускайся до прямого угла в локтях. 🎯"
    return "Я здесь чтобы помочь с тренировками! 💪 Спрашивай про технику, прогресс или восстановление."

# ---------- KEYBOARDS ----------
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏋️ Тренировка"), KeyboardButton(text="📊 Прогресс")],
            [KeyboardButton(text="💬 Общение"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="🏆 Достижения"), KeyboardButton(text="😴 Отдых")],
            [KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие..."
    )

def get_workout_keyboard(reps: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Сделал {reps} отжиманий", callback_data=f"done_{reps}")],
        [InlineKeyboardButton(text="📝 Написать свой результат", callback_data="custom_reps")]
    ])

def get_time_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌅 Утро (08:00)", callback_data="time_08:00")],
        [InlineKeyboardButton(text="☀️ День (12:30)", callback_data="time_12:30")],
        [InlineKeyboardButton(text="🌆 Вечер (19:00)", callback_data="time_19:00")],
        [InlineKeyboardButton(text="✏️ Своё время", callback_data="time_custom")]
    ])

def get_timezone_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Москва", callback_data="tz_Europe/Moscow")],
        [InlineKeyboardButton(text="🇷🇺 Екатеринбург", callback_data="tz_Asia/Yekaterinburg")],
        [InlineKeyboardButton(text="🇷🇺 Новосибирск", callback_data="tz_Asia/Novosibirsk")],
        [InlineKeyboardButton(text="🇪🇺 Берлин", callback_data="tz_Europe/Berlin")],
        [InlineKeyboardButton(text="🇬🇧 Лондон", callback_data="tz_Europe/London")],
        [InlineKeyboardButton(text="✏️ Написать город", callback_data="tz_custom")]
    ])

def get_rest_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="60 секунд", callback_data="rest_60")],
        [InlineKeyboardButton(text="90 секунд", callback_data="rest_90")],
        [InlineKeyboardButton(text="120 секунд", callback_data="rest_120")],
        [InlineKeyboardButton(text="180 секунд", callback_data="rest_180")]
    ])

def get_chat_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🏠 Выйти")]],
        resize_keyboard=True
    )

# ---------- UTILS ----------
# Маппинг популярных названий городов на pytz-строки
CITY_TZ_MAP = {
    "new york": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "moscow": "Europe/Moscow",
    "kiev": "Europe/Kiev",
    "minsk": "Europe/Minsk",
    "tokyo": "Asia/Tokyo",
    "beijing": "Asia/Shanghai",
    "sydney": "Australia/Sydney",
    "dubai": "Asia/Dubai",
    "istanbul": "Europe/Istanbul",
    "калининград": "Europe/Kaliningrad",
    "новосибирск": "Asia/Novosibirsk",
    "екатеринбург": "Asia/Yekaterinburg",
    "владивосток": "Asia/Vladivostok",
}

def normalize_timezone(user_input: str) -> str:
    """Пытается преобразовать строку в корректный pytz-часовой пояс."""
    stripped = user_input.strip()
    # Пробуем напрямую
    try:
        pytz.timezone(stripped)
        return stripped
    except pytz.UnknownTimeZoneError:
        pass
    # Пробуем с заменой пробелов на подчёркивания
    with_underscore = stripped.replace(" ", "_")
    try:
        pytz.timezone(with_underscore)
        return with_underscore
    except pytz.UnknownTimeZoneError:
        pass
    # Словарь популярных городов
    lower = stripped.lower()
    if lower in CITY_TZ_MAP:
        return CITY_TZ_MAP[lower]
    # Если ничего не подошло — возвращаем None (будет задан вопрос заново)
    return None

def normalize_time(user_input: str) -> time:
    user_input = user_input.strip().lower()
    match = re.search(r'(\d{1,2})[.:](\d{2})', user_input)
    if match:
        hours, minutes = int(match.group(1)), int(match.group(2))
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            return time(hours, minutes)
    time_map = {
        "утро": time(8, 0), "утром": time(8, 0),
        "день": time(12, 30), "днем": time(12, 30),
        "вечер": time(19, 0), "вечером": time(19, 0),
        "ночь": time(21, 0), "ночью": time(21, 0)
    }
    for key, val in time_map.items():
        if key in user_input:
            return val
    return time(8, 0)

def calculate_step_sets(base_reps: int) -> tuple[int, int, int]:
    if base_reps <= 10:
        return (base_reps, base_reps, max(3, base_reps - 2))
    elif base_reps < 40:
        return (base_reps, base_reps, max(5, base_reps - 5))
    else:
        return (base_reps, base_reps, max(int(base_reps * 0.8), base_reps - 10))

def to_utc(local_time: time, tz_str: str) -> time:
    try:
        tz = pytz.timezone(tz_str)
        local_dt = datetime.combine(date.today(), local_time)
        local_dt = tz.localize(local_dt)
        return local_dt.astimezone(pytz.UTC).time()
    except Exception:
        return local_time

def from_utc(utc_time: time, tz_str: str) -> time:
    try:
        tz = pytz.timezone(tz_str)
        utc_dt = datetime.combine(date.today(), utc_time).replace(tzinfo=pytz.UTC)
        return utc_dt.astimezone(tz).time()
    except Exception:
        return utc_time

def calculate_start_reps(max_reps: int) -> int:
    """Стартовая нагрузка: 80% от максимума, кратно 5, минимум 10."""
    return max(10, (int(max_reps * 0.8) // 5) * 5)

def calculate_weekly_progression(current_reps: int) -> int:
    return current_reps + 5

def calculate_decrease_reps(current_reps: int) -> int:
    return max(5, current_reps - 5)

# ---------- FSM ----------
class Onboarding(StatesGroup):
    waiting_for_max_reps = State()
    waiting_for_name = State()
    waiting_for_timezone = State()
    waiting_for_time = State()

class WorkoutSession(StatesGroup):
    waiting_for_set1 = State()
    waiting_for_set2 = State()
    waiting_for_set3 = State()
    waiting_for_feedback = State()

class Settings(StatesGroup):
    waiting_for_time = State()

class ChatMode(StatesGroup):
    chatting = State()

router = Router()

# ---------- ONBOARDING ----------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one_or_none()
        if user:
            set1, set2, set3 = calculate_step_sets(user.current_reps_per_set)
            await message.answer(
                f"С возвращением, {user.name}! 💪\n\n"
                f"📊 Неделя: {user.current_week}\n"
                f"📊 Нагрузка: {set1}-{set2}-{set3}\n"
                f"📊 Рекорд: {user.max_reps}\n\n"
                "Жми «🏋️ Тренировка»!",
                reply_markup=get_main_keyboard()
            )
            await state.clear()
            return

    await message.answer(
        "👋 Привет! Я <b>Отжимайкин</b> — твой персональный тренер по отжиманиям!\n\n"
        "💪 Вместе мы сделаем тебя сильнее день за днём.\n"
        "🎯 Я буду напоминать о тренировках, считать прогресс и поддерживать тебя.\n\n"
        "<i>Давай познакомимся! Ответь на несколько вопросов:</i>\n\n"
        "❓ <b>Сколько отжиманий ты можешь сделать за один подход на максимум?</b>\n"
        "Напиши число. Не стесняйся, я твой личный тренер 😉"
    )
    await state.set_state(Onboarding.waiting_for_max_reps)

@router.message(Onboarding.waiting_for_max_reps)
async def process_max_reps(message: Message, state: FSMContext):
    try:
        reps = int(message.text.strip())
        if reps < 1 or reps > 500:
            raise ValueError
        await state.update_data(max_reps=reps)
        await message.answer(
            f"🔥 {reps} отжиманий — отличный старт!\n\n"
            "❓ <b>Как мне к тебе обращаться?</b>\nНапиши своё имя или никнейм:"
        )
        await state.set_state(Onboarding.waiting_for_name)
    except ValueError:
        await message.answer("❌ Пожалуйста, введи целое число. Например: 15")

@router.message(Onboarding.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:50])
    await message.answer(
        f"Приятно познакомиться, <b>{message.text.strip()[:50]}</b>! 😊\n\n"
        "❓ <b>Теперь выбери свой часовой пояс:</b>\n"
        "Это нужно, чтобы напоминания приходили вовремя.",
        reply_markup=get_timezone_keyboard()
    )
    await state.set_state(Onboarding.waiting_for_timezone)

@router.callback_query(Onboarding.waiting_for_timezone)
async def process_timezone_callback(callback: CallbackQuery, state: FSMContext):
    if callback.data == "tz_custom":
        await callback.message.answer("Напиши название своего города. Например: «Калининград», «Asia/Tokyo»")
        await callback.answer()
        return
    tz_str = callback.data.replace("tz_", "")
    try:
        pytz.timezone(tz_str)
        await state.update_data(timezone=tz_str)
    except pytz.UnknownTimeZoneError:
        await callback.message.answer("❌ Неизвестный часовой пояс. Попробуй ещё раз или введи вручную.")
        await callback.answer()
        return
    await callback.message.answer(
        "✅ Часовой пояс установлен!\n\n"
        "❓ <b>Во сколько тебе удобно получать напоминания?</b>",
        reply_markup=get_time_keyboard()
    )
    await state.set_state(Onboarding.waiting_for_time)
    await callback.answer()

@router.message(Onboarding.waiting_for_timezone)
async def process_timezone_text(message: Message, state: FSMContext):
    tz_str = normalize_timezone(message.text)
    if tz_str is None:
        await message.answer(
            "😕 Не могу определить такой часовой пояс. Попробуй ввести в формате «Europe/Moscow» или просто «Москва»."
        )
        return
    await state.update_data(timezone=tz_str)
    await message.answer(
        f"✅ Часовой пояс установлен: {tz_str}\n\n"
        "❓ <b>Во сколько тебе удобно получать напоминания?</b>",
        reply_markup=get_time_keyboard()
    )
    await state.set_state(Onboarding.waiting_for_time)

@router.callback_query(Onboarding.waiting_for_time)
async def process_time_callback(callback: CallbackQuery, state: FSMContext):
    if callback.data == "time_custom":
        await callback.message.answer("Напиши время в формате ЧЧ:ММ, например 07:30 или 14:45")
        await callback.answer()
        return
    h, m = map(int, callback.data.replace("time_", "").split(":"))
    await finish_onboarding(callback.message, state, time(h, m), callback.from_user.id)
    await callback.answer()

@router.message(Onboarding.waiting_for_time)
async def process_time_text(message: Message, state: FSMContext):
    local_time = normalize_time(message.text)
    await finish_onboarding(message, state, local_time, message.from_user.id)

async def finish_onboarding(message: Message, state: FSMContext, local_time: time, user_id: int):
    data = await state.get_data()
    tz = data.get("timezone", "Europe/Moscow")
    start_reps = calculate_start_reps(data["max_reps"])
    set1, set2, set3 = calculate_step_sets(start_reps)

    async with async_session() as session:
        session.add(User(
            user_id=user_id,
            name=data["name"],
            max_reps=data["max_reps"],
            timezone=tz,
            reminder_time=to_utc(local_time, tz),
            current_reps_per_set=start_reps,
            current_week=1
        ))
        await session.commit()

    await message.answer(
        f"🎉 Отлично, <b>{data['name']}</b>! Регистрация завершена!\n\n"
        f"📊 <b>Твои данные:</b>\n"
        f"• Максимум отжиманий: {data['max_reps']}\n"
        f"• Программа: {set1}-{set2}-{set3} отжиманий\n"
        f"• Напоминание: {local_time.strftime('%H:%M')}\n"
        f"• Стартовая неделя программы\n\n"
        f"💪 <b>Почему такая нагрузка?</b>\n"
        "Я использую проверенную методику ступенчатых подходов!\n"
        f"Ты начнёшь с комфортных {set1}-{set2}-{set3}, и каждую неделю нагрузка будет расти.\n\n"
        "Готов? Жми «🏋️ Тренировка»!",
        reply_markup=get_main_keyboard()
    )
    await state.clear()

# ---------- WORKOUT HANDLERS ----------
@router.message(F.text == "🏋️ Тренировка")
@router.callback_query(F.data == "start_workout")
@router.message(Command("workout"))
async def start_workout_handler(event: Union[Message, CallbackQuery], state: FSMContext):
    if isinstance(event, CallbackQuery):
        message = event.message
        await event.answer()
    else:
        message = event

    user_id = event.from_user.id

    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one_or_none()
        if not user:
            await message.answer("Сначала давай познакомимся! Нажми /start")
            return

        today = date.today()
        workout = await session.execute(
            select(Workout).where(Workout.user_id == user_id, Workout.date == today)
        )
        workout = workout.scalar_one_or_none()

        if workout and workout.completed:
            await message.answer("✅ Ты уже молодец сегодня! Отдыхай до завтра.", reply_markup=get_main_keyboard())
            return

        if workout and workout.rest_day:
            workout.rest_day = False
            await message.answer("🔥 Отлично! Отменяю день отдыха — начинаем тренировку!\nОтдых не потрачен, используешь в другой раз 💪")

        if not workout:
            workout = Workout(user_id=user_id, date=today)
            session.add(workout)

        user.pending_step = 1
        await session.commit()

        set1, set2, set3 = calculate_step_sets(user.current_reps_per_set)

        await message.answer(
            f"🔥 <b>ВРЕМЯ ТРЕНИРОВКИ!</b>\n\n"
            f"<b>🎯 Разминка (обязательно!):</b>\n"
            f"• Вращение руками — 10 раз вперёд и 10 назад\n"
            f"• Круговые движения плечами — 5 раз в каждую сторону\n"
            f"• Разминка запястий — вращение кистями 10 секунд\n"
            f"• Наклоны корпуса в стороны — 5 раз\n\n"
            f"💪 <b>Подход 1 из 3:</b>\n"
            f"Сделай {set1} отжиманий и нажми кнопку!",
            reply_markup=get_workout_keyboard(set1)
        )
        await state.set_state(WorkoutSession.waiting_for_set1)
        await state.update_data(current_set=1, reps=[set1, set2, set3])

@router.callback_query(F.data == "custom_reps")
async def custom_reps_start(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("📝 Напиши, сколько отжиманий ты сделал в этом подходе:")

@router.message(WorkoutSession.waiting_for_set1, F.text.regexp(r'^\d+$'))
@router.message(WorkoutSession.waiting_for_set2, F.text.regexp(r'^\d+$'))
@router.message(WorkoutSession.waiting_for_set3, F.text.regexp(r'^\d+$'))
async def custom_reps_input(message: Message, state: FSMContext):
    try:
        done_reps = int(message.text.strip())
        if done_reps < 0:
            raise ValueError

        data = await state.get_data()
        current_set = data.get("current_set", 1)
        await state.update_data({f"set{current_set}_reps": done_reps})

        if current_set >= 3:
            await finish_workout(message, state, message.from_user.id)
        else:
            await process_set_complete(message, state, current_set, message.from_user.id)
    except ValueError:
        await message.answer("Пожалуйста, введи целое число отжиманий")

@router.callback_query(F.data.regexp(r'done_\d+'))
async def complete_set(callback: CallbackQuery, state: FSMContext):
    reps_done = int(callback.data.split("_")[1])
    data = await state.get_data()
    current_set = data.get("current_set", 1)

    await state.update_data({f"set{current_set}_reps": reps_done})
    await callback.answer("🔥 Отлично!")

    if current_set >= 3:
        await finish_workout(callback.message, state, callback.from_user.id)
    else:
        await process_set_complete(callback.message, state, current_set, callback.from_user.id)

async def process_set_complete(message: Message, state: FSMContext, current_set: int, user_id: int):
    next_set = current_set + 1

    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()

        data = await state.get_data()
        reps_array = data.get("reps", [user.current_reps_per_set]*3)
        if not isinstance(reps_array, list):
            reps_array = [reps_array] * 3

        rest_seconds = user.rest_seconds

        await state.update_data(current_set=next_set)
        user.pending_step = next_set
        await session.commit()

        set_state_next = getattr(WorkoutSession, f"waiting_for_set{next_set}")
        await state.set_state(set_state_next)

        if next_set == 2:
            await message.answer(
                "✅ Первый подход сделан!\n\n"
                "<b>🧘 Заминка:</b>\n"
                "• Встряхни руки, расслабь плечи\n"
                "• Сделай 2-3 глубоких вдоха\n\n"
                f"😌 <b>Отдыхай {rest_seconds} секунд</b> 💤"
            )
        else:
            await message.answer(f"😌 <b>Отдыхай {rest_seconds} секунд</b> 💤")

        await asyncio.sleep(rest_seconds)

        next_reps = reps_array[next_set - 1]
        await message.answer(
            f"⏰ <b>Подход {next_set} из 3:</b>\n"
            f"Сделай {next_reps} отжиманий!",
            reply_markup=get_workout_keyboard(next_reps)
        )

async def finish_workout(message: Message, state: FSMContext, user_id: int):
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()
        today = date.today()
        workout = await session.execute(
            select(Workout).where(Workout.user_id == user_id, Workout.date == today)
        )
        workout = workout.scalar_one()

        data = await state.get_data()
        workout.set1_reps = data.get("set1_reps", 0)
        workout.set2_reps = data.get("set2_reps", 0)
        workout.set3_reps = data.get("set3_reps", 0)
        workout.completed = True
        user.pending_step = 0

        await check_achievements(user_id, session)
        await session.commit()

        total = workout.set1_reps + workout.set2_reps + workout.set3_reps

    await state.set_state(WorkoutSession.waiting_for_feedback)
    await message.answer(
        f"🎉 <b>ТРЕНИРОВКА ЗАВЕРШЕНА!</b>\n\n"
        f"📊 Твои результаты:\n"
        f"• Подход 1: {workout.set1_reps} отж.\n"
        f"• Подход 2: {workout.set2_reps} отж.\n"
        f"• Подход 3: {workout.set3_reps} отж.\n"
        f"• <b>Всего: {total} отжиманий</b> 🔥\n\n"
        f"<b>🧘 Не забудь заминку:</b>\n"
        f"• Растяни грудные мышцы и трицепс\n"
        f"• Сделай наклоны вперёд\n"
        f"• Выпей воды!\n\n"
        f"💭 <b>Как прошла тренировка?</b>\n"
        "Напиши: всё сделал, были сложности или просто поделись ощущениями.",
        reply_markup=ReplyKeyboardRemove()
    )

# ---------- FEEDBACK ----------
@router.message(WorkoutSession.waiting_for_feedback)
async def process_workout_feedback(message: Message, state: FSMContext):
    user_id = message.from_user.id
    async with async_session() as session:
        hist = await session.execute(
            select(DialogueHistory).where(DialogueHistory.user_id == user_id)
            .order_by(DialogueHistory.timestamp.desc()).limit(4)
        )
        history = [{"role": h.role, "content": h.message} for h in hist.scalars().all()]
        history.reverse()

        ai_response = await ask_deepseek(message.text, history)
        session.add(DialogueHistory(user_id=user_id, role="user", message=message.text))
        session.add(DialogueHistory(user_id=user_id, role="assistant", message=ai_response))
        await session.commit()

    await message.answer(ai_response, reply_markup=get_main_keyboard())
    await state.clear()

# ---------- CHAT MODE ----------
@router.message(F.text == "💬 Общение")
async def start_chat_mode(message: Message, state: FSMContext):
    user_id = message.from_user.id
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one_or_none()
        if not user:
            await message.answer("Сначала /start")
            return

    await state.set_state(ChatMode.chatting)
    await message.answer(
        f"💬 <b>Режим общения с тренером активирован!</b>\n\n"
        f"Привет, {user.name}! Я Отжимайкин 🏋️\n"
        "Спрашивай что угодно про тренировки:\n"
        "• Техника отжиманий\n"
        "• Боль в мышцах после нагрузки\n"
        "• Как увеличить количество повторений\n"
        "• Питание и восстановление\n"
        "• Или просто поболтаем о спорте!\n\n"
        "Для выхода нажми кнопку «🏠 Выйти»",
        reply_markup=get_chat_keyboard()
    )

@router.message(ChatMode.chatting, F.text == "🏠 Выйти")
async def exit_chat_mode(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("💪 Возвращаемся к тренировкам!", reply_markup=get_main_keyboard())

@router.message(ChatMode.chatting)
async def chat_with_trainer(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await bot.send_chat_action(chat_id=user_id, action="typing")

    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()

        last_workout = await session.execute(
            select(Workout).where(Workout.user_id == user_id, Workout.completed == True)
            .order_by(Workout.date.desc()).limit(1)
        )
        last_w = last_workout.scalar_one_or_none()

        context = (
            f"Пользователь: {user.name}, нагрузка: {user.current_reps_per_set} отж/подход, "
            f"неделя: {user.current_week}, рекорд: {user.max_reps}."
        )
        if last_w:
            context += f" Последняя тренировка: {last_w.date.strftime('%d.%m')}: {last_w.set1_reps}-{last_w.set2_reps}-{last_w.set3_reps}."

        hist = await session.execute(
            select(DialogueHistory).where(DialogueHistory.user_id == user_id)
            .order_by(DialogueHistory.timestamp.desc()).limit(6)
        )
        history = [{"role": h.role, "content": h.message} for h in hist.scalars().all()]
        history.reverse()

        ai_response = await ask_deepseek(message.text, history, context)
        session.add(DialogueHistory(user_id=user_id, role="user", message=message.text))
        session.add(DialogueHistory(user_id=user_id, role="assistant", message=ai_response))
        await session.commit()

    await message.answer(ai_response, reply_markup=get_chat_keyboard())

# ---------- PROGRESS & ACHIEVEMENTS ----------
@router.message(F.text == "📊 Прогресс")
@router.message(Command("progress"))
async def show_progress(message: Message):
    user_id = message.from_user.id
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one_or_none()
        if not user:
            await message.answer("Сначала /start")
            return

        week_ago = date.today() - timedelta(days=7)
        workouts = await session.execute(
            select(Workout).where(Workout.user_id == user_id, Workout.date >= week_ago)
            .order_by(Workout.date.desc())
        )
        workouts = workouts.scalars().all()

        completed = sum(1 for w in workouts if w.completed)
        total_reps = sum(w.set1_reps + w.set2_reps + w.set3_reps for w in workouts if w.completed)

        # Новый расчёт серии дней подряд
        all_completed = await session.execute(
            select(Workout.date).where(
                Workout.user_id == user_id,
                Workout.completed == True,
                Workout.date >= date.today() - timedelta(days=30)
            ).order_by(Workout.date.desc())
        )
        completed_days = [row[0] for row in all_completed.all()]
        streak = 0
        if completed_days:
            last_date = completed_days[0]
            if last_date >= date.today() - timedelta(days=1):
                streak = 1
                check = last_date - timedelta(days=1)
                for d in completed_days[1:]:
                    if d == check:
                        streak += 1
                        check -= timedelta(days=1)
                    else:
                        break

        set1, set2, set3 = calculate_step_sets(user.current_reps_per_set)
        await message.answer(
            f"📊 <b>ПРОГРЕСС {user.name}</b>\n\n"
            f"🏆 Неделя: {user.current_week}\n"
            f"💪 Нагрузка: {set1}-{set2}-{set3}\n"
            f"⭐ Рекорд: {user.max_reps}\n\n"
            f"📈 Эта неделя: {completed}/7 тренировок\n"
            f"🔥 Всего отжиманий: {total_reps}\n"
            f"📅 Дней подряд: {streak}\n\n"
            "<i>Продолжай в том же духе!</i>",
            reply_markup=get_main_keyboard()
        )

@router.message(F.text == "🏆 Достижения")
async def show_achievements(message: Message):
    user_id = message.from_user.id
    async with async_session() as session:
        ach_list = await session.execute(select(Achievement).where(Achievement.user_id == user_id))
        ach_list = ach_list.scalars().all()
        if not ach_list:
            await message.answer("🎯 Пока нет достижений. Начни тренироваться!", reply_markup=get_main_keyboard())
            return
        text = "\n".join(f"• {a.title} — {a.awarded_at.strftime('%d.%m.%Y')}" for a in ach_list)
        await message.answer(f"🏆 <b>ДОСТИЖЕНИЯ</b>\n\n{text}", reply_markup=get_main_keyboard())

async def check_achievements(user_id: int, session: AsyncSession):
    existing = set((await session.execute(
        select(Achievement.title).where(Achievement.user_id == user_id)
    )).scalars().all())

    total = (await session.execute(
        select(func.count(Workout.id)).where(Workout.user_id == user_id, Workout.completed == True)
    )).scalar()

    if "first_workout" not in existing and total >= 1:
        session.add(Achievement(user_id=user_id, title="💪 Первая тренировка", description="Начало пути!"))

    streak = await calculate_streak(user_id, session)
    if streak >= 5 and "streak_5" not in existing:
        session.add(Achievement(user_id=user_id, title="🔥 5 дней подряд", description="Дисциплина!"))
    if streak >= 10 and "streak_10" not in existing:
        session.add(Achievement(user_id=user_id, title="💎 10 дней подряд", description="Железная воля!"))

async def calculate_streak(user_id: int, session: AsyncSession) -> int:
    """Возвращает текущую серию дней с выполненными тренировками."""
    completed = (await session.execute(
        select(Workout.date).where(
            Workout.user_id == user_id,
            Workout.completed == True
        ).order_by(Workout.date.desc())
    )).scalars().all()
    
    if not completed:
        return 0
    
    streak = 0
    last_date = completed[0]
    # Серия жива, если последняя тренировка была не раньше вчера
    if last_date >= date.today() - timedelta(days=1):
        streak = 1
        check = last_date - timedelta(days=1)
        for d in completed[1:]:
            if d == check:
                streak += 1
                check -= timedelta(days=1)
            else:
                break
    return streak

# ---------- SETTINGS ----------
@router.message(F.text == "⚙️ Настройки")
@router.message(Command("settings"))
async def show_settings(message: Message):
    user_id = message.from_user.id
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one_or_none()
        if not user:
            await message.answer("Сначала /start")
            return

        set1, set2, set3 = calculate_step_sets(user.current_reps_per_set)
        local_time = from_utc(user.reminder_time, user.timezone)

        await message.answer(
            f"⚙️ <b>НАСТРОЙКИ</b>\n\n"
            f"👤 {user.name}\n"
            f"🕐 {local_time.strftime('%H:%M')}\n"
            f"⏱ Отдых: {user.rest_seconds} сек\n"
            f"💪 {set1}-{set2}-{set3}\n"
            f"🔔 {'Вкл' if user.reminder_on else 'Выкл'}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🕐 Время", callback_data="change_time")],
                [InlineKeyboardButton(text="🔔 Напоминания", callback_data="toggle_remind")],
                [InlineKeyboardButton(text="🔧 Отдых", callback_data="change_rest")],
                [InlineKeyboardButton(text="⬆️ Сложнее", callback_data="increase_difficulty")],
                [InlineKeyboardButton(text="⬇️ Легче", callback_data="decrease_difficulty")]
            ])
        )

@router.callback_query(F.data == "increase_difficulty")
async def increase_difficulty(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()
        user.current_reps_per_set = calculate_weekly_progression(user.current_reps_per_set)
        await session.commit()
        set1, set2, set3 = calculate_step_sets(user.current_reps_per_set)
    await callback.answer("✅")
    await callback.message.answer(f"⬆️ Нагрузка: {set1}-{set2}-{set3}", reply_markup=get_main_keyboard())

@router.callback_query(F.data == "decrease_difficulty")
async def decrease_difficulty(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()
        new_reps = calculate_decrease_reps(user.current_reps_per_set)
        if new_reps == user.current_reps_per_set:
            await callback.answer("⚠️ Минимум!")
            return
        user.current_reps_per_set = new_reps
        await session.commit()
        set1, set2, set3 = calculate_step_sets(new_reps)
    await callback.answer("✅")
    await callback.message.answer(f"⬇️ Нагрузка: {set1}-{set2}-{set3}", reply_markup=get_main_keyboard())

@router.callback_query(F.data == "change_rest")
async def change_rest(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("⏱ Выбери отдых:", reply_markup=get_rest_keyboard())

@router.callback_query(F.data.regexp(r'rest_\d+'))
async def process_rest_change(callback: CallbackQuery):
    sec = int(callback.data.split("_")[1])
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == callback.from_user.id))
        user = user.scalar_one()
        user.rest_seconds = sec
        await session.commit()
    await callback.answer(f"✅ {sec} сек")
    await callback.message.answer(f"⏱ Отдых: {sec} сек", reply_markup=get_main_keyboard())

@router.callback_query(F.data == "change_time")
async def change_time(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("🕐 Выбери время:", reply_markup=get_time_keyboard())
    await state.set_state(Settings.waiting_for_time)

@router.callback_query(Settings.waiting_for_time)
async def process_new_time(callback: CallbackQuery, state: FSMContext):
    if callback.data == "time_custom":
        await callback.message.answer("Напиши время в формате ЧЧ:ММ")
        await callback.answer()
        return
    h, m = map(int, callback.data.replace("time_", "").split(":"))
    await save_new_time(callback.message, state, time(h, m), callback.from_user.id)
    await callback.answer()

@router.message(Settings.waiting_for_time)
async def process_custom_time(message: Message, state: FSMContext):
    await save_new_time(message, state, normalize_time(message.text), message.from_user.id)

async def save_new_time(message: Message, state: FSMContext, local_time: time, user_id: int):
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()
        user.reminder_time = to_utc(local_time, user.timezone)
        await session.commit()
    await message.answer(f"✅ Время: {local_time.strftime('%H:%M')}", reply_markup=get_main_keyboard())
    await state.clear()

@router.callback_query(F.data == "toggle_remind")
async def toggle_reminders(callback: CallbackQuery):
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == callback.from_user.id))
        user = user.scalar_one()
        user.reminder_on = not user.reminder_on
        await session.commit()
        s = "вкл" if user.reminder_on else "выкл"
    await callback.answer(f"🔔 {s}!")
    await callback.message.answer(f"🔔 Напоминания {s}!", reply_markup=get_main_keyboard())

# ---------- REST & HELP ----------
@router.message(F.text == "😴 Отдых")
@router.message(Command("restday"))
async def rest_day(message: Message):
    user_id = message.from_user.id
    today = date.today()
    async with async_session() as session:
        count = (await session.execute(
            select(func.count(Workout.id)).where(
                Workout.user_id == user_id,
                Workout.rest_day == True,
                Workout.date >= today - timedelta(days=10)
            )
        )).scalar()
        if count:
            await message.answer("😴 Уже отдыхал недавно (раз в 10 дней).", reply_markup=get_main_keyboard())
            return

        existing = await session.execute(
            select(Workout).where(Workout.user_id == user_id, Workout.date == today)
        )
        existing = existing.scalar_one_or_none()
        if existing:
            existing.rest_day = True
        else:
            session.add(Workout(user_id=user_id, date=today, rest_day=True))
        await session.commit()

    await message.answer(
        "😴 <b>День отдыха активирован!</b>\n\n"
        "Восстановление — важная часть прогресса.\n"
        "Если захочешь потренироваться — просто нажми «🏋️ Тренировка».\n"
        "Возвращайся с новыми силами! 💪",
        reply_markup=get_main_keyboard()
    )

@router.message(F.text == "❓ Помощь")
@router.message(Command("help"))
async def show_help(message: Message):
    await message.answer(
        "🤖 <b>ОТЖИМАЙКИН</b>\n\n"
        "🏋️ /workout — тренировка\n"
        "📊 /progress — прогресс\n"
        "💬 Общение — чат с ИИ\n"
        "⚙️ /settings — настройки\n"
        "😴 /restday — отдых\n\n"
        "💡 3 ступенчатых подхода, +5 в неделю, ачивки!",
        reply_markup=get_main_keyboard()
    )

# ---------- BACKGROUND TASKS ----------
async def reminder_checker():
    logger.info("Reminder checker started")
    while True:
        try:
            now = datetime.now(timezone.utc)
            async with async_session() as session:
                users = (await session.execute(
                    select(User).where(User.reminder_on == True)
                )).scalars().all()
                for user in users:
                    try:
                        if (now.hour == user.reminder_time.hour
                            and now.minute == user.reminder_time.minute
                            and user.last_reminder_date != now.date()):
                            set1, set2, set3 = calculate_step_sets(user.current_reps_per_set)
                            await bot.send_message(
                                user.user_id,
                                f"🔔 {user.name}, время размять плечи!\n\n"
                                f"Сегодня: {set1}-{set2}-{set3} отжиманий.\nЖду тебя! 🔥",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                    InlineKeyboardButton(text="🏋️ Начать", callback_data="start_workout")
                                ]])
                            )
                            user.last_reminder_date = now.date()
                            await session.commit()
                    except Exception as e:
                        logger.error(f"Reminder error {user.user_id}: {e}")
        except Exception as e:
            logger.error(f"Checker error: {e}")
        await asyncio.sleep(60)

async def weekly_reports():
    logger.info("Weekly reports started")
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.weekday() == 6 and now.hour == 20 and now.minute == 0:
                async with async_session() as session:
                    users = (await session.execute(select(User))).scalars().all()
                    for user in users:
                        try:
                            workouts = (await session.execute(
                                select(Workout).where(
                                    Workout.user_id == user.user_id,
                                    Workout.date >= now.date() - timedelta(days=7)
                                )
                            )).scalars().all()
                            completed = sum(1 for w in workouts if w.completed)
                            total = sum(w.set1_reps + w.set2_reps + w.set3_reps for w in workouts if w.completed)
                            if completed >= 6:
                                user.current_reps_per_set = calculate_weekly_progression(user.current_reps_per_set)
                                user.current_week += 1
                                set1, set2, set3 = calculate_step_sets(user.current_reps_per_set)
                                await bot.send_message(user.user_id,
                                    f"📊 Неделя {user.current_week-1} завершена!\n"
                                    f"Тренировок: {completed}/7, отжиманий: {total}\n\n"
                                    f"🎉 Повышаю до {set1}-{set2}-{set3}!")
                            else:
                                await bot.send_message(user.user_id,
                                    f"📊 Неделя завершена!\n"
                                    f"Тренировок: {completed}/7\n"
                                    f"Продолжаем с {user.current_reps_per_set} 💪")
                            await session.commit()
                        except Exception as e:
                            logger.error(f"Report error {user.user_id}: {e}")
        except Exception as e:
            logger.error(f"Weekly error: {e}")
        await asyncio.sleep(60)

async def keep_alive_self_ping():
    await asyncio.sleep(30)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{WEBHOOK_URL}/health", timeout=10) as r:
                    if r.status == 200:
                        logger.debug("Ping OK")
        except:
            pass
        await asyncio.sleep(840)

# ---------- WEB ----------
async def health_check(request):
    return web.Response(text=json.dumps({
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "PushUp Bot"
    }), content_type="application/json")

# ---------- MAIN ----------
async def main():
    global bot, dp
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    app = web.Application()
    app.router.add_get("/health", health_check)
    app.router.add_get("/", health_check)

    handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

    await init_db()
    asyncio.create_task(reminder_checker())
    asyncio.create_task(weekly_reports())
    asyncio.create_task(keep_alive_self_ping())

    try:
        await bot.set_webhook(f"{WEBHOOK_URL}/webhook", drop_pending_updates=True)
        logger.info(f"Webhook set to {WEBHOOK_URL}/webhook")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")

    logger.info(f"Bot running on port {PORT}")
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await bot.delete_webhook()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
