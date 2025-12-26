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
# 这里的路径必须与 Volume 挂载的 /data 目录一致
DB_PATH = "/data/bot.db"
STATIC_DIR = "/data/static"

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 确保目录存在并挂载静态资源
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 验证状态存储
auth_states = {}

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        # 认证会员表
        conn.execute('''CREATE TABLE IF NOT EXISTS verified_users (
                        user_id INTEGER, group_id INTEGER, name TEXT, sort_order INTEGER DEFAULT 0,
                        teacher_name TEXT, chat_link TEXT, channel_link TEXT, area TEXT, 
                        price TEXT, chest_size TEXT, height TEXT, bi_contact TEXT, photo_url TEXT,
                        PRIMARY KEY(user_id, group_id))''')
        # 群组表
        conn.execute('''CREATE TABLE IF NOT EXISTS groups (group_id INTEGER PRIMARY KEY, group_name TEXT)''')
        conn.commit()

# --- 2. 鉴权逻辑 ---
def check_auth(token: str):
    if token not in auth_states or not auth_states[token]["verified"]:
        raise HTTPException(status_code=403, detail="未授权或验证过期")
    if datetime.now() > auth_states[token]["exp"]:
        auth_states.pop(token, None)
        raise HTTPException(status_code=403, detail="登录已过期")
    return auth_states[token]

# --- 3. 机器人处理 ---

@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    sid = str(uuid.uuid4())
    code = ''.join(random.choices(string.digits, k=6))
    auth_states[sid] = {"code": code, "verified": False, "exp": datetime.now() + timedelta(hours=6)}
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔐 点击进入登录页面", url=f"https://{DOMAIN}/login?sid={sid}"))
    await msg.answer(f"<b>方丈管理系统</b>\n请点击下方按钮并在网页查看验证码后发回给我。", reply_markup=builder.as_markup())

@dp.message(F.chat.type == "private", F.text.regexp(r'^\d{6}$'))
async def verify_code(msg: types.Message):
    for sid, state in auth_states.items():
        if state["code"] == msg.text and not state["verified"]:
            state["verified"] = True
            await msg.answer("✅ 验证成功！网页已同步跳转。")
            return
    await msg.answer("❌ 验证码无效或已失效。")

# 自动感知群组并记录
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_watcher(msg: types.Message):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO groups VALUES (?, ?)", (msg.chat.id, msg.chat.title))
        conn.commit()

# --- 4. Web 路由 ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, sid: str):
    if sid not in auth_states: return "链接无效"
    return templates.TemplateResponse("login.html", {"request": request, "sid": sid, "code": auth_states[sid]["code"]})

@app.get("/check_status/{sid}")
async def check_status(sid: str):
    is_ok = auth_states.get(sid, {}).get("verified", False)
    return JSONResponse({"status": "ok" if is_ok else "pending"})

@app.get("/portal", response_class=HTMLResponse)
async def portal(request: Request, token: str):
    check_auth(token)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        groups = conn.execute("SELECT * FROM groups").fetchall()
    return templates.TemplateResponse("portal.html", {"request": request, "groups": groups, "token": token})

@app.get("/manage", response_class=HTMLResponse)
async def manage_page(request: Request, token: str, gid: int, q: str = None):
    check_auth(token)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        # 搜索逻辑
        sql = "SELECT * FROM verified_users WHERE group_id = ?"
        params = [gid]
        if q:
            sql += " AND (name LIKE ? OR user_id LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%"])
        users = conn.execute(sql + " ORDER BY sort_order DESC", params).fetchall()
        
        # 容错处理：如果群组没在数据库里，手动创建一个虚拟对象防止报错
        group_row = conn.execute("SELECT * FROM groups WHERE group_id=?", (gid,)).fetchone()
        group_data = group_row if group_row else {"group_id": gid, "group_name": "未知群组/请先在群里发言"}
        
    return templates.TemplateResponse("manage.html", {
        "request": request, "token": token, "gid": gid, 
        "users": users, "group": group_data, "q": q or ""
    })

@app.post("/save_user")
async def save_user(
    token: str=Form(...), gid: int=Form(...), user_id: int=Form(...), name: str=Form(...),
    sort: int=Form(0), t_name: str=Form(""), chat_link: str=Form(""), chan_link: str=Form(""),
    area: str=Form(""), price: str=Form(""), chest: str=Form(""), height: str=Form(""), 
    bi_contact: str=Form(""), photo: UploadFile = File(None)
):
    check_auth(token)
    photo_url = ""
    if photo and photo.filename:
        ext = photo.filename.split(".")[-1]
        fname = f"{gid}_{user_id}.{ext}"
        save_path = os.path.join(STATIC_DIR, fname)
        with open(save_path, "wb") as f: f.write(await photo.read())
        photo_url = f"/static/{fname}"

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''INSERT OR REPLACE INTO verified_users 
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (user_id, gid, name, sort, t_name, chat_link, chan_link, area, price, chest, height, bi_contact, photo_url))
        conn.commit()
    return RedirectResponse(url=f"/manage?token={token}&gid={gid}", status_code=303)

@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
