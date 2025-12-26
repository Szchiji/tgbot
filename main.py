import os, asyncio, sqlite3, random, string, uuid, logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import uvicorn

# --- 1. 环境变量安全配置 ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))  # 您的数字ID
DB_PATH = os.getenv("DATABASE_URL", "/data/bot.db")
DOMAIN = os.getenv("RAILWAY_STATIC_URL", "localhost:8080")

if not TOKEN or not ADMIN_ID:
    logging.error("未检测到环境变量 BOT_TOKEN 或 ADMIN_ID")

# --- 2. 初始化核心组件 ---
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 内存缓存：存储验证码和登录Token
login_codes = {} 
valid_sessions = {}

# --- 3. 数据库初始化 ---
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        # 会员表：包含您截图中的所有字段
        conn.execute('''CREATE TABLE IF NOT EXISTS verified_users (
                        user_id INTEGER, group_id INTEGER, name TEXT, sort_order INTEGER DEFAULT 0,
                        teacher_name TEXT, chat_link TEXT, channel_link TEXT, area TEXT, 
                        price TEXT, chest_size TEXT, height TEXT, bi_contact TEXT,
                        PRIMARY KEY(user_id, group_id))''')
        # 群组表：用于后台选择切换
        conn.execute('''CREATE TABLE IF NOT EXISTS groups (group_id INTEGER PRIMARY KEY, group_name TEXT)''')
        # 打卡记录表
        conn.execute('''CREATE TABLE IF NOT EXISTS checkins (user_id INTEGER, group_id INTEGER, date TEXT)''')
        conn.commit()

# --- 4. 机器人逻辑：安全验证 ---

@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start_private(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("❌ 抱歉，您不是系统管理员。")
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🔐 获取后台登录验证码", callback_data="get_login_code"))
    await msg.answer("<b>方丈机器人管理系统</b>\n点击下方按钮获取动态验证码：", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "get_login_code")
async def btn_get_code(call: types.CallbackQuery):
    code = ''.join(random.choices(string.digits, k=6))
    login_codes[ADMIN_ID] = code
    await call.message.answer(f"🔢 您的后台验证码为：<code>{code}</code>\n请直接在这里回复该数字。")
    await call.answer()

@dp.message(F.chat.type == "private", F.text.regexp(r'^\d{6}$'))
async def verify_login_msg(msg: types.Message):
    if msg.from_user.id == ADMIN_ID and msg.text == login_codes.get(ADMIN_ID):
        token = str(uuid.uuid4())
        # 设置Token 30分钟有效
        valid_sessions[token] = datetime.now() + timedelta(minutes=30)
        login_url = f"https://{DOMAIN}/admin?token={token}"
        
        await msg.answer(f"✅ 验证成功！登录链接已生成（30分钟有效）：\n\n<a href='{login_url}'>👉 点击进入方丈式管理中心</a>")
        login_codes.pop(ADMIN_ID, None)

# --- 5. 机器人逻辑：群组交互 ---

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_msg(msg: types.Message):
    gid, uid, text = msg.chat.id, msg.from_user.id, msg.text or ""

    # 自动保存机器人所在的群信息
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO groups VALUES (?, ?)", (gid, msg.chat.title or f"群组{gid}"))
        conn.commit()

    # 检查是否是认证会员
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT * FROM verified_users WHERE user_id=? AND group_id=?", (uid, gid)).fetchone()

    if user:
        # 认证会员发言自动点赞
        try: await msg.react([types.ReactionTypeEmoji(emoji="👍")])
        except: pass
        
        # 认证会员打卡
        if text == "打卡":
            today = datetime.now().strftime("%Y-%m-%d")
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("INSERT OR REPLACE INTO checkins VALUES (?, ?, ?)", (uid, gid, today))
                conn.commit()
            await msg.reply(f"✅ <b>{user['name']}</b> 打卡成功！")

# --- 6. Web后台：路由逻辑 ---

@app.get("/admin", response_class=HTMLResponse)
async def admin_portal(request: Request, token: str):
    # 安全检查
    if token not in valid_sessions or datetime.now() > valid_sessions[token]:
        return HTMLResponse("登录已过期，请重新在机器人获取验证码。", status_code=403)
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        groups = conn.execute("SELECT * FROM groups").fetchall()
    
    return templates.TemplateResponse("portal.html", {"request": request, "token": token, "groups": groups})

@app.get("/manage", response_class=HTMLResponse)
async def group_manage(request: Request, token: str, gid: int):
    if token not in valid_sessions: raise HTTPException(status_code=403)
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute("SELECT * FROM verified_users WHERE group_id=?", (gid,)).fetchall()
        group = conn.execute("SELECT * FROM groups WHERE group_id=?", (gid,)).fetchone()
        
    return templates.TemplateResponse("manage.html", {
        "request": request, "token": token, "gid": gid, "users": users, "group": group, "admin_id": ADMIN_ID
    })

@app.post("/save_user")
async def api_save_user(token: str=Form(...), gid: int=Form(...), user_id: int=Form(...), name: str=Form(...), 
                        sort: int=Form(0), t_name: str=Form(""), chat_link: str=Form(""), chan_link: str=Form(""),
                        area: str=Form(""), price: str=Form(""), chest: str=Form(""), height: str=Form(""), bi_contact: str=Form("")):
    if token not in valid_sessions: return "Unauthorized"
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''INSERT OR REPLACE INTO verified_users 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
                     (user_id, gid, name, sort, t_name, chat_link, chan_link, area, price, chest, height, bi_contact))
        conn.commit()
    
    return RedirectResponse(url=f"/manage?token={token}&gid={gid}", status_code=303)

# --- 7. 生命周期管理 ---
@app.on_event("startup")
async def on_startup():
    init_db()
    # 异步启动机器人轮询
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    # Railway 默认使用 8080 端口
    uvicorn.run(app, host="0.0.0.0", port=8080)
