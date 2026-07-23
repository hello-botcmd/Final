#!/usr/bin/env python3
"""
Telegram Account Manager Bot — Shadow Mode V99
Complete system: add accounts (OTP/session/tdata), smart join (3 modes),
view boost, reactions, online management, auto-profile update.
"""

import asyncio
import logging
import random
import re
import os
import json
import time
import zipfile
import io
import math
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict, Any
from pathlib import Path

# ── Telethon ──
from telethon import TelegramClient, functions, types, errors
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, FloodWaitError, RPCError
)
from telethon.tl.functions.account import UpdateStatusRequest

# ── MongoDB (Motor — async) ──
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.server_api import ServerApi

# ── python-telegram-bot v20+ ──
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputFile, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode

# ── Config ──
import config

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────
# CONSTANTS & STATE
# ───────────────────────────────────────────────

# Conversation states
(PHONE, OTP, TFA, SESSION_FILE, TDATA,
 JOIN_LINK, JOIN_TIME_MIN, JOIN_TIME_MAX,
 VIEW_LINK, VIEW_COUNT,
 REACT_LINK, REACT_TYPES, REACT_COUNT,
 CONFIRM) = range(14)

# Cancel command
CANCEL = "🚫 Cancel"

# Reaction emoji mapping
REACTIONS_MAP = {
    '❤️': '❤',
    '🥰': '🥰',
    '😊': '😊',
    '☺️': '☺',
    '🔥': '🔥',
    '👍': '👍',
    '👎': '👎',
    '😁': '😁',
    '😂': '😂',
    '😮': '😮',
    '😢': '😢',
    '😡': '😡',
    '💯': '💯',
    '🎉': '🎉',
    '💔': '💔',
    '🤡': '🤡',
    '😈': '😈',
    '💩': '💩',
    '⚡': '⚡',
    '🦀': '🦀',
}

# Active operations tracker for /cancel
active_operations: Dict[int, asyncio.Task] = {}
cancel_flags: Dict[int, bool] = {}

# Active Telethon clients cache
active_clients: Dict[str, TelegramClient] = {}

# ───────────────────────────────────────────────
# MONGODB — MOTOR
# ───────────────────────────────────────────────

class Database:
    """Async MongoDB handler using Motor."""

    def __init__(self):
        self.client = None
        self.db = None
        self.accounts = None
        self.settings = None

    async def connect(self):
        """Initialize MongoDB connection."""
        try:
            self.client = AsyncIOMotorClient(
                config.MONGO_URI,
                server_api=ServerApi('1'),
                serverSelectionTimeoutMS=5000
            )
            await self.client.admin.command('ping')
            self.db = self.client[config.DB_NAME]
            self.accounts = self.db['accounts']
            self.settings = self.db['settings']
            logger.info("✅ MongoDB connected successfully")
            return True
        except Exception as e:
            logger.error(f"❌ MongoDB connection failed: {e}")
            return False

    async def add_account(self, phone: str, session_string: str, 
                          name: str = "") -> Dict:
        """Add an account. Returns the doc. Prevents duplicates."""
        existing = await self.accounts.find_one({"phone": phone})
        if existing:
            # Update session string if it changed
            await self.accounts.update_one(
                {"phone": phone},
                {"$set": {
                    "session_string": session_string,
                    "name": name or existing.get("name", ""),
                    "added_at": datetime.utcnow(),
                    "status": "connected"
                }}
            )
            doc = await self.accounts.find_one({"phone": phone})
            return doc

        doc = {
            "phone": phone,
            "session_string": session_string,
            "name": name or phone,
            "status": "connected",
            "online": False,
            "join_mode": None,
            "added_at": datetime.utcnow(),
            "last_used": None
        }
        await self.accounts.insert_one(doc)
        return doc

    async def get_account(self, phone: str) -> Optional[Dict]:
        return await self.accounts.find_one({"phone": phone})

    async def get_all_accounts(self) -> List[Dict]:
        cursor = self.accounts.find({})
        return await cursor.to_list(length=None)

    async def get_connected_accounts(self) -> List[Dict]:
        cursor = self.accounts.find({"status": "connected"})
        return await cursor.to_list(length=None)

    async def update_account(self, phone: str, update_data: Dict):
        await self.accounts.update_one({"phone": phone}, {"$set": update_data})

    async def remove_account(self, phone: str):
        await self.accounts.delete_one({"phone": phone})

    async def set_account_offline(self, phone: str):
        await self.accounts.update_one(
            {"phone": phone},
            {"$set": {"online": False}}
        )

    async def get_stats(self) -> Dict:
        total = await self.accounts.count_documents({})
        connected = await self.accounts.count_documents({"status": "connected"})
        disconnected = await self.accounts.count_documents({"status": "disconnected"})
        return {"total": total, "connected": connected, "disconnected": disconnected}

    async def clear_all(self):
        await self.accounts.delete_many({})

    async def set_name_list(self, names: List[str]):
        await self.settings.update_one(
            {"key": "name_list"},
            {"$set": {"value": names}},
            upsert=True
        )

    async def get_name_list(self) -> List[str]:
        doc = await self.settings.find_one({"key": "name_list"})
        return doc.get("value", []) if doc else []

db = Database()


# ───────────────────────────────────────────────
# UTILITY FUNCTIONS
# ───────────────────────────────────────────────

def extract_phone_from_session(text: str) -> Optional[str]:
    """Extract phone from session text format."""
    match = re.search(r'Phone:\s*(\+?\d+)', text)
    return match.group(1) if match else None

def extract_session_string(text: str) -> Optional[str]:
    """Extract Telethon session string from the format."""
    lines = text.strip().split('\n')
    for line in lines:
        line = line.strip()
        # Skip header lines
        if line.startswith('Phone:') or line.startswith('Format:'):
            continue
        if len(line) > 50:  # Session strings are long base64
            return line
    return None

def parse_session_file(content: str) -> List[Tuple[str, str]]:
    """
    Parse session file with multiple accounts.
    Format:
    Phone: +xxx | Format: TELETHON
    session_string_here
    
    Returns list of (phone, session_string)
    """
    accounts = []
    blocks = content.strip().split('\n\n')
    
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        
        phone = extract_phone_from_session(block)
        session_str = extract_session_string(block)
        
        if phone and session_str:
            accounts.append((phone, session_str))
    
    return accounts

async def get_telethon_client(session_string: str) -> TelegramClient:
    """Create a Telethon client from session string."""
    client = TelegramClient(
        StringSession(session_string),
        config.API_ID,
        config.API_HASH,
        connection_retries=5,
        device_model="iPhone 15 Pro Max",
        system_version="4.16.30-vx-CUSTOM",
        app_version="10.12.0",
        lang_code="en",
        system_lang_code="en"
    )
    return client

async def set_online(client: TelegramClient) -> bool:
    """Set account as online permanently."""
    try:
        await client(functions.account.UpdateStatusRequest(offline=False))
        return True
    except Exception as e:
        logger.error(f"Failed to set online: {e}")
        return False

async def set_offline(client: TelegramClient) -> bool:
    """Set account as offline."""
    try:
        await client(functions.account.UpdateStatusRequest(offline=True))
        return True
    except Exception as e:
        logger.error(f"Failed to set offline: {e}")
        return False

async def update_profile_name(client: TelegramClient, name: str):
    """Update account first/last name."""
    try:
        parts = name.split(' ', 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ''
        await client(functions.account.UpdateProfileRequest(
            first_name=first_name,
            last_name=last_name
        ))
        return True
    except Exception as e:
        logger.error(f"Failed to update profile name: {e}")
        return False

def format_number(num: int) -> str:
    """Format number with commas."""
    return f"{num:,}"

def is_admin(user_id: int) -> bool:
    """Check if user is owner or admin."""
    return user_id == config.OWNER_ID or user_id in config.ADMIN_IDS

async def is_operation_cancelled(user_id: int) -> bool:
    """Check if current operation should be cancelled."""
    return cancel_flags.get(user_id, False)

def premium_text(text: str) -> str:
    """Wrap text in premium formatting."""
    return f"✨ {text}"

# ───────────────────────────────────────────────
# KEYBOARDS
# ───────────────────────────────────────────────

def main_keyboard():
    """Main menu keyboard."""
    buttons = [
        [InlineKeyboardButton("➕ Add Account", callback_data="add_account")],
        [InlineKeyboardButton("📥 Join Channel/Group", callback_data="join")],
        [InlineKeyboardButton("👁 View Boost", callback_data="view_boost")],
        [InlineKeyboardButton("💜 Reaction", callback_data="reaction")],
        [InlineKeyboardButton("🟢 All Accounts Online", callback_data="all_online")],
        [InlineKeyboardButton("📊 Total Accounts", callback_data="total_accounts")],
    ]
    return InlineKeyboardMarkup(buttons)

def add_account_keyboard():
    """Add account method keyboard."""
    buttons = [
        [InlineKeyboardButton("📱 Phone + OTP + 2FA", callback_data="add_phone")],
        [InlineKeyboardButton("📄 Upload Session File", callback_data="add_session_file")],
        [InlineKeyboardButton("📁 Upload TData (ZIP)", callback_data="add_tdata")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ]
    return InlineKeyboardMarkup(buttons)

def cancel_keyboard():
    buttons = [[InlineKeyboardButton(CANCEL, callback_data="cancel_op")]]
    return InlineKeyboardMarkup(buttons)

def reaction_keyboard():
    """Reaction type selection."""
    row1 = [InlineKeyboardButton(r, callback_data=f"react_{r}") for r in ['❤️', '🥰', '😊', '☺️']]
    row2 = [InlineKeyboardButton(r, callback_data=f"react_{r}") for r in ['🔥', '👍', '😂', '😮']]
    row3 = [InlineKeyboardButton(r, callback_data=f"react_{r}") for r in ['🎉', '💔', '😈', '⚡']]
    row4 = [InlineKeyboardButton("🎲 Mix Random", callback_data="react_mix"),
            InlineKeyboardButton("✅ Done Selecting", callback_data="react_done")]
    row5 = [InlineKeyboardButton(CANCEL, callback_data="cancel_op")]
    return InlineKeyboardMarkup([row1, row2, row3, row4, row5])


print("✅ Part 1 loaded — Database & Core ready")# ───────────────────────────────────────────────
# ACCOUNT MANAGEMENT — ADD VIA PHONE + OTP + 2FA
# ───────────────────────────────────────────────

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the phone+OTP flow."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
        return ConversationHandler.END
    
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        premium_text("📱 **Add Account via Phone**\n\n"
                     "Please send the phone number in international format.\n"
                     "Example: `+917248843065`\n\n"
                     "Use /cancel to abort."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_keyboard()
    )
    return PHONE


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive phone number and send OTP."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    phone = update.message.text.strip()
    cancel_flags[user_id] = False
    
    # Validate phone
    if not re.match(r'^\+?\d{7,15}$', phone):
        await update.message.reply_text(
            "❌ **Invalid phone number.** Use international format like `+917248843065`",
            parse_mode=ParseMode.MARKDOWN
        )
        return PHONE
    
    context.user_data['add_phone'] = phone
    
    msg = await update.message.reply_text(
        premium_text(f"⏳ Sending OTP to `{phone}`..."),
        parse_mode=ParseMode.MARKDOWN
    )
    
    try:
        client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
        await client.connect()
        
        sent = await client.send_code_request(phone)
        context.user_data['add_client'] = client
        context.user_data['add_phone_code_hash'] = sent.phone_code_hash
        
        await msg.edit_text(
            premium_text(f"✅ OTP sent to `{phone}`\n\n"
                         "Please send the **OTP code** you received.\n"
                         "Send only the numbers (e.g., `12345`)\n\n"
                         "Use /cancel to abort."),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=cancel_keyboard()
        )
        return OTP
        
    except FloodWaitError as e:
        await msg.edit_text(f"⏳ Flood wait: {e.seconds} seconds. Try again later.")
        return ConversationHandler.END
    except Exception as e:
        await msg.edit_text(f"❌ Error sending OTP: {str(e)[:200]}")
        return ConversationHandler.END


async def receive_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive OTP code and verify."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    if await is_operation_cancelled(user_id):
        cancel_flags[user_id] = False
        return ConversationHandler.END
    
    code = update.message.text.strip()
    client = context.user_data.get('add_client')
    phone = context.user_data.get('add_phone')
    phone_code_hash = context.user_data.get('add_phone_code_hash')
    
    if not client or not phone:
        await update.message.reply_text("❌ Session expired. Start again with /start")
        return ConversationHandler.END
    
    msg = await update.message.reply_text(premium_text("⏳ Verifying OTP..."))
    
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        # Success - no 2FA
        return await finalize_phone_login(update, context, client, phone, msg)
        
    except SessionPasswordNeededError:
        # 2FA required
        await msg.edit_text(
            premium_text("🔐 **2FA Password Required**\n\n"
                         "This account has two-factor authentication enabled.\n"
                         "Please send your **2FA password**.\n\n"
                         "Use /cancel to abort."),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=cancel_keyboard()
        )
        return TFA
        
    except PhoneCodeInvalidError:
        await msg.edit_text("❌ **Invalid OTP code.** Please try again.\n\n"
                            "Send the correct code or use /cancel",
                            parse_mode=ParseMode.MARKDOWN)
        return OTP
        
    except PhoneCodeExpiredError:
        await msg.edit_text("❌ **OTP expired.** Please start again with /start")
        return ConversationHandler.END
        
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")
        return ConversationHandler.END


async def receive_tfa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive 2FA password."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    if await is_operation_cancelled(user_id):
        cancel_flags[user_id] = False
        return ConversationHandler.END
    
    password = update.message.text.strip()
    client = context.user_data.get('add_client')
    phone = context.user_data.get('add_phone')
    
    if not client or not phone:
        await update.message.reply_text("❌ Session expired. Start again with /start")
        return ConversationHandler.END
    
    msg = await update.message.reply_text(premium_text("⏳ Verifying 2FA password..."))
    
    try:
        await client.sign_in(password=password)
        return await finalize_phone_login(update, context, client, phone, msg)
    except errors.PasswordHashInvalidError:
        await msg.edit_text("❌ **Invalid 2FA password.** Try again.\n\n"
                            "Use /cancel to abort.",
                            parse_mode=ParseMode.MARKDOWN)
        return TFA
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")
        return ConversationHandler.END


async def finalize_phone_login(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                client: TelegramClient, phone: str, msg):
    """Finalize login - save session, update profile."""
    # Get session string
    session_string = client.session.save()
    
    # Get user info
    me = await client.get_me()
    
    # Auto-name from name.txt
    names = await db.get_name_list()
    if names:
        # Assign a random name from list
        name = random.choice(names)
        await update_profile_name(client, name)
    else:
        name = f"{me.first_name or ''} {me.last_name or ''}".strip() or phone
    
    # Save to DB
    await db.add_account(phone, session_string, name)
    
    # Also save session to file for backup
    os.makedirs("sessions", exist_ok=True)
    safe_phone = phone.replace('+', '')
    with open(f"sessions/{safe_phone}.session", "w") as f:
        f.write(f"Phone: {phone} | Format: TELETHON\n{session_string}")
    
    # Close client
    await client.disconnect()
    
    await msg.edit_text(
        premium_text(f"✅ **Account Added Successfully!**\n\n"
                     f"📱 **Phone:** `{phone}`\n"
                     f"👤 **Name:** {name}\n"
                     f"🆔 **User ID:** {me.id}\n"
                     f"📊 **Session saved to DB**"),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard()
    )
    
    # Cleanup
    context.user_data.pop('add_client', None)
    context.user_data.pop('add_phone', None)
    context.user_data.pop('add_phone_code_hash', None)
    
    return ConversationHandler.END


# ───────────────────────────────────────────────
# ACCOUNT MANAGEMENT — ADD VIA SESSION FILE
# ───────────────────────────────────────────────

async def ask_session_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask user to upload session file."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
        return ConversationHandler.END
    
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        premium_text("📄 **Upload Session File**\n\n"
                     "Send a **.txt** file containing session strings.\n\n"
                     "Format per account:\n"
                     "```\n"
                     "Phone: +917248843065 | Format: TELETHON\n"
                     "1BSABCyjyP_AF...your_session_string_here...\n\n"
                     "Phone: +917248843066 | Format: TELETHON\n"
                     "1BSABCyjyP_AF...next_session_string_here...\n"
                     "```\n\n"
                     "Multiple accounts supported in one file.\n"
                     "Use /cancel to abort."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_keyboard()
    )
    return SESSION_FILE


async def receive_session_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive and parse session file."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    if await is_operation_cancelled(user_id):
        cancel_flags[user_id] = False
        return ConversationHandler.END
    
    document = update.message.document
    if not document:
        await update.message.reply_text("❌ Please send a **.txt** file.",
                                        parse_mode=ParseMode.MARKDOWN)
        return SESSION_FILE
    
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Please send a **.txt** file.",
                                        parse_mode=ParseMode.MARKDOWN)
        return SESSION_FILE
    
    msg = await update.message.reply_text(premium_text("⏳ Parsing session file..."))
    
    try:
        file = await document.get_file()
        content_bytes = await file.download_as_bytearray()
        content = content_bytes.decode('utf-8')
        
        accounts = parse_session_file(content)
        
        if not accounts:
            await msg.edit_text("❌ No valid sessions found in file.\n"
                                "Check the format and try again.")
            return ConversationHandler.END
        
        # Process each account
        success = 0
        failed = 0
        results = []
        
        names = await db.get_name_list()
        
        for phone, session_str in accounts:
            try:
                # Verify session is valid
                client = await get_telethon_client(session_str)
                await client.connect()
                
                if not await client.is_user_authorized():
                    results.append(f"❌ `{phone}` — Session not authorized")
                    failed += 1
                    await client.disconnect()
                    continue
                
                me = await client.get_me()
                
                # Assign name
                if names:
                    name = random.choice(names)
                    await update_profile_name(client, name)
                else:
                    name = f"{me.first_name or ''} {me.last_name or ''}".strip() or phone
                
                # Save to DB
                await db.add_account(phone, session_str, name)
                
                # Save session file
                os.makedirs("sessions", exist_ok=True)
                safe_phone = phone.replace('+', '')
                with open(f"sessions/{safe_phone}.session", "w") as f:
                    f.write(f"Phone: {phone} | Format: TELETHON\n{session_str}")
                
                results.append(f"✅ `{phone}` — Added as {name}")
                success += 1
                
                await client.disconnect()
                
            except Exception as e:
                results.append(f"❌ `{phone}` — {str(e)[:80]}")
                failed += 1
        
        # Summary
        summary = (
            premium_text(f"📊 **Session File Import Complete**\n\n"
                         f"✅ **Successfully added:** {success}\n"
                         f"❌ **Failed:** {failed}\n"
                         f"📁 **Total in file:** {len(accounts)}\n\n")
        )
        
        # Show last 10 results
        if len(results) > 10:
            summary += "**Recent results:**\n" + "\n".join(results[-10:])
            summary += f"\n... and {len(results) - 10} more"
        else:
            summary += "\n".join(results)
        
        await msg.edit_text(summary, parse_mode=ParseMode.MARKDOWN,
                            reply_markup=main_keyboard())
        
    except Exception as e:
        await msg.edit_text(f"❌ Error processing file: {str(e)[:200]}")
    
    return ConversationHandler.END


# ───────────────────────────────────────────────
# ACCOUNT MANAGEMENT — ADD VIA TDATA
# ───────────────────────────────────────────────

async def ask_tdata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask user to upload tdata zip."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
        return ConversationHandler.END
    
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        premium_text("📁 **Upload TData Folder (ZIP)**\n\n"
                     "Send a **.zip** file containing the `tdata` folder.\n\n"
                     "The bot will extract accounts from the Telegram Desktop "
                     "session data.\n\n"
                     "⚠️ Only ZIP format supported.\n"
                     "Use /cancel to abort."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_keyboard()
    )
    return TDATA


async def receive_tdata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive tdata zip and extract accounts."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    if await is_operation_cancelled(user_id):
        cancel_flags[user_id] = False
        return ConversationHandler.END
    
    document = update.message.document
    if not document:
        await update.message.reply_text("❌ Please send a **.zip** file.")
        return TDATA
    
    if not document.file_name.endswith('.zip'):
        await update.message.reply_text("❌ Please send a **.zip** file.")
        return TDATA
    
    msg = await update.message.reply_text(premium_text("⏳ Extracting TData..."))
    
    try:
        file = await document.get_file()
        content_bytes = await file.download_as_bytearray()
        
        # Extract zip to temp directory
        import tempfile
        import shutil
        
        temp_dir = tempfile.mkdtemp()
        
        with zipfile.ZipFile(io.BytesIO(content_bytes)) as zf:
            zf.extractall(temp_dir)
        
        # Look for tdata folder
        tdata_path = None
        for root, dirs, files in os.walk(temp_dir):
            if os.path.basename(root) == "tdata" or "tdata" in dirs:
                tdata_path = os.path.join(root, "tdata") if "tdata" in dirs else root
        
        if not tdata_path:
            # Check if the zip contains a folder with key_datas
            for root, dirs, files in os.walk(temp_dir):
                for f in files:
                    if f.endswith('.s') or 'key' in f.lower():
                        tdata_path = root
                        break
        
        if not tdata_path:
            shutil.rmtree(temp_dir)
            await msg.edit_text("❌ Could not find valid tdata structure in the ZIP.")
            return ConversationHandler.END
        
        # Use Telethon's TDesktopAuth to extract session
        try:
            from telethon.tl.custom import TDesktopAuth
            
            auth = TDesktopAuth(tdata_path)
            accounts_data = auth.get_accounts()
            
            if not accounts_data:
                shutil.rmtree(temp_dir)
                await msg.edit_text("❌ No accounts found in tdata.")
                return ConversationHandler.END
            
            success = 0
            failed = 0
            results = []
            names = await db.get_name_list()
            
            for acc_data in accounts_data:
                phone = getattr(acc_data, 'phone', None) or f"unknown_{random.randint(1000,9999)}"
                
                try:
                    # Create client with tdata auth
                    client = TelegramClient(
                        StringSession(),
                        config.API_ID,
                        config.API_HASH
                    )
                    # Apply tdata authorization
                    await auth.apply_to_client(client)
                    await client.connect()
                    
                    if not await client.is_user_authorized():
                        results.append(f"❌ `{phone}` — Not authorized")
                        failed += 1
                        await client.disconnect()
                        continue
                    
                    me = await client.get_me()
                    session_string = client.session.save()
                    
                    # Assign name
                    if names:
                        name = random.choice(names)
                        await update_profile_name(client, name)
                    else:
                        name = f"{me.first_name or ''} {me.last_name or ''}".strip() or phone
                    
                    # Save to DB
                    actual_phone = getattr(me, 'phone', None) or phone
                    await db.add_account(actual_phone, session_string, name)
                    
                    results.append(f"✅ `{actual_phone}` — Added as {name}")
                    success += 1
                    
                    await client.disconnect()
                    
                except Exception as e:
                    results.append(f"❌ `{phone}` — {str(e)[:80]}")
                    failed += 1
            
            # Cleanup
            shutil.rmtree(temp_dir)
            
            summary = (
                premium_text(f"📊 **TData Import Complete**\n\n"
                             f"✅ **Added:** {success}\n"
                             f"❌ **Failed:** {failed}\n\n")
            )
            
            if len(results) > 10:
                summary += "**Recent:**\n" + "\n".join(results[-10:])
            else:
                summary += "\n".join(results)
            
            await msg.edit_text(summary, parse_mode=ParseMode.MARKDOWN,
                                reply_markup=main_keyboard())
            
        except ImportError:
            shutil.rmtree(temp_dir)
            # Fallback: try to manually parse key_datas
            await msg.edit_text(
                "⚠️ TDesktopAuth not available in this Telethon version.\n\n"
                "**Alternative:** Convert your tdata to session strings using "
                "an external tool like `TDesktopToTelethon` and import via "
                "Session File method instead.\n\n"
                "The **Session File** method is fully supported.",
                reply_markup=main_keyboard()
            )
        
    except Exception as e:
        await msg.edit_text(f"❌ TData error: {str(e)[:200]}",
                            reply_markup=main_keyboard())
        # Cleanup temp if exists
        try:
            shutil.rmtree(temp_dir)
        except:
            pass
    
    return ConversationHandler.END


print("✅ Part 2 loaded — Account Management ready")

# ───────────────────────────────────────────────
# JOIN OPERATIONS — SMART JOIN WITH 3 MODES
# ───────────────────────────────────────────────

async def ask_join_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start join flow — ask for channel/group link."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
        return ConversationHandler.END
    
    query = update.callback_query
    await query.answer()
    
    # Check if we have accounts
    accounts = await db.get_connected_accounts()
    if len(accounts) < 3:
        await query.edit_message_text(
            "❌ **Need at least 3 connected accounts** to use join distribution.\n"
            f"Current connected: {len(accounts)}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard()
        )
        return ConversationHandler.END
    
    await query.edit_message_text(
        premium_text("📥 **Join Channel/Group**\n\n"
                     "Send the **link** to join.\n\n"
                     "Support:\n"
                     "• Public: `@username` or `https://t.me/username`\n"
                     "• Private: `https://t.me/+invite_hash`\n\n"
                     "Use /cancel to abort."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_keyboard()
    )
    return JOIN_LINK


async def receive_join_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive join link — ask for time range."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    if await is_operation_cancelled(user_id):
        cancel_flags[user_id] = False
        return ConversationHandler.END
    
    link = update.message.text.strip()
    context.user_data['join_link'] = link
    
    msg = await update.message.reply_text(
        premium_text("✅ Link received!\n\n"
                     "⏱ **Set join delay range (seconds)**\n\n"
                     "Send **two numbers** separated by space/comma.\n"
                     "Example: `8 10` or `8,10`\n\n"
                     "Accounts will join with **randomized delays** "
                     "between these values.\n\n"
                     "Use /cancel to abort."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_keyboard()
    )
    return JOIN_TIME_MIN


async def receive_join_time_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive time range and show distribution preview."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    if await is_operation_cancelled(user_id):
        cancel_flags[user_id] = False
        return ConversationHandler.END
    
    text = update.message.text.strip()
    
    # Parse numbers
    nums = re.findall(r'\d+', text)
    if len(nums) < 2:
        await update.message.reply_text(
            "❌ Please send **two numbers** like: `8 10` or `8,10`",
            parse_mode=ParseMode.MARKDOWN
        )
        return JOIN_TIME_MIN
    
    min_delay = int(nums[0])
    max_delay = int(nums[1])
    
    if min_delay < 2 or max_delay < min_delay:
        await update.message.reply_text(
            "❌ Minimum delay is 2 seconds. Max must be >= min.\n"
            "Example: `8 10`"
        )
        return JOIN_TIME_MIN
    
    context.user_data['join_min_delay'] = min_delay
    context.user_data['join_max_delay'] = max_delay
    
    # Get accounts and calculate distribution
    accounts = await db.get_connected_accounts()
    random.shuffle(accounts)  # Randomize order
    
    total = len(accounts)
    
    # Distribution: 20% always online, 40% offline after 2min, 40% last seen recently
    online_count = max(1, round(total * 0.2))
    offline_after_count = max(1, round(total * 0.4))
    last_seen_count = total - online_count - offline_after_count
    
    if last_seen_count < 0:
        last_seen_count = 0
        offline_after_count = total - online_count
    
    # Assign modes
    join_modes = (
        ["always_online"] * online_count +
        ["offline_2min"] * offline_after_count +
        ["last_seen_recently"] * last_seen_count
    )
    random.shuffle(join_modes)
    
    context.user_data['join_accounts'] = accounts
    context.user_data['join_modes'] = join_modes
    
    # Build preview
    preview = (
        premium_text("📊 **Join Distribution Preview**\n\n"
                     f"📍 **Target:** `{context.user_data['join_link']}`\n"
                     f"⏱ **Delay Range:** {min_delay}s – {max_delay}s\n"
                     f"👥 **Total Accounts:** {total}\n\n"
                     "**Distribution:**\n"
                     f"🟢 **Always Online:** {online_count} accounts\n"
                     f"     → 20% — Join & stay permanently online\n"
                     f"🟡 **Online 2min then Offline:** {offline_after_count} accounts\n"
                     f"     → 40% — Join, stay 2min, show 'last seen recently'\n"
                     f"🔵 **Last Seen Recently:** {last_seen_count} accounts\n"
                     f"     → 40% — Join & show 'last seen recently' instantly\n\n"
                     "**Join Order:** Fully randomized\n\n"
                     "Ready to execute?")
    )
    
    buttons = [
        [InlineKeyboardButton("🚀 Start Joining", callback_data="join_confirm")],
        [InlineKeyboardButton(CANCEL, callback_data="cancel_op")]
    ]
    
    await update.message.reply_text(
        preview,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return CONFIRM


async def execute_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute the join operation with all 3 modes."""
    user_id = update.effective_user.id
    
    query = update.callback_query
    await query.answer()
    
    link = context.user_data['join_link']
    accounts = context.user_data['join_accounts']
    join_modes = context.user_data['join_modes']
    min_delay = context.user_data['join_min_delay']
    max_delay = context.user_data['join_max_delay']
    
    cancel_flags[user_id] = False
    
    # Resolve the link
    await query.edit_message_text(
        premium_text("🔍 Resolving target link..."),
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Test resolve with first account
    try:
        test_client = await get_telethon_client(accounts[0]['session_string'])
        await test_client.connect()
        
        entity = None
        if link.startswith('https://t.me/+') or link.startswith('https://t.me/joinchat/'):
            # Private invite link
            try:
                entity = await test_client( functions.messages.CheckChatInviteRequest(
                    hash=link.split('+')[-1].split('/')[-1]
                ))
                # We'll use the link directly for joining
                invite_hash = link.split('+')[-1].split('/')[-1]
                context.user_data['invite_hash'] = invite_hash
                context.user_data['is_private'] = True
            except Exception as e:
                await query.edit_message_text(
                    f"❌ Cannot resolve invite link: {str(e)[:100]}",
                    reply_markup=main_keyboard()
                )
                await test_client.disconnect()
                return ConversationHandler.END
        else:
            # Public channel/group
            username = link.replace('https://t.me/', '').replace('@', '').split('/')[0]
            try:
                entity = await test_client.get_entity(username)
                context.user_data['entity_username'] = username
                context.user_data['is_private'] = False
            except Exception as e:
                await query.edit_message_text(
                    f"❌ Cannot find entity: {str(e)[:100]}",
                    reply_markup=main_keyboard()
                )
                await test_client.disconnect()
                return ConversationHandler.END
        
        context.user_data['entity'] = entity
        await test_client.disconnect()
        
    except Exception as e:
        await query.edit_message_text(
            f"❌ Resolution error: {str(e)[:150]}",
            reply_markup=main_keyboard()
        )
        return ConversationHandler.END
    
    # Start execution
    status_msg = await query.edit_message_text(
        premium_text("🚀 **Join Operation Started**\n\n"
                     "```\n"
                     "Status: Initializing...\n"
                     "```"),
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Run join in background
    task = asyncio.create_task(
        run_join_operation(user_id, context, status_msg)
    )
    active_operations[user_id] = task
    
    return ConversationHandler.END


async def run_join_operation(user_id: int, context: ContextTypes.DEFAULT_TYPE,
                              status_msg):
    """Background task for join operation."""
    try:
        accounts = context.user_data['join_accounts']
        join_modes = context.user_data['join_modes']
        min_delay = context.user_data['join_min_delay']
        max_delay = context.user_data['join_max_delay']
        link = context.user_data['join_link']
        is_private = context.user_data.get('is_private', False)
        invite_hash = context.user_data.get('invite_hash', '')
        entity_username = context.user_data.get('entity_username', '')
        
        total = len(accounts)
        joined = 0
        failed = 0
        results = []
        
        def update_status():
            """Update the status message."""
            progress = "█" * int((joined + failed) / total * 20) if total > 0 else ""
            progress += "░" * (20 - len(progress))
            pct = int((joined + failed) / total * 100) if total > 0 else 0
            
            text = (
                premium_text("🚀 **Join Operation Running**\n\n"
                             f"```\n"
                             f"Target: {link}\n"
                             f"Progress: [{progress}] {pct}%\n"
                             f"Joined: {joined} | Failed: {failed} | Total: {total}\n"
                             f"```")
            )
            return text
        
        for i, (account, mode) in enumerate(zip(accounts, join_modes)):
            if await is_operation_cancelled(user_id):
                results.append("⛔ Operation cancelled by user")
                break
            
            phone = account['phone']
            session_str = account['session_string']
            
            try:
                client = await get_telethon_client(session_str)
                await client.connect()
                
                if not await client.is_user_authorized():
                    results.append(f"❌ `{phone}` — Session invalid")
                    failed += 1
                    await client.disconnect()
                    continue
                
                me = await client.get_me()
                # --- STEP 1: Come Online & Update Profile ---
                
                # Update profile name from name.txt
                names = await db.get_name_list()
                if names:
                    name = random.choice(names)
                    await update_profile_name(client, name)
                
                # Set online
                await set_online(client)
                
                # --- STEP 2: Join the channel/group ---
                try:
                    if is_private:
                        result = await client(functions.messages.ImportChatInviteRequest(
                            hash=invite_hash
                        ))
                    else:
                        entity = await client.get_entity(entity_username)
                        if hasattr(entity, 'megagroup') and entity.megagroup:
                            await client(functions.channels.JoinChannelRequest(
                                channel=entity
                            ))
                        else:
                            await client(functions.channels.JoinChannelRequest(
                                channel=entity
                            ))
                    
                    joined += 1
                    
                    # --- STEP 3: Apply mode-specific behavior ---
                    if mode == "always_online":
                        # Keep online continuously
                        set_online_task = asyncio.create_task(
                            keep_online_loop(client, user_id)
                        )
                        # Store task for cleanup
                        if 'online_tasks' not in context.user_data:
                            context.user_data['online_tasks'] = []
                        context.user_data['online_tasks'].append(set_online_task)
                        
                        results.append(f"🟢 `{phone}` — Joined & set **Always Online**")
                        
                    elif mode == "offline_2min":
                        # Stay online for 2 minutes, then go offline
                        await asyncio.sleep(120)  # 2 minutes
                        await set_offline(client)
                        # Set last seen recently by going online briefly
                        await set_online(client)
                        await asyncio.sleep(1)
                        await set_offline(client)
                        
                        results.append(f"🟡 `{phone}` — Joined, stayed 2min, now **offline**")
                        
                    elif mode == "last_seen_recently":
                        # Go online briefly then offline to show "last seen recently"
                        await asyncio.sleep(5)
                        await set_offline(client)
                        # Brief online moment
                        await set_online(client)
                        await asyncio.sleep(2)
                        await set_offline(client)
                        
                        results.append(f"🔵 `{phone}` — Joined & shows **last seen recently**")
                    
                    await client.disconnect()
                    
                except errors.FloodWaitError as e:
                    results.append(f"⏳ `{phone}` — Flood wait {e.seconds}s")
                    failed += 1
                    await client.disconnect()
                    
                except errors.UserAlreadyParticipantError:
                    results.append(f"⚠️ `{phone}` — Already in the group/channel")
                    joined += 1  # Still count as success
                    await client.disconnect()
                    
                except Exception as e:
                    results.append(f"❌ `{phone}` — Join failed: {str(e)[:60]}")
                    failed += 1
                    await client.disconnect()
                
            except Exception as e:
                results.append(f"❌ `{phone}` — Client error: {str(e)[:60]}")
                failed += 1
            
            # Update status message
            try:
                await status_msg.edit_text(
                    update_status() + "\n**Last:** " + results[-1][:100] if results else "",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass
            
            # Delay before next join (randomized between min and max)
            if i < total - 1 and not await is_operation_cancelled(user_id):
                delay = random.randint(min_delay, max_delay)
                for s in range(delay):
                    if await is_operation_cancelled(user_id):
                        break
                    await asyncio.sleep(1)
        
        # Final summary
        final_text = (
            premium_text("✅ **Join Operation Complete**\n\n"
                         f"📍 **Target:** `{link}`\n"
                         f"✅ **Joined:** {joined}\n"
                         f"❌ **Failed:** {failed}\n"
                         f"📊 **Total Attempted:** {joined + failed}\n\n")
        )
        
        # Last 15 results
        show_results = results[-15:]
        for r in show_results:
            final_text += f"{r[:120]}\n"
        
        if len(results) > 15:
            final_text += f"\n... and {len(results) - 15} more"
        
        await status_msg.edit_text(
            final_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard()
        )
        
    except Exception as e:
        await status_msg.edit_text(
            f"❌ **Join operation crashed:** {str(e)[:200]}",
            reply_markup=main_keyboard()
        )
    
    finally:
        cancel_flags[user_id] = False
        if user_id in active_operations:
            del active_operations[user_id]


async def keep_online_loop(client: TelegramClient, user_id: int):
    """Keep an account online by periodically calling UpdateStatus."""
    try:
        while not await is_operation_cancelled(user_id):
            try:
                await client(functions.account.UpdateStatusRequest(offline=False))
            except:
                pass
            await asyncio.sleep(30)  # Check every 30 seconds
    except:
        pass


print("✅ Part 3 loaded — Join Operations ready")
# ───────────────────────────────────────────────
# VIEW BOOST — Post View Increaser
# ───────────────────────────────────────────────

async def ask_view_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start view boost — ask for post links."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
        return ConversationHandler.END
    
    query = update.callback_query
    await query.answer()
    
    accounts = await db.get_connected_accounts()
    if not accounts:
        await query.edit_message_text(
            "❌ **No connected accounts.** Add accounts first.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard()
        )
        return ConversationHandler.END
    
    await query.edit_message_text(
        premium_text("👁 **View Boost**\n\n"
                     "Send **post link(s)** to boost views.\n\n"
                     "Format:\n"
                     "• Single: `https://t.me/username/123`\n"
                     "• Multiple (one per line):\n"
                     "  `https://t.me/channel1/100`\n"
                     "  `https://t.me/channel2/200`\n\n"
                     "Use /cancel to abort."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_keyboard()
    )
    return VIEW_LINK


async def receive_view_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive post links and ask for view count."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    if await is_operation_cancelled(user_id):
        cancel_flags[user_id] = False
        return ConversationHandler.END
    
    text = update.message.text.strip()
    links = [l.strip() for l in text.split('\n') if l.strip()]
    
    # Validate links
    valid_links = []
    for link in links:
        if re.match(r'https?://t\.me/\w+/\d+', link):
            valid_links.append(link)
    
    if not valid_links:
        await update.message.reply_text(
            "❌ No valid post links found.\n"
            "Format: `https://t.me/username/123`",
            parse_mode=ParseMode.MARKDOWN
        )
        return VIEW_LINK
    
    context.user_data['view_links'] = valid_links
    
    await update.message.reply_text(
        premium_text(f"✅ **{len(valid_links)} post(s)** detected.\n\n"
                     "📊 **How many views per post?**\n\n"
                     "Send a number (e.g., `500`)\n"
                     "The bot will distribute views across all accounts.\n\n"
                     "Use /cancel to abort."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_keyboard()
    )
    return VIEW_COUNT


async def receive_view_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive view count and execute view boost."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    if await is_operation_cancelled(user_id):
        cancel_flags[user_id] = False
        return ConversationHandler.END
    
    text = update.message.text.strip()
    
    try:
        views_per_post = int(re.search(r'\d+', text).group())
        if views_per_post < 1:
            raise ValueError
    except:
        await update.message.reply_text("❌ Please send a valid **positive number**.")
        return VIEW_COUNT
    
    links = context.user_data['view_links']
    accounts = await db.get_connected_accounts()
    
    if len(accounts) < views_per_post:
        # Each account views once, so we need enough accounts
        # Actually for view boost, we use multiple sessions per account
    
    msg = await update.message.reply_text(
        premium_text(f"🚀 **Starting View Boost**\n\n"
                     f"📊 **{len(links)} post(s)** x **{views_per_post} views** each\n"
                     f"👥 **{len(accounts)} accounts available**\n\n"
                     f"⏳ Initializing..."),
        parse_mode=ParseMode.MARKDOWN
    )
    
    cancel_flags[user_id] = False
    
    # Run in background
    task = asyncio.create_task(
        run_view_boost(user_id, accounts, links, views_per_post, msg)
    )
    active_operations[user_id] = task
    
    return ConversationHandler.END


async def run_view_boost(user_id: int, accounts: List[Dict], 
                          links: List[str], views_per_post: int, status_msg):
    """Background view boost operation."""
    try:
        total_views = 0
        total_failed = 0
        
        # Shuffle accounts for randomness
        random.shuffle(accounts)
        
        # Calculate views per account
        total_views_needed = views_per_post * len(links)
        
        # Give each account an equal share
        views_per_account = max(1, total_views_needed // len(accounts))
        
        final_text = premium_text("✅ **View Boost Complete**\n\n")
        
        for link_idx, link in enumerate(links):
            if await is_operation_cancelled(user_id):
                final_text += "\n⛔ Cancelled"
                break
            
            # Parse channel and message ID from link
            match = re.match(r'https?://t\.me/(\w+)/(\d+)', link)
            if not match:
                final_text += f"❌ Invalid link: {link}\n"
                continue
            
            channel_username = match.group(1)
            message_id = int(match.group(2))
            
            # Cycle through accounts, each views once per post
            view_count = 0
            fail_count = 0
            
            for acc_idx, account in enumerate(accounts):
                if await is_operation_cancelled(user_id):
                    break
                
                if view_count >= views_per_post:
                    break
                
                phone = account['phone']
                session_str = account['session_string']
                
                try:
                    client = await get_telethon_client(session_str)
                    await client.connect()
                    
                    if not await client.is_user_authorized():
                        fail_count += 1
                        await client.disconnect()
                        continue
                    
                    # Get entity
                    try:
                        entity = await client.get_entity(channel_username)
                    except:
                        fail_count += 1
                        await client.disconnect()
                        continue
                    
                    # View the message (mark as read)
                    await client(functions.messages.ReadMentionsRequest(
                        peer=entity
                    ))
                    try:
                        await client(functions.messages.ReadHistoryRequest(
                            peer=entity,
                            max_id=message_id
                        ))
                    except:
                        pass
                    
                    # Actually view by getting messages
                    try:
                        await client.get_messages(entity, ids=message_id)
                    except:
                        pass
                    
                    view_count += 1
                    await client.disconnect()
                    
                    # Random delay between views
                    delay = random.uniform(2, 5)
                    await asyncio.sleep(delay)
                    
                except Exception as e:
                    fail_count += 1
                    logger.error(f"View failed for {phone}: {e}")
            
            total_views += view_count
            total_failed += fail_count
            
            final_text += f"📄 **Post {link_idx+1}:** {view_count} views ✅\n"
            
            # Update status
            try:
                await status_msg.edit_text(
                    premium_text(f"👁 **View Boosting...**\n\n"
                                 f"Post {link_idx+1}/{len(links)}\n"
                                 f"✅ Views: {view_count} | ❌ Failed: {fail_count}\n"
                                 f"Total: {total_views} views"),
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass
        
        final_text += f"\n📊 **Total:** {total_views} views delivered"
        if total_failed:
            final_text += f" | ❌ {total_failed} failed"
        
        await status_msg.edit_text(
            final_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard()
        )
        
    except Exception as e:
        await status_msg.edit_text(
            f"❌ View boost crashed: {str(e)[:200]}",
            reply_markup=main_keyboard()
        )
    finally:
        cancel_flags[user_id] = False
        if user_id in active_operations:
            del active_operations[user_id]


# ───────────────────────────────────────────────
# REACTION OPERATIONS
# ───────────────────────────────────────────────

async def ask_reaction_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start reaction flow — ask for post link."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
        return ConversationHandler.END
    
    query = update.callback_query
    await query.answer()
    
    accounts = await db.get_connected_accounts()
    if not accounts:
        await query.edit_message_text(
            "❌ **No connected accounts.** Add accounts first.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard()
        )
        return ConversationHandler.END
    
    await query.edit_message_text(
        premium_text("💜 **Post Reaction**\n\n"
                     "Send the **post link** to react to.\n\n"
                     "Format:\n"
                     "`https://t.me/username/123`\n\n"
                     "Supports both public and private channels/groups.\n\n"
                     "Use /cancel to abort."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_keyboard()
    )
    return REACT_LINK


async def receive_reaction_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive reaction link and ask for reaction types."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    if await is_operation_cancelled(user_id):
        cancel_flags[user_id] = False
        return ConversationHandler.END
    
    link = update.message.text.strip()
    
    if not re.match(r'https?://t\.me/\w+/\d+', link):
        await update.message.reply_text(
            "❌ Invalid link format. Use: `https://t.me/username/123`",
            parse_mode=ParseMode.MARKDOWN
        )
        return REACT_LINK
    
    context.user_data['react_link'] = link
    context.user_data['react_selected'] = []
    
    await update.message.reply_text(
        premium_text("✅ Link received!\n\n"
                     "💜 **Select reaction types**\n\n"
                     "Choose from the buttons below.\n"
                     "You can select **multiple** reactions (mix).\n"
                     "Click **Done** when finished."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reaction_keyboard()
    )
    return REACT_TYPES


async def handle_reaction_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reaction type button clicks."""
    user_id = update.effective_user.id
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "react_done":
        selected = context.user_data.get('react_selected', [])
        if not selected:
            await query.edit_message_text(
                "❌ **No reactions selected.**\nPlease select at least one.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reaction_keyboard()
            )
            return REACT_TYPES
        
        # Show selected and ask for count
        selected_str = " ".join(selected)
        await query.edit_message_text(
            premium_text(f"✅ **Selected Reactions:** {selected_str}\n\n"
                         "📊 **How many reactions?**\n\n"
                         "Send a number (e.g., `50`)\n"
                         f"Accounts available: {len(context.user_data.get('accounts_cache', []))}\n\n"
                         "Use /cancel to abort."),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=cancel_keyboard()
        )
        return REACT_COUNT
    
    elif data == "react_mix":
        context.user_data['react_selected'] = ['mix']
        await query.edit_message_text(
            premium_text("✅ **Mix Random Reactions** selected!\n\n"
                         "📊 **How many reactions?**\n\n"
                         "Send a number (e.g., `50`)\n\n"
                         "Use /cancel to abort."),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=cancel_keyboard()
        )
        return REACT_COUNT
    
    elif data == "cancel_op":
        cancel_flags[user_id] = True
        await query.edit_message_text(
            "⛔ Operation cancelled.",
            reply_markup=main_keyboard()
        )
        return ConversationHandler.END
    
    else:
        # Toggle reaction selection
        emoji = data.replace("react_", "")
        selected = context.user_data.get('react_selected', [])
        
        if emoji in selected:
            selected.remove(emoji)
        else:
            selected.append(emoji)
        
        context.user_data['react_selected'] = selected
        selected_str = " ".join(selected) if selected else "None selected"
        
        await query.edit_message_text(
            premium_text(f"💜 **Select Reactions**\n\n"
                         f"**Selected:** {selected_str}\n\n"
                         f"Choose more or click **Done**."),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reaction_keyboard()
        )
        return REACT_TYPES


async def receive_reaction_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive reaction count and execute."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    
    if await is_operation_cancelled(user_id):
        cancel_flags[user_id] = False
        return ConversationHandler.END
    
    text = update.message.text.strip()
    
    try:
        count = int(re.search(r'\d+', text).group())
        if count < 1:
            raise ValueError
    except:
        await update.message.reply_text("❌ Please send a valid **positive number**.")
        return REACT_COUNT
    
    accounts = await db.get_connected_accounts()
    context.user_data['accounts_cache'] = accounts
    
    link = context.user_data['react_link']
    reactions = context.user_data['react_selected']
    
    msg = await update.message.reply_text(
        premium_text(f"🚀 **Starting Reactions**\n\n"
                     f"📍 **Post:** `{link}`\n"
                     f"💜 **Reactions:** {' '.join(reactions)}\n"
                     f"📊 **Count:** {count}\n"
                     f"👥 **Accounts:** {len(accounts)}\n\n"
                     f"⏳ Initializing..."),
        parse_mode=ParseMode.MARKDOWN
    )
    
    cancel_flags[user_id] = False
    
    task = asyncio.create_task(
        run_reactions(user_id, accounts, link, reactions, count, msg)
    )
    active_operations[user_id] = task
    
    return ConversationHandler.END


async def run_reactions(user_id: int, accounts: List[Dict], link: str,
                         reactions: List[str], count: int, status_msg):
    """Background reaction operation."""
    try:
        # Parse link
        match = re.match(r'https?://t\.me/(\w+)/(\d+)', link)
        channel_username = match.group(1)
        message_id = int(match.group(2))
        
        # Reaction emoji mapping
        emoji_map = {
            '❤️': '❤', '🥰': '🥰', '😊': '😊', '☺️': '☺',
            '🔥': '🔥', '👍': '👍', '😂': '😂', '😮': '😮',
            '🎉': '🎉', '💔': '💔', '😈': '😈', '⚡': '⚡',
        }
        
        # Map user-friendly names to Telethon reaction format
        all_emojis = [emoji_map.get(r, r) for r in reactions]
        
        random.shuffle(accounts)
        success = 0
        failed = 0
        
        for i, account in enumerate(accounts):
            if await is_operation_cancelled(user_id):
                break
            if success >= count:
                break
            
            phone = account['phone']
            session_str = account['session_string']
            
            try:
                client = await get_telethon_client(session_str)
                await client.connect()
                
                if not await client.is_user_authorized():
                    failed += 1
                    await client.disconnect()
                    continue
                
                # Get entity
                try:
                    entity = await client.get_entity(channel_username)
                except:
                    failed += 1
                    await client.disconnect()
                    continue
                
                # Choose reaction
                if 'mix' in reactions:
                    # Random reaction from all available
                    chosen = random.choice(list(emoji_map.values()))
                else:
                    chosen = random.choice(all_emojis)
                
                # Send reaction
                try:
                    await client(functions.messages.SendReactionRequest(
                        peer=entity,
                        msg_id=message_id,
                        reaction=[types.ReactionEmoji(emoticon=chosen)]
                    ))
                    success += 1
                except Exception as e:
                    failed += 1
                
                await client.disconnect()
                
                # Random delay
                delay = random.uniform(1, 3)
                await asyncio.sleep(delay)
                
            except Exception as e:
                failed += 1
                logger.error(f"Reaction failed for {phone}: {e}")
            
            # Update status every 5 accounts
            if (i + 1) % 5 == 0:
                try:
                    await status_msg.edit_text(
                        premium_text(f"💜 **Reacting...**\n\n"
                                     f"✅ Success: {success}\n"
                                     f"❌ Failed: {failed}\n"
                                     f"Progress: {i+1}/{len(accounts)}"),
                        parse_mode=ParseMode.MARKDOWN
                    )
                except:
                    pass
        
        final = (premium_text("✅ **Reactions Complete**\n\n"
                              f"📍 **Post:** `{link}`\n"
                              f"💜 **Reactions:** {' '.join(reactions)}\n"
                              f"✅ **Success:** {success}\n"
                              f"❌ **Failed:** {failed}")
        )
        
        await status_msg.edit_text(
            final,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard()
        )
        
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Reactions crashed: {str(e)[:200]}",
            reply_markup=main_keyboard()
        )
    finally:
        cancel_flags[user_id] = False
        if user_id in active_operations:
            del active_operations[user_id]


print("✅ Part 4 loaded — View Boost + Reactions ready")

# ───────────────────────────────────────────────
# ALL ACCOUNTS ONLINE
# ───────────────────────────────────────────────

async def set_all_online(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set all accounts online."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
        return
    
    query = update.callback_query
    await query.answer()
    
    accounts = await db.get_connected_accounts()
    if not accounts:
        await query.edit_message_text(
            "❌ **No connected accounts.**",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard()
        )
        return
    
    msg = await query.edit_message_text(
        premium_text(f"🟢 **Setting {len(accounts)} accounts online...**"),
        parse_mode=ParseMode.MARKDOWN
    )
    
    cancel_flags[user_id] = False
    
    task = asyncio.create_task(
        run_set_all_online(user_id, accounts, msg)
    )
    active_operations[user_id] = task


async def run_set_all_online(user_id: int, accounts: List[Dict], status_msg):
    """Background task to set all accounts online."""
    success = 0
    failed = 0
    
    try:
        for i, account in enumerate(accounts):
            if await is_operation_cancelled(user_id):
                break
            
            phone = account['phone']
            session_str = account['session_string']
            
            try:
                client = await get_telethon_client(session_str)
                await client.connect()
                
                if not await client.is_user_authorized():
                    failed += 1
                    await client.disconnect()
                    continue
                
                await set_online(client)
                
                # Also update profile from name.txt
                names = await db.get_name_list()
                if names:
                    name = random.choice(names)
                    await update_profile_name(client, name)
                
                await db.update_account(phone, {"online": True})
                success += 1
                
                await client.disconnect()
                
            except Exception as e:
                failed += 1
                logger.error(f"Set online failed for {phone}: {e}")
            
            # Update status
            if (i + 1) % 5 == 0:
                try:
                    await status_msg.edit_text(
                        premium_text(f"🟢 **Setting Online...**\n\n"
                                     f"✅ Online: {success}\n"
                                     f"❌ Failed: {failed}\n"
                                     f"Progress: {i+1}/{len(accounts)}"),
                        parse_mode=ParseMode.MARKDOWN
                    )
                except:
                    pass
        
        await status_msg.edit_text(
            premium_text(f"✅ **All Accounts Online**\n\n"
                         f"🟢 **Online:** {success}\n"
                         f"❌ **Failed:** {failed}\n"
                         f"📊 **Total:** {len(accounts)}"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard()
        )
        
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Error: {str(e)[:200]}",
            reply_markup=main_keyboard()
        )
    finally:
        cancel_flags[user_id] = False
        if user_id in active_operations:
            del active_operations[user_id]


# ───────────────────────────────────────────────
# TOTAL ACCOUNTS STATS
# ───────────────────────────────────────────────

async def show_total_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show total account statistics."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
        return
    
    query = update.callback_query
    await query.answer()
    
    stats = await db.get_stats()
    
    # Get list of accounts
    accounts = await db.get_all_accounts()
    
    # Online count
    online_count = sum(1 for a in accounts if a.get('online'))
    
    text = (
        premium_text("📊 **Account Statistics**\n\n"
                     "━━━━━━━━━━━━━━━━━━\n"
                     f"📦 **Total Accounts:** `{stats['total']}`\n"
                     f"🟢 **Connected:** `{stats['connected']}`\n"
                     f"🔴 **Disconnected:** `{stats['disconnected']}`\n"
                     f"🟢 **Currently Online:** `{online_count}`\n"
                     "━━━━━━━━━━━━━━━━━━\n\n")
    )
    
    if accounts:
        text += "**Recent Accounts:**\n"
        for acc in accounts[-10:]:
            name = acc.get('name', 'Unknown')[:20]
            phone = acc.get('phone', 'N/A')
            status = "🟢" if acc.get('online') else "🔴"
            text += f"{status} `{phone}` — {name}\n"
    
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard()
    )


# ───────────────────────────────────────────────
# /CANCEL COMMAND
# ───────────────────────────────────────────────

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any running operation."""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Unauthorized")
        return
    
    # Set cancel flag
    cancel_flags[user_id] = True
    
    # Cancel active task
    if user_id in active_operations:
        task = active_operations[user_id]
        if not task.done():
            task.cancel()
        del active_operations[user_id]
    
    await update.message.reply_text(
        premium_text("⛔ **Operation Cancelled**\n\n"
                     "All active operations have been stopped."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard()
    )


# ───────────────────────────────────────────────
# NAME.TXT HANDLER
# ───────────────────────────────────────────────

async def handle_name_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle name.txt file upload — stores names for auto-profile update."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Unauthorized")
        return
    
    document = update.message.document
    if not document or not document.file_name.lower() == 'name.txt':
        return  # Not a name file
    
    msg = await update.message.reply_text(premium_text("⏳ Processing name.txt..."))
    
    try:
        file = await document.get_file()
        content = (await file.download_as_bytearray()).decode('utf-8')
        
        names = [line.strip() for line in content.split('\n') 
                 if line.strip() and not line.startswith('#')]
        
        if not names:
            await msg.edit_text("❌ No valid names found in name.txt")
            return
        
        await db.set_name_list(names)
        
        await msg.edit_text(
            premium_text(f"✅ **Name List Updated**\n\n"
                         f"📝 **{len(names)} names** loaded\n"
                         f"**First 5:**\n" + "\n".join(f"• {n}" for n in names[:5])),
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)[:150]}")


# ───────────────────────────────────────────────
# MAIN MENU / HANDLER BACK
# ───────────────────────────────────────────────

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Go back to main menu."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await query.edit_message_text("⛔ Unauthorized")
        return
    
    await query.edit_message_text(
        premium_text("🏠 **Main Menu**\n\n"
                     "Welcome to **Telegram Account Manager**\n"
                     "Select an option below:"),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard()
    )


# ───────────────────────────────────────────────
# /START COMMAND
# ───────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text(
            "⛔ **Unauthorized Access**\n\n"
            "You are not authorized to use this bot.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Clean any existing state
    cancel_flags[user_id] = False
    if user_id in active_operations:
        del active_operations[user_id]
    
    # Reset conversation data
    context.user_data.clear()
    
    # Load cached accounts count
    stats = await db.get_stats()
    names = await db.get_name_list()
    
    await update.message.reply_text(
        premium_text("🏠 **Telegram Account Manager**\n\n"
                     "━━━━━━━━━━━━━━━━━━\n"
                     f"👥 **Accounts:** {stats['total']} total\n"
                     f"🟢 **Connected:** {stats['connected']}\n"
                     f"📝 **Names Loaded:** {len(names)}\n"
                     "━━━━━━━━━━━━━━━━━━\n\n"
                     "🎯 **Features:**\n"
                     "➕ Add Accounts (OTP/Session/TData)\n"
                     "📥 Smart Join (3 Behaviour Modes)\n"
                     "👁 View Boost\n"
                     "💜 Reactions\n"
                     "🟢 Mass Online\n\n"
                     "**Select a feature below:**"),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard()
    )


print("✅ Part 5 loaded — Online, Stats, Handlers ready")
# ───────────────────────────────────────────────
# CALLBACK QUERY HANDLER — ROUTER
# ───────────────────────────────────────────────

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route callback queries to appropriate handlers."""
    query = update.callback_query
    data = query.data
    
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await query.answer("⛔ Unauthorized", show_alert=True)
        return
    
    if data == "cancel_op":
        cancel_flags[user_id] = True
        await query.answer("⛔ Cancelling...")
        await query.edit_message_text(
            "⛔ **Operation Cancelled**",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard()
        )
        # End any conversation
        return
    
    elif data == "back_main":
        await back_to_main(update, context)
    
    elif data == "add_account":
        await query.answer()
        await query.edit_message_text(
            premium_text("➕ **Add Account**\n\n"
                         "Choose a method:"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=add_account_keyboard()
        )
    
    elif data == "add_phone":
        return await ask_phone(update, context)
    
    elif data == "add_session_file":
        return await ask_session_file(update, context)
    
    elif data == "add_tdata":
        return await ask_tdata(update, context)
    
    elif data == "join":
        return await ask_join_link(update, context)
    
    elif data == "join_confirm":
        return await execute_join(update, context)
    
    elif data == "view_boost":
        return await ask_view_link(update, context)
    
    elif data == "reaction":
        return await ask_reaction_link(update, context)
    
    elif data.startswith("react_"):
        return await handle_reaction_selection(update, context)
    
    elif data == "all_online":
        return await set_all_online(update, context)
    
    elif data == "total_accounts":
        return await show_total_accounts(update, context)
    
    else:
        await query.answer("Unknown option", show_alert=True)


# ───────────────────────────────────────────────
# CONVERSATION HANDLER SETUP
# ───────────────────────────────────────────────

def get_conv_handler_add_phone():
    """Conversation handler for phone+OTP+2FA flow."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_phone, pattern="^add_phone$")],
        states={
            PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone),
                CallbackQueryHandler(lambda u,c: cancel_op_cb(u,c), pattern="^cancel_op$")
            ],
            OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_otp),
                CallbackQueryHandler(lambda u,c: cancel_op_cb(u,c), pattern="^cancel_op$")
            ],
            TFA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_tfa),
                CallbackQueryHandler(lambda u,c: cancel_op_cb(u,c), pattern="^cancel_op$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        name="add_phone_conv",
        persistent=False,
    )


def get_conv_handler_session_file():
    """Conversation handler for session file upload."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_session_file, pattern="^add_session_file$")],
        states={
            SESSION_FILE: [
                MessageHandler(filters.Document.FileExtension("txt"), receive_session_file),
                CallbackQueryHandler(lambda u,c: cancel_op_cb(u,c), pattern="^cancel_op$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        name="add_session_conv",
        persistent=False,
    )


def get_conv_handler_tdata():
    """Conversation handler for tdata upload."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_tdata, pattern="^add_tdata$")],
        states={
            TDATA: [
                MessageHandler(filters.Document.FileExtension("zip"), receive_tdata),
                CallbackQueryHandler(lambda u,c: cancel_op_cb(u,c), pattern="^cancel_op$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        name="add_tdata_conv",
        persistent=False,
    )


def get_conv_handler_join():
    """Conversation handler for join flow."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_join_link, pattern="^join$")],
        states={
            JOIN_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_join_link),
                CallbackQueryHandler(lambda u,c: cancel_op_cb(u,c), pattern="^cancel_op$")
            ],
            JOIN_TIME_MIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_join_time_range),
                CallbackQueryHandler(lambda u,c: cancel_op_cb(u,c), pattern="^cancel_op$")
            ],
            CONFIRM: [
                CallbackQueryHandler(execute_join, pattern="^join_confirm$"),
                CallbackQueryHandler(lambda u,c: cancel_op_cb(u,c), pattern="^cancel_op$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        name="join_conv",
        persistent=False,
    )


def get_conv_handler_view():
    """Conversation handler for view boost."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_view_link, pattern="^view_boost$")],
        states={
            VIEW_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_view_link),
                CallbackQueryHandler(lambda u,c: cancel_op_cb(u,c), pattern="^cancel_op$")
            ],
            VIEW_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_view_count),
                CallbackQueryHandler(lambda u,c: cancel_op_cb(u,c), pattern="^cancel_op$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        name="view_conv",
        persistent=False,
    )


def get_conv_handler_reaction():
    """Conversation handler for reactions."""
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_reaction_link, pattern="^reaction$")],
        states={
            REACT_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_reaction_link),
                CallbackQueryHandler(lambda u,c: cancel_op_cb(u,c), pattern="^cancel_op$")
            ],
            REACT_TYPES: [
                CallbackQueryHandler(handle_reaction_selection, pattern="^react_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_reaction_link),
            ],
            REACT_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_reaction_count),
                CallbackQueryHandler(lambda u,c: cancel_op_cb(u,c), pattern="^cancel_op$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        name="react_conv",
        persistent=False,
    )


async def cancel_op_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle cancel via callback."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    cancel_flags[user_id] = True
    await query.edit_message_text(
        "⛔ Cancelled.",
        reply_markup=main_keyboard()
    )
    return ConversationHandler.END


# ───────────────────────────────────────────────
# MAIN — BOT ENTRY POINT
# ───────────────────────────────────────────────

async def main():
    """Main entry point — start the bot."""
    
    # Connect to MongoDB
    connected = await db.connect()
    if not connected:
        logger.error("Failed to connect to MongoDB. Exiting.")
        return
    
    # Ensure indexes
    await db.accounts.create_index("phone", unique=True)
    await db.settings.create_index("key", unique=True)
    
    # Create session dir
    os.makedirs("sessions", exist_ok=True)
    
    # Build application
    app = Application.builder().token(config.BOT_TOKEN).build()
    
    # ── Register command handlers ──
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    
    # ── Register conversation handlers ──
    app.add_handler(get_conv_handler_add_phone())
    app.add_handler(get_conv_handler_session_file())
    app.add_handler(get_conv_handler_tdata())
    app.add_handler(get_conv_handler_join())
    app.add_handler(get_conv_handler_view())
    app.add_handler(get_conv_handler_reaction())
    
    # ── Register callback router (catch-all for non-conversation callbacks) ──
    app.add_handler(CallbackQueryHandler(callback_router))
    
    # ── Register document handler for name.txt ──
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("txt") & ~filters.COMMAND,
        handle_name_file
    ))
    
    # ── Register fallback for text messages ──
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        async def unknown_text(update, context):
            await update.message.reply_text(
                "❓ Use /start to see the menu."
            )
    ))
    
    # ── Set bot commands ──
    await app.bot.set_my_commands([
        BotCommand("start", "Show main menu"),
        BotCommand("cancel", "Cancel current operation")
    ])
    
    logger.info("""
╔══════════════════════════════════════════╗
║     TELEGRAM ACCOUNT MANAGER BOT         ║
║          SHADOW MODE V99 ACTIVE          ║
╠══════════════════════════════════════════╣
║  ✅ MongoDB Connected                    ║
║  ✅ Bot configured                       ║
║  ✅ All handlers registered              ║
║  ⚡ Ready for deployment                 ║
╚══════════════════════════════════════════╝
    """)
    
    # ── Start polling ──
    await app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")


print("✅ PART 6 — MAIN ENTRY POINT READY")
