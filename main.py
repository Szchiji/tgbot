import os, asyncio, sqlite3, logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import uvicorn

# --- 基础配置 ---
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "/data/bot.db"
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# --- 数据库初始化 (完整复刻截图字段) ---
def init_db():
    os.makedirs("/data", exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        # 认证用户表
        conn.execute('''CREATE TABLE IF NOT EXISTS verified_users (
                        user_id INTEGER PRIMARY KEY, 
                        name TEXT, 
                        sort_order INTEGER DEFAULT 0,
                        teacher_name TEXT,
                        chat_link TEXT,      -- 名字跳转链接 (私聊)
                        channel_link TEXT,   -- 频道跳转链接
                        area TEXT,
                        price TEXT,
                        chest_size TEXT,
                        height TEXT,
                        bi_contact TEXT)''')
        # 今日打卡表
        conn.execute('''CREATE TABLE IF NOT EXISTS checkins (
                        user_id INTEGER PRIMARY KEY, checkin_date TEXT)''')
        # 消息模板表
        conn.execute('''CREATE TABLE IF NOT EXISTS msg_templates (
                        id TEXT PRIMARY KEY, header TEXT, item_format TEXT)''')
        
        # 预设默认模板 (复刻截图 UI)
        d_header = "<b>榨汁 🐓</b>\n<b>以下为今日开课老师</b>\n\n老师发送“打卡”完成登记，打卡未显示请联系推广员\n\n"
        d_item = "✅ {area} <a href='{chat_link}'>{name}</a> <a href='{chan_link}'>频道</a> 胸{chest} {price}"
        conn.execute("INSERT OR IGNORE INTO msg_templates VALUES ('juicing', ?, ?)", (d_header, d_item))
        conn.commit()

# --- 机器人业务逻辑 ---

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_msg(msg: types.Message):
    uid = msg.from_user.id
    text = msg.text or ""

    # 获取认证用户信息
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT * FROM verified_users WHERE user_id = ?", (uid,)).fetchone()

    # 1. 认证老师发言自动点赞
    if user:
        try: await msg.react([types.ReactionTypeEmoji(emoji="👍")])
        except: pass

        # 2. 老师发送“打卡”
        if text == "打卡":
            today = datetime.now().strftime("%Y-%m-%d")
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("INSERT OR REPLACE INTO checkins VALUES (?, ?)", (uid, today))
                conn.commit()
            await msg.reply(f"✅ <b>{user['name']}</b> 登记成功！已加入列表。")

    # 3. 任何人发送“今日榨汁”展示列表
    if text == "今日榨汁":
        content, kb = await render_juicing_list()
        await msg.answer(content, reply_markup=kb, disable_web_page_preview=True)

async def render_juicing_list():
    today = datetime.now().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        tpl = conn.execute("SELECT * FROM msg_templates WHERE id = 'juicing'").fetchone()
        # 关联查询今日打卡的老师
        users = conn.execute('''SELECT u.* FROM verified_users u JOIN checkins c ON u.user_id = c.user_id 
                                WHERE c.checkin_date = ? ORDER BY u.sort_order DESC''', (today,)).fetchall()
    
    if not users: return "<b>今日暂无老师开课。</b>", None

    res = tpl['header']
    for u in users:
        res += tpl['item_format'].format(
            area=u['area'] or "未知",
            name=u['name'] or "匿名",
            chat_link=u['chat_link'] or "https://t.me/",
            chan_link=u['channel_link'] or "https://t.me/",
            chest=u['chest_size'] or "-",
            price=u['price'] or "面议"
        ) + "\n"

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="上一页", callback_data="p"), types.InlineKeyboardButton(text="1/1", callback_data="n"))
    builder.row(types.InlineKeyboardButton(text="↗️ 榨汁推广员", url="https://t.me/your_admin_id"))
    return res, builder.as_markup()

# --- Web 路由 ---

@app.get("/members", response_class=HTMLResponse)
async def members_page(request: Request):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute("SELECT * FROM verified_users ORDER BY sort_order DESC").fetchall()
    return templates.TemplateResponse("members.html", {"request": request, "users": users})

@app.post("/members/save")
async def save_member(user_id: int = Form(...), name: str = Form(...), sort: int = Form(0),
                      t_name: str = Form(""), chat_link: str = Form(""), chan_link: str = Form(""),
                      area: str = Form(""), price: str = Form(""), chest: str = Form(""),
                      height: str = Form(""), bi_contact: str = Form("")):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''INSERT OR REPLACE INTO verified_users VALUES (?,?,?,?,?,?,?,?,?,?,?)''', 
                     (user_id, name, sort, t_name, chat_link, chan_link, area, price, chest, height, bi_contact))
        conn.commit()
    return RedirectResponse(url="/members", status_code=303)

@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
