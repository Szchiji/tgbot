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

# --- 1. 配置 ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
DOMAIN = os.getenv("RAILWAY_STATIC_URL", "localhost:8080")
DB_PATH = "/data/bot.db"
STATIC_DIR = "/data/static"

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
app = FastAPI()
templates = Jinja2Templates(directory="templates")

os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

auth_states = {}

# --- 2. 数据库初始化 (完整字段) ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        # 认证用户表
        conn.execute('''CREATE TABLE IF NOT EXISTS verified_users (
                        user_id INTEGER, group_id INTEGER, name TEXT, 
                        area TEXT, price TEXT, chest_size TEXT, height TEXT,
                        teacher_name TEXT, channel_link TEXT, sort_order INTEGER DEFAULT 0,
                        photo_url TEXT, PRIMARY KEY(user_id, group_id))''')
        # 群组表
        conn.execute('''CREATE TABLE IF NOT EXISTS groups (
                        group_id INTEGER PRIMARY KEY, group_name TEXT,
                        page_size INTEGER DEFAULT 20, like_emoji TEXT DEFAULT '👍',
                        list_template TEXT DEFAULT '✅ {area} {name} 频道 胸{chest_size} {price}')''')
        # 每日打卡表
        conn.execute('''CREATE TABLE IF NOT EXISTS checkins (
                        user_id INTEGER, group_id INTEGER, checkin_date TEXT,
                        PRIMARY KEY(user_id, group_id, checkin_date))''')
        # 底部按钮表
        conn.execute('''CREATE TABLE IF NOT EXISTS bottom_buttons (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER,
                        btn_text TEXT, btn_type TEXT, btn_value TEXT, sort_order INTEGER DEFAULT 0)''')
        conn.commit()

# --- 3. 鉴权工具 ---
def check_auth(token: str):
    if token not in auth_states or not auth_states[token]["verified"]:
        raise HTTPException(status_code=403, detail="未授权")
    return auth_states[token]

# --- 4. 机器人逻辑 (打卡、点赞、榨汁列表) ---

async def get_juice_markup(gid, page=1):
    today = datetime.now().strftime('%Y-%m-%d')
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        group = conn.execute("SELECT * FROM groups WHERE group_id=?", (gid,)).fetchone()
        page_size = group['page_size'] if group else 20
        tpl = group['list_template'] if group else "✅ {area} {name} 频道 胸{chest_size} {price}"
        
        users = conn.execute('''
            SELECT v.* FROM verified_users v 
            JOIN checkins c ON v.user_id = c.user_id AND v.group_id = c.group_id
            WHERE v.group_id = ? AND c.checkin_date = ? ORDER BY v.sort_order DESC
        ''', (gid, today)).fetchall()

    if not users: return "📅 今日暂无老师打卡开课。", None

    total_pages = (len(users) + page_size - 1) // page_size
    current_users = users[(page-1)*page_size : page*page_size]

    text = f"<b>榨汁 🍼 以下为今日开课老师</b>\n\n"
    for u in current_users:
        # 动态解析模板
        try:
            line = tpl.format(area=u['area'], name=u['name'], chest_size=u['chest_size'], price=u['price'])
        except:
            line = f"✅ {u['area']} {u['name']}"
        text += line + "\n"
    text += f"\n页码: {page}/{total_pages}"

    builder = InlineKeyboardBuilder()
    if page > 1: builder.button(text="⬅️ 上一页", callback_data=f"juice_{page-1}")
    if page < total_pages: builder.button(text="下一页 ➡️", callback_data=f"juice_{page+1}")
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        customs = conn.execute("SELECT * FROM bottom_buttons WHERE group_id=? ORDER BY sort_order", (gid,)).fetchall()
        for b in customs:
            if b['btn_type'] == 'url': builder.row(types.InlineKeyboardButton(text=b['btn_text'], url=b['btn_value']))
            else: builder.row(types.InlineKeyboardButton(text=b['btn_text'], callback_data=b['btn_value']))
    
    return text, builder.as_markup()

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group(msg: types.Message):
    gid, uid = msg.chat.id, msg.from_user.id
    today = datetime.now().strftime('%Y-%m-%d')
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT * FROM verified_users WHERE user_id=? AND group_id=?", (uid, gid)).fetchone()
        group = conn.execute("SELECT * FROM groups WHERE group_id=?", (gid,)).fetchone()
        if not group: 
            conn.execute("INSERT INTO groups (group_id, group_name) VALUES (?,?)", (gid, msg.chat.title))
            conn.commit()

    # 自动点赞
    if user:
        emoji = group['like_emoji'] if group else "👍"
        try: await msg.react([types.ReactionTypeEmoji(emoji=emoji)])
        except: pass

    # 打卡逻辑
    if msg.text == "打卡" and user:
        with sqlite3.connect(DB_PATH) as conn:
            try:
                conn.execute("INSERT INTO checkins VALUES (?,?,?)", (uid, gid, today))
                conn.commit()
                await msg.reply("✅ 打卡成功！")
            except: await msg.reply("ℹ️ 今日已打卡")

    # 列表展示
    if msg.text == "今日榨汁":
        text, markup = await get_juice_markup(gid)
        await msg.answer(text, reply_markup=markup, disable_web_page_preview=True)

@dp.callback_query(F.data.startswith("juice_"))
async def juice_page(call: types.CallbackQuery):
    page = int(call.data.split("_")[1])
    text, markup = await get_juice_markup(call.message.chat.id, page)
    await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)

# --- 5. Web 路由 (完整配置管理) ---

@app.get("/manage", response_class=HTMLResponse)
async def manage(request: Request, token: str, gid: int):
    check_auth(token)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute("SELECT * FROM verified_users WHERE group_id=? ORDER BY sort_order DESC", (gid,)).fetchall()
        group = conn.execute("SELECT * FROM groups WHERE group_id=?", (gid,)).fetchone()
        btns = conn.execute("SELECT * FROM bottom_buttons WHERE group_id=? ORDER BY sort_order", (gid,)).fetchall()
    return templates.TemplateResponse("manage.html", {"request": request, "token": token, "gid": gid, "users": users, "group": group, "bottom_btns": btns})

@app.post("/save_user")
async def save_user(token: str=Form(...), gid: int=Form(...), user_id: int=Form(...), name: str=Form(...), 
                    area: str=Form(""), price: str=Form(""), chest_size: str=Form(""), sort_order: int=Form(0)):
    check_auth(token)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''INSERT OR REPLACE INTO verified_users (user_id, group_id, name, area, price, chest_size, sort_order) 
                        VALUES (?,?,?,?,?,?,?)''', (user_id, gid, name, area, price, chest_size, sort_order))
        conn.commit()
    return RedirectResponse(url=f"/manage?token={token}&gid={gid}", status_code=303)

@app.post("/save_settings")
async def save_settings(token:str=Form(...), gid:int=Form(...), page_size:int=Form(...), like_emoji:str=Form(...), list_template:str=Form(...)):
    check_auth(token)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE groups SET page_size=?, like_emoji=?, list_template=? WHERE group_id=?", (page_size, like_emoji, list_template, gid))
        conn.commit()
    return RedirectResponse(url=f"/manage?token={token}&gid={gid}", status_code=303)

@app.post("/save_bottom_btn")
async def save_btn(token:str=Form(...), gid:int=Form(...), btn_text:str=Form(...), btn_type:str=Form(...), btn_value:str=Form(...), sort:int=Form(0)):
    check_auth(token)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO bottom_buttons (group_id, btn_text, btn_type, btn_value, sort_order) VALUES (?,?,?,?,?)", (gid, btn_text, btn_type, btn_value, sort))
        conn.commit()
    return RedirectResponse(url=f"/manage?token={token}&gid={gid}", status_code=303)

# 基础鉴权与启动逻辑 (省略重复的登录/Portal代码，请保留之前版本)
@app.on_event("startup")
async def on_startup():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
