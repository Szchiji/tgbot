import os, asyncio, sqlite3, logging, secrets
from datetime import datetime
from contextlib import contextmanager
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
import uvicorn

# --- 配置读取 ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")
WEB_ADMIN = os.getenv("WEB_ADMIN", "admin")
WEB_PASS = os.getenv("WEB_PASS", "admin888")

# --- 初始化服务 ---
app = FastAPI()
security = HTTPBasic()
templates = Jinja2Templates(directory="templates")

# 适配 aiogram 3.7.0+
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# --- 数据库逻辑 ---
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try: yield conn
    finally: conn.close()

def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if not os.path.exists(db_dir): os.makedirs(db_dir)
    with get_db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)')
        conn.execute('''CREATE TABLE IF NOT EXISTS members (
                            user_id INTEGER PRIMARY KEY, stage_name TEXT, 
                            expire_date TEXT, area TEXT, note TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS checkins (
                            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, 
                            stage_name TEXT, area TEXT, checkin_date TEXT, checkin_time TEXT)''')
        defaults = [('del_join', 1), ('del_leave', 1), ('del_pin', 1), ('calculator', 0)]
        conn.executemany("INSERT OR IGNORE INTO settings VALUES (?, ?)", defaults)
        conn.commit()

# --- 鉴权 ---
def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(credentials.username, WEB_ADMIN) and 
            secrets.compare_digest(credentials.password, WEB_PASS)):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return credentials.username

# --- Web 路由 ---

@app.get("/", response_class=HTMLResponse)
@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, user: str = Depends(authenticate)):
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        today_count = conn.execute("SELECT COUNT(*) FROM checkins WHERE checkin_date=?", (today,)).fetchone()[0]
        total_members = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        recent = conn.execute("SELECT * FROM checkins ORDER BY id DESC LIMIT 10").fetchall()
        area_stats = conn.execute("SELECT area, COUNT(*) as count FROM checkins GROUP BY area").fetchall()
    return templates.TemplateResponse("stats.html", {"request": request, "today_count": today_count, "total_members": total_members, "recent": recent, "area_stats": area_stats})

@app.get("/members", response_class=HTMLResponse)
async def members_page(request: Request, q: str = None, user: str = Depends(authenticate)):
    today_str = datetime.now().strftime('%Y-%m-%d')
    with get_db() as conn:
        sql = "SELECT * FROM members WHERE stage_name LIKE ? OR user_id LIKE ?" if q else "SELECT * FROM members"
        params = (f"%{q}%", f"%{q}%") if q else ()
        rows = conn.execute(sql, params).fetchall()
    members = [dict(r, is_expired=r['expire_date'] < today_str) for r in rows]
    return templates.TemplateResponse("members.html", {"request": request, "members": members, "search_q": q or ""})

@app.post("/members/save")
async def save_member(user_id: int = Form(...), stage_name: str = Form(...), expire_date: str = Form(...), area: str = Form(""), note: str = Form(""), user: str = Depends(authenticate)):
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

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: str = Depends(authenticate)):
    with get_db() as conn:
        sets = {s['key']: s['value'] for s in conn.execute("SELECT * FROM settings").fetchall()}
    return templates.TemplateResponse("settings.html", {"request": request, "settings": sets})

@app.post("/update_settings")
async def update_settings(del_join: bool = Form(False), del_leave: bool = Form(False), del_pin: bool = Form(False), calculator: bool = Form(False), user: str = Depends(authenticate)):
    with get_db() as conn:
        for k, v in {'del_join': del_join, 'del_leave': del_leave, 'del_pin': del_pin, 'calculator': calculator}.items():
            conn.execute("UPDATE settings SET value=? WHERE key=?", (1 if v else 0, k))
        conn.commit()
    return RedirectResponse(url="/settings", status_code=303)

# --- 机器人逻辑 ---

@dp.message(F.text.contains("打卡"))
async def handle_checkin(msg: types.Message):
    with get_db() as conn:
        member = conn.execute("SELECT * FROM members WHERE user_id = ?", (msg.from_user.id,)).fetchone()
    if not member or member['expire_date'] < datetime.now().strftime('%Y-%m-%d'): return
    now = datetime.now()
    with get_db() as conn:
        conn.execute("INSERT INTO checkins (user_id, stage_name, area, checkin_date, checkin_time) VALUES (?, ?, ?, ?, ?)",
                     (msg.from_user.id, member['stage_name'], member['area'], now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")))
        conn.commit()
    await msg.reply(f"✅ <b>{member['stage_name']}</b> 打卡成功！\n时间：{now.strftime('%H:%M:%S')}")

# --- 启动 ---
@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
