import os, asyncio, sqlite3, random, string, uuid
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import uvicorn

# --- 环境变量配置 ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0)) 
DB_PATH = os.getenv("DATABASE_URL", "/data/bot.db")
DOMAIN = os.getenv("RAILWAY_STATIC_URL", "localhost:8080")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
app = FastAPI()
templates = Jinja2Templates(directory="templates")

valid_sessions = {} # 存储 Token : 到期时间

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS verified_users (
                        user_id INTEGER, group_id INTEGER, name TEXT, sort_order INTEGER DEFAULT 0,
                        teacher_name TEXT, chat_link TEXT, channel_link TEXT, area TEXT, 
                        price TEXT, chest_size TEXT, height TEXT, bi_contact TEXT,
                        PRIMARY KEY(user_id, group_id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS groups (group_id INTEGER PRIMARY KEY, group_name TEXT)''')
        conn.commit()

# --- 机器人私聊：验证码安全验证 ---
@dp.message(Command("start"), F.chat.type == "private")
async def handle_start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    code = ''.join(random.choices(string.digits, k=6))
    os.environ[f"CODE_{ADMIN_ID}"] = code 
    await msg.answer(f"🔢 您的后台登录验证码：<code>{code}</code>\n请直接在此回复该数字。")

@dp.message(F.chat.type == "private", F.text.regexp(r'^\d{6}$'))
async def verify_code(msg: types.Message):
    if msg.from_user.id == ADMIN_ID and msg.text == os.environ.get(f"CODE_{ADMIN_ID}"):
        token = str(uuid.uuid4())
        valid_sessions[token] = datetime.now()
        login_url = f"https://{DOMAIN}/admin?token={token}"
        await msg.answer(f"✅ 验证成功！点击进入管理后台：\n\n<a href='{login_url}'>👉 进入方丈式管理中心</a>")
        os.environ.pop(f"CODE_{ADMIN_ID}", None)

# --- 自动回复与打卡逻辑 (略，同之前版本) ---

# --- Web 后台路由 ---
@app.get("/admin", response_class=HTMLResponse)
async def admin_portal(request: Request, token: str):
    if token not in valid_sessions: return HTMLResponse("验证失效", status_code=403)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        groups = conn.execute("SELECT * FROM groups").fetchall()
    return templates.TemplateResponse("portal.html", {"request": request, "token": token, "groups": groups})

@app.get("/manage", response_class=HTMLResponse)
async def manage_group(request: Request, token: str, gid: int):
    if token not in valid_sessions: raise HTTPException(403)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute("SELECT * FROM verified_users WHERE group_id=?", (gid,)).fetchall()
        group = conn.execute("SELECT * FROM groups WHERE group_id=?", (gid,)).fetchone()
    return templates.TemplateResponse("manage.html", {"request": request, "token": token, "gid": gid, "users": users, "group": group})

@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
