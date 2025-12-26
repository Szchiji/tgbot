import os, asyncio, sqlite3, logging, uuid
from datetime import datetime
from typing import Optional, List, Dict
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
import uvicorn

# --- 配置与初始化 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
DOMAIN = os.getenv("RAILWAY_STATIC_URL", "localhost:8080").rstrip('/')
if not DOMAIN.startswith('http'): DOMAIN = f"https://{DOMAIN}"

DB_PATH = "/data/bot.db"
STATIC_DIR = "/data/static"
os.makedirs(STATIC_DIR, exist_ok=True)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
app = FastAPI()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory="templates")

# 内存状态存储
auth_states: Dict[str, Dict] = {}

# --- 数据库模型封装 ---
class DB:
    @staticmethod
    def query(sql, params=(), one=False):
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(sql, params)
            rv = cursor.fetchall()
            conn.commit()
            return (rv[0] if rv else None) if one else rv

def init_db():
    os.makedirs("/data", exist_ok=True)
    DB.query('''CREATE TABLE IF NOT EXISTS groups (
        group_id INTEGER PRIMARY KEY, group_name TEXT, 
        page_size INTEGER DEFAULT 20, like_emoji TEXT DEFAULT '👍', 
        list_template TEXT DEFAULT '✅ {area} {name} 频道 胸{chest_size} {price}')''')
    DB.query('''CREATE TABLE IF NOT EXISTS verified_users (
        user_id INTEGER, group_id INTEGER, name TEXT, area TEXT, 
        price TEXT, chest_size TEXT, sort_order INTEGER DEFAULT 0, 
        PRIMARY KEY(user_id, group_id))''')
    DB.query('''CREATE TABLE IF NOT EXISTS checkins (
        user_id INTEGER, group_id INTEGER, checkin_date TEXT, 
        PRIMARY KEY(user_id, group_id, checkin_date))''')
    DB.query('''CREATE TABLE IF NOT EXISTS bottom_buttons (
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, 
        btn_text TEXT, btn_type TEXT, btn_value TEXT, sort_order INTEGER DEFAULT 0)''')

# --- 机器人逻辑 ---

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    sid = str(uuid.uuid4())
    code = "".join([str(os.urandom(1)[0] % 10) for _ in range(6)])
    auth_states[sid] = {"code": code, "verified": False, "at": datetime.now()}
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🔐 进入管理后台", url=f"{DOMAIN}/login?sid={sid}")
    await msg.answer(f"<b>管理系统验证</b>\n验证码: <code>{code}</code>\n请在网页打开后发回此代码。", reply_markup=builder.as_markup())

@dp.message(F.text.regexp(r'^\d{6}$'))
async def handle_auth_code(msg: types.Message):
    for sid, data in auth_states.items():
        if data["code"] == msg.text:
            data["verified"] = True
            return await msg.answer("✅ 验证成功！网页端已就绪。")
    await msg.answer("❌ 验证码无效。")

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_messages(msg: types.Message):
    gid, uid = msg.chat.id, msg.from_user.id
    today = datetime.now().strftime('%Y-%m-%d')
    
    # 自动注册群组
    group = DB.query("SELECT * FROM groups WHERE group_id=?", (gid,), one=True)
    if not group:
        DB.query("INSERT INTO groups (group_id, group_name) VALUES (?,?)", (gid, msg.chat.title))
        group = DB.query("SELECT * FROM groups WHERE group_id=?", (gid,), one=True)

    # 老师打卡/点赞逻辑
    user = DB.query("SELECT * FROM verified_users WHERE user_id=? AND group_id=?", (uid, gid), one=True)
    if user:
        # 1. 发言自动点赞
        try: await msg.react([types.ReactionTypeEmoji(emoji=group['like_emoji'])])
        except: pass
        
        # 2. 指令打卡
        if msg.text == "打卡":
            try:
                DB.query("INSERT INTO checkins VALUES (?,?,?)", (uid, gid, today))
                await msg.reply("✅ 打卡成功，今日已列入开课名单。")
            except sqlite3.IntegrityError:
                await msg.reply("ℹ️ 您今天已经打过卡了。")

    # 3. 公共查询指令
    if msg.text == "今日榨汁":
        await send_juice_list(msg.chat.id, msg)

async def send_juice_list(gid: int, msg: types.Message, page: int = 1):
    today = datetime.now().strftime('%Y-%m-%d')
    group = DB.query("SELECT * FROM groups WHERE group_id=?", (gid,), one=True)
    users = DB.query('''SELECT v.* FROM verified_users v 
                        JOIN checkins c ON v.user_id = c.user_id AND v.group_id = c.group_id
                        WHERE v.group_id = ? AND c.checkin_date = ? 
                        ORDER BY v.sort_order DESC''', (gid, today))
    
    if not users:
        return await msg.answer("📅 今日暂无老师打卡开课。")

    psize = group['page_size']
    total_pages = (len(users) + psize - 1) // psize
    items = users[(page-1)*psize : page*psize]
    
    text = f"<b>榨汁 🍼 今日开课名单 ({page}/{total_pages})</b>\n\n"
    for u in items:
        try: text += group['list_template'].format(**dict(u)) + "\n"
        except: text += f"✅ {u['area']} {u['name']}\n"

    builder = InlineKeyboardBuilder()
    if page > 1: builder.button(text="⬅️ 上一页", callback_data=f"page_{page-1}")
    if page < total_pages: builder.button(text="下一页 ➡️", callback_data=f"page_{page+1}")
    
    # 加载自定义底部按钮
    btns = DB.query("SELECT * FROM bottom_buttons WHERE group_id=? ORDER BY sort_order", (gid,))
    for b in btns:
        if b['btn_type'] == 'url': builder.row(types.InlineKeyboardButton(text=b['btn_text'], url=b['btn_value']))
        else: builder.row(types.InlineKeyboardButton(text=b['btn_text'], callback_data=b['btn_value']))

    await msg.answer(text, reply_markup=builder.as_markup())

# --- Web 路由逻辑 ---

def check_auth(sid: str):
    if sid not in auth_states or not auth_states[sid]["verified"]:
        raise HTTPException(status_code=403, detail="Unauthorized")

@app.get("/login", response_class=HTMLResponse)
async def web_login(request: Request, sid: str):
    if sid not in auth_states: return HTMLResponse("链接已失效")
    return templates.TemplateResponse("login.html", {"request": request, "sid": sid, "code": auth_states[sid]["code"]})

@app.get("/check_status/{sid}")
async def api_check_status(sid: str):
    return {"status": "verified" if auth_states.get(sid, {}).get("verified") else "waiting"}

@app.get("/portal", response_class=HTMLResponse)
async def web_portal(request: Request, sid: str):
    check_auth(sid)
    groups = DB.query("SELECT * FROM groups")
    return templates.TemplateResponse("portal.html", {"request": request, "sid": sid, "groups": groups})

@app.get("/manage", response_class=HTMLResponse)
async def web_manage(request: Request, sid: str, gid: int):
    check_auth(sid)
    group = DB.query("SELECT * FROM groups WHERE group_id=?", (gid,), one=True)
    users = DB.query("SELECT * FROM verified_users WHERE group_id=? ORDER BY sort_order DESC", (gid,))
    btns = DB.query("SELECT * FROM bottom_buttons WHERE group_id=? ORDER BY sort_order", (gid,))
    return templates.TemplateResponse("manage.html", {"request": request, "sid": sid, "gid": gid, "group": group, "users": users, "btns": btns})

@app.post("/api/save_group")
async def api_save_group(sid: str = Form(...), gid: int = Form(...), page_size: int = Form(...), like_emoji: str = Form(...), list_template: str = Form(...)):
    check_auth(sid)
    DB.query("UPDATE groups SET page_size=?, like_emoji=?, list_template=? WHERE group_id=?", (page_size, like_emoji, list_template, gid))
    return RedirectResponse(f"/manage?sid={sid}&gid={gid}", status_code=303)

@app.post("/api/save_user")
async def api_save_user(sid: str = Form(...), gid: int = Form(...), user_id: int = Form(...), name: str = Form(...), area: str = Form(""), price: str = Form(""), chest: str = Form(""), sort: int = Form(0)):
    check_auth(sid)
    DB.query("INSERT OR REPLACE INTO verified_users VALUES (?,?,?,?,?,?,?)", (user_id, gid, name, area, price, chest, sort))
    return RedirectResponse(f"/manage?sid={sid}&gid={gid}", status_code=303)

@app.on_event("startup")
async def on_startup():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
