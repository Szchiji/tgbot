import os, asyncio, sqlite3, logging, secrets
from datetime import datetime
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.storage.memory import MemoryStorage
import uvicorn

# --- 1. 核心配置（优先读取环境变量） ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")  # 对应 Railway Volume 挂载路径
WEB_ADMIN = os.getenv("WEB_ADMIN", "admin")
WEB_PASS = os.getenv("WEB_PASS", "admin888")

# --- 2. 初始化服务 ---
app = FastAPI()
security = HTTPBasic()
templates = Jinja2Templates(directory="templates")
bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

# --- 3. 数据库逻辑 ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if not os.path.exists(db_dir): os.makedirs(db_dir)
    with get_db() as conn:
        # 会员资料表
        conn.execute('''CREATE TABLE IF NOT EXISTS members 
            (user_id BIGINT PRIMARY KEY, stage_name TEXT, area TEXT, chest TEXT, p1000 TEXT, p2000 TEXT, link TEXT, expire DATE)''')
        # 系统设置表
        conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)''')
        # 打卡记录表
        conn.execute('''CREATE TABLE IF NOT EXISTS checkins (user_id BIGINT, date TEXT, PRIMARY KEY(user_id, date))''')
        
        # 初始化截图中的开关（0关，1开）
        defaults = [('del_join', 1), ('del_leave', 1), ('auto_react', 1), ('del_promote', 0)]
        conn.executemany("INSERT OR IGNORE INTO settings VALUES (?, ?)", defaults)
        conn.commit()

# 后台账号密码核验
def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    is_user = secrets.compare_digest(credentials.username, WEB_ADMIN)
    is_pass = secrets.compare_digest(credentials.password, WEB_PASS)
    if not (is_user and is_pass):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials.username

# --- 4. Web 路由：管理后台 ---
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(authenticate)):
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        members = conn.execute("SELECT * FROM members").fetchall()
        sets = {s['key']: s['value'] for s in conn.execute("SELECT * FROM settings").fetchall()}
        checked = [c['user_id'] for c in conn.execute("SELECT user_id FROM checkins WHERE date=?", (today,)).fetchall()]
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "members": members, "settings": sets, "checked": checked
    })

@app.post("/update_settings")
async def update_settings(
    del_join: bool = Form(False), 
    del_leave: bool = Form(False), 
    auto_react: bool = Form(False),
    user: str = Depends(authenticate)
):
    with get_db() as conn:
        conn.execute("UPDATE settings SET value=? WHERE key='del_join'", (1 if del_join else 0,))
        conn.execute("UPDATE settings SET value=? WHERE key='del_leave'", (1 if del_leave else 0,))
        conn.execute("UPDATE settings SET value=? WHERE key='auto_react'", (1 if auto_react else 0,))
        conn.commit()
    return RedirectResponse(url="/", status_code=303)

# --- 5. 机器人核心业务逻辑 ---
def check_setting(key):
    try:
        with get_db() as conn:
            res = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return res and res['value'] == 1
    except: return False

@dp.message(F.new_chat_members)
async def auto_delete_join(message: types.Message):
    if check_setting('del_join'): await message.delete()

@dp.message(F.left_chat_member)
async def auto_delete_leave(message: types.Message):
    if check_setting('del_leave'): await message.delete()

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
                await message.answer(f"✅ <b>{m['stage_name']}</b> 今日打卡成功！")
                if check_setting('auto_react'): 
                    await message.react([types.ReactionTypeEmoji(emoji="🔥")])
            except: await message.answer("📌 您今日已经打过卡了")
        else: await message.answer("❌ 权限不足或会员已到期")

# --- 6. 启动程序 ---
@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    # Railway 自动分配 PORT 环境变量
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
