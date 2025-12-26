import os
import asyncio
import sqlite3
import logging
import secrets
from datetime import datetime
from contextlib import contextmanager

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
import uvicorn

# --- 1. 环境变量配置 ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")
WEB_ADMIN = os.getenv("WEB_ADMIN", "admin")
WEB_PASS = os.getenv("WEB_PASS", "admin888")

# --- 2. 初始化 ---
app = FastAPI()
security = HTTPBasic()
templates = Jinja2Templates(directory="templates")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

# --- 3. 数据库操作 ---
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if not os.path.exists(db_dir):
        os.makedirs(db_dir)
    with get_db() as conn:
        # 核心三表：设置表、会员表、打卡记录表
        conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)')
        conn.execute('''CREATE TABLE IF NOT EXISTS members (
                            user_id INTEGER PRIMARY KEY, 
                            stage_name TEXT, 
                            expire_date TEXT, 
                            area TEXT,
                            note TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS checkins (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER,
                            stage_name TEXT,
                            area TEXT,
                            checkin_date TEXT,
                            checkin_time TEXT)''')
        # 初始化设置项
        defaults = [('del_join', 1), ('del_leave', 1), ('del_pin', 1), ('calculator', 0)]
        conn.executemany("INSERT OR IGNORE INTO settings VALUES (?, ?)", defaults)
        conn.commit()

# --- 4. Web 鉴权 ---
def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    is_user = secrets.compare_digest(credentials.username, WEB_ADMIN)
    is_pass = secrets.compare_digest(credentials.password, WEB_PASS)
    if not (is_user and is_pass):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials.username

# --- 5. Web 路由 ---

# A. 统计看板
@app.get("/stats", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse) # 默认首页设为统计
async def stats_page(request: Request, user: str = Depends(authenticate)):
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        today_count = conn.execute("SELECT COUNT(*) FROM checkins WHERE checkin_date=?", (today,)).fetchone()[0]
        total_members = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        recent_checkins = conn.execute("SELECT * FROM checkins ORDER BY id DESC LIMIT 10").fetchall()
        area_stats = conn.execute("SELECT area, COUNT(*) as count FROM checkins GROUP BY area").fetchall()
    return templates.TemplateResponse("stats.html", {
        "request": request, "today_count": today_count, 
        "total_members": total_members, "recent_checkins": recent_checkins, "area_stats": area_stats
    })

# B. 会员管理
@app.get("/members", response_class=HTMLResponse)
async def members_page(request: Request, q: str = None, user: str = Depends(authenticate)):
    with get_db() as conn:
        if q:
            query = f"%{q}%"
            members = conn.execute("SELECT * FROM members WHERE stage_name LIKE ? OR user_id LIKE ?", (query, query)).fetchall()
        else:
            members = conn.execute("SELECT * FROM members").fetchall()
    return templates.TemplateResponse("members.html", {"request": request, "members": members, "search_q": q or ""})

@app.post("/members/save")
async def save_member(user_id: int = Form(...), stage_name: str = Form(...), expire_date: str = Form(...), 
                      area: str = Form(""), note: str = Form(""), user: str = Depends(authenticate)):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO members VALUES (?, ?, ?, ?, ?)", (user_id, stage_name, expire_date, area, note))
        conn.commit()
    return RedirectResponse(url="/members", status_code=303)

@app.get("/members/delete/{uid}")
async def delete_member(uid: int, user: str = Depends(authenticate)):
    with get_db() as conn:
        conn.execute("DELETE FROM members WHERE user_id = ?", (uid,))
        conn.commit()
    return RedirectResponse(url="/members", status_code=303)

# C. 机器人设置
@app.get("/settings", response_class=HTMLResponse)
async def settings_ui(request: Request, user: str = Depends(authenticate)):
    with get_db() as conn:
        sets = {s['key']: s['value'] for s in conn.execute("SELECT * FROM settings").fetchall()}
    return templates.TemplateResponse("settings.html", {"request": request, "settings": sets})

# --- 6. 机器人逻辑 ---

@dp.message(F.text.contains("打卡"))
async def handle_checkin(msg: types.Message):
    with get_db() as conn:
        member = conn.execute("SELECT * FROM members WHERE user_id = ?", (msg.from_user.id,)).fetchone()
    
    if not member: return  # 陌生人：不管
    
    # 会员效期检查
    expire_dt = datetime.strptime(member['expire_date'], '%Y-%m-%d')
    if expire_dt < datetime.now():
        await msg.answer("❌ 您的服务已到期。")
        return

    # 记录打卡
    now = datetime.now()
    with get_db() as conn:
        conn.execute("INSERT INTO checkins (user_id, stage_name, area, checkin_date, checkin_time) VALUES (?, ?, ?, ?, ?)",
                     (msg.from_user.id, member['stage_name'], member['area'], now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")))
        conn.commit()
    await msg.reply(f"✅ <b>{member['stage_name']}</b> 打卡成功！\n地区：{member['area']}\n时间：{now.strftime('%H:%M:%S')}")

# --- 7. 运行 ---
@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
