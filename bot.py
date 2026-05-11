"""
Отжимайкин — Telegram-бот для тренировок отжиманий
Полный код в одном файле с интеграцией DeepSeek API
"""

import asyncio
import logging
import sys
import json
from datetime import datetime, date, time, timedelta
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
from sqlalchemy.orm import declarative_base, relationship, selectinload

# HTTP and external
import aiohttp
from aiohttp import web
import pytz

# ============ КОНФИГУРАЦИЯ ============

# ВАЖНО: Замените эти значения на свои или используйте переменные окружения
import os
from dotenv import load_dotenv

load_dotenv()

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "YOUR_DEEPSEEK_KEY")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/dbname")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-app.onrender.com")
PORT = int(os.getenv("PORT", 8080))

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============ DATABASE SETUP ============

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    
    user_id = Column(BigInteger, primary_key=True)
    name = Column(String(100))
    max_reps = Column(Integer)
    timezone = Column(String(50), default="Europe/Moscow")
    reminder_time = Column(Time)  # UTC
    reminder_on = Column(Boolean, default=True)
    current_week = Column(Integer, default=1)
    current_reps_per_set = Column(Integer)
    rest_seconds = Column(Integer, default=90)
    # Для восстановления состояния после перезапуска
    pending_step = Column(Integer, default=0)  # 0=нет активной тренировки, 1/2/3=на каком подходе
    next_step_time = Column(DateTime)  # время, когда нужно отправить следующий подход
    last_reminder_date = Column(Date)  # дата последнего отправленного напоминания
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
    description = Column(String(500))
    awarded_at = Column(DateTime, server_default=func.now())
    
    user = relationship("User", back_populates="achievements")

class DialogueHistory(Base):
    __tablename__ = "dialogue_history"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    role = Column(String(20), nullable=False)  # "user" или "assistant"
    message = Column(String(2000), nullable=False)
    timestamp = Column(DateTime, server_default=func.now())

# Создаем engine и session
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,  # Проверка соединения перед использованием
    pool_recycle=3600  # Пересоздание соединений каждый час
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

async def get_db():
    """Получение сессии базы данных"""
    async with async_session() as session:
        yield session

# ============ DEEPSEEK INTEGRATION ============

SYSTEM_PROMPT = """Ты — Отжимайкин, дружелюбный и мотивирующий фитнес-тренер для домашних отжиманий.
Ты всегда поддерживаешь, хвалишь за успехи, мягко подбадриваешь при неудачах.
Твой стиль: лёгкий юмор, эмодзи, краткость (1-3 предложения).
Ты умеешь понять, что ответил пользователь: сделал все подходы, пропустил часть, устал, болит спина и т.д.
Никогда не критикуй, не дави. Если пользователь пропустил тренировку — поддержи.
Используй эмодзи: 💪🔥🎉😊💥"""

async def ask_deepseek(
    user_message: str, 
    history: Optional[List[Dict[str, str]]] = None
) -> str:
    """
    Отправка запроса к DeepSeek API с контекстом диалога.
    Если API недоступен — возвращает локальный ответ.
    """
    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "YOUR_DEEPSEEK_KEY":
        return get_fallback_response(user_message)
    
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    if history:
        # Берем последние 6 сообщений для контекста
        messages.extend(history[-6:])
    
    messages.append({"role": "user", "content": user_message})
    
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": 200,
        "top_p": 0.9
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
                else:
                    logger.error(f"DeepSeek API error: {response.status}")
                    return get_fallback_response(user_message)
    except asyncio.TimeoutError:
        logger.error("DeepSeek API timeout")
        return get_fallback_response(user_message)
    except Exception as e:
        logger.error(f"DeepSeek API exception: {e}")
        return get_fallback_response(user_message)

def get_fallback_response(user_text: str) -> str:
    """Локальные ответы, если DeepSeek недоступен"""
    text = user_text.lower()
    
    if any(word in text for word in ["сделал", "выполнил", "готово", "ок", "да"]):
        return "Отлично! Ты молодец, продолжай в том же духе! 💪"
    elif any(word in text for word in ["пропустил", "не смог", "устал", "сложно"]):
        return "Ничего страшного! Отдыхай, завтра будет новый день и новые силы! 😊"
    elif any(word in text for word in ["боль", "болит", "травма"]):
        return "Здоровье важнее всего! Отдохни и проконсультируйся с врачом при необходимости. 🙏"
    else:
        return "Понял тебя! Продолжаем двигаться к цели! 🎯"

async def interpret_workout_feedback(message: str) -> Dict[str, bool]:
    """
    Интерпретация ответа пользователя о тренировке.
    Возвращает словарь с флагами выполения.
    
    ВАЖНО: Используем AI только для понимания смысла, 
    но решение принимаем на основе четких правил.
    """
    text = message.lower().strip()
    
    # Четкие правила для распознавания
    if text in ["все", "всё", "все сделал", "выполнил", "готово", "полностью"]:
        return {"completed_all": True, "partial": False}
    
    if any(word in text for word in ["не все", "частично", "половину", "2 подхода", "два подхода"]):
        return {"completed_all": False, "partial": True}
    
    if any(word in text for word in ["пропустил", "не делал", "не смог", "не было сил"]):
        return {"completed_all": False, "partial": False}
    
    # Если непонятно — пробуем AI
    if DEEPSEEK_API_KEY and DEEPSEEK_API_KEY != "YOUR_DEEPSEEK_KEY":
        try:
            prompt = f"""Ответь только "completed_all" если пользователь выполнил все подходы,
"partial" если часть, "none" если не делал.
Сообщение пользователя: "{message}"
Ответ:"""
            
            ai_response = await ask_deepseek(prompt, None)
            
            if "completed_all" in ai_response.lower():
                return {"completed_all": True, "partial": False}
            elif "partial" in ai_response.lower():
                return {"completed_all": False, "partial": True}
            else:
                return {"completed_all": False, "partial": False}
        except:
            pass
    
    # По умолчанию считаем выполненным частично
    return {"completed_all": False, "partial": True}

# ============ KEYBOARDS AND HELPERS ============

def get_main_keyboard():
    """Основная клавиатура с командами"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏋️ Тренировка"), KeyboardButton(text="📊 Прогресс")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="❓ Помощь")],
            [KeyboardButton(text="🏆 Достижения"), KeyboardButton(text="😴 Отдых")]
        ],
        resize_keyboard=True
    )

def get_workout_keyboard(reps: int):
    """Клавиатура для тренировки"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Сделал {reps} отжиманий", callback_data=f"done_{reps}")],
        [InlineKeyboardButton(text="📝 Написать свой результат", callback_data="custom_reps")],
        [InlineKeyboardButton(text="⏭ Пропустить подход", callback_data="skip_set")]
    ])

def get_time_keyboard():
    """Клавиатура для выбора времени"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌅 Утро (08:00)", callback_data="time_08:00")],
        [InlineKeyboardButton(text="☀️ День (12:30)", callback_data="time_12:30")],
        [InlineKeyboardButton(text="🌆 Вечер (19:00)", callback_data="time_19:00")],
        [InlineKeyboardButton(text="✏️ Написать своё время", callback_data="time_custom")]
    ])

def get_timezone_keyboard():
    """Клавиатура для выбора часового пояса"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Москва (MSK)", callback_data="tz_Europe/Moscow")],
        [InlineKeyboardButton(text="🇷🇺 Новосибирск (+4)", callback_data="tz_Asia/Novosibirsk")],
        [InlineKeyboardButton(text="🇷🇺 Владивосток (+7)", callback_data="tz_Asia/Vladivostok")],
        [InlineKeyboardButton(text="🇪🇺 Берлин", callback_data="tz_Europe/Berlin")],
        [InlineKeyboardButton(text="🇬🇧 Лондон", callback_data="tz_Europe/London")],
        [InlineKeyboardButton(text="🇺🇸 Нью-Йорк", callback_data="tz_America/New_York")],
        [InlineKeyboardButton(text="✏️ Написать город", callback_data="tz_custom")]
    ])

# ============ UTILITY FUNCTIONS ============

def normalize_time(user_input: str) -> time:
    """Преобразование текстового описания времени в объект time"""
    user_input = user_input.strip().lower()
    
    # Прямое указание времени ЧЧ:ММ
    import re
    match = re.search(r'(\d{1,2})[.:](\d{2})', user_input)
    if match:
        hours, minutes = int(match.group(1)), int(match.group(2))
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            return time(hours, minutes)
    
    # Ключевые слова
    time_map = {
        "утро": time(8, 0),
        "утром": time(8, 0),
        "день": time(12, 30),
        "днем": time(12, 30),
        "вечер": time(19, 0),
        "вечером": time(19, 0),
        "ночь": time(21, 0),
        "ночью": time(21, 0)
    }
    
    for key, val in time_map.items():
        if key in user_input:
            return val
    
    return time(8, 0)  # По умолчанию утро

def to_utc(local_time: time, tz_str: str) -> time:
    """Конвертация локального времени в UTC"""
    try:
        tz = pytz.timezone(tz_str)
        today = date.today()
        local_dt = datetime.combine(today, local_time)
        local_dt = tz.localize(local_dt)
        utc_dt = local_dt.astimezone(pytz.UTC)
        return utc_dt.time()
    except Exception as e:
        logger.error(f"Timezone conversion error: {e}")
        # По умолчанию считаем что это UTC
        return local_time

def from_utc(utc_time: time, tz_str: str) -> time:
    """Конвертация UTC в локальное время"""
    try:
        tz = pytz.timezone(tz_str)
        today = date.today()
        utc_dt = datetime.combine(today, utc_time).replace(tzinfo=pytz.UTC)
        local_dt = utc_dt.astimezone(tz)
        return local_dt.time()
    except:
        return utc_time

def calculate_start_reps(max_reps: int) -> int:
    """Расчет стартовой нагрузки (50% от максимума, минимум 3)"""
    return max(3, int(max_reps * 0.5))

def calculate_weekly_progression(current_reps: int) -> int:
    """Расчет повышения нагрузки (+15%, минимум +2)"""
    increase = max(2, int(current_reps * 0.15))
    return current_reps + increase

# ============ FSM STATES ============

class Onboarding(StatesGroup):
    """Состояния для процесса знакомства"""
    waiting_for_max_reps = State()
    waiting_for_name = State()
    waiting_for_timezone = State()
    waiting_for_time = State()

class WorkoutSession(StatesGroup):
    """Состояния для тренировки"""
    waiting_for_set1 = State()
    waiting_for_set2 = State()
    waiting_for_set3 = State()
    waiting_for_feedback = State()

class Settings(StatesGroup):
    """Состояния для настроек"""
    waiting_for_time = State()
    waiting_for_timezone = State()

# ============ HANDLERS ============

router = Router()

# ---------- ONBOARDING ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    
    async with async_session() as session:
        # Проверяем, есть ли пользователь в базе
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()
        
        if user:
            # Пользователь уже зарегистрирован
            welcome_back = (
                f"С возвращением, {user.name}! 💪\n\n"
                f"📊 Твой прогресс:\n"
                f"• Неделя: {user.current_week}\n"
                f"• Отжиманий в подходе: {user.current_reps_per_set}\n"
                f"• Лучший результат: {user.max_reps}\n\n"
                f"Готов тренироваться? Жми «🏋️ Тренировка»!"
            )
            await message.answer(welcome_back, reply_markup=get_main_keyboard())
            
            # Обновляем время последней активности
            user.updated_at = func.now()
            await session.commit()
            
            await state.clear()
            return
    
    # Новый пользователь
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
    """Обработка ответа о максимуме отжиманий"""
    try:
        reps = int(message.text.strip())
        if reps < 0 or reps > 500:
            raise ValueError("Invalid reps number")
        
        await state.update_data(max_reps=reps)
        
        await message.answer(
            f"🔥 {reps} отжиманий — отличный старт!\n\n"
            f"❓ <b>Как мне к тебе обращаться?</b>\n"
            f"Напиши своё имя или никнейм:"
        )
        await state.set_state(Onboarding.waiting_for_name)
        
    except ValueError:
        await message.answer(
            "❌ Пожалуйста, введи целое число.\n"
            "Например: 10, 15, 20"
        )

@router.message(Onboarding.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    """Обработка имени пользователя"""
    name = message.text.strip()[:50]  # Ограничиваем длину
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
    """Обработка выбора часового пояса через кнопки"""
    tz_data = callback.data
    
    if tz_data == "tz_custom":
        await callback.message.answer(
            "Напиши название своего города или часового пояса.\n"
            "Например: «Калининград», «Екатеринбург», «Asia/Tokyo»"
        )
        await callback.answer()
        return
    
    # Извлекаем timezone из callback_data
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
    """Обработка часового пояса текстом"""
    tz_input = message.text.strip()
    
    # Пытаемся найти часовой пояс
    try:
        pytz.timezone(tz_input)
        tz_str = tz_input
    except:
        # Пробуем найти по городу
        from pytz import country_timezones
        found = False
        for tz in pytz.all_timezones:
            if tz_input.lower() in tz.lower():
                tz_str = tz
                found = True
                break
        if not found:
            tz_str = "Europe/Moscow"  # По умолчанию
    
    await state.update_data(timezone=tz_str)
    
    await message.answer(
        f"✅ Часовой пояс установлен: {tz_str}\n\n"
        f"❓ <b>Во сколько тебе удобно получать напоминания о тренировке?</b>",
        reply_markup=get_time_keyboard()
    )
    await state.set_state(Onboarding.waiting_for_time)

@router.callback_query(Onboarding.waiting_for_time)
async def process_time_callback(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора времени через кнопки"""
    time_data = callback.data
    
    if time_data == "time_custom":
        await callback.message.answer(
            "Напиши время в формате ЧЧ:ММ\n"
            "Например: 07:30 или 14:45"
        )
        await callback.answer()
        return
    
    time_str = time_data.replace("time_", "")
    hours, minutes = map(int, time_str.split(":"))
    local_time = time(hours, minutes)
    
    await finish_onboarding(callback.message, state, local_time, callback.from_user.id)
    await callback.answer()

@router.message(Onboarding.waiting_for_time)
async def process_time_text(message: Message, state: FSMContext):
    """Обработка времени текстом"""
    local_time = normalize_time(message.text.strip())
    await finish_onboarding(message, state, local_time, message.from_user.id)

async def finish_onboarding(
    message: Message, 
    state: FSMContext, 
    local_time: time, 
    user_id: int
):
    """Завершение онбординга и создание пользователя"""
    data = await state.get_data()
    
    name = data["name"]
    max_reps = data["max_reps"]
    timezone = data.get("timezone", "Europe/Moscow")
    
    # Конвертируем время в UTC для хранения
    utc_time = to_utc(local_time, timezone)
    
    # Рассчитываем стартовую нагрузку
    start_reps = calculate_start_reps(max_reps)
    
    # Сохраняем пользователя в базу
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
    
    # Приветственное сообщение
    welcome = (
        f"🎉 Отлично, <b>{name}</b>! Регистрация завершена!\n\n"
        f"📊 <b>Твои данные:</b>\n"
        f"• Максимум отжиманий: {max_reps}\n"
        f"• Стартовая нагрузка: 3 подхода по {start_reps}\n"
        f"• Напоминание: {local_time.strftime('%H:%M')}\n"
        f"• Первая неделя программы\n\n"
        f"💪 <b>Тренировка начинается сегодня!</b>\n"
        f"Жми кнопку «🏋️ Тренировка» когда будешь готов!\n\n"
        f"🔥 <i>Главное — регулярность. Маленькими шагами к большим результатам!</i>"
    )
    
    await message.answer(welcome, reply_markup=get_main_keyboard())
    await state.clear()

# ---------- WORKOUT LOGIC ----------

@router.message(F.text == "🏋️ Тренировка")
@router.message(Command("workout"))
async def start_workout(message: Message, state: FSMContext):
    """Начало тренировки"""
    user_id = message.from_user.id
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one_or_none()
        
        if not user:
            await message.answer("Сначала давай познакомимся! Нажми /start")
            return
        
        today = date.today()
        
        # Проверяем, есть ли уже запись о тренировке сегодня
        workout = await session.execute(
            select(Workout).where(
                Workout.user_id == user_id,
                Workout.date == today
            )
        )
        workout = workout.scalar_one_or_none()
        
        if workout and workout.completed:
            await message.answer(
                "✅ Ты уже молодец сегодня! Отдыхай до завтра.\n"
                "Или используй /restday если хочешь отдохнуть."
            )
            return
        
        if workout and workout.rest_day:
            await message.answer("😴 Сегодня у тебя день отдыха. Восстановление важно!")
            return
        
        # Создаем новую запись о тренировке
        if not workout:
            workout = Workout(
                user_id=user_id,
                date=today,
                set1_reps=0,
                set2_reps=0,
                set3_reps=0
            )
            session.add(workout)
            await session.commit()
        
        # Проверяем незавершенные подходы
        if user.pending_step > 0:
            step = user.pending_step
            reps = user.current_reps_per_set
            
            await message.answer(
                f"🔄 У тебя была незавершённая тренировка!\n"
                f"Продолжаем с подхода {step}/3\n"
                f"Сделай {reps} отжиманий 💪",
                reply_markup=get_workout_keyboard(reps)
            )
            
            set_state_name = f"waiting_for_set{step}"
            await state.set_state(getattr(WorkoutSession, set_state_name))
            await state.update_data(current_set=step)
            return
        
        # Начинаем новую тренировку
        reps = user.current_reps_per_set
        
        # Сохраняем флаг активной тренировки
        user.pending_step = 1
        await session.commit()
        
        # Разминка
        warmup = (
            "🔥 <b>ВРЕМЯ ТРЕНИРОВКИ!</b>\n\n"
            "<i>Быстрая разминка:</i>\n"
            "• Вращение руками — 10 раз вперёд/назад\n"
            "• Круговые движения плечами — 5 раз\n"
            "• Разминка запястий — 10 секунд\n\n"
            f"💪 <b>Подход 1 из 3:</b>\n"
            f"Сделай {reps} отжиманий и нажми кнопку!"
        )
        
        await message.answer(warmup, reply_markup=get_workout_keyboard(reps))
        await state.set_state(WorkoutSession.waiting_for_set1)
        await state.update_data(current_set=1, reps=reps)

@router.callback_query(F.data == "skip_set")
async def skip_set(callback: CallbackQuery, state: FSMContext):
    """Пропуск подхода"""
    data = await state.get_data()
    current_set = data.get("current_set", 1)
    reps = data.get("reps", 0)
    
    await callback.answer("Подход пропущен")
    
    if current_set < 3:
        # Переходим к следующему подходу
        next_set = current_set + 1
        await state.update_data(current_set=next_set)
        
        await callback.message.answer(
            f"😊 Ничего страшного!\n"
            f"💪 <b>Подход {next_set} из 3:</b>\n"
            f"Сделай {reps} отжиманий!",
            reply_markup=get_workout_keyboard(reps)
        )
        
        set_state_name = f"waiting_for_set{next_set}"
        await state.set_state(getattr(WorkoutSession, set_state_name))
    else:
        # Все подходы закончены
        await finish_workout(callback.message, state, callback.from_user.id)

@router.callback_query(F.data == "custom_reps")
async def custom_reps_start(callback: CallbackQuery, state: FSMContext):
    """Пользователь хочет ввести своё количество"""
    await callback.answer()
    await callback.message.answer(
        "📝 Напиши, сколько отжиманий ты сделал в этом подходе:"
    )

@router.message(F.text.regexp(r'^\d+$'))
async def custom_reps_input(message: Message, state: FSMContext):
    """Обработка своего количества отжиманий"""
    current_state = await state.get_state()
    
    if not current_state or not current_state.startswith("WorkoutSession"):
        return  # Не в режиме тренировки
    
    try:
        done_reps = int(message.text.strip())
        if done_reps < 0:
            raise ValueError
        
        data = await state.get_data()
        current_set = data.get("current_set", 1)
        reps = data.get("reps", 0)
        
        await state.update_data({f"set{current_set}_reps": done_reps})
        
        if current_set < 3:
            # Переход к следующему подходу
            await process_set_complete(message, state, current_set, message.from_user.id)
        else:
            # Это был последний подход
            await state.update_data({f"set{current_set}_reps": done_reps})
            await finish_workout(message, state, message.from_user.id)
    
    except ValueError:
        await message.answer("Пожалуйста, введи целое число отжиманий")

@router.callback_query(F.data.regexp(r'done_\d+'))
async def complete_set(callback: CallbackQuery, state: FSMContext):
    """Пользователь выполнил подход"""
    reps_done = int(callback.data.split("_")[1])
    
    data = await state.get_data()
    current_set = data.get("current_set", 1)
    
    await state.update_data({f"set{current_set}_reps": reps_done})
    await callback.answer("🔥 Отлично!")
    
    await process_set_complete(callback.message, state, current_set, callback.from_user.id)

async def process_set_complete(
    message: Message, 
    state: FSMContext, 
    current_set: int, 
    user_id: int
):
    """Обработка завершения подхода и переход к следующему"""
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one()
        
        if current_set < 3:
            # Сохраняем прогресс
            next_set = current_set + 1
            rest_seconds = user.rest_seconds
            
            await state.update_data(current_set=next_set)
            
            # Обновляем pending_step в БД
            user.pending_step = next_set
            await session.commit()
            
            await message.answer(
                f"😌 <b>Отдыхай {rest_seconds} секунд</b>\n"
                f"Я напомню о следующем подходе 💤"
            )
            
            # Ждем и напоминаем
            await asyncio.sleep(rest_seconds)
            
            reps = user.current_reps_per_set
            await message.answer(
                f"⏰ Время второго подхода!\n"
                f"💪 <b>Подход {next_set} из 3:</b>\n"
                f"Сделай {reps} отжиманий!",
                reply_markup=get_workout_keyboard(reps)
            )
            
            set_state_name = f"waiting_for_set{next_set}"
            await state.set_state(getattr(WorkoutSession, set_state_name))
        
        else:
            # Последний подход выполнен
            await finish_workout(message, state, user_id)

async def finish_workout(message: Message, state: FSMContext, user_id: int):
    """Завершение тренировки и сбор обратной связи"""
    
    async with async_session() as session:
        # Получаем данные тренировки
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
        
        # Сохраняем результаты подходов
        data = await state.get_data()
        
        workout.set1_reps = data.get("set1_reps", user.current_reps_per_set)
        workout.set2_reps = data.get("set2_reps", user.current_reps_per_set)
        workout.set3_reps = data.get("set3_reps", user.current_reps_per_set)
        workout.completed = True
        
        # Сбрасываем pending_step
        user.pending_step = 0
        
        await session.commit()
        
        total_reps = workout.set1_reps + workout.set2_reps + workout.set3_reps
        
        # Проверяем ачивки
        await check_achievements(user_id, session)
        
        await state.clear()
    
    # Сообщение об окончании
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
    """Обработка ответа о тренировке с AI"""
    user_id = message.from_user.id
    
    # Интерпретируем ответ
    result = await interpret_workout_feedback(message.text)
    
    async with async_session() as session:
        # Получаем контекст диалога для AI
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
        
        # Генерируем ответ через AI
        ai_response = await ask_deepseek(message.text, history)
        
        # Сохраняем диалог
        session.add(DialogueHistory(user_id=user_id, role="user", message=message.text))
        session.add(DialogueHistory(user_id=user_id, role="assistant", message=ai_response))
        
        await session.commit()
    
    await message.answer(ai_response, reply_markup=get_main_keyboard())
    await state.clear()

# ---------- STATISTICS ----------

@router.message(F.text == "📊 Прогресс")
@router.message(Command("progress"))
async def show_progress(message: Message):
    """Показ прогресса пользователя"""
    user_id = message.from_user.id
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one_or_none()
        
        if not user:
            await message.answer("Сначала давай познакомимся! Нажми /start")
            return
        
        # Последние 7 дней тренировок
        week_ago = date.today() - timedelta(days=7)
        workouts = await session.execute(
            select(Workout).where(
                Workout.user_id == user_id,
                Workout.date >= week_ago
            ).order_by(Workout.date.desc())
        )
        workouts = workouts.scalars().all()
        
        # Статистика
        completed_this_week = sum(1 for w in workouts if w.completed)
        total_reps_this_week = sum(
            w.set1_reps + w.set2_reps + w.set3_reps 
            for w in workouts if w.completed
        )
        
        # Стрик дней
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

# ---------- ACHIEVEMENTS ----------

ACHIEVEMENTS = {
    "first_week": {"title": "👶 Первая неделя", "desc": "Программа началась!"},
    "first_workout": {"title": "💪 Первая тренировка", "desc": "Начало пути"},
    "streak_5": {"title": "🔥 5 дней подряд", "desc": "Отличная дисциплина!"},
    "streak_10": {"title": "💎 10 дней без пропусков", "desc": "Железная воля!"},
    "streak_30": {"title": "👑 30 дней подряд", "desc": "Ты легенда!"},
    "total_100": {"title": "💯 100 отжиманий за день", "desc": "Отличный результат!"},
    "total_200": {"title": "🚀 200 отжиманий за день", "desc": "Невероятно!"},
    "new_record": {"title": "⭐ Новый рекорд", "desc": "Побит личный рекорд!"},
    "week_4": {"title": "📅 Месяц тренировок", "desc": "Месяц регулярных занятий!"}
}

@router.message(F.text == "🏆 Достижения")
@router.message(Command("achievements"))
async def show_achievements(message: Message):
    """Показ достижений"""
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
    """Проверка и выдача достижений"""
    
    # Получаем существующие достижения
    existing = await session.execute(
        select(Achievement.title).where(Achievement.user_id == user_id)
    )
    existing_titles = set(existing.scalars().all())
    
    # Получаем статистику
    workouts_count = await session.execute(
        select(func.count(Workout.id)).where(
            Workout.user_id == user_id,
            Workout.completed == True
        )
    )
    workouts_count = workouts_count.scalar()
    
    # Первая тренировка
    if "first_workout" not in existing_titles and workouts_count >= 1:
        session.add(Achievement(
            user_id=user_id,
            title=ACHIEVEMENTS["first_workout"]["title"],
            description=ACHIEVEMENTS["first_workout"]["desc"]
        ))
    
    # Стрики
    streak = await calculate_streak(user_id, session)
    
    if streak >= 5 and "streak_5" not in existing_titles:
        session.add(Achievement(
            user_id=user_id,
            title=ACHIEVEMENTS["streak_5"]["title"],
            description=ACHIEVEMENTS["streak_5"]["desc"]
        ))
    
    if streak >= 10 and "streak_10" not in existing_titles:
        session.add(Achievement(
            user_id=user_id,
            title=ACHIEVEMENTS["streak_10"]["title"],
            description=ACHIEVEMENTS["streak_10"]["desc"]
        ))
    
    await session.flush()

async def calculate_streak(user_id: int, session: AsyncSession) -> int:
    """Расчет текущего стрика дней подряд"""
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

# ---------- SETTINGS ----------

@router.message(F.text == "⚙️ Настройки")
@router.message(Command("settings"))
async def show_settings(message: Message):
    """Показ настроек"""
    user_id = message.from_user.id
    
    async with async_session() as session:
        user = await session.execute(select(User).where(User.user_id == user_id))
        user = user.scalar_one_or_none()
        
        if not user:
            await message.answer("Сначала давай познакомимся! Нажми /start")
            return
        
        local_time = from_utc(user.reminder_time, user.timezone)
        
        settings_text = (
            f"⚙️ <b>НАСТРОЙКИ</b>\n\n"
            f"👤 Имя: {user.name}\n"
            f"🕐 Время напоминания: {local_time.strftime('%H:%M')}\n"
            f"🌍 Часовой пояс: {user.timezone}\n"
            f"🔔 Напоминания: {'Вкл' if user.reminder_on else 'Выкл'}\n"
            f"⏱ Отдых между подходами: {user.rest_seconds} сек\n\n"
            f"Используй кнопки для изменения:"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🕐 Изменить время", callback_data="change_time")],
            [InlineKeyboardButton(text="🔔 Напоминания вкл/выкл", callback_data="toggle_remind")],
            [InlineKeyboardButton(text="🔧 Изменить подходы", callback_data="change_reps")]
        ])
        
        await message.answer(settings_text, reply_markup=keyboard)

@router.callback_query(F.data == "change_time")
async def change_time(callback: CallbackQuery, state: FSMContext):
    """Изменение времени напоминания"""
    await callback.answer()
    await callback.message.answer(
        "🕐 Выбери новое время для напоминаний:",
        reply_markup=get_time_keyboard()
    )
    await state.set_state(Settings.waiting_for_time)

@router.callback_query(Settings.waiting_for_time)
async def process_new_time(callback: CallbackQuery, state: FSMContext):
    """Обработка нового времени"""
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
        
        # Конвертируем в UTC
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
    """Обработка своего времени"""
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
    """Включение/выключение напоминаний"""
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

@router.message(F.text == "😴 Отдых")
@router.message(Command("restday"))
async def rest_day(message: Message):
    """День отдыха"""
    user_id = message.from_user.id
    today = date.today()
    
    async with async_session() as session:
        # Проверяем, не было ли отдыха в последние 10 дней
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
        
        # Создаем запись об отдыхе
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
            session.add(Workout(
                user_id=user_id,
                date=today,
                rest_day=True
            ))
        
        await session.commit()
    
    await message.answer(
        "😴 <b>День отдыха активирован!</b>\n\n"
        "Восстановление — важная часть прогресса.\n"
        "Возвращайся завтра с новыми силами! 💪",
        reply_markup=get_main_keyboard()
    )

@router.message(Command("skip"))
async def skip_today(message: Message):
    """Пропуск тренировки сегодня"""
    user_id = message.from_user.id
    today = date.today()
    
    async with async_session() as session:
        existing = await session.execute(
            select(Workout).where(
                Workout.user_id == user_id,
                Workout.date == today
            )
        )
        existing = existing.scalar_one_or_none()
        
        if existing and existing.completed:
            await message.answer("Ты уже позанимался сегодня! Отдыхай 😊")
            return
        
        if existing:
            existing.skipped = True
        else:
            session.add(Workout(
                user_id=user_id,
                date=today,
                skipped=True
            ))
        
        await session.commit()
    
    await message.answer(
        "😊 Без проблем! Отдыхай сегодня.\n"
        "Завтра жду тебя с новыми силами! 💪",
        reply_markup=get_main_keyboard()
    )

@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
async def show_help(message: Message):
    """Помощь по командам"""
    help_text = (
        "🤖 <b>ОТЖИМАЙКИН — ПОМОЩЬ</b>\n\n"
        "🏋️ <b>Основные команды:</b>\n"
        "/workout — начать тренировку\n"
        "/progress — твой прогресс\n"
        "/achievements — достижения\n\n"
        "⚙️ <b>Управление:</b>\n"
        "/settings — настройки\n"
        "/skip — пропустить день\n"
        "/restday — день отдыха\n"
        "/settime — изменить время\n"
        "/remind — вкл/выкл напоминания\n\n"
        "💡 <b>Как это работает:</b>\n"
        "• Каждый день 3 подхода отжиманий\n"
        "• Нагрузка растёт раз в неделю\n"
        "• Отдых между подходами 90 сек\n"
        "• Ачивки за регулярность! 🏆"
    )
    await message.answer(help_text, reply_markup=get_main_keyboard())

# ---------- REMINDER SCHEDULER (KEEP-ALIVE) ----------

# Глобальный флаг для keep-alive задач
reminder_task = None
weekly_report_task = None
keep_alive_task = None

async def reminder_checker():
    """
    Проверка и отправка напоминаний каждые 60 секунд.
    Эта функция работает постоянно и не дает боту заснуть.
    """
    logger.info("Reminder checker started")
    
    while True:
        try:
            utc_now = datetime.utcnow()
            current_time = utc_now.time()
            current_date = utc_now.date()
            
            async with async_session() as session:
                # Получаем всех пользователей с включенными напоминаниями
                users = await session.execute(
                    select(User).where(User.reminder_on == True)
                )
                users = users.scalars().all()
                
                for user in users:
                    try:
                        # Конвертируем время напоминания в UTC
                        reminder_utc = user.reminder_time
                        
                        # Проверяем, совпадает ли час и минута
                        if (current_time.hour == reminder_utc.hour and 
                            current_time.minute == reminder_utc.minute and
                            user.last_reminder_date != current_date):
                            
                            # Отправляем напоминание
                            local_time = from_utc(reminder_utc, user.timezone)
                            
                            await bot.send_message(
                                user.user_id,
                                f"🔔 {user.name}, время размять плечи!\n\n"
                                f"Сегодня 3 подхода по {user.current_reps_per_set} отжиманий.\n"
                                f"Жду тебя! 🔥",
                                reply_markup=get_workout_keyboard(user.current_reps_per_set)
                            )
                            
                            # Обновляем дату последнего напоминания
                            user.last_reminder_date = current_date
                            await session.commit()
                            
                            logger.info(f"Reminder sent to {user.name} ({user.user_id})")
                    
                    except Exception as e:
                        logger.error(f"Error sending reminder to {user.user_id}: {e}")
            
        except Exception as e:
            logger.error(f"Reminder checker error: {e}")
        
        # Ждем 60 секунд до следующей проверки
        await asyncio.sleep(60)

async def weekly_reports():
    """
    Еженедельные отчеты по воскресеньям в 20:00 UTC
    """
    logger.info("Weekly report scheduler started")
    
    while True:
        try:
            now = datetime.utcnow()
            
            # Проверяем, воскресенье ли сейчас и 20:00 UTC
            if now.weekday() == 6 and now.hour == 20 and now.minute == 0:
                logger.info("Sending weekly reports...")
                
                async with async_session() as session:
                    users = await session.execute(select(User))
                    users = users.scalars().all()
                    
                    for user in users:
                        try:
                            # Статистика за неделю
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
                            
                            # Условие повышения нагрузки
                            if completed >= 6:  # 6 из 7 дней
                                new_reps = calculate_weekly_progression(user.current_reps_per_set)
                                
                                await bot.send_message(
                                    user.user_id,
                                    f"📊 <b>НЕДЕЛЬНЫЙ ОТЧЁТ</b>\n\n"
                                    f"💪 Тренировок: {completed}/7\n"
                                    f"🔥 Всего отжиманий: {total_reps}\n"
                                    f"📈 Текущий уровень: {user.current_reps_per_set} в подходе\n\n"
                                    f"🎉 <b>Отличная неделя! Повышаю нагрузку!</b>\n"
                                    f"Теперь: 3 подхода по <b>{new_reps}</b> отжиманий!\n"
                                    f"Ты становишься сильнее! 🚀"
                                )
                                
                                user.current_reps_per_set = new_reps
                                user.current_week += 1
                            else:
                                await bot.send_message(
                                    user.user_id,
                                    f"📊 <b>НЕДЕЛЬНЫЙ ОТЧЁТ</b>\n\n"
                                    f"💪 Тренировок: {completed}/7\n"
                                    f"🔥 Всего отжиманий: {total_reps}\n\n"
                                    f"💪 <b>Не спешим, главное — регулярность!</b>\n"
                                    f"На этой неделе пробуем ещё раз ту же нагрузку:\n"
                                    f"3 подхода по {user.current_reps_per_set} отжиманий.\n"
                                    f"Я верю в тебя! 💪"
                                )
                            
                            await session.commit()
                        
                        except Exception as e:
                            logger.error(f"Error sending report to {user.user_id}: {e}")
            
            # Проверяем каждые 60 секунд
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"Weekly report error: {e}")
            await asyncio.sleep(60)

async def keep_alive_self_ping():
    """
    Самостоятельный пинг для предотвращения засыпания.
    Отправляет HTTP-запрос самому себе каждые 14 минут.
    """
    logger.info("Keep-alive self ping started")
    
    # Ждем 30 секунд после старта
    await asyncio.sleep(30)
    
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                # Пингуем собственный health-check endpoint
                async with session.get(f"{WEBHOOK_URL}/health", timeout=10) as resp:
                    if resp.status == 200:
                        logger.debug("Keep-alive ping successful")
                    else:
                        logger.warning(f"Keep-alive ping returned {resp.status}")
        except Exception as e:
            logger.error(f"Keep-alive ping failed: {e}")
        
        # Пинг каждые 14 минут (Render засыпает после 15 минут без запросов)
        await asyncio.sleep(840)  # 14 минут

# ============ WEB SERVER ============

async def health_check(request):
    """Health check endpoint для Render и keep-alive"""
    return web.Response(
        text=json.dumps({
            "status": "ok",
            "timestamp": datetime.utcnow().isoformat(),
            "service": "PushUp Bot"
        }),
        content_type="application/json"
    )

async def manual_webhook_handler(request):
    """Ручной обработчик вебхука для отладки"""
    if request.method == "POST":
        try:
            data = await request.json()
            logger.debug(f"Webhook received: {data}")
        except Exception as e:
            logger.error(f"Webhook error: {e}")
    
    return web.Response(text="ok")

# ============ MAIN APPLICATION ============

async def main():
    """Главная функция запуска бота"""
    
    # Инициализация бота и диспатчера
    global bot, dp
    
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    
    dp = Dispatcher()
    dp.include_router(router)
    
    # Инициализация базы данных
    try:
        await init_db()
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        sys.exit(1)
    
    # Запускаем фоновые задачи
    global reminder_task, weekly_report_task, keep_alive_task
    
    reminder_task = asyncio.create_task(reminder_checker())
    weekly_report_task = asyncio.create_task(weekly_reports())
    keep_alive_task = asyncio.create_task(keep_alive_self_ping())
    
    # Настройка вебхука
    webhook_path = "/webhook"
    app = web.Application()
    
    # Health check endpoint
    app.router.add_get("/health", health_check)
    app.router.add_get("/", health_check)
    
    # Обработчик вебхука от Telegram
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=webhook_path)
    
    # Настройка приложения для aiogram
    setup_application(app, dp, bot=bot)
    
    # Установка вебхука Telegram
    try:
        await bot.set_webhook(
            f"{WEBHOOK_URL}{webhook_path}",
            drop_pending_updates=True
        )
        logger.info(f"Webhook set to {WEBHOOK_URL}{webhook_path}")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        # Пробуем запуститься в режиме polling если вебхук не работает
        logger.info("Falling back to polling mode...")
        await dp.start_polling(bot)
        return
    
    # Запуск веб-сервера
    logger.info(f"Starting web server on port {PORT}")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    
    try:
        await site.start()
        logger.info("Bot is running!")
        
        # Держим приложение запущенным
        while True:
            await asyncio.sleep(3600)
            
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
    finally:
        # Очистка
        reminder_task.cancel()
        weekly_report_task.cancel()
        keep_alive_task.cancel()
        
        await bot.delete_webhook()
        await dp.stop_polling()
        await runner.cleanup()
        logger.info("Bot stopped")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
