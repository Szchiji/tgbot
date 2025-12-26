import os, asyncio, sqlite3, logging, re
from datetime import datetime
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.storage.memory import MemoryStorage
import uvicorn

# --- 基础配置 ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = "/data/bot.db"

# --- 初始化 ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")
bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

# --- 数据库逻辑 ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    if not os.path.exists("/data"): os.makedirs("/data")
    with get_db() as conn:
        # 会员表
        conn.execute('''CREATE TABLE IF NOT EXISTS members 
            (user_id BIGINT PRIMARY KEY, stage_name TEXT, area TEXT, chest TEXT, 
             p1000 TEXT, p2000 TEXT, link TEXT, expire DATE)''')
        # 打卡表
        conn.execute('''CREATE TABLE IF NOT EXISTS checkins 
            (user_id BIGINT, date TEXT, PRIMARY KEY(user_id, date))''')
        # 系统设置开关
        conn.execute('''CREATE TABLE IF NOT EXISTS settings 
            (key TEXT PRIMARY KEY, value INTEGER DEFAULT 0)''')
        
        defaults = [('del_join', 1), ('del_leave', 1), ('auto_react', 1), ('auto_restrict', 1)]
        conn.executemany("INSERT OR IGNORE INTO settings VALUES (?, ?)", defaults)
        conn.commit()

def get_setting(key: str) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return True if row and row['value'] == 1 else False

# --- Web 管理后台路由 ---
@app.get("/", response_class=HTMLResponse)
async def admin_index(request: Request):
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        members = conn.execute("SELECT * FROM members").fetchall()
        checked = [c['user_id'] for c in conn.execute("SELECT user_id FROM checkins WHERE date=?", (today,)).fetchall()]
        sets = {s['key']: s['value'] for s in conn.execute("SELECT * FROM settings").fetchall()}
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "members": members, "checked": checked, "settings": sets, "today": today
    })

@app.post("/update_settings")
async def update_settings(del_join: bool = Form(False), auto_react: bool = Form(False), auto_restrict: bool = Form(False)):
    with get_db() as conn:
        conn.execute("UPDATE settings SET value=? WHERE key='del_join'", (1 if del_join else 0,))
        conn.execute("UPDATE settings SET value=? WHERE key='auto_react'", (1 if auto_react else 0,))
        conn.execute("UPDATE settings SET value=? WHERE key='auto_restrict'", (1 if auto_restrict else 0,))
        conn.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/mod_member/{uid}")
async def mod_member(uid: int, area: str = Form(...), expire: str = Form(...)):
    with get_db() as conn:
        conn.execute("UPDATE members SET area=?, expire=? WHERE user_id=?", (area, expire, uid))
        conn.commit()
    return RedirectResponse(url="/", status_code=303)

# --- 机器人事件处理 ---

# 自动删除入群消息
@dp.message(F.new_chat_members)
async def on_join(message: types.Message):
    if get_setting('del_join'): await message.delete()

# 打卡逻辑
@dp.message(F.text == "打卡")
async def bot_checkin(message: types.Message):
    uid = message.from_user.id
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        m = conn.execute("SELECT stage_name, expire FROM members WHERE user_id=?", (uid,)).fetchone()
        if m and m['expire'] >= today:
            try:
                conn.execute("INSERT INTO checkins VALUES (?,?)", (uid, today))
                conn.commit()
                await message.answer(f"✅ {m['stage_name']} 打卡成功！")
                if get_setting('auto_react'): await message.react([types.ReactionTypeEmoji(emoji="🔥")])
            except: await message.answer("📌 今日已打卡")
        else: await message.answer("❌ 无权限或已到期")

# 榜单查询
@dp.message(F.text == "今日榨汁")
async def bot_list(message: types.Message):
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute("SELECT m.* FROM checkins c JOIN members m ON c.user_id = m.user_id WHERE c.date=?", (today,)).fetchall()
    if not rows: return await message.answer("🍶 今日暂无老师打卡")
    
    bot_info = await bot.get_me()
    res = "🍶 <b>今日榨汁榜单</b>\n\n"
    for r in rows:
        url = f"https://t.me/{bot_info.username}?start=contact_{r['user_id']}"
        res += f"📍 {r['area']} | <b>{r['stage_name']}</b> {r['chest']} {r['p1000']}P\n💬 <a href='{url}'>发起私聊</a>\n\n"
    await message.answer(res, disable_web_page_preview=True)

# 启动
@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
