# bot_dual.py - 支持多网站的签到机器人
import os
import json
import logging
import random
import asyncio
import telegram
import tempfile
import shutil
import subprocess
from datetime import datetime, time
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import (
    Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, CallbackContext
)
from nodeseek_login_dual import login_and_get_cookie
from xserver_renew import login_xserver, renew_xserver

# ========== 配置 ==========
load_dotenv()
TOKEN = os.getenv("TG_BOT_TOKEN")
ADMIN_IDS = [int(s.strip()) for s in os.getenv("ADMIN_IDS", "").split(",") if s.strip()]

DATA_FILE = "data.json"

# 网站配置
SITES = {
    "ns": {
        "name": "NodeSeek",
        "domain": "www.nodeseek.com",
        "emoji": "🔵"
    },
    "df": {
        "name": "DeepFlood", 
        "domain": "www.deepflood.com",
        "emoji": "🟢"
    },
    "xs": {
        "name": "XServer",
        "domain": "www.xserver.ne.jp",
        "emoji": "🟣"
    }
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def ensure_user_structure(data, uid):
    """确保用户数据结构完整，避免 KeyError"""
    if uid not in data["users"]:
        data["users"][uid] = {}

    u = data["users"][uid]

    if "accounts" not in u:
        u["accounts"] = {"ns": {}, "df": {}, "xs": {}}  # 分网站存储账号
    else:
        # 兼容旧数据结构，迁移到新结构
        if not isinstance(u["accounts"], dict) or "ns" not in u["accounts"]:
            old_accounts = u["accounts"] if isinstance(u["accounts"], dict) else {}
            u["accounts"] = {"ns": old_accounts, "df": {}, "xs": {}}
        else:
            u["accounts"].setdefault("xs", {})
    
    if "mode" not in u:
        u["mode"] = {"ns": False, "df": False, "xs": False}  # 分网站模式
    elif not isinstance(u["mode"], dict):
        old_mode = u["mode"]
        u["mode"] = {"ns": old_mode, "df": False, "xs": False}
    else:
        u["mode"].setdefault("xs", False)
        
    if "tgUsername" not in u:
        u["tgUsername"] = ""
    if "sign_hour" not in u:
        u["sign_hour"] = 0
    if "sign_minute" not in u:
        u["sign_minute"] = 0

    return u

def save_data(data):
    """安全保存 JSON 数据"""
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tf:
        json.dump(data, tf, indent=2, ensure_ascii=False)
        tempname = tf.name
    shutil.move(tempname, DATA_FILE)

def load_data():
    """加载数据并自动修复缺失字段"""
    if not os.path.exists(DATA_FILE):
        return {"users": {}}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        print("⚠️ data.json 损坏，已重置为空")
        data = {"users": {}}
        save_data(data)
        return data

    changed = False
    for uid in data.get("users", {}):
        before = json.dumps(data["users"][uid], sort_keys=True)
        ensure_user_structure(data, uid)
        after = json.dumps(data["users"][uid], sort_keys=True)
        if before != after:
            changed = True

    if changed:
        save_data(data)

    return data

# 初始化空文件
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"users": {}}, f, indent=2, ensure_ascii=False)

# ========== 工具函数 ==========
def is_admin(user_id: str) -> bool:
    return int(user_id) in ADMIN_IDS

def mask_username(name: str) -> str:
    if len(name) <= 2:
        return name[0] + "***" + (name[1] if len(name) > 1 else "")
    return name[0] + "***" + name[-1]

def mode_text(mode: bool, site_type: str = "") -> str:
    if site_type == "xs":
        return "续期"
    return "随机模式" if mode else "固定模式"

def get_site_info(site_type: str) -> dict:
    return SITES.get(site_type, {"name": "未知", "domain": "unknown", "emoji": "❓"})

def has_any_accounts(user_data: dict) -> bool:
    """检查用户是否有任何账号"""
    accounts = user_data.get("accounts", {})
    return bool(accounts.get("ns", {}) or accounts.get("df", {}) or accounts.get("xs", {}))

async def notify_admins(app, message: str):
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.send_message(admin_id, message)
        except:
            pass

async def send_and_auto_delete(chat, text: str, delay: int, user_msg=None):
    sent = await chat.send_message(text)
    
    async def _delete_later():
        await asyncio.sleep(delay)
        try:
            await sent.delete()
        except Exception as e:
            print(f"Failed to delete bot message {sent.message_id}: {e}")
        
        if user_msg:
            try:
                await user_msg.delete()
            except Exception as e:
                print(f"Failed to delete user message {user_msg.message_id}: {e}")

    asyncio.create_task(_delete_later())
    return sent

# ========== 命令保护：检查是否有账号 ==========
def require_account(func):
    """装饰器：限制命令必须绑定账号"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = str(update.effective_user.id)
        data = load_data()
        user_data = data.get("users", {}).get(user_id, {})
        
        if not has_any_accounts(user_data):
            return await send_and_auto_delete(
                update.message.chat, 
                "⚠️ 无效指令，请先添加账号后使用\n格式: /add ns 账号#密码 或 /add df 账号#密码 或 /add xs 账号#密码", 
                5, 
                user_msg=update.message
            )
        return await func(update, context, *args, **kwargs)
    return wrapper

# ========== 命令处理 ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if is_admin(user_id):
        text = """欢迎使用多网站签到机器人！
------- 【菜 单】 --------
/start - 显示帮助
/check - 手动签到
/add   - 添加账号(请勿在群聊中使用)
/del   - 删除账号
/mode  - 签到模式
/list  - 账号列表
/hz    - 每日汇总
/log   - 签到记录
/stats - 签到统计
/settime - 自动签到时间（范围 0–10 点）
/txt  - 管理员喊话
------- 【说 明】 --------
🔵 NodeSeek (ns) | 🟢 DeepFlood (df) | 🟣 XServer (xs)
默认每天0-0时5分随机时间签到

add 格式: /add ns 账号#密码 或 /add df 账号#密码 或 /add xs 账号#密码
del 格式: /del ns 账号 或 /del df 账号 或 /del xs 账号 或 /del TGID
check 格式: /check 或 /check ns 或 /check df 或 /check xs
mode 格式: /mode ns true 或 /mode df false
list 格式: /list 或 /list ns 或 /list df 或 /list xs
log 格式: /log ns 7 账号 或 /log df 30
stats 格式: /stats ns 30 或 /stats df 7
settime 格式: /settime 7:00
txt 格式: /txt 内容 或 /txt TGID,内容
-------------------------"""
    else:
        text = """欢迎使用多网站签到机器人！
------- 【菜 单】 --------
/start - 显示帮助
/check - 手动签到
/add   - 添加账号(请勿在群聊中使用)
/del   - 删除账号
/mode  - 签到模式
/list  - 账号列表
/log   - 签到记录
/stats - 签到统计
/settime - 自动签到时间（范围 0–10 点）
------- 【说 明】 --------
🔵 NodeSeek (ns) | 🟢 DeepFlood (df) | 🟣 XServer (xs)
默认每天0-0时5分随机时间签到

add 格式: /add ns 账号#密码 或 /add df 账号#密码 或 /add xs 账号#密码
del 格式: /del ns 账号 或 /del df 账号 或 /del xs 账号 或 /del -all
check 格式: /check 或 /check ns 或 /check df 或 /check xs
mode 格式: /mode ns true 或 /mode df false
list 格式: /list 或 /list ns 或 /list df 或 /list xs
log 格式: /log ns 7 账号 或 /log df 30
stats 格式: /stats ns 30 或 /stats df 7
settime 格式: /settime 7:00"""

    await update.message.chat.send_message(text)

# ========== /add ==========
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    user_id = str(update.effective_user.id)
    tg_username = update.effective_user.username or ""

    # 限制只能私聊使用
    if chat_type != "private":
        await send_and_auto_delete(
            update.message.chat, 
            "🚨 安全警告：/add 功能只能在私聊中使用！", 
            5, 
            user_msg=update.message
        )
        return

    if len(context.args) < 2 or context.args[0] not in ["ns", "df", "xs"] or "#" not in context.args[1]:
        await send_and_auto_delete(
            update.message.chat, 
            "用法：/add ns 账号#密码 或 /add df 账号#密码 或 /add xs 账号#密码", 
            5, 
            user_msg=update.message
        )
        return

    site_type = context.args[0]
    try:
        account, password = context.args[1].split("#", 1)
    except ValueError:
        await send_and_auto_delete(
            update.message.chat, 
            "格式错误，应为：/add ns 账号#密码 或 /add df 账号#密码 或 /add xs 账号#密码", 
            3, 
            user_msg=update.message
        )
        return

    account_name = account.strip()
    password = password.strip()
    site_info = get_site_info(site_type)

    # 发送临时提示消息
    temp_msg = await update.message.chat.send_message(
        f"➡️ 正在为 {site_info['emoji']} {site_info['name']} 账号 {account_name} 登录..."
    )

    # 调用登录逻辑
    if site_type == "xs":
        success, message, new_cookie = login_xserver(account_name, password)
        if not success:
            await temp_msg.delete()
            await send_and_auto_delete(
                update.message.chat, 
                f"❌ {site_info['name']} 登录失败：{message}", 
                6, 
                user_msg=update.message
            )
            return
    else:
        new_cookie = login_and_get_cookie(account_name, password, site_type)
        if not new_cookie:
            await temp_msg.delete()
            await send_and_auto_delete(
                update.message.chat, 
                f"❌ {site_info['name']} 登录失败，请检查账号密码", 
                3, 
                user_msg=update.message
            )
            return

    # 读取 JSON 数据
    data = load_data()
    user_data = ensure_user_structure(data, user_id)
    
    # 判断是否是首次添加账号
    is_first_account = not has_any_accounts(user_data)

    user_data["tgUsername"] = tg_username

    # 写入账户信息
    user_data["accounts"][site_type][account_name] = {
        "username": account_name,
        "password": password,
        "cookie": new_cookie
    }

    save_data(data)

    # 如果是首次添加账号 → 刷新菜单
    if is_first_account:
        await post_init(context.application)

    # 创建用户日志文件
    log_file = f"./data/{user_id}.json"
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    if not os.path.exists(log_file):
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump({"logs": []}, f, ensure_ascii=False, indent=2)

    await temp_msg.delete()

    # 给用户反馈
    await send_and_auto_delete(
        update.message.chat,
        f"✅ {site_info['emoji']} {site_info['name']} 账号 {account_name} 成功获取 Cookie",
        180,
        user_msg=update.message
    )

    # 通知管理员
    for admin_id in ADMIN_IDS:
        await context.bot.send_message(
            chat_id=admin_id,
            text=f"✅ 用户 {tg_username or user_id} 添加 {site_info['emoji']} {site_info['name']} 账号 {account_name}"
        )

# ========== /del ==========
async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if not context.args:
        return await send_and_auto_delete(
            update.message.chat, 
            "⚠️ 格式错误: /del ns 账号 | /del df 账号 | /del xs 账号 | /del -all | /del TGID", 
            5, 
            user_msg=update.message
        )

    data = load_data()
    tgUsername = data.get("users", {}).get(user_id, {}).get("tgUsername", user_id)

    if is_admin(user_id):
        # 管理员操作
        if len(context.args) == 1:
            arg = context.args[0]
            if arg.isdigit():  # 按用户 ID 删除
                if arg not in data["users"]:
                    return await send_and_auto_delete(
                        update.message.chat, 
                        "⚠️ 未找到用户", 
                        3, 
                        user_msg=update.message
                    )
                del data["users"][arg]
                save_data(data)

                # 删除用户日志
                log_file = f"./data/{arg}.json"
                if os.path.exists(log_file):
                    os.remove(log_file)

                await post_init(context.application)
                return await send_and_auto_delete(
                    update.message.chat, 
                    f"✅ 已删除用户 {arg} 的所有账号", 
                    15, 
                    user_msg=update.message
                )
        
        elif len(context.args) == 2:
            site_type, account_name = context.args
            if site_type not in ["ns", "df", "xs"]:
                return await send_and_auto_delete(
                    update.message.chat, 
                    "⚠️ 网站类型错误，应为 ns 或 df 或 xs", 
                    3, 
                    user_msg=update.message
                )
            
            # 按账号名删除
            found = False
            for uid, u in list(data["users"].items()):
                if account_name in u.get("accounts", {}).get(site_type, {}):
                    del u["accounts"][site_type][account_name]
                    
                    # 检查是否还有其他账号
                    if not has_any_accounts(u):
                        del data["users"][uid]
                        log_file = f"./data/{uid}.json"
                        if os.path.exists(log_file):
                            os.remove(log_file)
                        await post_init(context.application)
                    
                    save_data(data)
                    found = True
                    
                    site_info = get_site_info(site_type)
                    await notify_admins(
                        context.application, 
                        f"管理员 {tgUsername} 删除了 {site_info['emoji']} {site_info['name']} 账号: {account_name}"
                    )
                    return await send_and_auto_delete(
                        update.message.chat, 
                        f"✅ 已删除 {site_info['emoji']} {site_info['name']} 账号: {account_name}", 
                        15, 
                        user_msg=update.message
                    )
            
            if not found:
                return await send_and_auto_delete(
                    update.message.chat, 
                    "⚠️ 未找到账号", 
                    3, 
                    user_msg=update.message
                )
    else:
        # 普通用户操作
        user_data = data.get("users", {}).get(user_id, {})
        if not has_any_accounts(user_data):
            return await send_and_auto_delete(
                update.message.chat, 
                "⚠️ 无效指令，请添加账号后使用", 
                5, 
                user_msg=update.message
            )

        if context.args[0] == "-all":
            # 删除所有账号
            deleted_accounts = []
            for site_type in ["ns", "df", "xs"]:
                accounts = user_data.get("accounts", {}).get(site_type, {})
                for acc_name in accounts:
                    site_info = get_site_info(site_type)
                    deleted_accounts.append(f"{site_info['emoji']} {acc_name}")
            
            del data["users"][user_id]
            save_data(data)

            log_file = f"./data/{user_id}.json"
            if os.path.exists(log_file):
                os.remove(log_file)

            await post_init(context.application)
            await notify_admins(
                context.application, 
                f"用户 {tgUsername} 删除了所有账号: {', '.join(deleted_accounts)}"
            )
            return await send_and_auto_delete(
                update.message.chat, 
                f"🗑 已删除所有账号: {', '.join(deleted_accounts)}", 
                15, 
                user_msg=update.message
            )
        
        elif len(context.args) == 2:
            site_type, account_name = context.args
            if site_type not in ["ns", "df", "xs"]:
                return await send_and_auto_delete(
                    update.message.chat, 
                    "⚠️ 网站类型错误，应为 ns 或 df 或 xs", 
                    3, 
                    user_msg=update.message
                )
            
            if account_name not in user_data.get("accounts", {}).get(site_type, {}):
                return await send_and_auto_delete(
                    update.message.chat, 
                    "⚠️ 未找到账号", 
                    3, 
                    user_msg=update.message
                )
            
            del user_data["accounts"][site_type][account_name]
            
            if not has_any_accounts(user_data):
                del data["users"][user_id]
                log_file = f"./data/{user_id}.json"
                if os.path.exists(log_file):
                    os.remove(log_file)
                await post_init(context.application)
            
            save_data(data)
            
            site_info = get_site_info(site_type)
            await notify_admins(
                context.application, 
                f"用户 {tgUsername} 删除了 {site_info['emoji']} {site_info['name']} 账号: {account_name}"
            )
            return await send_and_auto_delete(
                update.message.chat, 
                f"🗑 已删除 {site_info['emoji']} {site_info['name']} 账号: {account_name}", 
                15, 
                user_msg=update.message
            )

# ========== /mode ==========
@require_account
async def mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if len(context.args) != 2 or context.args[0] not in ["ns", "df", "xs"] or context.args[1] not in ["true", "false"]:
        return await send_and_auto_delete(
            update.message.chat, 
            "⚠️ 参数错误，应为 /mode ns true 或 /mode df false 或 /mode xs false", 
            5, 
            user_msg=update.message
        )

    site_type = context.args[0]
    mode_value = context.args[1] == "true"
    
    data = load_data()
    user_data = ensure_user_structure(data, user_id)
    user_data["mode"][site_type] = mode_value
    save_data(data)
    
    site_info = get_site_info(site_type)
    await send_and_auto_delete(
        update.message.chat, 
        f"✅ {site_info['emoji']} {site_info['name']} 签到模式: {mode_text(mode_value, site_type)}", 
        5, 
        user_msg=update.message
    )

# ========== /list ==========
async def list_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    # 解析参数
    site_filter = None
    if context.args and context.args[0] in ["ns", "df", "xs"]:
        site_filter = context.args[0]

    if is_admin(user_id):
        # 管理员查看所有用户
        text = "📋 所有用户账号:\n"
        for uid, u in data.get("users", {}).items():
            accounts_info = []
            for site_type in ["ns", "df", "xs"]:
                if site_filter and site_filter != site_type:
                    continue
                    
                site_accounts = u.get("accounts", {}).get(site_type, {})
                if site_accounts:
                    site_info = get_site_info(site_type)
                    mode = u.get("mode", {}).get(site_type, False)
                    accounts_list = list(site_accounts.keys())
                    accounts_info.append(
                        f"{site_info['emoji']} {site_info['name']}【{mode_text(mode, site_type)}】: {', '.join(accounts_list)}"
                    )
            
            if accounts_info:
                text += f"\n👤 {u.get('tgUsername', uid)}\n🆔 {uid}\n"
                text += "\n".join(accounts_info) + "\n"
        
        await send_and_auto_delete(
            update.message.chat, 
            text or "📭 暂无用户账号", 
            20, 
            user_msg=update.message
        )
    else:
        # 普通用户查看自己的账号
        user_data = data.get("users", {}).get(user_id, {})
        if not has_any_accounts(user_data):
            return await send_and_auto_delete(
                update.message.chat, 
                "⚠️ 无效指令，请添加账号后使用", 
                5, 
                user_msg=update.message
            )

        text = "📋 你的账号:\n"
        for site_type in ["ns", "df", "xs"]:
            if site_filter and site_filter != site_type:
                continue
                
            site_accounts = user_data.get("accounts", {}).get(site_type, {})
            if site_accounts:
                site_info = get_site_info(site_type)
                mode = user_data.get("mode", {}).get(site_type, False)
                accounts_list = list(site_accounts.keys())
                text += f"\n{site_info['emoji']} {site_info['name']}【{mode_text(mode, site_type)}】:\n"
                text += "\n".join(accounts_list) + "\n"
        
        await send_and_auto_delete(
            update.message.chat, 
            text, 
            20, 
            user_msg=update.message
        )

# ========== 时间工具 ==========
beijing = ZoneInfo("Asia/Shanghai")

def now_str():
    return datetime.now(beijing).strftime("%Y-%m-%d %H:%M:%S")

# ========== 写入日志函数 ==========
def append_user_log(tgid: str, log_entry: dict):
    """在 data/<TGID>.json 里追加日志，只记录含"收益"的日志"""
    if "收益" not in str(log_entry.get("result", "")):
        return

    path = f"./data/{tgid}.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user_data = json.load(f)
    else:
        user_data = {"logs": []}

    user_data.setdefault("logs", [])
    user_data["logs"].append(log_entry)
    user_data["logs"] = user_data["logs"][-30:]  # 只保留最近 30 条

    with open(path, "w", encoding="utf-8") as f:
        json.dump(user_data, f, indent=2, ensure_ascii=False)

# ========== 签到相关函数 ==========
async def retry_sign_if_invalid(uid, acc_name, site_type, res, data, mode):
    """Cookie 失效时自动刷新重试"""
    if "🚫 响应解析失败" not in res["result"] and "USER NOT FOUND" not in res["result"]:
        return res

    logging.warning("[%s] %s %s cookie 失效，尝试自动刷新...", uid, site_type, acc_name)

    account = data["users"][uid]["accounts"][site_type][acc_name]
    username, password = account["username"], account["password"]

    # 调用自动登录获取新 cookie
    new_cookie = login_and_get_cookie(username, password, site_type)
    if not new_cookie:
        logging.error("[%s] %s %s cookie 刷新失败", uid, site_type, acc_name)
        return {**res, "result": "🚫 Cookie 刷新失败", "no_log": True}

    # 保存新 cookie
    account["cookie"] = new_cookie
    save_data(data)

    # 再次签到
    payload = {
        "targets": {uid: {site_type: {acc_name: new_cookie}}},
        "userModes": {uid: {site_type: mode}}
    }

    try:
        proc = subprocess.run(
            ["node", "sign_dual.js", json.dumps(payload, ensure_ascii=False)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            logging.error("sign_dual.js 重试执行失败: %s", proc.stderr.strip())
            return {**res, "result": "🚫 Cookie 刷新后签到失败", "no_log": True}

        retry_results = json.loads(proc.stdout)
        retry_res = retry_results.get(uid, {}).get(site_type, [{}])[0]
        retry_res["cookie_refreshed"] = True
        return retry_res

    except Exception as e:
        logging.error("sign_dual.js 重试调用异常: %s", e)
        return {**res, "result": "🚫 Cookie 刷新后签到异常", "no_log": True}

async def run_sign_and_fix(targets, user_modes, data):
    """执行签到并处理 Cookie 刷新"""
    results = {}
    js_targets = {}
    xs_targets = {}

    # 转换为 sign_dual.js 需要的格式
    for uid, sites in targets.items():
        js_targets[uid] = {}
        for site_type, accounts in sites.items():
            if site_type == "xs":
                xs_targets.setdefault(uid, {})[site_type] = accounts
                continue
            js_targets[uid][site_type] = {
                name: acc["cookie"] for name, acc in accounts.items()
            }

    payload = {"targets": js_targets, "userModes": user_modes}

    if any(js_targets[uid] for uid in js_targets):
        try:
            proc = subprocess.run(
                ["node", "sign_dual.js", json.dumps(payload, ensure_ascii=False)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode != 0:
                logging.error("sign_dual.js 执行失败: %s", proc.stderr.strip())
                return {}

            results = json.loads(proc.stdout)
        except Exception as e:
            logging.error("调用 sign_dual.js 异常: %s", e)
            return {}

    # 处理失败重试
    for uid, sites in results.items():
        for site_type, logs in sites.items():
            fixed_logs = []
            for res in logs:
                acc_name = res["name"]
                mode = user_modes.get(uid, {}).get(site_type, False)
                fixed_res = await retry_sign_if_invalid(uid, acc_name, site_type, res, data, mode)
                fixed_logs.append(fixed_res)
            results[uid][site_type] = fixed_logs

    if xs_targets:
        for uid, sites in xs_targets.items():
            xs_accounts = sites.get("xs", {})
            if not xs_accounts:
                continue
            results.setdefault(uid, {}).setdefault("xs", [])
            for acc_name, acc in xs_accounts.items():
                res = await asyncio.to_thread(renew_xserver, acc_name, acc.get("password", ""))
                if res.get("cookie"):
                    acc["cookie"] = res["cookie"]
                    save_data(data)
                results[uid]["xs"].append(res)

    return results

# ========== /check ==========
async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    
    # 解析参数
    site_filter = None
    if context.args and context.args[0] in ["ns", "df", "xs"]:
        site_filter = context.args[0]

    targets, user_modes = {}, {}

    if is_admin(user_id):
        # 管理员签到所有用户
        for uid, u in data.get("users", {}).items():
            user_targets = {}
            user_site_modes = {}
            
            for site_type in ["ns", "df", "xs"]:
                if site_filter and site_filter != site_type:
                    continue
                    
                accounts = u.get("accounts", {}).get(site_type, {})
                if accounts:
                    user_targets[site_type] = accounts
                    user_site_modes[site_type] = u.get("mode", {}).get(site_type, False)
            
            if user_targets:
                targets[uid] = user_targets
                user_modes[uid] = user_site_modes
    else:
        # 普通用户签到自己的账号
        u = data.get("users", {}).get(user_id)
        if not u or not has_any_accounts(u):
            return await send_and_auto_delete(
                update.message.chat, 
                "⚠️ 你还没有绑定账号", 
                3, 
                user_msg=update.message
            )
        
        user_targets = {}
        user_site_modes = {}
        
        for site_type in ["ns", "df", "xs"]:
            if site_filter and site_filter != site_type:
                continue
                
            accounts = u.get("accounts", {}).get(site_type, {})
            if accounts:
                user_targets[site_type] = accounts
                user_site_modes[site_type] = u.get("mode", {}).get(site_type, False)
        
        if user_targets:
            targets[user_id] = user_targets
            user_modes[user_id] = user_site_modes

    if not targets:
        return await send_and_auto_delete(
            update.message.chat, 
            "⚠️ 没有可签到的账号", 
            3, 
            user_msg=update.message
        )

    waiting_msg = await update.message.chat.send_message("⏳ 签到中...")

    results = await run_sign_and_fix(targets, user_modes, data)

    manual_by = "admin" if is_admin(user_id) else "user"

    # 写入日志
    for uid, sites in results.items():
        for site_type, logs in sites.items():
            for r in logs:
                append_user_log(uid, {
                    **r,
                    "site_type": site_type,
                    "source": "manual",
                    "time": now_str(),
                    "by": manual_by
                })

    # 输出结果
    if is_admin(user_id):
        # 管理员使用分页显示
        await send_admin_check_results_paginated(
            context.application, 
            update.message.chat.id, 
            results, 
            user_modes, 
            data, 
            page=0
        )
    else:
        # 普通用户直接显示
        text = "📋 签到结果:\n"
        sites = results.get(user_id, {})
        
        for site_type, logs in sites.items():
            site_info = get_site_info(site_type)
            mode = user_modes.get(user_id, {}).get(site_type, False)
            text += f"\n{site_info['emoji']} {site_info['name']}【{mode_text(mode, site_type)}】:\n"
            
            for r in logs:
                line = f"{mask_username(r['name'])} - {r['result']}"
                if r.get("cookie_refreshed"):
                    line += " [♻️ Cookie]"
                text += line + "\n"

        await send_and_auto_delete(update.message.chat, text, 60, user_msg=update.message)

    try:
        await waiting_msg.delete()
    except Exception:
        pass

# ========== /log ==========
@require_account
async def log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    user_data = data.get("users", {}).get(user_id, {})
    if not has_any_accounts(user_data):
        return await send_and_auto_delete(
            update.message.chat, 
            "⚠️ 你还没有绑定账号，无法查询签到明细", 
            5, 
            user_msg=update.message
        )

    # 解析参数: /log ns 7 账号 或 /log df 30
    site_type = None
    days = 7
    filter_acc = None

    if context.args:
        if context.args[0] == "xs":
            return await send_and_auto_delete(
                update.message.chat,
                "⚠️ XServer 续期暂不支持查询签到明细",
                5,
                user_msg=update.message
            )
        if context.args[0] in ["ns", "df"]:
            site_type = context.args[0]
            if len(context.args) > 1:
                if context.args[1].isdigit():
                    days = int(context.args[1])
                    if len(context.args) > 2:
                        filter_acc = context.args[2]
                else:
                    filter_acc = context.args[1]

    # 构建查询目标
    targets = {user_id: {}}
    for s_type in ["ns", "df"]:
        if site_type and s_type != site_type:
            continue
            
        accounts = user_data.get("accounts", {}).get(s_type, {})
        if accounts:
            site_targets = {}
            for acc_name, acc in accounts.items():
                if filter_acc and acc_name != filter_acc:
                    continue
                cookie = acc.get("cookie")
                if cookie:
                    site_targets[acc_name] = cookie
            
            if site_targets:
                targets[user_id][s_type] = site_targets

    if not any(targets[user_id].values()):
        return await send_and_auto_delete(
            update.message.chat, 
            "⚠️ 没有找到匹配的账号或 Cookie", 
            5, 
            user_msg=update.message
        )

    payload = {"targets": targets, "days": days}
    waiting_msg = await update.message.chat.send_message("⏳ 正在查询中，请稍候...")

    try:
        res = subprocess.run(
            ["node", "stats_dual.js", json.dumps(payload)],
            capture_output=True, text=True, timeout=60
        )
        if res.returncode != 0:
            await waiting_msg.delete()
            return await send_and_auto_delete(
                update.message.chat, 
                f"⚠️ stats_dual.js 执行失败: {res.stderr}", 
                3, 
                user_msg=update.message
            )

        results = json.loads(res.stdout)
    except Exception as e:
        await waiting_msg.delete()
        return await send_and_auto_delete(
            update.message.chat, 
            f"⚠️ 查询异常: {e}", 
            3, 
            user_msg=update.message
        )

    text = f"📜 签到明细（{days} 天）：\n"
    user_results = results.get(user_id, {})

    for s_type, results_list in user_results.items():
        site_info = get_site_info(s_type)
        text += f"\n{site_info['emoji']} {site_info['name']}:\n"
        
        for r in results_list:
            acc_name = mask_username(r["name"])
            text += f"\n🔸 {acc_name} (签到收益)\n"

            if r.get("stats") and r["stats"]["days_count"] > 0:
                records = r["stats"]["records"]
                if not records:
                    text += "   ⚠️ 没有签到明细记录\n"
                else:
                    sorted_records = sorted(records, key=lambda x: x["date"], reverse=True)
                    for rec in sorted_records:
                        text += f"   {rec['date']}  🍗 +{rec['amount']}\n"
            else:
                text += f"   {r['result']}\n"

    await waiting_msg.delete()
    await send_and_auto_delete(update.message.chat, text, 20, user_msg=update.message)

# ========== /stats ==========
@require_account
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    user_data = data.get("users", {}).get(user_id, {})
    if not has_any_accounts(user_data):
        return await send_and_auto_delete(
            update.message.chat, 
            "⚠️ 你还没有绑定账号，无法查询签到收益", 
            3, 
            user_msg=update.message
        )

    # 解析参数: /stats ns 30 或 /stats df 7
    site_type = None
    days = 30

    if context.args:
        if context.args[0] == "xs":
            return await send_and_auto_delete(
                update.message.chat,
                "⚠️ XServer 续期暂不支持查询签到统计",
                5,
                user_msg=update.message
            )
        if context.args[0] in ["ns", "df"]:
            site_type = context.args[0]
            if len(context.args) > 1 and context.args[1].isdigit():
                days = int(context.args[1])

    # 构建查询目标
    targets = {user_id: {}}
    for s_type in ["ns", "df"]:
        if site_type and s_type != site_type:
            continue
            
        accounts = user_data.get("accounts", {}).get(s_type, {})
        if accounts:
            site_targets = {}
            for acc_name, acc in accounts.items():
                cookie = acc.get("cookie")
                if cookie:
                    site_targets[acc_name] = cookie
            
            if site_targets:
                targets[user_id][s_type] = site_targets

    if not any(targets[user_id].values()):
        return await send_and_auto_delete(
            update.message.chat, 
            "⚠️ 你所有账号都没有绑定 Cookie，无法查询", 
            3, 
            user_msg=update.message
        )

    payload = {"targets": targets, "days": days}
    waiting_msg = await update.message.chat.send_message("⏳ 正在查询中，请稍候...")

    try:
        res = subprocess.run(
            ["node", "stats_dual.js", json.dumps(payload)],
            capture_output=True, text=True, timeout=60
        )
        if res.returncode != 0:
            await waiting_msg.delete()
            return await send_and_auto_delete(
                update.message.chat, 
                f"⚠️ stats_dual.js 执行失败: {res.stderr}", 
                3, 
                user_msg=update.message
            )

        results = json.loads(res.stdout)
    except Exception as e:
        await waiting_msg.delete()
        return await send_and_auto_delete(
            update.message.chat, 
            f"⚠️ 查询异常: {e}", 
            3, 
            user_msg=update.message
        )

    text = f"📊 签到收益统计（{days} 天）：\n"
    user_results = results.get(user_id, {})

    for s_type, results_list in user_results.items():
        site_info = get_site_info(s_type)
        text += f"\n{site_info['emoji']} {site_info['name']}:\n"
        
        for r in results_list:
            acc_name = mask_username(r["name"])
            if r.get("stats") and r["stats"]["days_count"] > 0:
                stats_data = r["stats"]
                text += (
                    f"\n🔸 {acc_name}\n"
                    f"   🗓️ 签到天数 : {stats_data['days_count']} 天\n"
                    f"   🍗 总收益   : {stats_data['total_amount']} 个\n"
                    f"   📈 日均收益 : {stats_data['average']} 个\n"
                )
            else:
                text += f"\n🔸 {acc_name}\n   ⚠️ {r['result']}\n"

    await waiting_msg.delete()
    await send_and_auto_delete(update.message.chat, text, 20, user_msg=update.message)

# ========== /settime ==========
@require_account
async def settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    if user_id not in data.get("users", {}):
        return await send_and_auto_delete(
            update.message.chat, 
            "⚠️ 你还没有绑定账号，不能设置时间", 
            3, 
            user_msg=update.message
        )

    if not context.args:
        return await send_and_auto_delete(
            update.message.chat, 
            "用法: /settime 小时:分钟 (0–10点)，例如: /settime 8:30", 
            5, 
            user_msg=update.message
        )

    try:
        parts = context.args[0].split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return await send_and_auto_delete(
            update.message.chat,
            "⚠️ 时间格式错误，用法示例: /settime 8:30", 
            5, 
            user_msg=update.message
        )

    # 校验范围：0–10 点
    if not (0 <= hour <= 9):
        return await send_and_auto_delete(
            update.message.chat, 
            "⚠️ 签到时间范围只能是 0–10 点", 
            5, 
            user_msg=update.message
        )
    if not (0 <= minute < 60):
        return await send_and_auto_delete(
            update.message.chat, 
            "⚠️ 分钟必须是 0–59", 
            3, 
            user_msg=update.message
        )

    # 保存用户设置
    data["users"][user_id]["sign_hour"] = hour
    data["users"][user_id]["sign_minute"] = minute
    save_data(data)

    await send_and_auto_delete(
        update.message.chat, 
        f"✅ 已设置每日签到时间为 {hour:02d}:{minute:02d} (北京时间)", 
        10, 
        user_msg=update.message
    )

    # 重新注册用户的定时任务
    app: Application = context.application
    job_name = f"user_{user_id}_daily_check"

    # 移除旧任务
    old_jobs = app.job_queue.get_jobs_by_name(job_name)
    for j in old_jobs:
        j.schedule_removal()

    # 添加新任务（北京时间）
    app.job_queue.run_daily(
        lambda ctx, uid=user_id: asyncio.create_task(user_daily_check(app, uid)),
        time=time(hour=hour, minute=minute, tzinfo=beijing),
        name=job_name
    )

# ========== 定时签到 ==========
async def user_daily_check(app: Application, uid: str):
    uid = str(uid)
    data = load_data()
    u = data["users"].get(uid)
    if not u or not has_any_accounts(u):
        return

    delay = random.randint(0, 5 * 60)
    await asyncio.sleep(delay)

    # 构建签到目标
    targets = {uid: {}}
    user_modes = {uid: {}}
    
    for site_type in ["ns", "df", "xs"]:
        accounts = u.get("accounts", {}).get(site_type, {})
        if accounts:
            targets[uid][site_type] = accounts
            user_modes[uid][site_type] = u.get("mode", {}).get(site_type, False)

    if not any(targets[uid].values()):
        return

    # 执行签到
    results = await run_sign_and_fix(targets, user_modes, data)

    # 写入日志
    for site_type, logs in results.get(uid, {}).items():
        for r in logs:
            append_user_log(uid, {
                **r,
                "site_type": site_type,
                "source": "auto",
                "time": now_str(),
                "by": "system"
            })

    # 推送结果给用户
    text = "📋 自动签到结果:\n"
    for site_type, logs in results.get(uid, {}).items():
        site_info = get_site_info(site_type)
        mode = user_modes[uid].get(site_type, False)
        text += f"\n{site_info['emoji']} {site_info['name']}【{mode_text(mode, site_type)}】:\n"
        
        for r in logs:
            line = f"{mask_username(r['name'])} - {r['result']}"
            if r.get("cookie_refreshed"):
                line += " [♻️ Cookie]"
            text += line + "\n"

    try:
        await app.bot.send_message(chat_id=uid, text=text)
    except Exception:
        pass

# ========== 管理员签到结果分页 ==========
async def get_admin_check_page_content(results, user_modes, data, page: int = 0):
    """生成管理员签到结果分页内容"""
    # 收集所有有签到结果的用户
    users_with_results = []
    
    for uid, sites in results.items():
        if sites:  # 有签到结果
            users_with_results.append({
                'uid': uid,
                'user_info': data["users"][uid],
                'sites': sites
            })
    
    # 分页设置
    per_page = 5
    total_users = len(users_with_results)
    total_pages = (total_users + per_page - 1) // per_page if total_users > 0 else 1
    
    # 确保页码在有效范围内
    page = max(0, min(page, total_pages - 1))
    
    # 获取当前页的用户
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, total_users)
    current_page_users = users_with_results[start_idx:end_idx]
    
    # 构建消息文本
    text = f"📋 手动签到结果 (第{page + 1}/{total_pages}页):\n"
    
    if not current_page_users:
        text += "\n（暂无签到结果）"
    else:
        for user_data in current_page_users:
            uid = user_data['uid']
            u = user_data['user_info']
            sites = user_data['sites']
            
            text += f"\n👤 {u.get('tgUsername', uid)}\n🆔 {uid}\n"
            
            for site_type, logs in sites.items():
                site_info = get_site_info(site_type)
                mode = user_modes.get(uid, {}).get(site_type, False)
                text += f"{site_info['emoji']} {site_info['name']}【{mode_text(mode, site_type)}】:\n"
                
                for r in logs:
                    line = f"{mask_username(r['name'])} - {r['result']}"
                    if r.get("cookie_refreshed"):
                        line += " [♻️ Cookie]"
                    text += line + "\n"
    
    # 创建分页按钮
    keyboard = []
    nav_buttons = []
    
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"check_page_{page-1}"))
        
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"check_page_{page+1}"))
        
        keyboard.append(nav_buttons)
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    return text, reply_markup

async def send_admin_check_results_paginated(app: Application, chat_id: int, results, user_modes, data, page: int = 0):
    """发送管理员签到结果分页消息"""
    text, reply_markup = await get_admin_check_page_content(results, user_modes, data, page)
    
    # 存储结果数据供分页回调使用
    if not hasattr(app, 'temp_check_results'):
        app.temp_check_results = {}
    
    # 使用简化的时间戳作为唯一标识（避免下划线冲突）
    result_id = str(int(datetime.now().timestamp()))
    app.temp_check_results[result_id] = {
        'results': results,
        'user_modes': user_modes,
        'data': data,
        'chat_id': chat_id
    }
    
    print(f"DEBUG: 存储结果数据，ID: {result_id}")
    print(f"DEBUG: 当前存储的所有ID: {list(app.temp_check_results.keys())}")
    
    # 在按钮数据中包含结果ID
    if reply_markup:
        new_keyboard = []
        for row in reply_markup.inline_keyboard:
            new_row = []
            for button in row:
                callback_data = button.callback_data
                if callback_data.startswith("check_page_"):
                    page_num = callback_data.split("_")[2]
                    new_callback_data = f"check_page_{page_num}_{result_id}"
                    print(f"DEBUG: 生成按钮回调数据: {new_callback_data}")
                    new_row.append(InlineKeyboardButton(button.text, callback_data=new_callback_data))
                else:
                    new_row.append(button)
            new_keyboard.append(new_row)
        reply_markup = InlineKeyboardMarkup(new_keyboard)
    
    await app.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup
    )

async def check_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理签到结果分页回调"""
    query = update.callback_query
    user_id = str(query.from_user.id)
    
    if not is_admin(user_id):
        await query.answer("⚠️ 权限不足", show_alert=True)
        return
    
    data = query.data
    
    # 添加调试日志
    print(f"DEBUG: 收到回调数据: {data}")
    
    if data.startswith("check_page_"):
        try:
            # 格式: check_page_页码_结果ID
            # 由于结果ID可能包含下划线，需要特殊处理
            prefix = "check_page_"
            remaining = data[len(prefix):]  # 去掉前缀
            parts = remaining.split("_", 1)  # 只分割一次
            page = int(parts[0])
            result_id = parts[1] if len(parts) > 1 else None
            
            print(f"DEBUG: 解析结果 - page: {page}, result_id: {result_id}")
            
            if not result_id:
                await query.answer("⚠️ 缺少结果ID", show_alert=True)
                return
                
            if not hasattr(context.application, 'temp_check_results'):
                print("DEBUG: temp_check_results 属性不存在")
                await query.answer("⚠️ 数据存储未初始化，请重新执行签到", show_alert=True)
                return
            
            print(f"DEBUG: 可用的结果ID: {list(context.application.temp_check_results.keys())}")
            
            stored_data = context.application.temp_check_results.get(result_id)
            if not stored_data:
                print(f"DEBUG: 未找到结果ID {result_id} 对应的数据")
                await query.answer("⚠️ 数据已过期，请重新执行签到", show_alert=True)
                return
            
            text, reply_markup = await get_admin_check_page_content(
                stored_data['results'],
                stored_data['user_modes'], 
                stored_data['data'],
                page
            )
            
            # 更新按钮数据中的结果ID
            if reply_markup:
                new_keyboard = []
                for row in reply_markup.inline_keyboard:
                    new_row = []
                    for button in row:
                        callback_data = button.callback_data
                        if callback_data.startswith("check_page_"):
                            page_num = callback_data.split("_")[2]
                            new_callback_data = f"check_page_{page_num}_{result_id}"
                            new_row.append(InlineKeyboardButton(button.text, callback_data=new_callback_data))
                        else:
                            new_row.append(button)
                    new_keyboard.append(new_row)
                reply_markup = InlineKeyboardMarkup(new_keyboard)
            
            try:
                await query.edit_message_text(text=text, reply_markup=reply_markup)
            except Exception:
                try:
                    await query.message.delete()
                    await context.bot.send_message(
                        chat_id=query.message.chat.id,
                        text=text,
                        reply_markup=reply_markup
                    )
                except Exception:
                    pass
            
            await query.answer()
            
        except (IndexError, ValueError):
            await query.answer("⚠️ 页码错误", show_alert=True)
            return

# ========== /hz ==========
async def get_hz_page_content(page: int = 0):
    data = load_data()
    today = now_str()[:10]
    
    # 收集所有有签到记录的用户
    users_with_records = []
    
    for uid, u in data.get("users", {}).items():
        log_file = f"./data/{uid}.json"
        if os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8") as f:
                user_data = json.load(f)
                logs = user_data.get("logs", [])
        else:
            logs = []

        # 只取今天的签到收益
        todays = [
            l for l in logs
            if l.get("time", "")[:10] == today
            and "收益" in str(l.get("result", ""))
        ]
        
        if todays:
            users_with_records.append({
                'uid': uid,
                'user_info': u,
                'records': todays
            })
    
    # 分页设置
    per_page = 5
    total_users = len(users_with_records)
    total_pages = (total_users + per_page - 1) // per_page if total_users > 0 else 1
    
    # 确保页码在有效范围内
    page = max(0, min(page, total_pages - 1))
    
    # 获取当前页的用户
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, total_users)
    current_page_users = users_with_records[start_idx:end_idx]
    
    # 构建消息文本
    text = f"📋 今日签到成功汇总 (第{page + 1}/{total_pages}页):\n"
    
    if not current_page_users:
        text += "\n（今天暂无签到收益记录）"
    else:
        for user_data in current_page_users:
            uid = user_data['uid']
            u = user_data['user_info']
            todays = user_data['records']
            
            text += f"\n👤 {u.get('tgUsername', uid)}\n🆔 {uid}\n"
            
            # 按网站分组显示
            site_records = {}
            for r in todays:
                site_type = r.get("site_type", "ns")  # 默认为 ns
                if site_type not in site_records:
                    site_records[site_type] = []
                site_records[site_type].append(r)
            
            for site_type, records in site_records.items():
                site_info = get_site_info(site_type)
                mode = u.get("mode", {}).get(site_type, False)
                text += f"{site_info['emoji']} {site_info['name']}【{mode_text(mode, site_type)}】:\n"
                
                for r in records:
                    tag = "[手动]" if r.get("source") == "manual" else "[自动]"
                    line = f"{tag} {r['result']} - {mask_username(r['name'])}"
                    if r.get("cookie_refreshed"):
                        line += "  ♻️"
                    text += line + "\n"
    
    # 创建分页按钮
    keyboard = []
    nav_buttons = []
    
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"hz_page_{page-1}"))
        
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"hz_page_{page+1}"))
        
        keyboard.append(nav_buttons)
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    return text, reply_markup

async def admin_daily_summary_paginated(app: Application, target_admin_id: str = None, page: int = 0):
    text, reply_markup = await get_hz_page_content(page)
    
    if target_admin_id:
        await app.bot.send_message(
            chat_id=target_admin_id, 
            text=text,
            reply_markup=reply_markup
        )
    else:
        await notify_admins(app, text)

async def hz_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = str(query.from_user.id)
    
    if not is_admin(user_id):
        await query.answer("⚠️ 权限不足", show_alert=True)
        return
    
    data = query.data
    
    if data == "hz_noop":
        await query.answer()
        return
    
    if data.startswith("hz_page_"):
        try:
            page = int(data.split("_")[2])
        except (IndexError, ValueError):
            await query.answer("⚠️ 页码错误", show_alert=True)
            return
        
        text, reply_markup = await get_hz_page_content(page)
        
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup)
        except Exception:
            try:
                await query.message.delete()
                await context.bot.send_message(
                    chat_id=query.message.chat.id,
                    text=text,
                    reply_markup=reply_markup
                )
            except Exception:
                pass
        
        await query.answer()

async def hz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.message.chat.id

    if not is_admin(user_id):
        return

    # 限制时间：每天 10:10 ~ 23:59
    now_time = datetime.now().time()
    start = time(10, 10)
    end = time(23, 59)
    if not (start <= now_time <= end):
        return await send_and_auto_delete(
            update.message.chat,
            "⚠️ 请在 10:10 后使用",
            5,
            user_msg=update.message
        )

    if update.message.chat.type == "private":
        await admin_daily_summary_paginated(context.application, target_admin_id=user_id, page=0)
    else:
        await admin_daily_summary_paginated(context.application, target_admin_id=chat_id, page=0)

async def txt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    admin_name = update.effective_user.username or f"id:{user_id}"

    if update.message.chat.type != "private":
        if is_admin(user_id):
            await send_and_auto_delete(
                update.message.chat,
                "⚠️ /txt 群聊限制使用",
                5,
                user_msg=update.message
            )
        return

    if not is_admin(user_id):
        return

    args = " ".join(context.args)
    if not args:
        return await send_and_auto_delete(
            update.message.chat,
            "⚠️ 格式错误: /txt 内容 或 /txt TGID,内容",
            5,
            user_msg=update.message
        )

    data = load_data()

    # 单发
    if "," in args and args.split(",", 1)[0].isdigit():
        target, content = args.split(",", 1)
        if target not in data["users"]:
            return await send_and_auto_delete(
                update.message.chat,
                "⚠️ 未找到用户",
                3,
                user_msg=update.message
            )

        keyboard = [[
            InlineKeyboardButton("去回复", url="https://t.me/SerokBot_bot"),
            InlineKeyboardButton("己知晓", callback_data=f"ack_{user_id}")
        ]]

        await context.application.bot.send_message(
            target,
            f"📢 管理员 {admin_name} 喊话:\n{content}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        return await send_and_auto_delete(
            update.message.chat,
            f"✅ 已向 {target} 发送喊话",
            10,
            user_msg=update.message
        )

    # 群发
    sent = 0
    for uid in data["users"]:
        if uid == user_id:
            continue

        keyboard = [[
            InlineKeyboardButton("去回复", url="https://t.me/SerokBot_bot"),
            InlineKeyboardButton("己知晓", callback_data=f"ack_{user_id}")
        ]]

        try:
            await context.application.bot.send_message(
                uid,
                f"📢 管理员 {admin_name} 喊话:\n{args}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            sent += 1
        except Exception as e:
            logger.warning(f"发送失败: {uid}, 错误: {e}")

    await send_and_auto_delete(
        update.message.chat,
        f"✅ 已发送 {sent} 个用户",
        10,
        user_msg=update.message
    )

# 存放 每条喊话消息 -> 已确认的用户集合
acknowledged_users = {}

async def ack_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = str(query.from_user.id)
    username = query.from_user.username or f"id:{user_id}"
    data = query.data

    if not data.startswith("ack_"):
        return

    admin_id = int(data.split("_")[1])
    key = (query.message.chat.id, query.message.message_id)

    if key not in acknowledged_users:
        acknowledged_users[key] = set()

    if user_id in acknowledged_users[key]:
        await query.answer("⚠️ 你已知晓", show_alert=True)
        return

    acknowledged_users[key].add(user_id)

    try:
        await context.application.bot.send_message(
            admin_id,
            f"📣 用户 {username} 已知晓喊话内容"
        )
    except Exception as e:
        logger.warning(f"通知管理员失败: {admin_id}, 错误: {e}")

    await query.answer("✅ 已知晓")

# ========== 定时任务注册 ==========
def register_jobs(app: Application):
    data = load_data()

    # 管理员汇总任务 → 每天 10:05 (北京时间)
    async def admin_job(context: CallbackContext):
        for admin_id in ADMIN_IDS:
            try:
                await admin_daily_summary_paginated(context.application, target_admin_id=str(admin_id), page=0)
            except Exception as e:
                logger.warning(f"发送管理员汇总失败: {admin_id}, 错误: {e}")

    app.job_queue.run_daily(
        admin_job,
        time=time(hour=10, minute=5, tzinfo=beijing),
        name="admin_summary"
    )

    # 用户签到任务
    for uid, u in data.get("users", {}).items():
        hour = u.get("sign_hour", 0)
        minute = u.get("sign_minute", 0)

        async def user_job(context: CallbackContext, user_id=uid):
            await user_daily_check(app, user_id)

        app.job_queue.run_daily(
            user_job,
            time=time(hour=hour, minute=minute, tzinfo=beijing),
            name=f"user_{uid}_daily_check"
        )

# ========== 设置命令菜单 ==========
async def post_init(application: Application):
    data = load_data()

    # 普通用户菜单
    user_no_acc = [
        BotCommand("start", "显示帮助"),
        BotCommand("add", "添加账号"),
    ]
    user_with_acc = [
        BotCommand("start", "显示帮助"),
        BotCommand("check", "手动签到"),
        BotCommand("add", "添加账号"),
        BotCommand("del", "删除账号"),
        BotCommand("mode", "签到模式"),
        BotCommand("list", "账号列表"),
        BotCommand("log", "签到记录"),
        BotCommand("stats", "签到统计"),
        BotCommand("settime", "设置签到时间"),
    ]

    # 管理员菜单
    admin_no_acc = [
        BotCommand("start", "显示帮助"),
        BotCommand("check", "手动签到"),
        BotCommand("add", "添加账号"),
        BotCommand("del", "删除账号"),
        BotCommand("list", "账号列表"),
        BotCommand("hz", "每日汇总"),
        BotCommand("txt", "管理员喊话"),
    ]
    admin_with_acc = [
        BotCommand("start", "显示帮助"),
        BotCommand("check", "手动签到"),
        BotCommand("add", "添加账号"),
        BotCommand("del", "删除账号"),
        BotCommand("mode", "签到模式"),
        BotCommand("list", "账号列表"),
        BotCommand("log", "签到记录"),
        BotCommand("settime", "设置签到时间"),
        BotCommand("stats", "签到统计"),
        BotCommand("hz", "每日汇总"),
        BotCommand("txt", "管理员喊话"),
    ]

    # 群聊菜单
    group_commands = [
        BotCommand("start", "显示帮助"),
        BotCommand("check", "手动签到"),
        BotCommand("add", "添加账号"),
        BotCommand("del", "删除账号"),
        BotCommand("mode", "签到模式"),
        BotCommand("list", "账号列表"),
        BotCommand("log", "签到记录"),
        BotCommand("stats", "签到统计"),
        BotCommand("settime", "设置签到时间"),
    ]
    await application.bot.set_my_commands(group_commands, scope=telegram.BotCommandScopeAllGroupChats())

    # 默认菜单
    await application.bot.set_my_commands(user_no_acc)

    # 为每个用户设置专属菜单
    for uid, u in data.get("users", {}).items():
        has_account = has_any_accounts(u)
        if int(uid) in ADMIN_IDS:
            commands = admin_with_acc if has_account else admin_no_acc
        else:
            commands = user_with_acc if has_account else user_no_acc

        await application.bot.set_my_commands(
            commands,
            scope=telegram.BotCommandScopeChat(int(uid))
        )

    # 处理未绑定账号的管理员
    for admin_id in ADMIN_IDS:
        if str(admin_id) not in data.get("users", {}):
            await application.bot.set_my_commands(
                admin_no_acc,
                scope=telegram.BotCommandScopeChat(admin_id)
            )

# ========== 启动 ==========
def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # 注册命令处理器
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("del", delete))
    app.add_handler(CommandHandler("mode", mode))
    app.add_handler(CommandHandler("list", list_accounts))
    app.add_handler(CommandHandler("log", log))
    app.add_handler(CommandHandler("hz", hz))
    app.add_handler(CommandHandler("settime", settime))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("txt", txt))

    # 注册回调处理器
    app.add_handler(CallbackQueryHandler(hz_page_callback, pattern=r"^hz_"))
    app.add_handler(CallbackQueryHandler(check_page_callback, pattern=r"^check_page_"))
    app.add_handler(CallbackQueryHandler(ack_callback, pattern=r"^ack_"))

    # 注册定时任务
    register_jobs(app)

    print("🚀 多网站签到机器人启动成功！")
    print(f"🔵 NodeSeek: {SITES['ns']['domain']}")
    print(f"🟢 DeepFlood: {SITES['df']['domain']}")
    
    app.run_polling()

if __name__ == "__main__":
    main()
