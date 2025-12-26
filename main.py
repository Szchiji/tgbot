import os, asyncio, sqlite3, random, string, uuid
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import uvicorn

# --- 严格通过环境变量获取配置 ---
TOKEN = os.getenv("BOT_TOKEN")
# 若 ADMIN_ID 未设置，程序将报错以防万一
ADMIN_ID = int(os.getenv("ADMIN_ID", 0)) 
# 数据库路径，默认指向 /data/ 目录以适配云存储挂载
DB_PATH = os.getenv("DATABASE_URL", "/data/bot.db")
# 项目域名，用于生成验证码后的跳转链接
DOMAIN = os.getenv("RAILWAY_STATIC_URL", "localhost:8080")

if not TOKEN or not ADMIN_ID:
    raise ValueError("错误：请在环境变量中设置 BOT_TOKEN 和 ADMIN_ID！")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 存储登录会话（Token : 到期时间）
valid_sessions = {}

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        # 认证会员表：包含截图所有字段
        conn.execute('''CREATE TABLE IF NOT EXISTS verified_users (
                        user_id INTEGER, group_id INTEGER, name TEXT, sort_order INTEGER DEFAULT 0,
                        teacher_name TEXT, chat_link TEXT, channel_link TEXT, area TEXT, 
                        price TEXT, chest_size TEXT, height TEXT, bi_contact TEXT,
                        PRIMARY KEY(user_id, group_id))''')
        # 群组信息表
        conn.execute('''CREATE TABLE IF NOT EXISTS groups (group_id INTEGER PRIMARY KEY, group_name TEXT)''')
        conn.commit()

# --- 机器人私聊：安全验证逻辑 ---
@dp.message(Command("start"), F.chat.type == "private")
async def handle_start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("❌ 权限不足，只有主管理员可以操作。")
    
    # 随机生成验证码并存入内存
    code = ''.join(random.choices(string.digits, k=6))
    os.environ[f"CODE_{ADMIN_ID}"] = code # 临时存储
    await msg.answer(f"🔢 您的动态验证码为：<code>{code}</code>\n请直接回复该数字进入后台。")

@dp.message(F.chat.type == "private", F.text.regexp(r'^\d{6}$'))
async def check_code(msg: types.Message):
    stored_code = os.environ.get(f"CODE_{ADMIN_ID}")
    if msg.from_user.id == ADMIN_ID and msg.text == stored_code:
        token = str(uuid.uuid4())
        valid_sessions[token] = datetime.now()
        login_url = f"https://{DOMAIN}/admin?token={token}"
        await msg.answer(f"✅ 验证通过！链接10分钟内有效：\n\n<a href='{login_url}'>👉 点击进入方丈式管理中心</a>")
        os.environ.pop(f"CODE_{ADMIN_ID}", None)

# --- Web 后台逻辑 ---
@app.get("/admin", response_class=HTMLResponse)
async def portal(request: Request, token: str):
    # 安全检查：Token 是否有效
    if token not in valid_sessions:
        return HTMLResponse("登录已过期，请重新在机器人获取验证码。", status_code=403)
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        groups = conn.execute("SELECT * FROM groups").fetchall()
    
    return templates.TemplateResponse("portal.html", {"request": request, "token": token, "groups": groups})

# --- 启动服务 ---
@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
