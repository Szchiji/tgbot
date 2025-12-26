import os, asyncio, sqlite3, logging, secrets
from datetime import datetime
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties  # 新版本必须导入
import uvicorn

# --- 1. 配置读取 ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")
WEB_ADMIN = os.getenv("WEB_ADMIN", "admin")
WEB_PASS = os.getenv("WEB_PASS", "admin888")

# --- 2. 初始化服务 ---
app = FastAPI()
security = HTTPBasic()
templates = Jinja2Templates(directory="templates")

# 修正后的 Bot 初始化方式 (适配 aiogram 3.7.0+)
bot = Bot(
    token=TOKEN, 
    default=DefaultBotProperties(parse_mode="HTML")
)
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
        conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)''')
        # 初始化截图中的开关项
        defaults = [('del_join', 1), ('del_leave', 1), ('del_pin', 1), ('calculator', 0)]
        conn.executemany("INSERT OR IGNORE INTO settings VALUES (?, ?)", defaults)
        conn.commit()

def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    is_user = secrets.compare_digest(credentials.username, WEB_ADMIN)
    is_pass = secrets.compare_digest(credentials.password, WEB_PASS)
    if not (is_user and is_pass):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials.username

# --- 4. Web 路由 (适配 UI 截图) ---
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(authenticate)):
    with get_db() as conn:
        sets = {s['key']: s['value'] for s in conn.execute("SELECT * FROM settings").fetchall()}
    return templates.TemplateResponse("dashboard.html", {"request": request, "settings": sets})

@app.post("/update_settings")
async def update_settings(
    del_join: bool = Form(False), del_leave: bool = Form(False), 
    del_pin: bool = Form(False), calculator: bool = Form(False),
    user: str = Depends(authenticate)
):
    with get_db() as conn:
        conn.execute("UPDATE settings SET value=? WHERE key='del_join'", (1 if del_join else 0,))
        conn.execute("UPDATE settings SET value=? WHERE key='del_leave'", (1 if del_leave else 0,))
        conn.execute("UPDATE settings SET value=? WHERE key='del_pin'", (1 if del_pin else 0,))
        conn.execute("UPDATE settings SET value=? WHERE key='calculator'", (1 if calculator else 0,))
        conn.commit()
    return RedirectResponse(url="/", status_code=303)

# --- 5. 机器人业务逻辑 ---
def is_on(key):
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row and row['value'] == 1
    except: return False

@dp.message(F.new_chat_members)
async def on_join(msg: types.Message):
    if is_on('del_join'): await msg.delete()

@dp.message(F.left_chat_member)
async def on_leave(msg: types.Message):
    if is_on('del_leave'): await msg.delete()

@dp.message(F.pinned_message)
async def on_pin(msg: types.Message):
    if is_on('del_pin'): await msg.delete()

# --- 6. 程序入口 ---
@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    # Railway 自动分配端口
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
