"""Telegram bot — main entrypoint."""
from __future__ import annotations
import asyncio
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Dict

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from . import config, pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("argus.bot")

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


@dataclass
class PendingJob:
    url: str
    created: float = field(default_factory=time.time)


# user_id -> last sent URL waiting for hoster choice
_pending: Dict[int, PendingJob] = {}
# user_id -> active job count (cap concurrency per user)
_active: Dict[int, int] = {}
MAX_PER_USER = 3


def _kb_choose() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📦 Ranoz (4.99 GB)", callback_data="h:ranoz"),
            InlineKeyboardButton(text="⏳ Tempshare (2 GB, 7д)", callback_data="h:tempshare"),
        ],
        [InlineKeyboardButton(text="🚀 Оба сразу", callback_data="h:both")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="h:cancel")],
    ])


WELCOME = (
    "<b>ARGUS — рекордер стримов</b>\n\n"
    "Пришли мне ссылку на стрим (m3u8 / flv / любой URL, который понимает yt-dlp).\n"
    "Я буду писать его до самого конца, потом порежу, сожму через ezgif и залью на файлообменник.\n\n"
    "<i>Команды:</i>\n"
    "/start — это сообщение\n"
    "/status — мои активные задачи\n"
)


async def cmd_start(m: Message) -> None:
    await m.answer(WELCOME)


async def cmd_status(m: Message) -> None:
    n = _active.get(m.from_user.id, 0)
    if n == 0:
        await m.answer("Активных задач нет.")
    else:
        await m.answer(f"Активных задач: <b>{n}</b>")


async def on_url(m: Message) -> None:
    text = (m.text or "").strip()
    if not URL_RE.match(text):
        await m.answer("Это не похоже на ссылку. Пришли URL стрима, начинающийся с http(s)://")
        return

    user_id = m.from_user.id
    if _active.get(user_id, 0) >= MAX_PER_USER:
        await m.answer(f"⚠️ Достигнут лимит одновременных задач ({MAX_PER_USER}). Дождись завершения.")
        return

    _pending[user_id] = PendingJob(url=text)
    await m.answer(
        f"🎯 Ссылка принята:\n<code>{text}</code>\n\nКуда заливать готовую запись?",
        reply_markup=_kb_choose(),
    )


async def on_choose(cq: CallbackQuery) -> None:
    user_id = cq.from_user.id
    choice = (cq.data or "").split(":", 1)[1]
    pj = _pending.pop(user_id, None)
    if not pj:
        await cq.answer("Сессия истекла, пришли ссылку заново.", show_alert=True)
        return
    if choice == "cancel":
        await cq.message.edit_text("✖️ Отменено.")
        await cq.answer()
        return

    hosters = {"ranoz": ["ranoz"], "tempshare": ["tempshare"],
               "both": ["ranoz", "tempshare"]}.get(choice, ["ranoz"])
    label_map = {"ranoz": "Ranoz", "tempshare": "Tempshare"}
    h_text = " + ".join(label_map[h] for h in hosters)

    await cq.message.edit_text(
        f"⏳ Запускаю запись.\nЦель: <b>{h_text}</b>\n"
        f"URL: <code>{pj.url}</code>\n\n"
        f"Я буду присылать прогресс. Записываю до конца стрима — это может занять часы."
    )
    await cq.answer("Поехали!")

    asyncio.create_task(_run_job(cq.bot, cq.from_user.id, cq.message.chat.id, pj.url, hosters))


async def _run_job(bot: Bot, user_id: int, chat_id: int,
                   url: str, hosters: list[str]) -> None:
    _active[user_id] = _active.get(user_id, 0) + 1
    last_msg = {"id": None, "text": ""}

    async def progress(msg: str):
        # Edit a single status message instead of spamming
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
        result = await pipeline.run_pipeline(url, hosters, progress)
    except Exception as e:
        log.exception("pipeline crashed")
        await bot.send_message(chat_id, f"❌ Внутренняя ошибка: {e}")
        return
    finally:
        _active[user_id] = max(0, _active.get(user_id, 1) - 1)

    if not result.ok:
        await bot.send_message(chat_id,
            f"❌ Не удалось завершить задачу.\nОшибка: <code>{result.error}</code>")
        return

    lines = [
        "🎉 <b>Готово!</b>",
        f"⏱ Заняло: {int(result.elapsed)}s",
        f"📥 Запись: { _h(result.raw_size) } ({int(result.duration_sec)}s)",
        f"📤 После сжатия: { _h(result.final_size) }",
        "",
    ]
    for label, urls in result.links.items():
        lines.append(f"<b>{label}</b> ({len(urls)} файл(ов)):")
        for u in urls:
            lines.append(f"• {u}")
        lines.append("")
    await bot.send_message(chat_id, "\n".join(lines), disable_web_page_preview=True)


def _h(n: int) -> str:
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} PB"


async def main() -> None:
    if not config.BOT_TOKEN:
        print("ERROR: BOT_TOKEN is empty. Set it in /opt/argus-bot/.env", file=sys.stderr)
        sys.exit(1)

    bot = Bot(config.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_status, Command("status"))
    dp.message.register(on_url, F.text)
    dp.callback_query.register(on_choose, F.data.startswith("h:"))

    log.info("ARGUS bot starting…")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
