"""Telegram bot — main entrypoint."""
from __future__ import annotations
import asyncio
import logging
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import aiohttp

from . import config, pipeline, storage, ffwrap, buzzcast_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("argus.bot")

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
@dataclass
class Pending:
    """Per-user wizard state while choosing options for the next job."""
    url: str
    hosters: list[str] | None = None
    quality: str | None = None
    ts_duration: int | None = None
    msg_id: int | None = None    # message we keep editing
    chat_id: int | None = None
    created: float = field(default_factory=time.time)


@dataclass
class ActiveJob:
    task: asyncio.Task
    registry: ffwrap.ProcRegistry


_pending: Dict[int, Pending] = {}            # user_id -> Pending
_active: Dict[str, ActiveJob] = {}           # job_id -> ActiveJob


# ---------------------------------------------------------------------------
# Texts
# ---------------------------------------------------------------------------
WELCOME = (
    "👁️ <b>ARGUS</b> — запись стримов без лишних хлопот.\n\n"
    "<b>📺 Основные функции:</b>\n"
    "• Запись любых стримов (m3u8/flv/любой URL)\n"
    "• Запись стримов BuzzCast по ID пользователя\n"
    "• Автоматический мониторинг и запись стримов\n\n"
    "<b>🎯 Доступные команды:</b>\n"
    "/start — начало диалога\n"
    "/status — активные задачи\n"
    "/cancel &lt;job_id&gt; — отменить задачу\n"
    "/buzzcast &lt;userId&gt; — записать стрим BuzzCast\n"
    "/monitoring — управление мониторингом\n"
    "/monitoring &lt;userId&gt; — добавить в мониторинг"
)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------
def _kb_hoster() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📦 Ranoz (4.99 GB)", callback_data="h:ranoz"),
            InlineKeyboardButton(text="⏳ Tempshare (2 GB)", callback_data="h:tempshare"),
        ],
        [InlineKeyboardButton(text="🚀 Оба сразу", callback_data="h:both")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="h:cancel")],
    ])


def _kb_quality() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"q:{key}")]
        for key, (_, _, label) in config.QUALITY_PRESETS.items()
    ]
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="q:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_ts_duration() -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(text=f"{d} {'день' if d == 1 else 'дня' if d == 3 else 'дней'}",
                             callback_data=f"d:{d}")
        for d in config.TEMPSHARE_DURATIONS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="d:cancel")],
    ])


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def _kb_quick_actions() -> InlineKeyboardMarkup:
    """Quick action buttons for /start."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Мои задачи", callback_data="quick:status")],
        [InlineKeyboardButton(text="📡 Мониторинг", callback_data="quick:monitoring")],
        [InlineKeyboardButton(text="📺 BuzzCast запись", callback_data="quick:buzzcast")],
    ])


async def cmd_start(m: Message) -> None:
    await m.answer(WELCOME, reply_markup=_kb_quick_actions())


async def cmd_status(m: Message) -> None:
    rows = storage.list_user_active(m.from_user.id)
    if not rows:
        await m.answer("Активных задач нет.")
        return
    lines = [f"<b>Активных задач: {len(rows)}</b>", ""]
    for j in rows:
        age = int(time.time() - j.created_at)
        lines.append(
            f"• <code>{j.id}</code>  <i>{j.status}</i> · {j.progress or '—'}  "
            f"({age}s)\n  {j.url[:80]}"
        )
    lines.append("")
    lines.append("Отменить:  /cancel &lt;job_id&gt;")
    await m.answer("\n".join(lines), disable_web_page_preview=True)


async def cmd_cancel(m: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg:
        await m.answer("Укажи id задачи: <code>/cancel &lt;job_id&gt;</code>\n"
                       "Список активных — /status")
        return
    job = storage.get(arg)
    if not job or job.user_id != m.from_user.id:
        await m.answer("Задача не найдена.")
        return
    if job.status not in storage.ACTIVE_STATUSES:
        await m.answer(f"Задача уже в статусе <i>{job.status}</i>.")
        return

    aj = _active.get(arg)
    if aj is None:
        # process-restart edge case: not in memory, just mark cancelled in DB
        storage.update(arg, status="cancelled", error="cancelled (no live task)")
        await m.answer(f"🛑 Задача <code>{arg}</code> помечена отменённой.")
        return

    aj.registry.kill_all()
    aj.task.cancel()
    await m.answer(f"🛑 Задача <code>{arg}</code> отменяется…")


async def cmd_buzzcast(m: Message, command: CommandObject) -> None:
    """Handle /buzzcast <userId> command."""
    arg = (command.args or "").strip()
    if not arg:
        await m.answer(
            "📺 <b>BuzzCast запись</b>\n\n"
            "Использование: <code>/buzzcast &lt;userId&gt;</code>\n"
            "Пример: <code>/buzzcast 12345678</code>\n\n"
            "Бот проверит, в эфире ли пользователь, и если да — "
            "запустит запись стрима."
        )
        return
    
    user_id = m.from_user.id
    buzzcast_id = arg
    
    # Show progress message
    status_msg = await m.answer(
        f"🔍 Ищу пользователя BuzzCast <code>{buzzcast_id}</code>..."
    )
    
    try:
        async with aiohttp.ClientSession() as session:
            client = buzzcast_client.BuzzCastClient()
            stream_url, info = await client.get_stream_url(session, buzzcast_id)
            
            if info is None:
                await status_msg.edit_text(
                    f"❌ Пользователь <code>{buzzcast_id}</code> не найден на BuzzCast."
                )
                return
            
            if stream_url is None:
                # User exists but offline
                nick = info.get("userNickName", "Unknown")
                await status_msg.edit_text(
                    f"💤 Пользователь <b>{nick}</b> (ID: <code>{buzzcast_id}</code>) "
                    f"сейчас не в эфире.\n\n"
                    f"Используй <code>/monitoring {buzzcast_id}</code> для автоматического "
                    f"мониторинга и записи при выходе в эфир."
                )
                return
            
            # User is live!
            nick = info.get("userNickName", info.get("infoName", "Unknown"))
            online = info.get("onlineNum", 0)
            await status_msg.edit_text(
                f"✅ Стрим найден!\n"
                f"👤 <b>{nick}</b> (ID: <code>{buzzcast_id}</code>)\n"
                f"👥 Зрителей: {online}\n"
                f"🔗 URL: <code>{stream_url[:60]}...</code>\n\n"
                f"Выбери параметры записи:"
            )
            
            # Start wizard with this URL
            _pending[user_id] = Pending(url=stream_url)
            sent = await m.answer(
                f"🎯 BuzzCast стрим: <b>{nick}</b>\n"
                f"<code>{stream_url}</code>\n\n"
                f"Куда заливать готовую запись?",
                reply_markup=_kb_hoster(),
                disable_web_page_preview=True,
            )
            _pending[user_id].msg_id = sent.message_id
            _pending[user_id].chat_id = sent.chat.id
            
    except Exception as e:
        log.exception("buzzcast command failed")
        await status_msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_monitoring(m: Message, command: CommandObject) -> None:
    """Handle /monitoring [userId] command."""
    arg = (command.args or "").strip()
    user_id = m.from_user.id
    
    if not arg:
        # Show monitoring list
        entries = storage.monitoring_list(user_id)
        if not entries:
            await m.answer(
                "📡 <b>Мониторинг BuzzCast</b>\n\n"
                "Список пуст. Добавь пользователей для мониторинга:\n"
                "<code>/monitoring &lt;userId&gt;</code>\n\n"
                "Бот будет проверять каждые 5 минут, начался ли стрим, "
                "и автоматически начинать запись."
            )
            return
        
        lines = [f"📡 <b>Мониторинг ({len(entries)})</b>", ""]
        for entry in entries:
            status = "🟢 активен" if entry.active else "⏸ пауза"
            job_info = f" [📹 {entry.active_job_id[:8]}]" if entry.active_job_id else ""
            lines.append(
                f"• <code>{entry.buzzcast_user_id}</code> {status}{job_info}\n"
                f"  📤 {', '.join(entry.hosters)} · {entry.quality}"
            )
        lines.append("")
        lines.append("➕ Добавить: <code>/monitoring &lt;userId&gt;</code>")
        
        # Add remove buttons
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"🗑 Удалить {e.buzzcast_user_id}",
                callback_data=f"mon_del:{e.id}"
            )]
            for e in entries
        ])
        
        await m.answer("\n".join(lines), reply_markup=kb)
        return
    
    # Add user to monitoring - start wizard
    buzzcast_id = arg
    status_msg = await m.answer(
        f"🔍 Проверяю пользователя BuzzCast <code>{buzzcast_id}</code>..."
    )
    
    try:
        async with aiohttp.ClientSession() as session:
            client = buzzcast_client.BuzzCastClient()
            user_profile = await client.search_user(session, buzzcast_id)
            
            if user_profile is None:
                await status_msg.edit_text(
                    f"❌ Пользователь <code>{buzzcast_id}</code> не найден на BuzzCast."
                )
                return
            
            nick = user_profile.get("userNickName", "Unknown")
            live_state = user_profile.get("liveState", 0)
            live_str = "🔴 В эфире" if live_state else "⚫ Не в эфире"
            
            await status_msg.edit_text(
                f"✅ Пользователь найден!\n"
                f"👤 <b>{nick}</b> (ID: <code>{buzzcast_id}</code>)\n"
                f"📡 Статус: {live_str}\n\n"
                f"Настрой параметры записи для мониторинга:"
            )
            
            # Start wizard - store buzzcast_id in URL field with prefix
            _pending[user_id] = Pending(url=f"buzzcast:{buzzcast_id}")
            sent = await m.answer(
                f"📡 Мониторинг: <b>{nick}</b> (<code>{buzzcast_id}</code>)\n\n"
                f"Куда заливать записи?",
                reply_markup=_kb_hoster(),
            )
            _pending[user_id].msg_id = sent.message_id
            _pending[user_id].chat_id = sent.chat.id
            
    except Exception as e:
        log.exception("monitoring command failed")
        await status_msg.edit_text(f"❌ Ошибка: {e}")


async def on_monitoring_delete(cq: CallbackQuery) -> None:
    """Handle monitoring entry deletion."""
    _, entry_id_str = cq.data.split(":", 1)
    try:
        entry_id = int(entry_id_str)
    except ValueError:
        await cq.answer("Некорректный ID", show_alert=True)
        return
    
    # Get entry to check ownership
    entries = storage.monitoring_list(cq.from_user.id)
    entry = next((e for e in entries if e.id == entry_id), None)
    
    if entry is None:
        await cq.answer("Запись не найдена", show_alert=True)
        return
    
    storage.monitoring_update(entry_id, active=0)
    await cq.answer(f"✅ Мониторинг {entry.buzzcast_user_id} удалён")
    
    # Refresh list
    entries = storage.monitoring_list(cq.from_user.id)
    if not entries:
        await cq.message.edit_text(
            "📡 <b>Мониторинг BuzzCast</b>\n\n"
            "Список пуст."
        )
        return
    
    lines = [f"📡 <b>Мониторинг ({len(entries)})</b>", ""]
    for e in entries:
        if not e.active:
            continue
        status = "🟢 активен" if e.active else "⏸ пауза"
        job_info = f" [📹 {e.active_job_id[:8]}]" if e.active_job_id else ""
        lines.append(
            f"• <code>{e.buzzcast_user_id}</code> {status}{job_info}\n"
            f"  📤 {', '.join(e.hosters)} · {e.quality}"
        )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🗑 Удалить {e.buzzcast_user_id}",
            callback_data=f"mon_del:{e.id}"
        )]
        for e in entries if e.active
    ])
    
    await cq.message.edit_text("\n".join(lines), reply_markup=kb)


async def on_quick_action(cq: CallbackQuery) -> None:
    """Handle quick action buttons from /start."""
    action = cq.data.split(":", 1)[1]
    
    if action == "status":
        await cq.answer()
        rows = storage.list_user_active(cq.from_user.id)
        if not rows:
            await cq.message.answer("Активных задач нет.")
            return
        lines = [f"<b>Активных задач: {len(rows)}</b>", ""]
        for j in rows:
            age = int(time.time() - j.created_at)
            lines.append(
                f"• <code>{j.id}</code>  <i>{j.status}</i> · {j.progress or '—'}  "
                f"({age}s)\n  {j.url[:80]}"
            )
        lines.append("")
        lines.append("Отменить:  /cancel &lt;job_id&gt;")
        await cq.message.answer("\n".join(lines), disable_web_page_preview=True)
    
    elif action == "monitoring":
        await cq.answer()
        entries = storage.monitoring_list(cq.from_user.id)
        if not entries:
            await cq.message.answer(
                "📡 <b>Мониторинг BuzzCast</b>\n\n"
                "Список пуст. Добавь пользователей:\n"
                "<code>/monitoring &lt;userId&gt;</code>"
            )
            return
        
        lines = [f"📡 <b>Мониторинг ({len(entries)})</b>", ""]
        for entry in entries:
            if not entry.active:
                continue
            status = "🟢 активен" if entry.active else "⏸ пауза"
            job_info = f" [📹 {entry.active_job_id[:8]}]" if entry.active_job_id else ""
            lines.append(
                f"• <code>{entry.buzzcast_user_id}</code> {status}{job_info}\n"
                f"  📤 {', '.join(entry.hosters)} · {entry.quality}"
            )
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"🗑 Удалить {e.buzzcast_user_id}",
                callback_data=f"mon_del:{e.id}"
            )]
            for e in entries if e.active
        ])
        
        await cq.message.answer("\n".join(lines), reply_markup=kb)
    
    elif action == "buzzcast":
        await cq.answer()
        await cq.message.answer(
            "📺 <b>BuzzCast запись</b>\n\n"
            "Использование: <code>/buzzcast &lt;userId&gt;</code>\n"
            "Пример: <code>/buzzcast 12345678</code>"
        )



async def on_url(m: Message) -> None:
    text = (m.text or "").strip()
    if not URL_RE.match(text):
        await m.answer("Это не похоже на ссылку. Пришли URL стрима, начинающийся с http(s)://")
        return
    user_id = m.from_user.id
    _pending[user_id] = Pending(url=text)
    sent = await m.answer(
        f"🎯 Ссылка принята:\n<code>{text}</code>\n\n"
        f"Куда заливать готовую запись?",
        reply_markup=_kb_hoster(),
        disable_web_page_preview=True,
    )
    _pending[user_id].msg_id = sent.message_id
    _pending[user_id].chat_id = sent.chat.id


async def on_hoster(cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    choice = (cq.data or "").split(":", 1)[1]
    pj = _pending.get(user_id)
    if not pj:
        await cq.answer("Сессия истекла, пришли ссылку заново.", show_alert=True)
        return
    if choice == "cancel":
        _pending.pop(user_id, None)
        await cq.message.edit_text("✖️ Отменено.")
        await cq.answer()
        return
    pj.hosters = {"ranoz": ["ranoz"], "tempshare": ["tempshare"],
                  "both": ["ranoz", "tempshare"]}[choice]

    label_map = {"ranoz": "Ranoz", "tempshare": "Tempshare"}
    h_text = " + ".join(label_map[h] for h in pj.hosters)
    await cq.message.edit_text(
        f"🎯 <code>{pj.url}</code>\n"
        f"📤 Хостер: <b>{h_text}</b>\n\n"
        f"Выбери качество сжатия:",
        reply_markup=_kb_quality(),
        disable_web_page_preview=True,
    )
    await cq.answer()


async def on_quality(cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    choice = (cq.data or "").split(":", 1)[1]
    pj = _pending.get(user_id)
    if not pj or not pj.hosters:
        await cq.answer("Сессия истекла, пришли ссылку заново.", show_alert=True)
        return
    if choice == "cancel":
        _pending.pop(user_id, None)
        await cq.message.edit_text("✖️ Отменено.")
        await cq.answer()
        return
    if choice not in config.QUALITY_PRESETS:
        await cq.answer("Неизвестное качество.", show_alert=True)
        return
    pj.quality = choice

    if "tempshare" in pj.hosters:
        # ask for storage duration
        # Check if this is monitoring setup
        url_display = pj.url
        if pj.url.startswith("buzzcast:"):
            buzzcast_id = pj.url.replace("buzzcast:", "")
            url_display = f"Мониторинг BuzzCast ID: {buzzcast_id}"
        
        await cq.message.edit_text(
            f"🎯 {url_display}\n"
            f"📤 Хостер: {' + '.join(pj.hosters)}\n"
            f"🗜 Качество: {config.QUALITY_PRESETS[choice][2]}\n\n"
            f"Сколько дней хранить файл в Tempshare?",
            reply_markup=_kb_ts_duration(),
            disable_web_page_preview=True,
        )
        await cq.answer()
        return

    # tempshare not selected
    pj.ts_duration = config.DEFAULT_TS_DURATION
    
    # Check if this is monitoring setup
    if pj.url.startswith("buzzcast:"):
        buzzcast_id = pj.url.replace("buzzcast:", "")
        storage.monitoring_add(
            user_id=user_id,
            chat_id=cq.message.chat.id,
            buzzcast_user_id=buzzcast_id,
            hosters=pj.hosters or [],
            quality=pj.quality or config.DEFAULT_QUALITY,
            ts_duration=pj.ts_duration or config.DEFAULT_TS_DURATION,
        )
        await cq.message.edit_text(
            f"✅ Мониторинг добавлен!\n"
            f"📡 BuzzCast ID: <code>{buzzcast_id}</code>\n"
            f"📤 Хостер: {' + '.join(pj.hosters)}\n"
            f"🗜 Качество: {config.QUALITY_PRESETS[pj.quality][2]}\n\n"
            f"Бот будет проверять каждые 5 минут и автоматически "
            f"начнёт запись при выходе в эфир."
        )
        await cq.answer("Мониторинг активирован!")
        _pending.pop(user_id, None)
        return
    
    # Normal job
    await _start_job(cq.bot, cq, pj)


async def on_duration(cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    choice = (cq.data or "").split(":", 1)[1]
    pj = _pending.get(user_id)
    if not pj or not pj.hosters or not pj.quality:
        await cq.answer("Сессия истекла, пришли ссылку заново.", show_alert=True)
        return
    if choice == "cancel":
        _pending.pop(user_id, None)
        await cq.message.edit_text("✖️ Отменено.")
        await cq.answer()
        return
    try:
        days = int(choice)
    except ValueError:
        await cq.answer("Некорректное значение.", show_alert=True)
        return
    if days not in config.TEMPSHARE_DURATIONS:
        await cq.answer("Некорректный срок.", show_alert=True)
        return
    pj.ts_duration = days
    
    # Check if this is a monitoring setup
    if pj.url.startswith("buzzcast:"):
        buzzcast_id = pj.url.replace("buzzcast:", "")
        storage.monitoring_add(
            user_id=user_id,
            chat_id=cq.message.chat.id,
            buzzcast_user_id=buzzcast_id,
            hosters=pj.hosters or [],
            quality=pj.quality or config.DEFAULT_QUALITY,
            ts_duration=pj.ts_duration or config.DEFAULT_TS_DURATION,
        )
        await cq.message.edit_text(
            f"✅ Мониторинг добавлен!\n"
            f"📡 BuzzCast ID: <code>{buzzcast_id}</code>\n"
            f"📤 Хостер: {' + '.join(pj.hosters)}\n"
            f"🗜 Качество: {config.QUALITY_PRESETS[pj.quality][2]}\n"
            f"⏳ Срок: {pj.ts_duration} дн.\n\n"
            f"Бот будет проверять каждые 5 минут и автоматически "
            f"начнёт запись при выходе в эфир."
        )
        await cq.answer("Мониторинг активирован!")
        _pending.pop(user_id, None)
        return
    
    # Normal job start
    await _start_job(cq.bot, cq, pj)


async def _start_job(bot: Bot, cq: CallbackQuery, pj: Pending) -> None:
    job_id = uuid.uuid4().hex[:10]
    user_id = cq.from_user.id
    label_map = {"ranoz": "Ranoz", "tempshare": "Tempshare"}
    h_text = " + ".join(label_map[h] for h in (pj.hosters or []))
    q_label = config.QUALITY_PRESETS[pj.quality][2]  # type: ignore

    # persist
    storage.insert(storage.Job(
        id=job_id,
        user_id=user_id,
        chat_id=cq.message.chat.id,
        url=pj.url,
        hosters=pj.hosters or [],
        quality=pj.quality or config.DEFAULT_QUALITY,
        ts_duration=pj.ts_duration or 0,
        status="pending",
    ))

    summary_lines = [
        f"⏳ Запускаю запись.  ID: <code>{job_id}</code>",
        f"🎯 URL: <code>{pj.url}</code>",
        f"📤 Хостер: <b>{h_text}</b>",
        f"🗜 Качество: <b>{q_label}</b>",
    ]
    if "tempshare" in (pj.hosters or []):
        summary_lines.append(f"⏳ Tempshare: <b>{pj.ts_duration} дн.</b>")
    summary_lines.append("")
    summary_lines.append("Я буду присылать прогресс. "
                        "Запись идёт до конца стрима — это могут быть часы.")
    summary_lines.append(f"\nОтменить: <code>/cancel {job_id}</code>")

    await cq.message.edit_text("\n".join(summary_lines),
                               disable_web_page_preview=True)
    await cq.answer("Поехали!")
    _pending.pop(user_id, None)

    registry = ffwrap.ProcRegistry()
    task = asyncio.create_task(_run_job(bot, job_id, pj, registry))
    _active[job_id] = ActiveJob(task=task, registry=registry)


async def _run_job(bot: Bot, job_id: str, pj: Pending,
                   registry: ffwrap.ProcRegistry) -> None:
    chat_id = pj.chat_id  # type: ignore
    last_msg = {"id": None, "text": ""}

    async def progress(msg: str):
        if last_msg["id"] is None:
            sent = await bot.send_message(chat_id, msg)
            last_msg["id"] = sent.message_id
            last_msg["text"] = msg
        else:
            if msg != last_msg["text"]:
                try:
                    await bot.edit_message_text(msg, chat_id=chat_id,
                                                message_id=last_msg["id"])
                    last_msg["text"] = msg
                except Exception:
                    sent = await bot.send_message(chat_id, msg)
                    last_msg["id"] = sent.message_id
                    last_msg["text"] = msg

    try:
        result = await pipeline.run_pipeline(
            job_id=job_id,
            stream_url=pj.url,
            hosters=pj.hosters or [],
            quality=pj.quality or config.DEFAULT_QUALITY,
            ts_duration=pj.ts_duration or config.DEFAULT_TS_DURATION,
            registry=registry,
            progress=progress,
        )
    except asyncio.CancelledError:
        registry.kill_all()
        storage.update(job_id, status="cancelled", error="cancelled by user")
        await bot.send_message(chat_id,
            f"🛑 Задача <code>{job_id}</code> отменена.")
        return
    except Exception as e:
        log.exception("pipeline crashed")
        storage.update(job_id, status="failed", error=str(e))
        await bot.send_message(chat_id, f"❌ Внутренняя ошибка: {e}")
        return
    finally:
        _active.pop(job_id, None)

    if result.cancelled:
        await bot.send_message(chat_id,
            f"🛑 Задача <code>{job_id}</code> отменена.")
        return
    if not result.ok:
        await bot.send_message(chat_id,
            f"❌ <code>{job_id}</code> не завершилась.\n"
            f"Ошибка: <code>{result.error}</code>")
        return

    lines = [
        f"🎉 <b>Готово!</b>  <code>{job_id}</code>",
        f"⏱ Заняло: {int(result.elapsed)}s",
        f"📥 Запись: {_h(result.raw_size)} ({int(result.duration_sec)}s)",
        f"📤 После сжатия: {_h(result.final_size)}",
        "",
    ]
    for label, urls in result.links.items():
        lines.append(f"<b>{label}</b> ({len(urls)} файл(ов)):")
        for u in urls:
            lines.append(f"• {u}")
        lines.append("")
    await bot.send_message(chat_id, "\n".join(lines),
                           disable_web_page_preview=True)


def _h(n: int) -> str:
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} PB"


# ---------------------------------------------------------------------------
# Monitoring background task
# ---------------------------------------------------------------------------
async def monitoring_loop(bot: Bot) -> None:
    """Background task that checks monitored users every 5 minutes."""
    log.info("Monitoring loop started")
    
    while True:
        try:
            await asyncio.sleep(300)  # 5 minutes
            
            entries = storage.monitoring_get_active()
            if not entries:
                continue
            
            log.info(f"Checking {len(entries)} monitored users")
            
            async with aiohttp.ClientSession() as session:
                for entry in entries:
                    try:
                        # Skip if already recording
                        if entry.active_job_id:
                            job = storage.get(entry.active_job_id)
                            if job and job.status in storage.ACTIVE_STATUSES:
                                continue  # Still recording
                            else:
                                # Job finished or doesn't exist, clear it
                                storage.monitoring_update(entry.id, active_job_id="")
                        
                        # Check if user is live
                        client = buzzcast_client.BuzzCastClient()
                        stream_url, info = await client.get_stream_url(
                            session, entry.buzzcast_user_id
                        )
                        
                        storage.monitoring_update(entry.id, last_check=int(time.time()))
                        
                        if stream_url is None:
                            # Not live
                            continue
                        
                        # User is live! Start recording
                        log.info(f"User {entry.buzzcast_user_id} is live, starting recording")
                        
                        job_id = uuid.uuid4().hex[:10]
                        nick = info.get("userNickName", info.get("infoName", "Unknown"))
                        
                        storage.insert(storage.Job(
                            id=job_id,
                            user_id=entry.user_id,
                            chat_id=entry.chat_id,
                            url=stream_url,
                            hosters=entry.hosters,
                            quality=entry.quality,
                            ts_duration=entry.ts_duration,
                            status="pending",
                        ))
                        
                        storage.monitoring_update(entry.id, active_job_id=job_id)
                        
                        # Notify user
                        try:
                            await bot.send_message(
                                entry.chat_id,
                                f"🔴 <b>Мониторинг: стрим начался!</b>\n\n"
                                f"👤 <b>{nick}</b> (ID: <code>{entry.buzzcast_user_id}</code>)\n"
                                f"⏳ Запускаю запись... ID: <code>{job_id}</code>\n\n"
                                f"Отменить: <code>/cancel {job_id}</code>"
                            )
                        except Exception:
                            pass
                        
                        # Start recording task
                        pj = Pending(
                            url=stream_url,
                            hosters=entry.hosters,
                            quality=entry.quality,
                            ts_duration=entry.ts_duration,
                            chat_id=entry.chat_id,
                        )
                        registry = ffwrap.ProcRegistry()
                        task = asyncio.create_task(_run_job(bot, job_id, pj, registry))
                        _active[job_id] = ActiveJob(task=task, registry=registry)
                        
                    except Exception as e:
                        log.exception(f"Error checking monitored user {entry.buzzcast_user_id}: {e}")
                        continue
        
        except Exception as e:
            log.exception(f"Monitoring loop error: {e}")
            await asyncio.sleep(60)  # Wait 1 minute on error


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
async def main() -> None:
    if not config.BOT_TOKEN:
        print("ERROR: BOT_TOKEN is empty. Set it in /opt/argus-bot/.env",
              file=sys.stderr)
        sys.exit(1)

    storage.init()
    n = storage.mark_stale_interrupted()
    if n:
        log.warning("marked %d stale active job(s) as 'interrupted'", n)

    bot = Bot(config.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_status, Command("status"))
    dp.message.register(cmd_cancel, Command("cancel"))
    dp.message.register(cmd_buzzcast, Command("buzzcast"))
    dp.message.register(cmd_monitoring, Command("monitoring"))
    dp.message.register(on_url, F.text)
    dp.callback_query.register(on_hoster, F.data.startswith("h:"))
    dp.callback_query.register(on_quality, F.data.startswith("q:"))
    dp.callback_query.register(on_duration, F.data.startswith("d:"))
    dp.callback_query.register(on_monitoring_delete, F.data.startswith("mon_del:"))
    dp.callback_query.register(on_quick_action, F.data.startswith("quick:"))

    log.info("ARGUS bot starting…")
    
    # Start monitoring background task
    monitoring_task = asyncio.create_task(monitoring_loop(bot))
    
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        monitoring_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
