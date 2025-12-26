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

# --- 1. 严格从环境变量读取配置 ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
# Railway 建议将数据库和图片放在 /data 目录下并挂载 Volume
DB_PATH = os.getenv("DATABASE_URL", "/data/bot.db")
STATIC_DIR = os.getenv("STATIC_DIR", "/data/static")
DOMAIN = os.getenv("RAILWAY_STATIC_URL", "localhost:8080")

if not TOKEN or not ADMIN_ID:
    raise ValueError("环境变量 BOT_TOKEN 或 ADMIN_ID 未设置！")

# --- 2. 初始化核心组件 ---
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 确保存储目录存在
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
# 挂载静态目录，以便通过网页访问上传的老师照片
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 存储 Token 会话（生产环境建议使用 Redis，此处为演示使用内存）
valid_sessions = {}

# --- 3. 数据库初始化 ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        # 认证会员表：增加了 photo_url 字段
        conn.execute('''CREATE TABLE IF NOT EXISTS verified_users (
                        user_id INTEGER, group_id INTEGER, name TEXT, sort_order INTEGER DEFAULT 0,
                        teacher_name TEXT, chat_link TEXT, channel_link TEXT, area TEXT, 
                        price TEXT, chest_size TEXT, height TEXT, bi_contact TEXT, photo_url TEXT,
                        PRIMARY KEY(user_id, group_id))''')
        # 群组信息表
        conn.execute('''CREATE TABLE IF NOT EXISTS groups (group_id INTEGER PRIMARY KEY, group_name TEXT)''')
        conn.commit()

# --- 4. 机器人逻辑：私聊验证码 ---

@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start_private(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("❌ 非法访问：您不是系统预设管理员。")
    
    code = ''.join(random.choices(string.digits, k=6))
    # 将验证码存入内存，有效期5分钟
    valid_sessions[f"CODE_{ADMIN_ID}"] = {"code": code, "exp": datetime.now() + timedelta(minutes=5)}
    await msg.answer(f"🔢 您的后台登录验证码为：<code>{code}</code>\n请直接回复该数字进行身份确认。")

@dp.message(F.chat.type == "private", F.text.regexp(r'^\d{6}$'))
async def verify_login_code(msg: types.Message):
    session = valid_sessions.get(f"CODE_{ADMIN_ID}")
    if not session or datetime.now() > session["exp"]:
        return await msg.answer("❌ 验证码已过期，请重新发送 /start 获取。")
    
    if msg.from_user.id == ADMIN_ID and msg.text == session["code"]:
        token = str(uuid.uuid4())
        # 登录 Token 有效期 6 小时
        valid_sessions[token] = datetime.now() + timedelta(hours=6)
        login_url = f"https://{DOMAIN}/portal?token={token}"
        await msg.answer(f"✅ 验证成功！\n\n<a href='{login_url}'>👉 点击进入方丈式管理中心</a>\n链接6小时内有效。")
        valid_sessions.pop(f"CODE_{ADMIN_ID}", None)

# --- 5. Web 后台路由：多群切换与管理 ---

def check_auth(token: str):
    if token not in valid_sessions or datetime.now() > valid_sessions[token]:
        raise HTTPException(status_code=403, detail="登录已过期")

@app.get("/portal", response_class=HTMLResponse)
async def portal(request: Request, token: str):
    check_auth(token)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        groups = conn.execute("SELECT * FROM groups").fetchall()
    return templates.TemplateResponse("portal.html", {"request": request, "token": token, "groups": groups})

@app.get("/manage", response_class=HTMLResponse)
async def group_manage(request: Request, token: str, gid: int, q: str = None):
    check_auth(token)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        # 实现搜索逻辑
        query = "SELECT * FROM verified_users WHERE group_id = ?"
        params = [gid]
        if q:
            query += " AND (name LIKE ? OR user_id LIKE ? OR area LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
        
        users = conn.execute(query + " ORDER BY sort_order DESC", params).fetchall()
        group = conn.execute("SELECT * FROM groups WHERE group_id=?", (gid,)).fetchone()
    
    return templates.TemplateResponse("manage.html", {
        "request": request, "token": token, "gid": gid, 
        "users": users, "group": group, "q": q or ""
    })

# --- 6. 核心功能：保存会员与上传图片 ---

@app.post("/save_user")
async def api_save_user(
    token: str=Form(...), gid: int=Form(...), user_id: int=Form(...), 
    name: str=Form(...), sort: int=Form(0), t_name: str=Form(""), 
    chat_link: str=Form(""), chan_link: str=Form(""), area: str=Form(""), 
    price: str=Form(""), chest: str=Form(""), height: str=Form(""), 
    bi_contact: str=Form(""), photo: UploadFile = File(None)
):
    check_auth(token)
    photo_url = ""
    # 图片上传处理逻辑
    if photo and photo.filename:
        ext = photo.filename.split(".")[-1]
        filename = f"avatar_{gid}_{user_id}.{ext}"
        filepath = os.path.join(STATIC_DIR, filename)
        with open(filepath, "wb") as buffer:
            buffer.write(await photo.read())
        photo_url = f"/static/{filename}"

    with sqlite3.connect(DB_PATH) as conn:
        # 如果 photo_url 为空，且是更新操作，可以考虑保留原路径（此处简化为覆盖）
        conn.execute('''INSERT OR REPLACE INTO verified_users 
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (user_id, gid, name, sort, t_name, chat_link, chan_link, area, price, chest, height, bi_contact, photo_url))
        conn.commit()
    return RedirectResponse(url=f"/manage?token={token}&gid={gid}", status_code=303)

# --- 7. 补全侧边栏路由防止 404 ---
@app.get("/dashboard")
async def dashboard_page(request: Request, token: str, gid: int):
    check_auth(token)
    return templates.TemplateResponse("dashboard.html", {"request": request, "token": token, "gid": gid})

@app.get("/settings")
async def settings_page(request: Request, token: str, gid: int):
    check_auth(token)
    return templates.TemplateResponse("settings.html", {"request": request, "token": token, "gid": gid})

# --- 8. 启动与服务控制 ---
@app.on_event("startup")
async def on_startup():
    init_db()
    # 异步启动机器人轮询，不阻塞 FastAPI
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
