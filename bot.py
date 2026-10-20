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
import pymysql

# ================= 從 Railway 環境變數讀取配置 =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_TYPE = os.getenv("DB_TYPE", "mysql").lower()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", 3306))
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

QUOTA_PER_DOLLAR = int(os.getenv("QUOTA_PER_DOLLAR", 500000))
SITE_URL = os.getenv("SITE_URL", "https://www.si-hk.cn/")

NEWAPI_BASE_URL = os.getenv("NEWAPI_BASE_URL", f"{SITE_URL.rstrip('/')}/v1")
NEWAPI_API_KEY = os.getenv("NEWAPI_API_KEY")
INLINE_AI_MODEL = os.getenv("INLINE_AI_MODEL", "gpt-4o-mini")
INLINE_COOLDOWN = int(
    os.getenv("INLINE_COOLDOWN", 15)
)  # 每個用戶 Inline 冷卻時間(秒)，防刷 Token
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

# 記憶體快取：記錄用戶最後一次使用 Inline 的時間 {user_id: timestamp}
user_cooldowns = {}


def get_db_connection():
    if DB_TYPE == "mysql":
        return pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            connect_timeout=5,
        )
    elif DB_TYPE in ["postgres", "postgresql"]:
        return psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            dbname=DB_NAME,
            connect_timeout=5,
        )
    else:
        raise ValueError("不支援的資料庫類型，請填寫 mysql 或 postgres")


def _sync_process_claim(tg_id: int, quota: int) -> tuple[bool, str]:
    """同步資料庫操作函數（將由 asyncio.to_thread 在背景執行緒中調用，不卡死 Bot）"""
    conn = get_db_connection()
    cursor = conn.cursor()
    tag_name = f"TG贈送_{tg_id}"

    try:
        sql_check = (
            "SELECT `key` FROM redemptions WHERE name = %s LIMIT 1"
            if DB_TYPE == "mysql"
            else "SELECT key FROM redemptions WHERE name = %s LIMIT 1"
        )
        cursor.execute(sql_check, (tag_name,))
        existing = cursor.fetchone()
        if existing:
            return True, existing[0]

        code = str(uuid.uuid4()).replace("-", "")
        created_time = int(time.time())

        sql_insert = (
            """INSERT INTO redemptions (user_id, `key`, status, name, quota, created_time, redeemed_time) VALUES (1, %s, 1, %s, %s, %s, 0)"""
            if DB_TYPE == "mysql"
            else """INSERT INTO redemptions (user_id, key, status, name, quota, created_time, redeemed_time) VALUES (1, %s, 1, %s, %s, %s, 0)"""
        )

        cursor.execute(sql_insert, (code, tag_name, quota, created_time))
        conn.commit()
        return False, code
    finally:
        cursor.close()
        conn.close()


# ================= 1. /start 領取額度功能 =================
@dp.message(CommandStart())
async def send_welcome(message: types.Message):
    user_id = message.from_user.id
    # 使用 html.escape 防止用戶名字帶有 < > & 等符號導致解析崩潰
    username = html.escape(message.from_user.first_name or "朋友")

    usd_amount = round(random.uniform(0.05, 0.1), 2)
    quota = int(usd_amount * QUOTA_PER_DOLLAR)

    try:
        # 放入背景執行緒處理資料庫，避免阻塞其他用戶
        already_claimed, code = await asyncio.to_thread(
            _sync_process_claim, user_id, quota
        )

        switch_inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🚀 在聊天室试用 AI (Inline模式)",
                        switch_inline_query="帮我总结一下",
                    )
                ]
            ]
        )

        if already_claimed:
            await message.reply(
                f"👋 欢迎回来，<b>{username}</b>！\n\n"
                f"⚠️ 您之前已经领取过新手体验额度！\n"
                f"🎫 您的兑换码是：<code>{code}</code>\n\n"
                f"💡 <b>小提示</b>：在任何聊天室输入<code>@{BOT_USERNAME} 问题</code> 即可免费呼叫 AI！",
                parse_mode="HTML",
                reply_markup=switch_inline_kb,
            )
            return

        reply_text = (
            f"🎉 欢迎 <b>{username}</b>！\n\n"
            f"🎁 系统已自动为您生成新手体验额度：\n"
            f"💰 随机金额：<b>${usd_amount:.2f} </b>\n"
            f"🎫 您的兑换码 (点击复制)：\n\n"
            f"<code>{code}</code>\n\n"
            f'👉 請前往 <a href="{SITE_URL}">我们的网站</a> 储值使用！\n'
            f"🔥 <b>隐藏功能</b>：在任何与朋友的聊天室输入 <code>@{BOT_USERNAME} 你的问题</code>，即可直接召唤 AI 为你解答！"
        )
        await message.reply(
            reply_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=switch_inline_kb,
        )

    except Exception as e:
        logging.error(f"資料庫操作失敗: {e}")
        await message.reply("😭 系统繁忙，生成兑换码失败，请稍后再试。")


# ================= 2. Inline Mode 病毒行銷功能 =================


@dp.inline_query()
async def inline_query_handler(query: InlineQuery):
    prompt = query.query.strip()
    user_id = query.from_user.id

    if not prompt:
        return

    # 檢查冷卻時間 (防刷機制)
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
                message_text=f"⚡ 我正在使用 @{BOT_USERNAME} 的免费 AI 服务！\n👉 点击 <a href='https://t.me/{BOT_USERNAME}?start=viral'>这里免费领取 0.05~0.1 体验额度</a>！",
                parse_mode="HTML",
            ),
        )
        await query.answer([result], cache_time=1, is_personal=True)
        return

    # 限制輸入長度，防止惡意長文消耗 Token
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

    # 記錄冷卻時間
    user_cooldowns[user_id] = time.time()

    viral_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎁 点击免费领取 0.05~0.1 AI 额度",
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
            text=f"🤔 <b>提问</b>：{safe_prompt}\n\n⚠️ 管理员尚未配置 AI 金钥，请先点击下方按钮领取网页版额度！",
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
            timeout=25.0,  # 設定超時防止無限等待
        )

        raw_answer = response.choices[0].message.content.strip()
        safe_answer = html.escape(raw_answer)  # 轉義 HTML 特殊符號，保證 100% 不會因為格式崩潰

        final_text = (
            f"💡 <b>提問</b>：{safe_prompt}\n"
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
            text=f"🤔 <b>提问</b>：{safe_prompt}\n\n❌ 抱歉，目前呼叫 AI 的人数过多，请稍后再试，或点击下方按钮前往网站使用高速通道！",
            parse_mode="HTML",
            reply_markup=viral_kb,
        )


async def main():
    global BOT_USERNAME
    if not BOT_TOKEN:
        raise ValueError("未设定 BOT_TOKEN 环境变数！")

    bot_info = await bot.get_me()
    BOT_USERNAME = bot_info.username
    print(f"🚀 @{BOT_USERNAME} 终极加固版已在 Railway 上启动...")

    # 清除啟動前積壓的舊訊息，防止重啟時瞬間併發
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
