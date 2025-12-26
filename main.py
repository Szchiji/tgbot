import os, asyncio, sqlite3, random, string, uuid, logging
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Form, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
import uvicorn

# --- 1. 环境变量配置 ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
# 为适配 Railway 持久化，建议挂载 Volume 到 /data
DB_PATH = os.getenv("DATABASE_URL", "/data/bot.db")
STATIC_DIR = os.getenv("STATIC_DIR", "/data/static")
DOMAIN = os.getenv("RAILWAY_STATIC_URL", "localhost:8080")

# --- 2. 初始化 ---
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 创建必要目录并挂载静态资源（用于显示老师照片）
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 存储 Token 会话
valid_sessions = {}

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        # 认证会员表：增加了 photo_url 字段
        conn.execute('''CREATE TABLE IF NOT EXISTS verified_users (
                        user_id INTEGER, group_id INTEGER, name TEXT, sort_order INTEGER DEFAULT 0,
                        teacher_name TEXT, chat_link TEXT, channel_link TEXT, area TEXT, 
                        price TEXT, chest_size TEXT, height TEXT, bi_contact TEXT, photo_url TEXT,
                        PRIMARY KEY(user_id, group_id))''')
        # 群组表
        conn.execute('''CREATE TABLE IF NOT EXISTS groups (group_id INTEGER PRIMARY KEY, group_name TEXT)''')
        conn.commit()

# --- 3. 机器人私聊逻辑：验证码发放 ---

@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    code = ''.join(random.choices(string.digits, k=6))
    valid_sessions[f"CODE_{ADMIN_ID}"] = {"code": code, "exp": datetime.now() + timedelta(minutes=5)}
    await msg.answer(f"🔢 后台验证码：<code>{code}</code>\n请直接回复此数字以登录后台。")

@dp.message(F.chat.type == "private", F.text.regexp(r'^\d{6}$'))
async def verify_code(msg: types.Message):
    session = valid_sessions.get(f"CODE_{ADMIN_ID}")
    if not session or datetime.now() > session["exp"]:
        return await msg.answer("❌ 验证码过期，请重新发送 /start")
    
    if msg.from_user.id == ADMIN_ID and msg.text == session["code"]:
        token = str(uuid.uuid4())
        valid_sessions[token] = datetime.now() + timedelta(hours=6)
        login_url = f"https://{DOMAIN}/portal?token={token}"
        await msg.answer(f"✅ 验证成功！\n\n<a href='{login_url}'>👉 进入管理后台</a>")
        valid_sessions.pop(f"CODE_{ADMIN_ID}", None)

# --- 4. Web 后台：功能路由 ---

def check_auth(token: str):
    if token not in valid_sessions or datetime.now() > valid_sessions[token]:
        raise HTTPException(status_code=403, detail="Login Expired")

@app.get("/portal", response_class=HTMLResponse)
async def portal_page(request: Request, token: str):
    check_auth(token)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        groups = conn.execute("SELECT * FROM groups").fetchall()
    return templates.TemplateResponse("portal.html", {"request": request, "token": token, "groups": groups})

@app.get("/manage", response_class=HTMLResponse)
async def manage_page(request: Request, token: str, gid: int, q: str = None):
    check_auth(token)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        # 搜索逻辑：支持按名字或 ID 模糊搜索
        query = "SELECT * FROM verified_users WHERE group_id = ?"
        params = [gid]
        if q:
            query += " AND (name LIKE ? OR user_id LIKE ? OR area LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
        users = conn.execute(query + " ORDER BY sort_order DESC", params).fetchall()
        group = conn.execute("SELECT * FROM groups WHERE group_id=?", (gid,)).fetchone()
    return templates.TemplateResponse("manage.html", {"request": request, "token": token, "gid": gid, "users": users, "group": group, "q": q or ""})

@app.post("/save_user")
async def save_action(
    token: str=Form(...), gid: int=Form(...), user_id: int=Form(...), 
    name: str=Form(...), sort: int=Form(0), t_name: str=Form(""), 
    chat_link: str=Form(""), chan_link: str=Form(""), area: str=Form(""), 
    price: str=Form(""), chest: str=Form(""), height: str=Form(""), 
    bi_contact: str=Form(""), photo: UploadFile = File(None)
):
    check_auth(token)
    photo_url = ""
    if photo and photo.filename:
        ext = photo.filename.split(".")[-1]
        filename = f"{gid}_{user_id}.{ext}"
        filepath = os.path.join(STATIC_DIR, filename)
        with open(filepath, "wb") as f: f.write(await photo.read())
        photo_url = f"/static/{filename}"

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''INSERT OR REPLACE INTO verified_users 
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (user_id, gid, name, sort, t_name, chat_link, chan_link, area, price, chest, height, bi_contact, photo_url))
        conn.commit()
    return RedirectResponse(url=f"/manage?token={token}&gid={gid}", status_code=303)

# 补全侧边栏其他页面路由，防止点击 404
@app.get("/dashboard")
async def dash_page(request: Request, token: str, gid: int):
    check_auth(token)
    return templates.TemplateResponse("dashboard.html", {"request": request, "token": token, "gid": gid})

@app.get("/settings")
async def sett_page(request: Request, token: str, gid: int):
    check_auth(token)
    return templates.TemplateResponse("settings.html", {"request": request, "token": token, "gid": gid})

@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
