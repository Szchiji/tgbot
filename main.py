import os, asyncio, sqlite3, uuid, logging
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
import uvicorn

# 强制开启最高等级日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BOT_DEBUG")

TOKEN = os.getenv("BOT_TOKEN")
# 如果没有配置 ADMIN_ID，这里会报错，提醒你检查变量
ADMIN_ID = int(os.getenv("ADMIN_ID")) 
DOMAIN = os.getenv("RAILWAY_STATIC_URL", "localhost:8080").rstrip('/')
if not DOMAIN.startswith('http'): DOMAIN = f"https://{DOMAIN}"

DB_PATH = "/data/bot.db"
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
app = FastAPI()
templates = Jinja2Templates(directory="templates")
auth_states = {}

@dp.message()
async def global_debug_handler(msg: types.Message):
    # 只要机器人收到任何消息，Railway 的 Logs 里就一定会显示这一行
    logger.info(f"！！！收到消息测试！！！来自用户: {msg.from_user.id} 内容: {msg.text}")
    
    if msg.text == "/start":
        if msg.from_user.id != ADMIN_ID:
            await msg.answer(f"❌ 鉴权失败。你的ID是 {msg.from_user.id}，但后台配置的是 {ADMIN_ID}")
            return

        sid = str(uuid.uuid4())
        code = str(os.urandom(3).hex())
        auth_states[sid] = {"code": code, "verified": False}
        
        login_url = f"{DOMAIN}/login?sid={sid}"
        # 按钮
        btn = types.InlineKeyboardButton(text="🔐 点击进入登录页面", url=login_url)
        markup = types.InlineKeyboardMarkup(inline_keyboard=[[btn]])
        
        await msg.answer(f"<b>验证码:</b> <code>{code}</code>\n请点击下方按钮登录管理后台。", reply_markup=markup)

@app.get("/login", response_class=HTMLResponse)
async def web_login(request: Request, sid: str):
    logger.info(f"网页访问测试: sid={sid}")
    if sid not in auth_states:
        return HTMLResponse("验证链接已过期，请回机器人重新发 /start")
    return templates.TemplateResponse("login.html", {"request": request, "sid": sid, "code": auth_states[sid]["code"]})

@app.on_event("startup")
async def on_startup():
    # 确保 /data 目录存在
    os.makedirs("/data", exist_ok=True)
    asyncio.create_task(dp.start_polling(bot))
    logger.info("机器人轮询已启动，正在等待消息...")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
