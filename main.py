import os, asyncio, sqlite3, random, string, uuid, logging
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Form, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
import uvicorn

# --- 1. 配置与初始化 ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
DOMAIN = os.getenv("RAILWAY_STATIC_URL", "localhost:8080")
DB_PATH = os.getenv("DATABASE_URL", "/data/bot.db")
STATIC_DIR = os.getenv("STATIC_DIR", "/data/static")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 目录准备
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 状态机: { sid: { "code": "123", "verified": False, "exp": datetime } }
auth_states = {}

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS verified_users (
                        user_id INTEGER, group_id INTEGER, name TEXT, sort_order INTEGER DEFAULT 0,
                        teacher_name TEXT, chat_link TEXT, channel_link TEXT, area TEXT, 
                        price TEXT, chest_size TEXT, height TEXT, bi_contact TEXT, photo_url TEXT,
                        PRIMARY KEY(user_id, group_id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS groups (group_id INTEGER PRIMARY KEY, group_name TEXT)''')
        conn.commit()

# --- 2. 鉴权中间件 ---
def check_auth(token: str):
    if token not in auth_states or not auth_states[token]["verified"]:
        raise HTTPException(status_code=403, detail="未授权或验证过期")
    if datetime.now() > auth_states[token]["exp"]:
        auth_states.pop(token, None)
        raise HTTPException(status_code=403, detail="会话过期")
    return auth_states[token]

# --- 3. 机器人逻辑 ---

@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    sid = str(uuid.uuid4())
    code = ''.join(random.choices(string.digits, k=6))
    auth_states[sid] = {"code": code, "verified": False, "exp": datetime.now() + timedelta(hours=6)}
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔐 点击打开验证网页", url=f"https://{DOMAIN}/login?sid={sid}"))
    await msg.answer("<b>方丈后台管理系统</b>\n请点击按钮获取验证码，并在网页显示后将数字发回给我。", reply_markup=builder.as_markup())

@dp.message(F.chat.type == "private", F.text.regexp(r'^\d{6}$'))
async def handle_code(msg: types.Message):
    for sid, state in auth_states.items():
        if state["code"] == msg.text and not state["verified"]:
            state["verified"] = True
            await msg.answer("✅ 验证成功！网页已同步跳转至群组列表。")
            return
    await msg.answer("❌ 验证码无效。")

# 自动记录机器人入群
@dp.my_chat_member()
async def on_my_chat_member(update: types.ChatMemberUpdated):
    if update.new_chat_member.status in ["member", "administrator"]:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO groups VALUES (?, ?)", (update.chat.id, update.chat.title))
            conn.commit()

# 监听群消息以自动记录群组
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_msg(msg: types.Message):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO groups VALUES (?, ?)", (msg.chat.id, msg.chat.title))
        conn.commit()

# --- 4. Web 路由逻辑 ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, sid: str):
    if sid not in auth_states: return "会话失效"
    return templates.TemplateResponse("login.html", {"request": request, "sid": sid, "code": auth_states[sid]["code"]})

@app.get("/check_status/{sid}")
async def check_status(sid: str):
    return JSONResponse({"status": "ok" if auth_states.get(sid, {}).get("verified") else "pending"})

@app.get("/portal", response_class=HTMLResponse)
async def portal(request: Request, token: str):
    check_auth(token)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        groups = conn.execute("SELECT * FROM groups").fetchall()
    return templates.TemplateResponse("portal.html", {"request": request, "groups": groups, "token": token})

@app.get("/sync_groups")
async def sync_groups(token: str):
    check_auth(token)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT DISTINCT group_id FROM verified_users").fetchall()
        for row in rows:
            try:
                chat = await bot.get_chat(row['group_id'])
                conn.execute("INSERT OR REPLACE INTO groups VALUES (?, ?)", (chat.id, chat.title))
            except: continue
        conn.commit()
    return RedirectResponse(f"/portal?token={token}")

@app.get("/manage", response_class=HTMLResponse)
async def manage(request: Request, token: str, gid: int, q: str = None):
    check_auth(token)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM verified_users WHERE group_id = ?"
        params = [gid]
        if q:
            sql += " AND (name LIKE ? OR user_id LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%"])
        users = conn.execute(sql + " ORDER BY sort_order DESC", params).fetchall()
        group = conn.execute("SELECT * FROM groups WHERE group_id=?", (gid,)).fetchone()
    return templates.TemplateResponse("manage.html", {"request": request, "token": token, "gid": gid, "users": users, "group": group, "q": q or ""})

@app.post("/save_user")
async def save_user(token: str=Form(...), gid: int=Form(...), user_id: int=Form(...), name: str=Form(...),
                    sort: int=Form(0), t_name: str=Form(""), chat_link: str=Form(""), chan_link: str=Form(""),
                    area: str=Form(""), price: str=Form(""), chest: str=Form(""), height: str=Form(""), 
                    bi_contact: str=Form(""), photo: UploadFile = File(None)):
    check_auth(token)
    photo_url = ""
    if photo and photo.filename:
        ext = photo.filename.split(".")[-1]
        fname = f"{gid}_{user_id}.{ext}"
        with open(os.path.join(STATIC_DIR, fname), "wb") as f: f.write(await photo.read())
        photo_url = f"/static/{fname}"

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''INSERT OR REPLACE INTO verified_users VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (user_id, gid, name, sort, t_name, chat_link, chan_link, area, price, chest, height, bi_contact, photo_url))
        conn.commit()
    return RedirectResponse(url=f"/manage?token={token}&gid={gid}", status_code=303)

@app.get("/dashboard")
async def dashboard(request: Request, token: str, gid: int):
    check_auth(token)
    return templates.TemplateResponse("dashboard.html", {"request": request, "token": token, "gid": gid})

@app.get("/settings")
async def settings(request: Request, token: str, gid: int):
    check_auth(token)
    return templates.TemplateResponse("settings.html", {"request": request, "token": token, "gid": gid})

@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
