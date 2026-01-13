# bot_dual.py - XServer 续期机器人
import os
import json
import logging
import random
import asyncio
import tempfile
import shutil
from datetime import datetime, time
from zoneinfo import ZoneInfo

import telegram
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackContext

from xserver_renew import login_xserver, renew_xserver

# ========== 配置 ==========
load_dotenv()
TOKEN = os.getenv("TG_BOT_TOKEN")
ADMIN_IDS = [int(s.strip()) for s in os.getenv("ADMIN_IDS", "").split(",") if s.strip()]

DATA_FILE = "data.json"

SITES = {
    "xs": {
        "name": "XServer",
        "domain": "www.xserver.ne.jp",
        "emoji": "🟣",
    }
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def ensure_user_structure(data, uid):
    """确保用户数据结构完整，避免 KeyError"""
    if uid not in data["users"]:
        data["users"][uid] = {}

    u = data["users"][uid]

    if "accounts" not in u or not isinstance(u["accounts"], dict):
        u["accounts"] = {"xs": {}}
    else:
        u["accounts"] = {"xs": u["accounts"].get("xs", {})}

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


def get_site_info(site_type: str) -> dict:
    return SITES.get(site_type, {"name": "未知", "domain": "unknown", "emoji": "❓"})


def has_any_accounts(user_data: dict) -> bool:
    accounts = user_data.get("accounts", {})
    return bool(accounts.get("xs", {}))


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
                "⚠️ 无效指令，请先添加账号后使用\n格式: /add xs 账号#密码",
                5,
                user_msg=update.message,
            )
        return await func(update, context, *args, **kwargs)

    return wrapper


# ========== 命令处理 ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if is_admin(user_id):
        text = """欢迎使用 XServer 续期机器人！
------- 【菜 单】 --------
/start - 显示帮助
/check - 手动续期
/add   - 添加账号(请勿在群聊中使用)
/del   - 删除账号
/list  - 账号列表
/settime - 自动续期时间（范围 0–10 点）
/txt  - 管理员喊话
------- 【说 明】 --------
🟣 XServer (xs)
默认每天0-0时5分随机时间续期

add 格式: /add xs 账号#密码
del 格式: /del xs 账号 或 /del TGID
check 格式: /check 或 /check xs
list 格式: /list 或 /list xs
settime 格式: /settime 7:00
-------------------------"""
    else:
        text = """欢迎使用 XServer 续期机器人！
------- 【菜 单】 --------
/start - 显示帮助
/check - 手动续期
/add   - 添加账号(请勿在群聊中使用)
/del   - 删除账号
/list  - 账号列表
/settime - 自动续期时间（范围 0–10 点）
------- 【说 明】 --------
🟣 XServer (xs)
默认每天0-0时5分随机时间续期

add 格式: /add xs 账号#密码
del 格式: /del xs 账号 或 /del -all
check 格式: /check 或 /check xs
list 格式: /list 或 /list xs
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
            user_msg=update.message,
        )
        return

    if len(context.args) < 2 or context.args[0] != "xs" or "#" not in context.args[1]:
        await send_and_auto_delete(
            update.message.chat,
            "用法：/add xs 账号#密码",
            5,
            user_msg=update.message,
        )
        return

    try:
        account, password = context.args[1].split("#", 1)
    except ValueError:
        await send_and_auto_delete(
            update.message.chat,
            "格式错误，应为：/add xs 账号#密码",
            3,
            user_msg=update.message,
        )
        return

    account_name = account.strip()
    password = password.strip()
    site_info = get_site_info("xs")

    # 发送临时提示消息
    temp_msg = await update.message.chat.send_message(
        f"➡️ 正在为 {site_info['emoji']} {site_info['name']} 账号 {account_name} 登录..."
    )

    success, message, new_cookie = login_xserver(account_name, password)
    if not success:
        await temp_msg.delete()
        await send_and_auto_delete(
            update.message.chat,
            f"❌ {site_info['name']} 登录失败：{message}",
            6,
            user_msg=update.message,
        )
        return

    data = load_data()
    user_data = ensure_user_structure(data, user_id)

    is_first_account = not has_any_accounts(user_data)

    user_data["tgUsername"] = tg_username

    user_data["accounts"]["xs"][account_name] = {
        "username": account_name,
        "password": password,
        "cookie": new_cookie,
    }

    save_data(data)

    if is_first_account:
        await post_init(context.application)

    await temp_msg.delete()

    await send_and_auto_delete(
        update.message.chat,
        f"✅ {site_info['emoji']} {site_info['name']} 账号 {account_name} 登录成功",
        180,
        user_msg=update.message,
    )

    for admin_id in ADMIN_IDS:
        await context.bot.send_message(
            chat_id=admin_id,
            text=f"✅ 用户 {tg_username or user_id} 添加 {site_info['emoji']} {site_info['name']} 账号 {account_name}",
        )


# ========== /del ==========
async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not context.args:
        return await send_and_auto_delete(
            update.message.chat,
            "⚠️ 格式错误: /del xs 账号 | /del -all | /del TGID",
            5,
            user_msg=update.message,
        )

    data = load_data()
    tg_username = data.get("users", {}).get(user_id, {}).get("tgUsername", user_id)

    if is_admin(user_id):
        if len(context.args) == 1:
            arg = context.args[0]
            if arg.isdigit():
                if arg not in data["users"]:
                    return await send_and_auto_delete(
                        update.message.chat,
                        "⚠️ 未找到用户",
                        3,
                        user_msg=update.message,
                    )
                del data["users"][arg]
                save_data(data)

                await post_init(context.application)
                return await send_and_auto_delete(
                    update.message.chat,
                    f"✅ 已删除用户 {arg} 的所有账号",
                    15,
                    user_msg=update.message,
                )

        elif len(context.args) == 2:
            site_type, account_name = context.args
            if site_type != "xs":
                return await send_and_auto_delete(
                    update.message.chat,
                    "⚠️ 网站类型错误，应为 xs",
                    3,
                    user_msg=update.message,
                )

            found = False
            for uid, u in list(data["users"].items()):
                if account_name in u.get("accounts", {}).get(site_type, {}):
                    del u["accounts"][site_type][account_name]

                    if not has_any_accounts(u):
                        del data["users"][uid]
                        await post_init(context.application)

                    save_data(data)
                    found = True

                    site_info = get_site_info(site_type)
                    await send_and_auto_delete(
                        update.message.chat,
                        f"✅ 已删除 {site_info['emoji']} {site_info['name']} 账号: {account_name}",
                        15,
                        user_msg=update.message,
                    )
                    return

            if not found:
                return await send_and_auto_delete(
                    update.message.chat,
                    "⚠️ 未找到账号",
                    3,
                    user_msg=update.message,
                )
    else:
        user_data = data.get("users", {}).get(user_id, {})
        if not has_any_accounts(user_data):
            return await send_and_auto_delete(
                update.message.chat,
                "⚠️ 无效指令，请添加账号后使用",
                5,
                user_msg=update.message,
            )

        if context.args[0] == "-all":
            deleted_accounts = []
            for acc_name in user_data.get("accounts", {}).get("xs", {}):
                deleted_accounts.append(f"🟣 {acc_name}")

            del data["users"][user_id]
            save_data(data)

            await post_init(context.application)
            return await send_and_auto_delete(
                update.message.chat,
                f"🗑 已删除所有账号: {', '.join(deleted_accounts)}",
                15,
                user_msg=update.message,
            )

        if len(context.args) == 2:
            site_type, account_name = context.args
            if site_type != "xs":
                return await send_and_auto_delete(
                    update.message.chat,
                    "⚠️ 网站类型错误，应为 xs",
                    3,
                    user_msg=update.message,
                )

            if account_name not in user_data.get("accounts", {}).get(site_type, {}):
                return await send_and_auto_delete(
                    update.message.chat,
                    "⚠️ 未找到账号",
                    3,
                    user_msg=update.message,
                )

            del user_data["accounts"][site_type][account_name]

            if not has_any_accounts(user_data):
                del data["users"][user_id]
                await post_init(context.application)

            save_data(data)
            return await send_and_auto_delete(
                update.message.chat,
                f"✅ 已删除 🟣 {account_name}",
                10,
                user_msg=update.message,
            )

    return await send_and_auto_delete(
        update.message.chat,
        "⚠️ 参数错误",
        3,
        user_msg=update.message,
    )


# ========== /list ==========
@require_account
async def list_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    user_data = data.get("users", {}).get(user_id, {})
    if not has_any_accounts(user_data):
        return await send_and_auto_delete(
            update.message.chat,
            "⚠️ 无效指令，请添加账号后使用",
            5,
            user_msg=update.message,
        )

    text = "📋 你的账号:\n"
    site_info = get_site_info("xs")
    accounts = user_data.get("accounts", {}).get("xs", {})
    if accounts:
        text += f"\n{site_info['emoji']} {site_info['name']}【续期】:\n"
        text += "\n".join(accounts.keys()) + "\n"

    await send_and_auto_delete(update.message.chat, text, 20, user_msg=update.message)


# ========== 时间工具 ==========
beijing = ZoneInfo("Asia/Shanghai")


def now_str():
    return datetime.now(beijing).strftime("%Y-%m-%d %H:%M:%S")


async def send_error_screenshot(app: Application, uid: str, res: dict):
    screenshot_path = res.get("screenshot_path")
    if not screenshot_path:
        return
    if not os.path.exists(screenshot_path):
        return
    caption = f"📸 {res.get('name', '')} 续期错误截图"
    try:
        with open(screenshot_path, "rb") as photo:
            await app.bot.send_photo(chat_id=uid, photo=photo, caption=caption)
    except Exception as exc:
        logger.warning("发送截图失败: %s", exc)
    finally:
        try:
            os.remove(screenshot_path)
        except Exception:
            pass


# ========== 续期相关函数 ==========
async def run_xserver_renewals(targets, data):
    results = {}

    for uid, accounts in targets.items():
        results.setdefault(uid, {}).setdefault("xs", [])
        for acc_name, acc in accounts.items():
            res = await asyncio.to_thread(renew_xserver, acc_name, acc.get("password", ""))
            if res.get("cookie"):
                acc["cookie"] = res["cookie"]
                save_data(data)
            results[uid]["xs"].append(res)

    return results


# ========== /check ==========
@require_account
async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    site_filter = None
    if context.args and context.args[0] == "xs":
        site_filter = "xs"

    targets = {}

    if is_admin(user_id):
        for uid, u in data.get("users", {}).items():
            accounts = u.get("accounts", {}).get("xs", {})
            if accounts and (site_filter is None or site_filter == "xs"):
                targets[uid] = accounts
    else:
        u = data.get("users", {}).get(user_id)
        if not u or not has_any_accounts(u):
            return await send_and_auto_delete(
                update.message.chat,
                "⚠️ 你还没有绑定账号",
                3,
                user_msg=update.message,
            )

        accounts = u.get("accounts", {}).get("xs", {})
        if accounts:
            targets[user_id] = accounts

    if not targets:
        return await send_and_auto_delete(
            update.message.chat,
            "⚠️ 没有可续期的账号",
            3,
            user_msg=update.message,
        )

    waiting_msg = await update.message.chat.send_message("⏳ 续期中...")

    results = await run_xserver_renewals(targets, data)

    if is_admin(user_id):
        summary_lines = ["📋 续期结果:"]
        for uid, sites in results.items():
            user_line = f"\n👤 {uid}"
            summary_lines.append(user_line)
            logs = sites.get("xs", [])
            for r in logs:
                summary_lines.append(f"🟣 {mask_username(r['name'])} - {r['result']}")
            for r in logs:
                await send_error_screenshot(context.application, uid, r)

        await send_and_auto_delete(update.message.chat, "\n".join(summary_lines), 60, user_msg=update.message)
    else:
        text = "📋 续期结果:\n"
        logs = results.get(user_id, {}).get("xs", [])
        for r in logs:
            line = f"🟣 {mask_username(r['name'])} - {r['result']}"
            text += line + "\n"
        await send_and_auto_delete(update.message.chat, text, 60, user_msg=update.message)
        for r in logs:
            await send_error_screenshot(context.application, user_id, r)

    try:
        await waiting_msg.delete()
    except Exception:
        pass


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
            user_msg=update.message,
        )

    if not context.args:
        return await send_and_auto_delete(
            update.message.chat,
            "用法: /settime 小时:分钟 (0–10点)，例如: /settime 8:30",
            5,
            user_msg=update.message,
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
            user_msg=update.message,
        )

    if not (0 <= hour <= 9):
        return await send_and_auto_delete(
            update.message.chat,
            "⚠️ 续期时间范围只能是 0–10 点",
            5,
            user_msg=update.message,
        )
    if not (0 <= minute < 60):
        return await send_and_auto_delete(
            update.message.chat,
            "⚠️ 分钟必须是 0–59",
            3,
            user_msg=update.message,
        )

    data["users"][user_id]["sign_hour"] = hour
    data["users"][user_id]["sign_minute"] = minute
    save_data(data)

    await send_and_auto_delete(
        update.message.chat,
        f"✅ 已设置每日续期时间为 {hour:02d}:{minute:02d} (北京时间)",
        10,
        user_msg=update.message,
    )

    app: Application = context.application
    job_name = f"user_{user_id}_daily_check"

    old_jobs = app.job_queue.get_jobs_by_name(job_name)
    for j in old_jobs:
        j.schedule_removal()

    app.job_queue.run_daily(
        lambda ctx, uid=user_id: asyncio.create_task(user_daily_check(app, uid)),
        time=time(hour=hour, minute=minute, tzinfo=beijing),
        name=job_name,
    )


# ========== 定时续期 ==========
async def user_daily_check(app: Application, uid: str):
    uid = str(uid)
    data = load_data()
    u = data["users"].get(uid)
    if not u or not has_any_accounts(u):
        return

    delay = random.randint(0, 5 * 60)
    await asyncio.sleep(delay)

    targets = {uid: u.get("accounts", {}).get("xs", {})}
    if not targets[uid]:
        return

    results = await run_xserver_renewals(targets, data)

    text = "📋 自动续期结果:\n"
    logs = results.get(uid, {}).get("xs", [])
    for r in logs:
        text += f"🟣 {mask_username(r['name'])} - {r['result']}\n"

    try:
        await app.bot.send_message(chat_id=uid, text=text)
    except Exception:
        pass

    for r in logs:
        await send_error_screenshot(app, uid, r)


# ========== 定时任务注册 ==========

def register_jobs(app: Application):
    data = load_data()

    for uid, u in data.get("users", {}).items():
        hour = u.get("sign_hour", 0)
        minute = u.get("sign_minute", 0)

        async def user_job(context: CallbackContext, user_id=uid):
            await user_daily_check(app, user_id)

        app.job_queue.run_daily(
            user_job,
            time=time(hour=hour, minute=minute, tzinfo=beijing),
            name=f"user_{uid}_daily_check",
        )


# ========== 设置命令菜单 ==========
async def post_init(application: Application):
    data = load_data()

    user_no_acc = [
        BotCommand("start", "显示帮助"),
        BotCommand("add", "添加账号"),
    ]
    user_with_acc = [
        BotCommand("start", "显示帮助"),
        BotCommand("check", "手动续期"),
        BotCommand("add", "添加账号"),
        BotCommand("del", "删除账号"),
        BotCommand("list", "账号列表"),
        BotCommand("settime", "设置续期时间"),
    ]

    admin_no_acc = [
        BotCommand("start", "显示帮助"),
        BotCommand("check", "手动续期"),
        BotCommand("add", "添加账号"),
        BotCommand("del", "删除账号"),
        BotCommand("list", "账号列表"),
        BotCommand("txt", "管理员喊话"),
    ]
    admin_with_acc = [
        BotCommand("start", "显示帮助"),
        BotCommand("check", "手动续期"),
        BotCommand("add", "添加账号"),
        BotCommand("del", "删除账号"),
        BotCommand("list", "账号列表"),
        BotCommand("settime", "设置续期时间"),
        BotCommand("txt", "管理员喊话"),
    ]

    group_commands = [
        BotCommand("start", "显示帮助"),
        BotCommand("check", "手动续期"),
        BotCommand("add", "添加账号"),
        BotCommand("del", "删除账号"),
        BotCommand("list", "账号列表"),
        BotCommand("settime", "设置续期时间"),
    ]
    await application.bot.set_my_commands(group_commands, scope=telegram.BotCommandScopeAllGroupChats())

    await application.bot.set_my_commands(user_no_acc)

    for uid, u in data.get("users", {}).items():
        has_account = has_any_accounts(u)
        if int(uid) in ADMIN_IDS:
            commands = admin_with_acc if has_account else admin_no_acc
        else:
            commands = user_with_acc if has_account else user_no_acc

        await application.bot.set_my_commands(
            commands,
            scope=telegram.BotCommandScopeChat(int(uid)),
        )

    for admin_id in ADMIN_IDS:
        if str(admin_id) not in data.get("users", {}):
            await application.bot.set_my_commands(
                admin_no_acc,
                scope=telegram.BotCommandScopeChat(admin_id),
            )


# ========== /txt ==========
async def txt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    admin_name = update.effective_user.username or f"id:{user_id}"

    if update.message.chat.type != "private":
        if is_admin(user_id):
            await send_and_auto_delete(
                update.message.chat,
                "⚠️ /txt 群聊限制使用",
                5,
                user_msg=update.message,
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
            user_msg=update.message,
        )

    data = load_data()

    if "," in args and args.split(",", 1)[0].isdigit():
        target, content = args.split(",", 1)
        if target not in data["users"]:
            return await send_and_auto_delete(
                update.message.chat,
                "⚠️ 未找到用户",
                3,
                user_msg=update.message,
            )

        await context.application.bot.send_message(
            target,
            f"📢 管理员 {admin_name} 喊话:\n{content}",
        )

        return await send_and_auto_delete(
            update.message.chat,
            f"✅ 已向 {target} 发送喊话",
            10,
            user_msg=update.message,
        )

    sent = 0
    for uid in data["users"]:
        if uid == user_id:
            continue

        try:
            await context.application.bot.send_message(
                uid,
                f"📢 管理员 {admin_name} 喊话:\n{args}",
            )
            sent += 1
        except Exception as exc:
            logger.warning("发送失败: %s, 错误: %s", uid, exc)

    await send_and_auto_delete(
        update.message.chat,
        f"✅ 已发送 {sent} 个用户",
        10,
        user_msg=update.message,
    )


# ========== 启动 ==========

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("del", delete))
    app.add_handler(CommandHandler("list", list_accounts))
    app.add_handler(CommandHandler("settime", settime))
    app.add_handler(CommandHandler("txt", txt))

    register_jobs(app)

    print("🚀 XServer 续期机器人启动成功！")
    print(f"🟣 XServer: {SITES['xs']['domain']}")

    app.run_polling()


if __name__ == "__main__":
    main()
