"""
Отжимайкин — Telegram-бот для тренировок отжиманий
Полный код в одном файле с интеграцией DeepSeek API
"""

import asyncio
import logging
import sys
import json
import re
from datetime import datetime, date, time, timedelta, timezone
from typing import Optional, List, Dict, Any

# Aiogram imports
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

# SQLAlchemy imports
from sqlalchemy import (
    Column, BigInteger, String, Integer, Boolean, 
    Date, Time, DateTime, ForeignKey, UniqueConstraint,
    func, select, update, text
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship

# HTTP and external
import aiohttp
from aiohttp import web
import pytz

# ============ КОНФИГУРАЦИЯ ============

import os
from dotenv import load_dotenv

load_dotenv()

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PORT = int(os.getenv("PORT", "8080"))

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ============ DATABASE SETUP ============

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    
    user_id = Column(BigInteger, primary_key=True)
    name = Column(String(100), nullable=True)
    max_reps = Column(Integer, nullable=True)
    timezone = Column(String(50), default="Europe/Moscow")
    reminder_time = Column(Time, nullable=True)
    reminder_on = Column(Boolean, default=True)
    current_week = Column(Integer, default=1)
    current_reps_per_set = Column(Integer, default=10)
    rest_seconds = Column(Integer, default=90)
    pending_step = Column(Integer, default=0)
    next_step_time = Column(DateTime, nullable=True)
    last_reminder_date = Column(Date, nullable=True)
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
    
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="unique_user_date"),
    )
    
    user = relationship("User", back_populates="workouts")

class Achievement(Base):
    __tablename__ = "achievements"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(String(500), nullable=True)
    awarded_at = Column(DateTime, server_default=func.now())
    
    user = relationship("User", back_populates="achievements")

class DialogueHistory(Base):
    __tablename__ = "dialogue_history"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    role = Column(String(20), nullable=False)
    message = Column(String(2000), nullable=False)
    timestamp = Column(DateTime, server_default=func.now())

# Создаем engine и session
_db_url = DATABASE_URL
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql+asyncpg://", 1)

engine = create_async_engine(
    _db_url,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=3600
)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def init_db():
    """Инициализация базы данных"""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        raise

# ============ Глобальные переменные ============
bot = None
dp = None

# ============ DEEPSEEK INTEGRATION ============

SYSTEM_PROMPT = """Ты — Отжимайкин, дружелюбный и мотивирующий фитнес-тренер для домашних отжиманий.
Ты всегда поддерживаешь, хвалишь за успехи, мягко подбадриваешь при неудачах.
Твой стиль: лёгкий юмор, эмодзи, краткость (1-3 предложения).
Никогда не критикуй, не дави. Если пользователь пропустил тренировку — поддержи.
Используй эмодзи: 💪🔥🎉😊💥"""

async def ask_deepseek(user_message: str, history=None) -> str:
    """Отправка запроса к DeepSeek API"""
    if not DEEPSEEK_API_KEY:
        return "💪 Продолжай в том же духе!"
    
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    if history:
        messages.extend(history[-6:])
    
    messages.append({"role": "user", "content": user_message})
    
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": 200
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.deepseek.com/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"DeepSeek API error: {e}")
    
    return "💪 Отличная работа! Продолжай тренироваться!"

# ============ KEYBOARDS ============

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏋️ Тренировка"), KeyboardButton(text="📊 Прогресс")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="❓ Помощь")],
            [KeyboardButton(text="🏆 Достижения"), KeyboardButton(text="😴 Отдых")]
        ],
        resize_keyboard=True
    )

def get_workout_keyboard(reps: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Сделал {reps} отжиманий", callback_data=f"done_{reps}")],
        [InlineKeyboardButton(text="📝 Написать свой результат", callback_data="custom_reps")],
        [InlineKeyboardButton(text="⏭ Пропустить подход", callback_data="skip_set")]
    ])

def get_time_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌅 Утро (08:00)", callback_data="time_08:00")],
        [InlineKeyboardButton(text="☀️ День (12:30)", callback_data="time_12:30")],
        [InlineKeyboardButton(text="🌆 Вечер (19:00)", callback_data="time_19:00")],
        [InlineKeyboardButton(text="✏️ Написать своё время", callback_data="time_custom")]
    ])

def get_timezone_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Москва (MSK)", callback_data="tz_Europe/Moscow")],
        [InlineKeyboardButton(text="🇷🇺 Екатеринбург (+2)", callback_data="tz_Asia/Yekaterinburg")],
        [InlineKeyboardButton(text="🇷🇺 Новосибирск (+4)", callback_data="tz_Asia/Novosibirsk")],
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

# ============ UTILITY FUNCTIONS ============

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
    
def calculate_step_sets(base_reps: int) -> tuple:
    if base_reps <= 10:
        return (base_reps, base_reps, max(3, base_reps - 2))
    elif base_reps < 40:
        return (base_reps, base_reps, max(5, base_reps - 5))
    else:
        return (base_reps, base_reps, max(int(base_reps * 0.8), base_reps - 10))
        
def to_utc(local_time: time, tz_str: str) -> time:
    try:
        tz = pytz.timezone(tz_str)
        today = date.today()
        local_dt = datetime.combine(today, local_time)
        local_dt = tz.localize(local_dt)
        utc_dt = local_dt.astimezone(pytz.UTC)
        return utc_dt.time()
    except Exception as e:
        logger.error(f"Timezone conversion error: {e}")
        return local_time

def from_utc(utc_time: time, tz_str: str) -> time:
    try:
        tz = pytz.timezone(tz_str)
        today = date.today()
        utc_dt = datetime.combine(today, utc_time).replace(tzinfo=pytz.UTC)
        local_dt = utc_dt.astimezone(tz)
        return local_dt.time()
    except:
        return utc_time

def calculate_start_reps(max_reps: int) -> int:
    start = max(5, int(max_reps * 0.8))
    return (start // 5) * 5

def calculate_weekly_progression(current_reps: int) -> int:
    return current_reps + 5

def calculate_decrease_reps(current_reps: int) -> int:
    """Понижение на 5, но не менее 5"""
    return max(5, current_reps - 5)

# ============ FSM STATES ============

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
    waiting_for_timezone = State()

# ============ HANDLERS ============

router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    async with async_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()
        
        if user:
            set1, set2, set3 = calculate_step_sets(user.current_reps_per_set)
            welcome_back = (
                f"С возвращением, {user.name}! 💪\n\n"
                f"📊 Твой прогресс:\n"
                f"• Неделя: {user.current_week}\n"
                f"• Нагрузка: {set1}-{set2}-{set3} отжиманий\n"
                f"• Лучший результат: {user.max_reps}\n\n"
                f"Готов тренироваться? Жми «🏋️ Тренировка»!"
            )
            await message.answer(welcome_back, reply_markup=get_main_keyboard())
            await state.clear()
            return
    
    welcome_text = (
        "👋 Привет! Я <b>Отжимайкин</b> — твой персональный тренер по отжиманиям!\n\n"
        "💪 Вместе мы сделаем тебя сильнее день за днём.\n"
        "🎯 Я буду напоминать о тренировках, считать прогресс и поддерживать тебя.\n\n"
        "<i>Давай познакомимся! Ответь на несколько вопросов:</i>\n\n"
        "❓ <b>Сколько отжиманий ты можешь сделать за один подход на максимум?</b>\n"
        "Напиши число. Не стесняйся, я твой личный тренер 😉"
    )
    
    await message.answer(welcome_text)
    await state.set_state(Onboarding.waiting_for_max_reps)

@router.message(Onboarding.waiting_for_max_reps)
async def process_max_reps(message: Message, state: FSMContext):
    try:
        reps = int(message.text.strip())
        if reps < 0 or reps > 500:
            raise ValueError
        
        await state.update_data(max_reps=reps)
        
        await message.answer(
            f"🔥 {reps} отжиманий — отличный старт!\n\n"
            f"❓ <b>Как мне к тебе обращаться?</b>\n"
            f"Напиши своё имя или никнейм:"
        )
        await state.set_state(Onboarding.waiting_for_name)
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введи целое число. Например: 10, 15, 20")

@router.message(Onboarding.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    name = message.text.strip()[:50]
    await state.update_data(name=name)
    
    await message.answer(
        f"Приятно познакомиться, <b>{name}</b>! 😊\n\n"
        f"❓ <b>Теперь выбери свой часовой пояс:</b>\n"
        f"Это нужно, чтобы напоминания приходили вовремя.",
        reply_markup=get_timezone_keyboard()
    )
    await state.set_state(Onboarding.waiting_for_timezone)

@router.callback_query(Onboarding.waiting_for_timezone)
async def process_timezone_callback(callback: CallbackQuery, state: FSMContext):
    tz_data = callback.data
    
    if tz_data == "tz_custom":
        await callback.message.answer(
            "Напиши название своего города или часового пояса.\n"
            "Например: «Калининград», «Asia/Tokyo»"
        )
        await callback.answer()
        return
    
    tz_str = tz_data.replace("tz_", "")
    
    try:
        pytz.timezone(tz_str)
        await state.update_data(timezone=tz_str)
        
        await callback.message.answer(
            f"✅ Часовой пояс установлен!\n\n"
            f"❓ <b>Во сколько тебе удобно получать напоминания о тренировке?</b>",
            reply_markup=get_time_keyboard()
        )
        await state.set_state(Onboarding.waiting_for_time)
    except:
        await callback.message.answer("❌ Ошибка. Попробуй ещё раз или напиши город.")
    
    await callback.answer()

@router.message(Onboarding.waiting_for_timezone)
async def process_timezone_text(message: Message, state: FSMContext):
    tz_input = message.text.strip()
    
    try:
        pytz.timezone(tz_input)
        tz_str = tz_input
    except:
        tz_str = "Europe/Moscow"
    
    await state.update_data(timezone=tz_str)
    
    await message.answer(
        f"✅ Часовой пояс установлен: {tz_str}\n\n"
        f"❓ <b>Во сколько тебе удобно получать напоминания о тренировке?</b>",
        reply_markup=get_time_keyboard()
    )
    await state.set_state(Onboarding.waiting_for_time)

@router.callback_query(Onboarding.waiting_for_time)
async def process_time_callback(callback: CallbackQuery, state: FSMContext):
    time_data = callback.data
    
    if time_data == "time_custom":
        await callback.message.answer("Напиши время в формате ЧЧ:ММ\nНапример: 07:30 или 14:45")
        await callback.answer()
        return
    
    time_str = time_data.replace("time_", "")
    hours, minutes = map(int, time_str.split(":"))
    local_time = time(hours, minutes)
    
    await finish_onboarding(callback.message, state, local_time, callback.from_user.id)
    await callback.answer()

@router.message(Onboarding.waiting_for_time)
async def process_time_text(message: Message, state: FSMContext):
    local_time = normalize_time(message.text.strip())
    await finish_onboarding(message, state, local_time, message.from_user.id)

async def finish_onboarding(message: Message, state: FSMContext, local_time: time, user_id: int):
    data = await state.get_data()
    
    name = data["name"]
    max_reps = data["max_reps"]
    timezone = data.get("timezone", "Europe/Moscow")
    
    utc_time = to_utc(local_time, timezone)
    start_reps = calculate_start_reps(max_reps)
    set1, set2, set3 = calculate_step_sets(start_reps)
    
    async with async_session() as session:
        new_user = User(
            user_id=user_id,
            name=name,
            max_reps=max_reps,
            timezone=timezone,
            reminder_time=utc_time,
            current_reps_per_set=start_reps,
            current_week=1
        )
        session.add(new_user)
        await session.commit()
        logger.info(f"New user registered: {name} (ID: {user_id})")
    
    welcome = (
        f"🎉 Отлично, <b>{name}</b>! Регистрация завершена!\n\n"
        f"📊 <b>Твои данные:</b>\n"
        f"• Максимум отжиманий: {max_reps}\n"
        f"• Программа: {set1}-{set2}-{set3} отжиманий\n"
        f"• Напоминание: {local_time.strftime('%H:%M')}\n"
        f"• Стартовая неделя программы\n\n"
        f"💪 <b>Почему такая нагрузка?</b>\n"
        f"Я использую проверенную методику ступенчатых подходов!\n"
        f"Ты начнёшь с комфортных {set1}-{set2}-{set3}, и каждую неделю нагрузка будет расти.\n\n"
        f"Готов? Жми «🏋️ Тренировка»!"
    )
    
    await message.answer(welcome, reply_markup=get_main_keyboard())
    await state.clear()

# ============ ЕДИНЫЙ ОБРАБОТЧИК НАЧАЛА ТРЕНИРОВКИ ============

@router.message(F.text == "🏋️ Тренировка")
@router.message(Command("workout"))
@router.callback_query(F.data == "start_workout")
async def start_workout_handler(event: Message | CallbackQuery, state: FSMContext):
    """Единый обработчик начала тренировки (кнопка, команда, callback)"""
    
    # Извлекаем message из события
    if isinstance(event, CallbackQuery):
        message = event.message
        await event.answer()
    else:
        message = event
    
    user_id = message.from_user.id if hasattr(message, 'from_user') else event.from_user.id
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one_or_none()
        
        if not user:
            await message.answer("Сначала давай познакомимся! Нажми /start")
            return
        
        today = date.today()
        
        workout = await session.execute(
            select(Workout).where(
                Workout.user_id == user_id,
                Workout.date == today
            )
        )
        workout = workout.scalar_one_or_none()
        
        if workout and workout.completed:
            await message.answer(
                "✅ Ты уже молодец сегодня! Отдыхай до завтра.",
                reply_markup=get_main_keyboard()
            )
            return
        
        # Если был день отдыха — отменяем
        if workout and workout.rest_day:
            workout.rest_day = False
            await message.answer(
                "🔥 Отлично! Отменяю день отдыха — начинаем тренировку!\n"
                "Отдых не потрачен, используешь в другой раз 💪"
            )
        
        if not workout:
            workout = Workout(user_id=user_id, date=today)
            session.add(workout)
        
        user.pending_step = 1
        await session.commit()
        
        base_reps = user.current_reps_per_set
        set1, set2, set3 = calculate_step_sets(base_reps)
        
        warmup = (
            "🔥 <b>ВРЕМЯ ТРЕНИРОВКИ!</b>\n\n"
            "<i>Быстрая разминка:</i>\n"
            "• Вращение руками — 10 раз вперёд/назад\n"
            "• Круговые движения плечами — 5 раз\n"
            "• Разминка запястий — 10 секунд\n\n"
            f"💪 <b>Подход 1 из 3:</b>\n"
            f"Сделай {set1} отжиманий и нажми кнопку!"
        )
        
        await message.answer(warmup, reply_markup=get_workout_keyboard(set1))
        await state.set_state(WorkoutSession.waiting_for_set1)
        await state.update_data(current_set=1, reps=[set1, set2, set3])

@router.callback_query(F.data == "skip_set")
async def skip_set(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current_set = data.get("current_set", 1)
    reps_data = data.get("reps", 0)
    
    # Получаем массив подходов
    if isinstance(reps_data, list):
        reps_array = reps_data
    else:
        reps_array = [reps_data, reps_data, max(5, reps_data - 5)]
    
    await callback.answer("Подход пропущен")
    
    if current_set < 3:
        next_set = current_set + 1
        await state.update_data(current_set=next_set)
        
        next_reps = reps_array[next_set - 1] if next_set - 1 < len(reps_array) else reps_array[-1]
        
        await callback.message.answer(
            f"😊 Ничего страшного!\n"
            f"💪 <b>Подход {next_set} из 3:</b>\n"
            f"Сделай {next_reps} отжиманий!",
            reply_markup=get_workout_keyboard(next_reps)
        )
        
        set_state_name = f"waiting_for_set{next_set}"
        await state.set_state(getattr(WorkoutSession, set_state_name))
    else:
        await finish_workout(callback.message, state, callback.from_user.id)

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
        
        if current_set < 3:
            await process_set_complete(message, state, current_set, message.from_user.id)
        else:
            await finish_workout(message, state, message.from_user.id)
    
    except ValueError:
        await message.answer("Пожалуйста, введи целое число отжиманий")

@router.callback_query(F.data.regexp(r'done_\d+'))
async def complete_set(callback: CallbackQuery, state: FSMContext):
    reps_done = int(callback.data.split("_")[1])
    
    data = await state.get_data()
    current_set = data.get("current_set", 1)
    
    await state.update_data({f"set{current_set}_reps": reps_done})
    await callback.answer("🔥 Отлично!")
    
    await process_set_complete(callback.message, state, current_set, callback.from_user.id)

async def process_set_complete(message: Message, state: FSMContext, current_set: int, user_id: int):
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()
        
        data = await state.get_data()
        reps_data = data.get("reps", user.current_reps_per_set)
        
        # Проверяем, массив это или одно число
        if isinstance(reps_data, list):
            reps_array = reps_data
        else:
            reps_array = [reps_data, reps_data, max(5, reps_data - 5)]
        
        if current_set < 3:
            next_set = current_set + 1
            rest_seconds = user.rest_seconds
            
            await state.update_data(current_set=next_set)
            user.pending_step = next_set
            await session.commit()
            
            await message.answer(
                f"😌 <b>Отдыхай {rest_seconds} секунд</b>\n"
                f"Я напомню о следующем подходе 💤"
            )
            
            await asyncio.sleep(rest_seconds)
            
            next_reps = reps_array[next_set - 1] if next_set - 1 < len(reps_array) else reps_array[-1]
            await message.answer(
                f"⏰ Время подхода!\n"
                f"💪 <b>Подход {next_set} из 3:</b>\n"
                f"Сделай {next_reps} отжиманий!",
                reply_markup=get_workout_keyboard(next_reps)
            )
            
            set_state_name = f"waiting_for_set{next_set}"
            await state.set_state(getattr(WorkoutSession, set_state_name))
        else:
            await finish_workout(message, state, user_id)

async def finish_workout(message: Message, state: FSMContext, user_id: int):
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()
        
        today = date.today()
        workout = await session.execute(
            select(Workout).where(
                Workout.user_id == user_id,
                Workout.date == today
            )
        )
        workout = workout.scalar_one()
        
        data = await state.get_data()
        
        workout.set1_reps = data.get("set1_reps", user.current_reps_per_set)
        workout.set2_reps = data.get("set2_reps", user.current_reps_per_set)
        workout.set3_reps = data.get("set3_reps", user.current_reps_per_set)
        workout.completed = True
        
        user.pending_step = 0
        await session.commit()
        
        total_reps = workout.set1_reps + workout.set2_reps + workout.set3_reps
        
        await check_achievements(user_id, session)
        
        await state.clear()
    
    completion_msg = (
        f"🎉 <b>ТРЕНИРОВКА ЗАВЕРШЕНА!</b>\n\n"
        f"📊 Твои результаты:\n"
        f"• Подход 1: {workout.set1_reps} отж.\n"
        f"• Подход 2: {workout.set2_reps} отж.\n"
        f"• Подход 3: {workout.set3_reps} отж.\n"
        f"• <b>Всего: {total_reps} отжиманий</b> 🔥\n\n"
        f"💭 <b>Как прошла тренировка?</b>\n"
        f"Напиши: всё сделал, были сложности или сколько подходов осилил."
    )
    
    await message.answer(completion_msg, reply_markup=ReplyKeyboardRemove())
    await state.set_state(WorkoutSession.waiting_for_feedback)

@router.message(WorkoutSession.waiting_for_feedback)
async def process_workout_feedback(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    async with async_session() as session:
        history_records = await session.execute(
            select(DialogueHistory)
            .where(DialogueHistory.user_id == user_id)
            .order_by(DialogueHistory.timestamp.desc())
            .limit(6)
        )
        history = [
            {"role": rec.role, "content": rec.message} 
            for rec in history_records.scalars().all()
        ]
        history.reverse()
        
        ai_response = await ask_deepseek(message.text, history)
        
        session.add(DialogueHistory(user_id=user_id, role="user", message=message.text))
        session.add(DialogueHistory(user_id=user_id, role="assistant", message=ai_response))
        
        await session.commit()
    
    await message.answer(ai_response, reply_markup=get_main_keyboard())
    await state.clear()

@router.message(F.text == "📊 Прогресс")
@router.message(Command("progress"))
async def show_progress(message: Message):
    user_id = message.from_user.id
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one_or_none()
        
        if not user:
            await message.answer("Сначала давай познакомимся! Нажми /start")
            return
        
        week_ago = date.today() - timedelta(days=7)
        workouts = await session.execute(
            select(Workout).where(
                Workout.user_id == user_id,
                Workout.date >= week_ago
            ).order_by(Workout.date.desc())
        )
        workouts = workouts.scalars().all()
        
        completed_this_week = sum(1 for w in workouts if w.completed)
        total_reps_this_week = sum(
            w.set1_reps + w.set2_reps + w.set3_reps 
            for w in workouts if w.completed
        )
        
        streak = 0
        check_date = date.today()
        for w in workouts:
            if w.date == check_date and w.completed:
                streak += 1
                check_date -= timedelta(days=1)
            elif w.date < check_date:
                break
        
        progress_text = (
            f"📊 <b>ПРОГРЕСС {user.name}</b>\n\n"
            f"🏆 Текущий уровень:\n"
            f"• Неделя программы: {user.current_week}\n"
            f"• Отжиманий в подходе: {user.current_reps_per_set}\n"
            f"• Личный рекорд: {user.max_reps}\n\n"
            f"📈 Эта неделя:\n"
            f"• Тренировок: {completed_this_week}/7\n"
            f"• Всего отжиманий: {total_reps_this_week}\n"
            f"• Дней подряд: {streak} 🔥\n\n"
            f"💪 <i>Продолжай в том же духе!</i>"
        )
        
        await message.answer(progress_text, reply_markup=get_main_keyboard())

@router.message(F.text == "🏆 Достижения")
@router.message(Command("achievements"))
async def show_achievements(message: Message):
    user_id = message.from_user.id
    
    async with async_session() as session:
        user_achievements = await session.execute(
            select(Achievement).where(Achievement.user_id == user_id)
        )
        user_achievements = user_achievements.scalars().all()
        
        if not user_achievements:
            await message.answer(
                "🎯 У тебя пока нет достижений!\n"
                "Начни тренироваться и они появятся! 💪",
                reply_markup=get_main_keyboard()
            )
            return
        
        ach_list = "\n".join([
            f"• {ach.title} — {ach.awarded_at.strftime('%d.%m.%Y')}"
            for ach in user_achievements
        ])
        
        await message.answer(
            f"🏆 <b>ТВОИ ДОСТИЖЕНИЯ</b>\n\n{ach_list}",
            reply_markup=get_main_keyboard()
        )

async def check_achievements(user_id: int, session: AsyncSession):
    existing = await session.execute(
        select(Achievement.title).where(Achievement.user_id == user_id)
    )
    existing_titles = set(existing.scalars().all())
    
    workouts_count = await session.execute(
        select(func.count(Workout.id)).where(
            Workout.user_id == user_id,
            Workout.completed == True
        )
    )
    workouts_count = workouts_count.scalar()
    
    if "first_workout" not in existing_titles and workouts_count >= 1:
        session.add(Achievement(
            user_id=user_id,
            title="💪 Первая тренировка",
            description="Начало пути!"
        ))
    
    streak = await calculate_streak(user_id, session)
    
    if streak >= 5 and "streak_5" not in existing_titles:
        session.add(Achievement(
            user_id=user_id,
            title="🔥 5 дней подряд",
            description="Отличная дисциплина!"
        ))
    
    await session.flush()

async def calculate_streak(user_id: int, session: AsyncSession) -> int:
    workouts = await session.execute(
        select(Workout).where(
            Workout.user_id == user_id,
            Workout.completed == True
        ).order_by(Workout.date.desc())
    )
    workouts = workouts.scalars().all()
    
    if not workouts:
        return 0
    
    streak = 1
    check_date = date.today() - timedelta(days=1)
    
    for workout in workouts:
        if workout.date == date.today():
            continue
        if workout.date == check_date:
            streak += 1
            check_date -= timedelta(days=1)
        else:
            break
    
    return streak

# ============ НАСТРОЙКИ ============

@router.message(F.text == "⚙️ Настройки")
@router.message(Command("settings"))
async def show_settings(message: Message):
    user_id = message.from_user.id
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one_or_none()
        
        if not user:
            await message.answer("Сначала давай познакомимся! Нажми /start")
            return
        
        local_time = from_utc(user.reminder_time, user.timezone)
        set1, set2, set3 = calculate_step_sets(user.current_reps_per_set)
        
        settings_text = (
            f"⚙️ <b>НАСТРОЙКИ</b>\n\n"
            f"👤 Имя: {user.name}\n"
            f"🕐 Время напоминания: {local_time.strftime('%H:%M')}\n"
            f"🌍 Часовой пояс: {user.timezone}\n"
            f"🔔 Напоминания: {'Вкл' if user.reminder_on else 'Выкл'}\n"
            f"⏱ Отдых между подходами: {user.rest_seconds} сек\n"
            f"💪 Текущая нагрузка: {set1}-{set2}-{set3} отжиманий"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🕐 Изменить время", callback_data="change_time")],
            [InlineKeyboardButton(text="🔔 Напоминания вкл/выкл", callback_data="toggle_remind")],
            [InlineKeyboardButton(text="🔧 Изменить отдых", callback_data="change_rest")],
            [InlineKeyboardButton(text="⬆️ Повысить сложность", callback_data="increase_difficulty")],
            [InlineKeyboardButton(text="⬇️ Понизить сложность", callback_data="decrease_difficulty")]
        ])
        
        await message.answer(settings_text, reply_markup=keyboard)


@router.callback_query(F.data == "increase_difficulty")
async def increase_difficulty(callback: CallbackQuery):
    """Ручное повышение сложности"""
    user_id = callback.from_user.id
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()
        
        new_reps = calculate_weekly_progression(user.current_reps_per_set)
        user.current_reps_per_set = new_reps
        await session.commit()
        
        set1, set2, set3 = calculate_step_sets(new_reps)
    
    await callback.answer("✅ Сложность повышена!")
    await callback.message.answer(
        f"⬆️ <b>Сложность повышена!</b>\n\n"
        f"Новая нагрузка: {set1}-{set2}-{set3} отжиманий\n"
        f"Так держать! 💪",
        reply_markup=get_main_keyboard()
    )


@router.callback_query(F.data == "decrease_difficulty")
async def decrease_difficulty(callback: CallbackQuery):
    """Ручное понижение сложности"""
    user_id = callback.from_user.id
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()
        
        new_reps = calculate_decrease_reps(user.current_reps_per_set)
        
        if new_reps == user.current_reps_per_set:
            await callback.answer("⚠️ Достигнут минимальный уровень!")
            await callback.message.answer(
                "⚠️ <b>Нельзя понизить!</b>\n"
                "Ты уже на минимальном уровне сложности (5 отжиманий).",
                reply_markup=get_main_keyboard()
            )
            return
        
        user.current_reps_per_set = new_reps
        await session.commit()
        
        set1, set2, set3 = calculate_step_sets(new_reps)
    
    await callback.answer("✅ Сложность понижена!")
    await callback.message.answer(
        f"⬇️ <b>Сложность понижена!</b>\n\n"
        f"Новая нагрузка: {set1}-{set2}-{set3} отжиманий\n"
        f"Главное — комфорт и техника! 😊",
        reply_markup=get_main_keyboard()
    )


@router.callback_query(F.data == "change_rest")
async def change_rest(callback: CallbackQuery):
    """Изменение времени отдыха"""
    await callback.answer()
    await callback.message.answer(
        "⏱ <b>Выбери время отдыха между подходами:</b>",
        reply_markup=get_rest_keyboard()
    )


@router.callback_query(F.data.regexp(r'rest_\d+'))
async def process_rest_change(callback: CallbackQuery):
    """Обработка выбора времени отдыха"""
    rest_seconds = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()
        
        user.rest_seconds = rest_seconds
        await session.commit()
    
    await callback.answer(f"✅ Отдых: {rest_seconds} сек!")
    await callback.message.answer(
        f"⏱ <b>Время отдыха изменено!</b>\n"
        f"Теперь между подходами: <b>{rest_seconds} секунд</b>\n"
        f"Это поможет тебе восстановиться 💪",
        reply_markup=get_main_keyboard()
    )


@router.callback_query(F.data == "change_time")
async def change_time(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "🕐 Выбери новое время для напоминаний:",
        reply_markup=get_time_keyboard()
    )
    await state.set_state(Settings.waiting_for_time)

@router.callback_query(Settings.waiting_for_time)
async def process_new_time(callback: CallbackQuery, state: FSMContext):
    if callback.data == "time_custom":
        await callback.message.answer("Напиши время в формате ЧЧ:ММ")
        await callback.answer()
        return
    
    time_str = callback.data.replace("time_", "")
    hours, minutes = map(int, time_str.split(":"))
    local_time = time(hours, minutes)
    
    user_id = callback.from_user.id
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()
        
        utc_time = to_utc(local_time, user.timezone)
        user.reminder_time = utc_time
        await session.commit()
    
    await callback.message.answer(
        f"✅ Время напоминаний изменено на {local_time.strftime('%H:%M')}!",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()
    await state.clear()

@router.message(Settings.waiting_for_time)
async def process_custom_time(message: Message, state: FSMContext):
    local_time = normalize_time(message.text)
    user_id = message.from_user.id
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()
        
        utc_time = to_utc(local_time, user.timezone)
        user.reminder_time = utc_time
        await session.commit()
    
    await message.answer(
        f"✅ Время напоминаний изменено на {local_time.strftime('%H:%M')}!",
        reply_markup=get_main_keyboard()
    )
    await state.clear()

@router.callback_query(F.data == "toggle_remind")
async def toggle_reminders(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()
        
        user.reminder_on = not user.reminder_on
        await session.commit()
        
        status = "включены" if user.reminder_on else "выключены"
    
    await callback.answer(f"Напоминания {status}!")
    await callback.message.answer(
        f"🔔 Напоминания {status}!",
        reply_markup=get_main_keyboard()
    )

# ============ ОТДЫХ И ПОМОЩЬ ============

@router.message(F.text == "😴 Отдых")
@router.message(Command("restday"))
async def rest_day(message: Message):
    user_id = message.from_user.id
    today = date.today()
    
    async with async_session() as session:
        ten_days_ago = today - timedelta(days=10)
        rest_count = await session.execute(
            select(func.count(Workout.id)).where(
                Workout.user_id == user_id,
                Workout.rest_day == True,
                Workout.date >= ten_days_ago
            )
        )
        rest_count = rest_count.scalar()
        
        if rest_count > 0:
            await message.answer(
                "😴 Ты уже использовал день отдыха недавно.\n"
                "Можно отдыхать не чаще раза в 10 дней.",
                reply_markup=get_main_keyboard()
            )
            return
        
        existing = await session.execute(
            select(Workout).where(
                Workout.user_id == user_id,
                Workout.date == today
            )
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
    help_text = (
        "🤖 <b>ОТЖИМАЙКИН — ПОМОЩЬ</b>\n\n"
        "🏋️ <b>Основные команды:</b>\n"
        "/workout — начать тренировку\n"
        "/progress — твой прогресс\n"
        "/achievements — достижения\n\n"
        "⚙️ <b>Управление:</b>\n"
        "/settings — настройки\n"
        "/skip — пропустить день\n"
        "/restday — день отдыха\n\n"
        "💡 <b>Как это работает:</b>\n"
        "• Каждый день 3 ступенчатых подхода\n"
        "• Нагрузка растёт раз в неделю\n"
        "• Можно повысить/понизить вручную\n"
        "• Отдых между подходами настраивается\n"
        "• Ачивки за регулярность! 🏆"
    )
    await message.answer(help_text, reply_markup=get_main_keyboard())

# ============ KEEP-ALIVE И НАПОМИНАНИЯ ============

async def reminder_checker():
    logger.info("Reminder checker started")
    
    while True:
        try:
            utc_now = datetime.now(timezone.utc)
            current_time = utc_now.time()
            current_date = utc_now.date()
            
            async with async_session() as session:
                users = await session.execute(
                    select(User).where(User.reminder_on == True)
                )
                users = users.scalars().all()
                
                for user in users:
                    try:
                        reminder_utc = user.reminder_time
                        
                        if (current_time.hour == reminder_utc.hour and 
                            current_time.minute == reminder_utc.minute and
                            user.last_reminder_date != current_date):
                            
                            set1, set2, set3 = calculate_step_sets(user.current_reps_per_set)
                            
                            await bot.send_message(
                                user.user_id,
                                f"🔔 {user.name}, время размять плечи!\n\n"
                                f"Сегодня: {set1}-{set2}-{set3} отжиманий.\n"
                                f"Жду тебя! 🔥",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                    InlineKeyboardButton(text="🏋️ Начать тренировку", callback_data="start_workout")
                                ]])
                            )
                            
                            user.last_reminder_date = current_date
                            await session.commit()
                    
                    except Exception as e:
                        logger.error(f"Error sending reminder to {user.user_id}: {e}")
        
        except Exception as e:
            logger.error(f"Reminder checker error: {e}")
        
        await asyncio.sleep(60)

async def weekly_reports():
    logger.info("Weekly report scheduler started")
    
    while True:
        try:
            now = datetime.now(timezone.utc)
            
            if now.weekday() == 6 and now.hour == 20 and now.minute == 0:
                logger.info("Sending weekly reports...")
                
                async with async_session() as session:
                    users = await session.execute(select(User))
                    users = users.scalars().all()
                    
                    for user in users:
                        try:
                            week_start = now.date() - timedelta(days=7)
                            workouts = await session.execute(
                                select(Workout).where(
                                    Workout.user_id == user.user_id,
                                    Workout.date >= week_start
                                )
                            )
                            workouts = workouts.scalars().all()
                            
                            completed = sum(1 for w in workouts if w.completed)
                            total_reps = sum(
                                w.set1_reps + w.set2_reps + w.set3_reps 
                                for w in workouts if w.completed
                            )
                            
                            if completed >= 6:
                                new_reps = calculate_weekly_progression(user.current_reps_per_set)
                                user.current_reps_per_set = new_reps
                                user.current_week += 1
                                
                                set1, set2, set3 = calculate_step_sets(new_reps)
                                
                                await bot.send_message(
                                    user.user_id,
                                    f"📊 <b>НЕДЕЛЬНЫЙ ОТЧЁТ</b>\n\n"
                                    f"💪 Тренировок: {completed}/7\n"
                                    f"🔥 Всего отжиманий: {total_reps}\n\n"
                                    f"🎉 <b>Отличная неделя! Повышаю нагрузку!</b>\n"
                                    f"Теперь: {set1}-{set2}-{set3} отжиманий! 🚀"
                                )
                            else:
                                await bot.send_message(
                                    user.user_id,
                                    f"📊 <b>НЕДЕЛЬНЫЙ ОТЧЁТ</b>\n\n"
                                    f"💪 Тренировок: {completed}/7\n"
                                    f"🔥 Всего отжиманий: {total_reps}\n\n"
                                    f"💪 Не спешим, главное — регулярность!\n"
                                    f"Продолжаем с {user.current_reps_per_set} отжиманиями."
                                )
                            
                            await session.commit()
                        
                        except Exception as e:
                            logger.error(f"Error sending report to {user.user_id}: {e}")
            
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"Weekly report error: {e}")
            await asyncio.sleep(60)

async def keep_alive_self_ping():
    logger.info("Keep-alive self ping started")
    await asyncio.sleep(30)
    
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{WEBHOOK_URL}/health", timeout=10) as resp:
                    if resp.status == 200:
                        logger.debug("Keep-alive ping successful")
        except Exception as e:
            logger.error(f"Keep-alive ping failed: {e}")
        
        await asyncio.sleep(840)

# ============ WEB SERVER ============

async def health_check(request):
    return web.Response(
        text=json.dumps({
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "PushUp Bot"
        }),
        content_type="application/json"
    )

# ============ MAIN ============

async def main():
    global bot, dp
    
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    
    dp = Dispatcher()
    dp.include_router(router)
    
    await init_db()
    
    asyncio.create_task(reminder_checker())
    asyncio.create_task(weekly_reports())
    asyncio.create_task(keep_alive_self_ping())
    
    webhook_path = "/webhook"
    app = web.Application()
    
    app.router.add_get("/health", health_check)
    app.router.add_get("/", health_check)
    
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=webhook_path)
    
    setup_application(app, dp, bot=bot)
    
    try:
        await bot.set_webhook(
            f"{WEBHOOK_URL}{webhook_path}",
            drop_pending_updates=True
        )
        logger.info(f"Webhook set to {WEBHOOK_URL}{webhook_path}")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        return
    
    logger.info(f"Starting web server on port {PORT}")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    
    try:
        await site.start()
        logger.info("Bot is running!")
        
        while True:
            await asyncio.sleep(3600)
    
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
    finally:
        await bot.delete_webhook()
        await runner.cleanup()
        logger.info("Bot stopped")

if __name__ == "__main__":
    asyncio.run(main())
