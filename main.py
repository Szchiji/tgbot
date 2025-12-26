import os, asyncio, sqlite3, logging, uuid
from datetime import datetime
from fastapi import FastAPI, Request, Form, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
import uvicorn

# --- 1. 基础配置 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BOT_DEBUG")

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
DOMAIN = os.getenv("RAILWAY_STATIC_URL", "localhost:8080").rstrip('/')
if not DOMAIN.startswith('http'):
    DOMAIN = f"https://{DOMAIN}"

DB_PATH = "/data/bot.db"
STATIC_DIR = "/data/static"

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
app = FastAPI()

os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory="templates")

# 内存存储验证状态
auth_states = {}

# --- 2. 数据库初始化 ---
def init_db():
    os.makedirs("/data", exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('''CREATE TABLE IF NOT EXISTS verified_users (
                        user_id INTEGER, group_id INTEGER, name TEXT, 
                        area TEXT, price TEXT, chest_size TEXT, height TEXT,
                        teacher_name TEXT, channel_link TEXT, sort_order INTEGER DEFAULT 0,
                        photo_url TEXT, PRIMARY KEY(user_id, group_id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS groups (
                        group_id INTEGER PRIMARY KEY, group_name TEXT,
                        page_size INTEGER DEFAULT 20, like_emoji TEXT DEFAULT '👍',
                        list_template TEXT DEFAULT '✅ {area} {name} 频道 胸{chest_size} {price}')''')
        conn.execute('''CREATE TABLE IF NOT EXISTS checkins (
                        user_id INTEGER, group_id INTEGER, checkin_date TEXT,
                        PRIMARY KEY(user_id, group_id, checkin_date))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS bottom_buttons (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER,
                        btn_text TEXT, btn_type TEXT, btn_value TEXT, sort_order INTEGER DEFAULT 0)''')
        conn.commit()

# --- 3. 机器人端逻辑 ---

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    logger.info(f"收到 /start 来自: {msg.from_user.id}")
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer(f"❌ 无权访问。你的ID是: {msg.from_user.id}")
    
    sid = str(uuid.uuid4())
    # 生成6位纯数字验证码
    code = "".join([str(os.urandom(1)[0] % 10) for _ in range(6)])
    auth_states[sid] = {"code": code, "verified": False}
    
    login_url = f"{DOMAIN}/login?sid={sid}"
    builder = InlineKeyboardBuilder()
    builder.button(text="🔐 点击进入登录页面", url=login_url)
    
    await msg.answer(
        f"<b>机器人管理系统</b>\n\n验证码: <code>{code}</code>\n请点击下方按钮并在网页打开后，将验证码发回给我。", 
        reply_markup=builder.as_markup()
    )

@dp.message(F.text.regexp(r'^\d{6}$'))
async def handle_code(msg: types.Message):
    code = msg.text
    for sid, data in auth_states.items():
        if data["code"] == code:
            data["verified"] = True
            logger.info(f"SID {sid} 验证成功")
            return await msg.answer("✅ 验证成功！网页已同步跳转。")
    await msg.answer("❌ 验证码无效或已过期")

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_activity(msg: types.Message):
    gid, uid = msg.chat.id, msg.from_user.id
    today = datetime.now().strftime('%Y-%m-%d')
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT * FROM verified_users WHERE user_id=? AND group_id=?", (uid, gid)).fetchone()
        group = conn.execute("SELECT * FROM groups WHERE group_id=?", (gid,)).fetchone()
        if not group:
            conn.execute("INSERT OR IGNORE INTO groups (group_id, group_name) VALUES (?,?)", (gid, msg.chat.title or "未知群组"))
            conn.commit()

    if user and msg.text == "打卡":
        with sqlite3.connect(DB_PATH) as conn:
            try:
                conn.execute("INSERT INTO checkins VALUES (?,?,?)", (uid, gid, today))
                conn.commit()
                await msg.reply("✅ 打卡成功！")
            except: await msg.reply("ℹ️ 今日已打卡")

    if msg.text == "今日榨汁":
        text, markup = await get_juice_markup(gid)
        await msg.answer(text, reply_markup=markup, disable_web_page_preview=True)

async def get_juice_markup(gid, page=1):
    today = datetime.now().strftime('%Y-%m-%d')
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        group = conn.execute("SELECT * FROM groups WHERE group_id=?", (gid,)).fetchone()
        psize = group['page_size'] if group else 20
        tpl = group['list_template'] if group else "✅ {area} {name} 频道 胸{chest_size} {price}"
        
        users = conn.execute('''
            SELECT v.* FROM verified_users v JOIN checkins c ON v.user_id = c.user_id AND v.group_id = c.group_id
            WHERE v.group_id = ? AND c.checkin_date = ? ORDER BY v.sort_order DESC
        ''', (gid, today)).fetchall()

    if not users: return "📅 今日暂无老师打卡开课。", None

    total_pages = (len(users) + psize - 1) // psize
    curr = users[(page-1)*psize : page*psize]
    text = f"<b>榨汁 🍼 以下为今日开课老师</b>\n\n"
    for u in curr:
        try: line = tpl.format(area=u['area'], name=u['name'], chest_size=u['chest_size'], price=u['price'])
        except: line = f"✅ {u['area']} {u['name']}"
        text += line + "\n"
    
    builder = InlineKeyboardBuilder()
    if page > 1: builder.button(text="⬅️ 上一页", callback_data=f"p_{gid}_{page-1}")
    if page < total_pages: builder.button(text="下一页 ➡️", callback_data=f"p_{gid}_{page+1}")
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        btns = conn.execute("SELECT * FROM bottom_buttons WHERE group_id=? ORDER BY sort_order", (gid,)).fetchall()
        for b in btns:
            if b['btn_type'] == 'url': builder.row(types.InlineKeyboardButton(text=b['btn_text'], url=b['btn_value']))
            else: builder.row(types.InlineKeyboardButton(text=b['btn_text'], callback_data=b['btn_value']))
    return text, builder.as_markup()

@dp.callback_query(F.data.startswith("p_"))
async def handle_pagination(call: types.CallbackQuery):
    _, gid, page = call.data.split("_")
    text, markup = await get_juice_markup(int(gid), int(page))
    await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)

# --- 4. Web 端路由 ---

@app.get("/login", response_class=HTMLResponse)
async def web_login(request: Request, sid: str):
    if sid not in auth_states:
        return HTMLResponse("验证链接已过期，请重新在 Telegram 发送 /start")
    return templates.TemplateResponse("login.html", {"request": request, "sid": sid, "code": auth_states[sid]["code"]})

# 关键：添加状态检查接口供网页轮询
@app.get("/check_status/{sid}")
async def check_status(sid: str):
    if sid in auth_states and auth_states[sid]["verified"]:
        return {"status": "verified"}
    return {"status": "waiting"}

@app.get("/portal")
async def portal(request: Request, sid: str):
    if sid not in auth_states or not auth_states[sid]["verified"]:
        return RedirectResponse(f"/login?sid={sid}")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        groups = conn.execute("SELECT * FROM groups").fetchall()
    return templates.TemplateResponse("portal.html", {"request": request, "sid": sid, "groups": groups})

@app.get("/manage")
async def manage_page(request: Request, sid: str, gid: int):
    if sid not in auth_states or not auth_states[sid]["verified"]:
        raise HTTPException(status_code=403)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute("SELECT * FROM verified_users WHERE group_id=? ORDER BY sort_order DESC", (gid,)).fetchall()
        group = conn.execute("SELECT * FROM groups WHERE group_id=?", (gid,)).fetchone()
        btns = conn.execute("SELECT * FROM bottom_buttons WHERE group_id=? ORDER BY sort_order", (gid,)).fetchall()
    return templates.TemplateResponse("manage.html", {"request": request, "sid": sid, "gid": gid, "users": users, "group": group, "bottom_btns": btns})

@app.post("/save_user")
async def save_user(sid: str=Form(...), gid: int=Form(...), user_id: int=Form(...), name: str=Form(...), 
                    area: str=Form(""), price: str=Form(""), chest_size: str=Form(""), sort_order: int=Form(0)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''INSERT OR REPLACE INTO verified_users (user_id, group_id, name, area, price, chest_size, sort_order) 
                        VALUES (?,?,?,?,?,?,?)''', (user_id, gid, name, area, price, chest_size, sort_order))
        conn.commit()
    return RedirectResponse(url=f"/manage?sid={sid}&gid={gid}", status_code=303)

@app.post("/save_settings")
async def save_settings(sid:str=Form(...), gid:int=Form(...), page_size:int=Form(...), like_emoji:str=Form(...), list_template:str=Form(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE groups SET page_size=?, like_emoji=?, list_template=? WHERE group_id=?", (page_size, like_emoji, list_template, gid))
        conn.commit()
    return RedirectResponse(url=f"/manage?sid={sid}&gid={gid}", status_code=303)

@app.post("/save_bottom_btn")
async def save_btn(sid:str=Form(...), gid:int=Form(...), btn_text:str=Form(...), btn_type:str=Form(...), btn_value:str=Form(...), sort:int=Form(0)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO bottom_buttons (group_id, btn_text, btn_type, btn_value, sort_order) VALUES (?,?,?,?,?)", (gid, btn_text, btn_type, btn_value, sort))
        conn.commit()
    return RedirectResponse(url=f"/manage?sid={sid}&gid={gid}", status_code=303)

# --- 5. 启动 ---
@app.on_event("startup")
async def on_startup():
    init_db()
    asyncio.create_task(dp.start_polling(bot))
    logger.info("Application startup complete.")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
