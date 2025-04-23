import os
import telegram
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from datetime import datetime, timedelta

# 获取 Bot Token（已填入你的 Bot Token）
TOKEN = '7555347469:AAFbmv7QUfJHre7G8hV8OUfzXHVd1iqvAUg'
updater = Updater(TOKEN, use_context=True)
dispatcher = updater.dispatcher

# 会员信息存储（简单示例，实际使用中可替换为数据库）
members = {}

# 管理员设置（已替换为你的管理员聊天ID）
admin_chat_id = '6383212444'

# 添加会员命令
def add_member(update, context):
    user = update.message.from_user
    username = user.username
    members[username] = {
        'telegram_id': user.id,
        'added_on': datetime.now(),
    }
    update.message.reply_text(f'会员 {username} 已添加！有效期一个月。')

# 查询会员命令
def query_member(update, context):
    username = ' '.join(context.args)
    if username in members:
        member = members[username]
        added_on = member['added_on']
        expiry_date = added_on + timedelta(days=30)
        update.message.reply_text(f"会员 {username} 状态：有效，过期日期：{expiry_date.strftime('%Y-%m-%d')}")
    else:
        update.message.reply_text(f"会员 {username} 未找到！")

# 删除过期会员
def remove_expired_members():
    current_time = datetime.now()
    expired_members = [username for username, data in members.items() if (current_time - data['added_on']).days > 30]
    for username in expired_members:
        del members[username]
        print(f"已删除过期会员：{username}")

# 自动回复命令
def start(update, context):
    update.message.reply_text("欢迎使用机器人！")

# 创建菜单
def create_menu():
    keyboard = [
        [InlineKeyboardButton("查询会员", callback_data='query_member')],
        [InlineKeyboardButton("联系会员", callback_data='contact_member')]
    ]
    return InlineKeyboardMarkup(keyboard)

# 处理菜单按钮点击
def button(update, context):
    query = update.callback_query
    if query.data == 'query_member':
        query.edit_message_text(text="请输入会员用户名进行查询：")
    elif query.data == 'contact_member':
        query.edit_message_text(text="正在联系会员...")

# 定期任务检查过期会员（假设你用 Heroku 定时任务或者其他定时工具运行此函数）
def schedule_expiry_check():
    remove_expired_members()

# 添加命令处理
dispatcher.add_handler(CommandHandler("addmember", add_member))
dispatcher.add_handler(CommandHandler("querymember", query_member))
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CallbackQueryHandler(button))

# 启动机器人
updater.start_polling()
updater.idle()