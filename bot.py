import asyncio
import html
import logging
import os
import random
import time
import uuid
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import (
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from openai import AsyncOpenAI
import psycopg2

# ================= 从 Railway 环境变量读取配置 =================
BOT_TOKEN = os.getenv("BOT_TOKEN")

# 优先读取 Railway Postgres 的完整连接字符串 DATABASE_URL
DATABASE_URL = os.getenv("DATABASE_URL")

# 备用：如果没有设置 DATABASE_URL，则读取单独的变量
DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", 5432))  # Postgres 默认端口为 5432
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME", "railway")

QUOTA_PER_DOLLAR = int(os.getenv("QUOTA_PER_DOLLAR", 500000))
SITE_URL = os.getenv("SITE_URL", "https://你的网站网址")

NEWAPI_BASE_URL = os.getenv("NEWAPI_BASE_URL", f"{SITE_URL.rstrip('/')}/v1")
NEWAPI_API_KEY = os.getenv("NEWAPI_API_KEY")
INLINE_AI_MODEL = os.getenv("INLINE_AI_MODEL", "gpt-4o-mini")
INLINE_COOLDOWN = int(
    os.getenv("INLINE_COOLDOWN", 15)
)  # 每个用户 Inline 冷却时间(秒)
# ===============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

ai_client = (
    AsyncOpenAI(api_key=NEWAPI_API_KEY, base_url=NEWAPI_BASE_URL)
    if NEWAPI_API_KEY
    else None
)
BOT_USERNAME = ""
user_cooldowns = {}


def get_pg_connection():
    """获取 PostgreSQL 数据库连接"""
    if DATABASE_URL:
        # 直接使用 Railway 提供的连接字符串，最稳定
        return psycopg2.connect(DATABASE_URL, connect_timeout=5)
    else:
        return psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            dbname=DB_NAME,
            connect_timeout=5,
        )


def _sync_process_claim(tg_id: int, quota: int) -> tuple[bool, str]:
    """同步执行 PostgreSQL 查询与插入 (由 asyncio.to_thread 在后台调用)"""
    conn = get_pg_connection()
    cursor = conn.cursor()
    tag_name = f"TG赠送_{tg_id}"

    try:
        # 1. 检查是否已领取过 (兼容简繁历史标签，防止重复领取)
        cursor.execute(
            'SELECT "key" FROM redemptions WHERE name IN (%s, %s) LIMIT 1',
            (tag_name, f"TG贈送_{tg_id}"),
        )
        existing = cursor.fetchone()
        if existing:
            return True, existing[0]

        # 2. 生成 32 位无横杠兑换码
        code = str(uuid.uuid4()).replace("-", "")
        created_time = int(time.time())

        # 3. 插入 New API 的 redemptions 表
        sql_insert = """
            INSERT INTO redemptions (user_id, "key", status, name, quota, created_time, redeemed_time) 
            VALUES (1, %s, 1, %s, %s, %s, 0)
        """
        cursor.execute(sql_insert, (code, tag_name, quota, created_time))
        conn.commit()
        return False, code

    except Exception as e:
        conn.rollback()  # Postgres 发生错误时必须回滚
        raise e
    finally:
        cursor.close()
        conn.close()


# ================= 1. /start 领取额度功能 =================
@dp.message(CommandStart())
async def send_welcome(message: types.Message):
    user_id = message.from_user.id
    username = html.escape(message.from_user.first_name or "朋友")

    usd_amount = round(random.uniform(0.50, 1.00), 2)
    quota = int(usd_amount * QUOTA_PER_DOLLAR)

    try:
        already_claimed, code = await asyncio.to_thread(
            _sync_process_claim, user_id, quota
        )

        switch_inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🚀 在聊天室试用 AI (内联模式)",
                        switch_inline_query="帮我",
                    )
                ]
            ]
        )

        if already_claimed:
            await message.reply(
                f"👋 欢迎回来，<b>{username}</b>！\n\n"
                f"⚠️ 您之前已经领取过新手体验额度啦！\n"
                f"🎫 您的兑换码是：<code>{code}</code>\n\n"
                f"💡 <b>小提示</b>：在任何聊天窗口输入 <code>@{BOT_USERNAME} 问题</code> 即可免费调用 AI！",
                parse_mode="HTML",
                reply_markup=switch_inline_kb,
            )
            return

        reply_text = (
            f"🎉 欢迎 <b>{username}</b>！\n\n"
            f"🎁 系统已自动为您生成新手体验额度：\n"
            f"💰 随机金额：<b>${usd_amount:.2f} 美元</b>\n"
            f"🎫 您的兑换码 (点击复制)：\n\n"
            f"<code>{code}</code>\n\n"
            f'👉 请前往 <a href="{SITE_URL}">我们的网站</a> 在「钱包/充值」页面使用！\n'
            f"🔥 <b>隐藏功能</b>：在任何与朋友的聊天窗口输入 <code>@{BOT_USERNAME} 你的问题</code>，即可直接召唤 AI 为你解答！"
        )
        await message.reply(
            reply_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=switch_inline_kb,
        )

    except Exception as e:
        logging.error(f"PostgreSQL 数据库操作失败: {e}")
        await message.reply("😭 系统繁忙，生成兑换码失败，请稍后再试。")


# ================= 2. Inline Mode 病毒营销功能 =================
@dp.inline_query()
async def inline_query_handler(query: InlineQuery):
    prompt = query.query.strip()
    user_id = query.from_user.id

    if not prompt:
        return

    now = time.time()
    if user_id in user_cooldowns and (
        now - user_cooldowns[user_id] < INLINE_COOLDOWN
    ):
        left_time = int(INLINE_COOLDOWN - (now - user_cooldowns[user_id]))
        result = InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title=f"⏳ 请稍候 {left_time} 秒后再试...",
            description="免费通道冷却中，点击可领取专属额度无限畅聊！",
            input_message_content=InputTextMessageContent(
                message_text=f"⚡ 我正在使用 @{BOT_USERNAME} 的免费 AI 服务！\n👉 点击 <a href='https://t.me/{BOT_USERNAME}?start=viral'>这里免费领取 0.05~0.10 体验额度</a>！",
                parse_mode="HTML",
            ),
        )
        await query.answer([result], cache_time=1, is_personal=True)
        return

    if len(prompt) > 200:
        prompt = prompt[:200] + "..."

    safe_prompt = html.escape(prompt)

    loading_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏳ 正在召唤 AI 思考中...",
                    url=f"https://t.me/{BOT_USERNAME}?start=inline",
                )
            ]
        ]
    )

    result = InlineQueryResultArticle(
        id=str(uuid.uuid4()),
        title="✨ 点击让 AI 为您解答",
        description=f"提问：{prompt}",
        thumbnail_url="https://cdn-icons-png.flaticon.com/512/4712/4712109.png",
        input_message_content=InputTextMessageContent(
            message_text=f"🤔 <b>提问</b>：{safe_prompt}\n\n⏳ <i>AI 正在高速运转大脑中，请稍候...</i>",
            parse_mode="HTML",
        ),
        reply_markup=loading_kb,
    )

    await query.answer([result], cache_time=1, is_personal=True)


@dp.chosen_inline_result()
async def chosen_inline_handler(chosen_result: ChosenInlineResult):
    prompt = chosen_result.query.strip()[:200]
    safe_prompt = html.escape(prompt)
    inline_message_id = chosen_result.inline_message_id
    user_id = chosen_result.from_user.id

    if not inline_message_id:
        return

    user_cooldowns[user_id] = time.time()

    viral_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎁 点击免费领取 0.05~$0.01 AI 额度",
                    url=f"https://t.me/{BOT_USERNAME}?start=viral",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 我也要在聊天室用 AI", switch_inline_query=""
                )
            ],
        ]
    )

    if not ai_client:
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=f"🤔 <b>提问</b>：{safe_prompt}\n\n⚠️ 管理员尚未配置 AI 密钥，请先点击下方按钮领取网页版额度！",
            parse_mode="HTML",
            reply_markup=viral_kb,
        )
        return

    try:
        response = await ai_client.chat.completions.create(
            model=INLINE_AI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "你是一个聪明、简洁的 AI 助手。请用简体中文直接回答问题，不要使用 Markdown 语法，字数控制在 250 字以内。",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=450,
            temperature=0.7,
            timeout=25.0,
        )

        raw_answer = response.choices[0].message.content.strip()
        safe_answer = html.escape(raw_answer)

        final_text = (
            f"💡 <b>提问</b>：{safe_prompt}\n"
            f"━━━━━━━━━━━━━━\n"
            f"{safe_answer}\n\n"
            f'⚡ <i>由 @{BOT_USERNAME} 强力驱动，<a href="https://t.me/{BOT_USERNAME}?start=viral">点击获取免费 AI 额度</a></i>'
        )

        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=final_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=viral_kb,
        )

    except Exception as e:
        logging.error(f"Inline AI 生成失败: {e}")
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=f"🤔 <b>提问</b>：{safe_prompt}\n\n❌ 抱歉，当前调用 AI 的人数过多，请稍后再试，或点击下方按钮前往网站使用高速通道！",
            parse_mode="HTML",
            reply_markup=viral_kb,
        )


async def main():
    global BOT_USERNAME
    if not BOT_TOKEN:
        raise ValueError("未设置 BOT_TOKEN 环境变量！")

    bot_info = await bot.get_me()
    BOT_USERNAME = bot_info.username
    print(f"🚀 @{BOT_USERNAME} (PostgreSQL 简中版) 已在 Railway 上启动...")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
