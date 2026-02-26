import logging
import re
import html
import pytz
import sqlite3
import asyncio
from datetime import datetime, timedelta
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler, # <--- ADD THIS
    filters,
    ContextTypes
    TypeHandler,          # <--- ADD THIS
    ApplicationHandlerStop # <--- ADD THIS
)
import os
from keep_alive import keep_alive

# ========== CONFIGURATION ==========
TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = [8507307665] 
IST = pytz.timezone('Asia/Kolkata')

# ========== SQLITE DATABASE SETUP ==========
import pymongo

class PersistentDB:
    def __init__(self, uri="YOUR_MONGODB_URI_HERE", db_name="shining_star_db"):
        self.client = pymongo.MongoClient(uri)
        self.db = self.client[db_name]
        
        # Check if we already have a recorded start time
        start_info = self.db["system_info"].find_one({"type": "uptime"})
        if not start_info:
            self.db["system_info"].insert_one({"type": "uptime", "start_time": datetime.now(IST)})

        # Check if we already have stats
        if not self.db["system_info"].find_one({"type": "stats"}):
            self.db["system_info"].insert_one({"type": "stats", "scanned": 0, "caught": 0})

        # Collections (equivalent to Tables)
        self.group_settings = self.db["group_settings"]
        self.allowlist = self.db["allowlist"]
        self.warnings = self.db["warnings"]
        self.users = self.db["users"]
        self.groups = self.db["groups"]

    def add_user(self, user_id):
        self.users.update_one({"user_id": user_id}, {"$set": {"user_id": user_id}}, upsert=True)

    def add_group(self, chat_id, title="Unknown Group"):
        self.groups.update_one({"chat_id": chat_id}, {"$set": {"title": title}}, upsert=True)

    def get_groups(self):
        return [(g["chat_id"], g["title"]) for g in self.groups.find()]

    def get_all_targets(self):
        users = [u["user_id"] for u in self.users.find()]
        groups = [g["chat_id"] for g in self.groups.find()]
        return list(set(users + groups))

    def get_settings(self, chat_id):
        row = self.group_settings.find_one({"chat_id": chat_id})
        return (row["warn_limit"], row["mute_hours"]) if row else (5, 1)

    def set_limits(self, chat_id, warn_limit, mute_hours):
        self.group_settings.update_one(
            {"chat_id": chat_id}, 
            {"$set": {"warn_limit": warn_limit, "mute_hours": mute_hours}}, 
            upsert=True
        )

    def is_allowed(self, user_id):
        return self.allowlist.find_one({"user_id": user_id}) is not None

    def add_to_allowlist(self, user_id):
        if not self.is_allowed(user_id):
            self.allowlist.insert_one({"user_id": user_id})
            return True
        return False

    def remove_from_allowlist(self, user_id):
        result = self.allowlist.delete_one({"user_id": user_id})
        return result.deleted_count > 0

    def get_allowlist(self):
        return [row["user_id"] for row in self.allowlist.find()]

    def reset_warnings(self, user_id):
        self.warnings.delete_one({"user_id": user_id})

    def add_warning(self, user_id):
        row = self.warnings.find_one({"user_id": user_id})
        count = row["count"] if row else 0
        new_count = count + 1
        self.warnings.update_one({"user_id": user_id}, {"$set": {"count": new_count}}, upsert=True)
        return new_count

    def remove_warning(self, user_id):
        row = self.warnings.find_one({"user_id": user_id})
        if row and row["count"] > 0:
            new_count = row["count"] - 1
            if new_count == 0:
                self.warnings.delete_one({"user_id": user_id})
            else:
                self.warnings.update_one({"user_id": user_id}, {"$set": {"count": new_count}})
            return new_count
        return 0

    def get_stats(self):
        app_c = self.allowlist.count_documents({})
        warn_c = self.warnings.count_documents({})
        return app_c, warn_c

     # Ye PersistentDB class ke andar add karein
    def get_start_time(self):
        data = self.db["system_info"].find_one({"type": "uptime"})
        return data["start_time"].astimezone(IST)

    def increment_stat(self, field):
        # field can be 'scanned' or 'caught'
        self.db["system_info"].update_one(
            {"type": "stats"},
            {"$inc": {field: 1}},
            upsert=True
        )

    def get_system_counters(self):
        data = self.db["system_info"].find_one({"type": "stats"})
        if data:
            return data.get("scanned", 0), data.get("caught", 0)
        return 0, 0

# Initialize with your Mongo URI
# Tip: Use environment variables instead of hardcoding for security!
MONGO_URI = os.environ.get('MONGO_URI')
db = PersistentDB(uri=MONGO_URI)

# ========== LOGGING ==========
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== HELPERS ==========
async def is_user_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS: return True
    if update.effective_chat.type == 'private': return True # DM me sab khud admin hote hain
    try:
        chat_member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
        return chat_member.status in ['administrator', 'creator']
    except: return False

def has_link(text):
    if not text: return False
    link_patterns = [r'http[s]?://\S+', r'www\.\S+', r't\.me/\S+', r'\S+\.(com|org|net|in|co|io|xyz|me|info)\b']
    for pattern in link_patterns:
        if re.search(pattern, text, re.IGNORECASE): return True
    return False

async def delete_after_delay(message, delay=30):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except:
        pass

async def global_bot_admin_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # DMs are fine, skip the check so users can still message the bot privately
    if not update.effective_chat or update.effective_chat.type == 'private':
        return
    
    try:
        # Check the bot's status in the current group
        bot_member = await context.bot.get_chat_member(update.effective_chat.id, context.bot.id)
        if bot_member.status not in ['administrator', 'creator']:
            # Bot is not admin -> stop ALL further processing immediately
            raise ApplicationHandlerStop
    except Exception:
        # If there's an error (e.g., bot doesn't have access), stay completely silent
        raise ApplicationHandlerStop
        
# ========== CALLBACK HANDLER ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    # ✅ Sabhi "Delete" buttons (del_msg ya del_123) ke liye common logic
    if "del" in query.data:
        try:
            await query.message.delete()
            await query.answer() # Button click hone par loading circle hatane ke liye
        except Exception:
            pass
        return

    # 🔒 GLOBAL ADMIN CHECK (Sirf non-delete buttons ke liye)
    if not await is_user_admin(update, context):
        await query.answer("❌ you are not administrator", show_alert=True)
        return

    await query.answer()
    # ... baki ka code ...

    # Shared Help List
    help_text = (
        "❓ <b>Bot Help Menu</b>\n\n"
        "👤 <b>User Commands:</b>\n"
        "• <code>/start</code> : Check bot status\n"
        "• <code>/help</code> : Show this menu\n"
        "• <code>/status</code> : Group statistics\n\n"
        "🛠 <b>Admin Commands:</b>\n"
        "• <code>/approve</code> | <code>/unapprove</code> : Whitelist management\n"
        "• <code>/aplist</code> : View whitelisted users\n"
        "• <code>/settings &lt;warn&gt; &lt;hours&gt;</code> : Configure limits\n"
    )

    if query.data == "help_combined":
        # Agar button group me click hua hai
        if query.message.chat.type != 'private':
            bot_user = await context.bot.get_me()
            # Bot ke DM me le jaane wala link (sath me 'help' parameter)
            dm_url = f"https://t.me/{bot_user.username}?start=help"
            
            keyboard = [[InlineKeyboardButton("📥 Get Help in DM", url=dm_url)]]
            
            # Group me reply bhejna
            msg = await query.message.reply_text(
                f"Hi {query.from_user.first_name}, please click the button below to see the help menu in your DMs!",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            # Group me clean rakhne ke liye 15 seconds baad ye message auto-delete ho jayega
            asyncio.create_task(delete_after_delay(msg, 15))
            
            # Loading circle hatane ke liye
            await query.answer() 
            return
        
        # Agar button already DM me click hua hai, toh wahi Help menu dikha do
        else:
            keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="dm_back")]]
            await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            return

    if query.data == "dm_back":
        # Fetch the bot user info so it's defined in this scope
        bot_user = await context.bot.get_me()
        
        start_text = (
            f"    <b>Welcome to {html.escape(bot_user.first_name)}!</b>\n\n"
            "I am an advanced security bot designed to manage group security.\n\n"
            "  •  <b>Auto-Link Delete</b>: Instantly removes links from messages.\n"
            "  •  <b>Bio Scanner</b>: Blocks users who have links in their Telegram Bio.\n"
            "  •  <b>Self-Cleaning</b>: Bot replies auto-delete after 30 seconds.\n"
            "  •  <b>Dynamic Buttons</b>: Approve/Unapprove buttons switch instantly on click.\n"
            "  •  <b>Auto-Punish</b>: Automatically mutes users after a specific warning limit.\n"
            "  •  <b>Database Memory</b>: Saves all settings and whitelists permanently.\n"
            "  •  <b>Custom Limits</b>: Set your own warning counts and mute hours.\n\n"
            "💡 <b>Make the bot an Admin in the group with 'Delete Messages' & 'Ban Users' permissions!</b>\n"
        )
        
        # "Back" is replaced by "Support" here too
        keyboard = [
            [InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{bot_user.username}?startgroup=true")],
            [InlineKeyboardButton("Help❓", callback_data="help_combined")],
            [InlineKeyboardButton("🛠 Support", url="https://t.me/+rjE5xZlIK4U3ODA1"), InlineKeyboardButton("🗑 Delete", callback_data="del_msg")]
        ]
        await query.edit_message_text(start_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return

    if query.data == "del_msg":
        try: await query.message.delete()
        except: pass
        return

    if "_" in query.data:
        parts = query.data.split("_")
        action, target_id = parts[0], int(parts[1])

        if action == "approve":
            db.add_to_allowlist(target_id)
            db.reset_warnings(target_id)
            keyboard = [[InlineKeyboardButton("❌ Unapprove", callback_data=f"unapprove_{target_id}"), InlineKeyboardButton("🛡 Unwarn", callback_data=f"unwarn_{target_id}")], [InlineKeyboardButton("🗑 Delete", callback_data=f"del_{target_id}")]]
            await query.edit_message_text(f"✅ User `{target_id}` has been Approved.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        elif action == "unapprove":
            db.remove_from_allowlist(target_id)
            keyboard = [[InlineKeyboardButton("✅ Approve", callback_data=f"approve_{target_id}"), InlineKeyboardButton("🛡 Unwarn", callback_data=f"unwarn_{target_id}")], [InlineKeyboardButton("🗑 Delete", callback_data=f"del_{target_id}")]]
            await query.edit_message_text(f"❌ User `{target_id}` Unapproved.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        elif action == "unwarn":
            new_count = db.remove_warning(target_id)
            await query.edit_message_text(f"🛡 Warning removed. Current: {new_count}")
        elif action == "unmute":
            try:
                await context.bot.restrict_chat_member(
                    query.message.chat_id, 
                    target_id, 
                    ChatPermissions(can_send_messages=True, can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True)
                )
                db.reset_warnings(target_id) # <--- RESETS WARNINGS TO 0
                await query.edit_message_text(f"🔊 User `{target_id}` Unmuted. Warnings restarted!")
            except: pass

# ========== MESSAGE HANDLER ==========
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user: return

    # Ye line har message ko DB mein count karegi
    db.increment_stat("scanned")

    user = update.message.from_user
    chat_id = update.effective_chat.id

    if update.effective_chat.type == 'private':
        db.add_user(user.id)
        return
    
    db.add_group(chat_id, update.effective_chat.title)

    # 1. Check if the bot itself is an admin in the group
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if bot_member.status not in ['administrator', 'creator']:
            return  # The bot is not an admin, so it stays entirely silent
    except Exception:
        return # Cannot fetch bot status, stay silent just in case

    # 2. Check if the user is an Admin, Bot Owner, or Whitelisted (Approved)
    is_group_admin = await is_user_admin(update, context)
    is_approved = db.is_allowed(user.id)
    
    if is_group_admin or is_approved:
        return  # Do nothing. Admins and approved users are completely safe from warnings.

    # 3. Proceed with scanning for regular users
    warn_limit, mute_hrs = db.get_settings(chat_id)
    msg_text = update.message.text or update.message.caption
    
    violation, reason = False, ""
    try:
        u_chat = await context.bot.get_chat(user.id)
        if u_chat.bio and has_link(u_chat.bio): violation, reason = True, "Link in Bio"
    except: pass
    
    if not violation and has_link(msg_text): violation, reason = True, "Link in Message"

    if violation:
        # --- YE LINE YAHAN ADD KAREIN ---
        if reason == "Link in Bio":
            db.increment_stat("caught") 
        # -------------------------------

        try: await update.message.delete()
        except: pass
                
        count = db.add_warning(user.id)
        safe_name = html.escape(user.full_name) # Safely encodes <, >, &, etc.
        
        # --- ENFORCE MUTE FOR ANYONE AT OR ABOVE LIMIT ---
        if count >= warn_limit:
            
            # 1. ALWAYS try to mute them first
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat_id, 
                    user_id=user.id, 
                    permissions=ChatPermissions(can_send_messages=False), 
                    until_date=timedelta(hours=mute_hrs)
                )
            except Exception as e:
                # If Telegram rejects the mute (missing permissions)
                err_text = f"⚠️ <b>Failed to mute {safe_name}!</b>\n❗ Please ensure I have the <b>'Ban Users'</b> admin permission in this group."
                msg = await context.bot.send_message(chat_id, err_text, parse_mode='HTML')
                asyncio.create_task(delete_after_delay(msg, 10))
                db.remove_warning(user.id) # Revert the count so it tries again next time
                return # Stop processing
            
            # 2. If mute is successful, decide which message to show
            if count == warn_limit:
                # EXACT LIMIT HIT: Show the big message with Unmute button
                text = f"🚫 <b>User is muted now</b>\n👤 <b>Name:</b> {safe_name}\n🆔 <b>ID:</b> <code>{user.id}</code>\n🕒 <b>Duration:</b> {mute_hrs} Hours\n📝 <b>Reason:</b> {reason}"
                keyboard = [[InlineKeyboardButton("🔊 Unmute", callback_data=f"unmute_{user.id}")], [InlineKeyboardButton("🗑 Delete", callback_data=f"del_{user.id}")]]
                await context.bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            else:
                # PAST THE LIMIT (Concurrent extra messages): Show short auto-deleting text
                text = f"🚫 <b>User {safe_name} is muted already.</b>\n"
                msg = await context.bot.send_message(chat_id, text, parse_mode='HTML')
                asyncio.create_task(delete_after_delay(msg))
            
        # --- MESSAGE 1 & 2 (Warnings below limit) ---
        else:
            text = f"⚠️ <b>MESSAGE REMOVED</b>\n👤 <b>User:</b> {safe_name}\n🆔 <b>ID:</b> <code>{user.id}</code>\n📝 <b>Reason:</b> {reason}\n 📊 <b>Warnings:</b> {count}/{warn_limit}\n\n🛑 NOTICE: PLEASE REMOVE ANY LINKS FROM YOUR BIO IMMEDIATELY.\n\n📌 REPEATED VIOLATIONS MAY LEAD TO MUTE/BAN."
            btn_text, btn_data = ("❌ Unapprove", f"unapprove_{user.id}") if is_approved else ("✅ Approve", f"approve_{user.id}")
            keyboard = [[InlineKeyboardButton(btn_text, callback_data=btn_data), InlineKeyboardButton("🛡 Unwarn", callback_data=f"unwarn_{user.id}")], [InlineKeyboardButton("🗑 Delete", callback_data=f"del_{user.id}")]]
            
            await context.bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# ========== CHAT MEMBER HANDLER (Detects Manual Unmutes) ==========
async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.chat_member: 
        return
    
    old = update.chat_member.old_chat_member
    new = update.chat_member.new_chat_member
    
    # If user was restricted but is now allowed to send messages (manually unmuted or time expired)
    if old.status == 'restricted':
        can_send_now = new.status in ['member', 'administrator', 'creator'] or (new.status == 'restricted' and new.can_send_messages)
        if can_send_now:
            db.reset_warnings(new.user.id)

# ========== COMMANDS ==========

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # 1. Agar group ke link se aaya (Deep Linking Check)
    if context.args and context.args[0] == 'help':
        await help_command(update, context)
        return  # Yahan se code wapas laut jayega, aage nahi badhega
    # ------------------------------
   
    bot_user = await context.bot.get_me()
    
    if update.effective_chat.type == 'private':
        db.add_user(update.effective_user.id)
    else:
        db.add_group(update.effective_chat.id, update.effective_chat.title)

    start_text = (
        f"    <b>Welcome to {html.escape(bot_user.first_name)}!</b>\n\n"
        "I am an advanced security bot designed to manage group security.\n\n"
        "  •  <b>Auto-Link Delete</b>: Instantly removes links from messages.\n"
        "  •  <b>Bio Scanner</b>: Blocks users who have links in their Telegram Bio.\n"
        "  •  <b>Self-Cleaning</b>: Bot replies auto-delete after 30 seconds.\n"
        "  •  <b>Dynamic Buttons</b>: Approve/Unapprove buttons switch instantly on click.\n"
        "  •  <b>Auto-Punish</b>: Automatically mutes users after a specific warning limit.\n"
        "  •  <b>Database Memory</b>: Saves all settings and whitelists permanently.\n"
        "  •  <b>Custom Limits</b>: Set your own warning counts and mute hours.\n\n"
        "💡 <b>Make the bot an Admin in the group with 'Delete Messages' & 'Ban Users' permissions!</b>\n"
    )
    
    # "Back" is replaced by "Support" here
    keyboard = [
        [InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{bot_user.username}?startgroup=true")],
        [InlineKeyboardButton("Help❓", callback_data="help_combined")],
        [InlineKeyboardButton("🛠 Support", url="https://t.me/+rjE5xZlIK4U3ODA1"), InlineKeyboardButton("🗑 Delete", callback_data="del_msg")]
    ]
    
    await update.message.reply_text(start_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<b>📚 Help & Commands</b>\n\n"
        "👤 <b>User Commands:</b>\n"
        "• <code>/start</code> : Check bot status\n"
        "• <code>/help</code> : Show this menu\n"
        "• <code>/status</code> : Group statistics\n\n"
        "🛠 <b>Admin Commands:</b>\n"
        "• <code>/approve</code> | <code>/unapprove</code> : Whitelist management\n"
        "• <code>/aplist</code> : View whitelisted users\n"
        "• <code>/settings &lt;warn&gt; &lt;hours&gt;</code> : Configure limits\n"
    )
    
    keyboard = [[InlineKeyboardButton("🗑 Delete", callback_data="del_msg")]]
    user_id = update.effective_user.id
    
    try:
        # Try to send the help menu directly to the user's DM
        await context.bot.send_message(
            chat_id=user_id,
            text=help_text, 
            reply_markup=InlineKeyboardMarkup(keyboard), 
            parse_mode='HTML'
        )
        
        # If triggered in a group, send a quick confirmation and auto-delete it
        if update.effective_chat.type != 'private':
            msg = await update.message.reply_text("✅ I have sent the help menu to your DMs!")
            asyncio.create_task(delete_after_delay(msg, 5))
            
    except Exception:
        # If the bot fails to DM (because the user hasn't started the bot in private yet)
        bot_user = await context.bot.get_me()
        fail_keyboard = [[InlineKeyboardButton("🤖 Click here to start me", url=f"https://t.me/{bot_user.username}?start=true")]]
        
        msg = await update.message.reply_text(
            f"❌ I cannot send you a DM, {update.effective_user.first_name}.\n"
            "Please start me in private first by clicking the button below!",
            reply_markup=InlineKeyboardMarkup(fail_keyboard)
        )
        # Delete this warning after 15 seconds to keep the group clean
        asyncio.create_task(delete_after_delay(msg, 15))
        
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. Get current bot info
    bot_user = await context.bot.get_me()
    bot_name = html.escape(bot_user.first_name)

    # 2. Uptime calculation (Hours, Minutes, Seconds only)
    bot_start_time = db.get_start_time()
    now = datetime.now(IST)
    uptime_delta = now - bot_start_time
    
    # Calculate total seconds to ensure days are converted into hours
    total_seconds = int(uptime_delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    # Format: 25h 15m 30s
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    # 3. Get database stats
    total_scanned, bio_caught = db.get_system_counters()
    approved_count, total_warnings = db.get_stats()
    all_groups = db.get_groups()
    active_groups_count = len(all_groups)

    # 4. Final text
    status_text = (
        f"{bot_name}\n\n"
        "<b> 📊SYSTEM STATS</b>\n" 
        "----------------------------\n"
        "-------------\n"
        f"<b>⏱ Uptime:</b> <code>{uptime_str}</code>\n"
        f"<b>🔍 Total Scanned:</b> <code>{total_scanned}</code>\n"
        f"<b>🧬 Bio Links Caught:</b> <code>{bio_caught}</code>\n"
        f"<b>⚠️ Total Warnings:</b> <code>{total_warnings}</code>\n"
        f"<b>✅ Approved Users:</b> <code>{approved_count}</code>\n"
        f"<b>📂 Monitored Groups:</b> <code>{active_groups_count}</code>\n"
    )

    keyboard = [[InlineKeyboardButton("🗑 Delete", callback_data="del_msg")]]
    
    await update.message.reply_text(
        status_text, 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode='HTML'
    )

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await is_user_admin(update, context):
        msg = await update.message.reply_text("❌ you are not administrator")
        asyncio.create_task(delete_after_delay(msg, 10))
        return

    try:
        limit, hours = int(context.args[0]), int(context.args[1])
        db.set_limits(update.effective_chat.id, limit, hours)
        msg = await update.message.reply_text(f"✅ Settings Updated!\nLimit: {limit} warns\nMute: {hours} hours")
        asyncio.create_task(delete_after_delay(msg))
    except:
        msg = await update.message.reply_text("❌ Usage: `/settings 5 1`")
        asyncio.create_task(delete_after_delay(msg))

# Is block ko 'is_user_admin' ke niche paste karein
async def resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Ye function User ID, Username (@) aur Reply teeno ko handle karke 
    numeric ID return karta hai.
    """
    # 1. Agar kisi ke message par reply kiya gaya hai
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user.id
    
    # 2. Agar command ke saath argument diya gaya hai
    if context.args:
        arg = context.args[0]
        # Agar numeric ID hai (e.g. 123456)
        if arg.isdigit(): 
            return int(arg)
        # Agar username hai (e.g. @username)
        if arg.startswith('@'):
            try:
                chat = await context.bot.get_chat(arg)
                return chat.id
            except Exception:
                return None
                
    return None

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_admin(update, context):
        msg = await update.message.reply_text("❌ you are not administrator")
        asyncio.create_task(delete_after_delay(msg, 10))
        return

    target_id = await resolve_target(update, context)
    if target_id:
        db.add_to_allowlist(target_id)
        db.reset_warnings(target_id)
        msg = await update.message.reply_text(f"✅ User `{target_id}` whitelisted.")
    else:
        msg = await update.message.reply_text("❌ Usage: Reply to user or `/approve <ID>`")
    asyncio.create_task(delete_after_delay(msg))

async def unapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_admin(update, context):
        msg = await update.message.reply_text("❌ you are not administrator")
        asyncio.create_task(delete_after_delay(msg, 10))
        return

    target_id = await resolve_target(update, context)
    if target_id and db.remove_from_allowlist(target_id):
        msg = await update.message.reply_text(f"❌ User `{target_id}` removed from whitelist.")
    else:
        msg = await update.message.reply_text("❌ User not found or not in whitelist.")
    asyncio.create_task(delete_after_delay(msg))

async def aplist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    if not await is_user_admin(update, context):
        msg = await update.message.reply_text("❌ you are not administrator")
        asyncio.create_task(delete_after_delay(msg, 10))
        return
    
    users = db.get_allowlist()
    msg_text = "✅ <b>Approved Users:</b>\n" + "\n".join([f"<code>{u}</code>" for u in users]) if users else "Empty."
    msg = await update.message.reply_text(msg_text, parse_mode='HTML')
    asyncio.create_task(delete_after_delay(msg))

async def grouplist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin Check
    if update.effective_user.id not in ADMIN_IDS:
        msg = await update.message.reply_text("❌ You are not the bot owner.")
        return

    all_groups = db.get_groups() # Database se list li
    active_groups = []

    status_msg = await update.message.reply_text("🔍 Checking for active groups...")

    for g in all_groups:
        chat_id = g[0]
        group_name = g[1]
        try:
            # Check if bot is still in the group
            chat = await context.bot.get_chat(chat_id)
            active_groups.append(f"✅ {group_name} (`{chat_id}`)")
        except Exception:
            # Agar bot nikal chuka hai ya access nahi hai toh skip karein
            continue

    if active_groups:
        text = "📂 **Active Groups:**\n\n" + "\n".join(active_groups)
    else:
        text = "📂 **No active groups found.**"

    # Purana status message edit karke list dikhayein
    await status_msg.edit_text(text, parse_mode='Markdown')

async def getlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    if not await is_user_admin(update, context):
        msg = await update.message.reply_text("❌ you are not bot owner")
        asyncio.create_task(delete_after_delay(msg, 10))
        return
    
    if update.effective_user.id not in ADMIN_IDS or not context.args: return
    try:
        index = int(context.args[0]) - 1
        groups = db.get_groups()
        link = await context.bot.export_chat_invite_link(groups[index][0])
        await update.message.reply_text(f"🔗 Link for {groups[index][1]}:\n{link}")
    except Exception as e: await update.message.reply_text(f"❌ Error: {e}")

async def gmsg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    if not await is_user_admin(update, context):
        msg = await update.message.reply_text("❌ you are not bot owner")
        asyncio.create_task(delete_after_delay(msg, 10))
        return
    
    if update.effective_user.id not in ADMIN_IDS or len(context.args) < 2: return
    try:
        index = int(context.args[0]) - 1
        groups = db.get_groups()
        await context.bot.send_message(chat_id=groups[index][0], text=" ".join(context.args[1:]))
        await update.message.reply_text("✅ Sent.")
    except Exception as e: await update.message.reply_text(f"❌ Error: {e}")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    if not await is_user_admin(update, context):
        msg = await update.message.reply_text("❌ you are not bot owner")
        asyncio.create_task(delete_after_delay(msg, 10))
        return
    
    if update.effective_user.id not in ADMIN_IDS or not update.message.reply_to_message: return
    msg = update.message.reply_to_message
    targets = db.get_all_targets()
    for tid in targets:
        try:
            await context.bot.copy_message(chat_id=tid, from_chat_id=msg.chat_id, message_id=msg.message_id)
            await asyncio.sleep(0.05)
        except: pass
    await update.message.reply_text("📢 Broadcast Done.")

def main():
    app = Application.builder().token(TOKEN).build()

    # --- ADD THIS LINE RIGHT HERE ---
    app.add_handler(TypeHandler(Update, global_bot_admin_check), group=-1)
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("unapprove", unapprove_command))
    app.add_handler(CommandHandler("aplist", aplist_command))
    app.add_handler(CommandHandler("grouplist", grouplist_command))
    app.add_handler(CommandHandler("getlink", getlink_command))
    app.add_handler(CommandHandler("gmsg", gmsg_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler((~filters.COMMAND), message_handler))
   
    app.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.CHAT_MEMBER))

    print("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    print("Script started...")
    keep_alive()  # <--- Start the web server FIRST
    main()
