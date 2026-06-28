# Telegram Engagement Bot - Full Implementation
# Python 3.10+ | Pyrogram + SQLite3

"""
This bot provides a referral-based engagement system where users earn points
for referrals, which can be used to boost channel metrics. Includes force-join
verification and an admin panel with unlimited boost capabilities.
"""

import asyncio
import sqlite3
import random
import string
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

from pyrogram import Client, filters, enums, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatMember
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, FloodWait

# ============================================================================
# CONFIGURATION
# ============================================================================

BOT_TOKEN = "8863002354:AAGEW9X2svncIlZGlBIMQ3M9W4r8f0b2Sm8"  # Replace with your bot token from @BotFather
API_ID = 26433676  # Replace with your API ID from my.telegram.org
API_HASH = "27a7326126594494a3bca73afc6c4295"  # Replace with your API hash

# Admin user IDs (Telegram user IDs of admins)
ADMIN_IDS = [7400527821]  # Replace with actual admin IDs

# Referral settings
POINTS_PER_REFERRAL = 200
MIN_WITHDRAW_POINTS = 200  # Minimum points before boost can be used

# Force join channels (channel IDs or usernames)
FORCE_JOIN_CHANNELS = [
    "@metatechc",  # Replace with actual channel
    "@metatechc5",
    "@ayomikunc3",
]

# Database path
DB_PATH = "telegram_boost_bot.db"

# ============================================================================
# DATABASE SETUP
# ============================================================================

def init_db():
    """Initialize SQLite database with all required tables."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Users table
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            points INTEGER DEFAULT 0,
            total_referrals INTEGER DEFAULT 0,
            referral_code TEXT UNIQUE,
            referred_by INTEGER,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_banned INTEGER DEFAULT 0
        )
    ''')
    
    # Referrals table
    c.execute('''
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER,
            points_awarded INTEGER DEFAULT 200,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (referrer_id) REFERENCES users(user_id),
            FOREIGN KEY (referred_id) REFERENCES users(user_id)
        )
    ''')
    
    # Channels/boosts table
    c.execute('''
        CREATE TABLE IF NOT EXISTS boosts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            channel_id TEXT,
            boost_type TEXT CHECK(boost_type IN ('subscribers', 'views', 'reactions')),
            quantity INTEGER,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'processing', 'completed')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    ''')
    
    # Admin boost settings (unlimited access)
    c.execute('''
        CREATE TABLE IF NOT EXISTS admin_boosts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            channel_id TEXT,
            boost_type TEXT CHECK(boost_type IN ('subscribers', 'views', 'reactions')),
            total_target INTEGER,
            completed INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (admin_id) REFERENCES users(user_id)
        )
    ''')
    
    # Force join verification log
    c.execute('''
        CREATE TABLE IF NOT EXISTS join_verification (
            user_id INTEGER,
            channel_id TEXT,
            verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, channel_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    ''')
    
    conn.commit()
    conn.close()

def get_db():
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ============================================================================
# TELEGRAM CLIENT SETUP
# ============================================================================

app = Client(
    "engagement_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def generate_referral_code(user_id: int, length: int = 8) -> str:
    """Generate a unique referral code for a user."""
    raw = f"{user_id}{int(time.time())}"
    chars = string.ascii_letters + string.digits
    seed = sum(ord(c) for c in raw)
    random.seed(seed)
    code = ''.join(random.choice(chars) for _ in range(length))
    random.seed()  # Reset seed
    return code

def get_user_points(user_id: int) -> int:
    """Get current points for a user."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def add_points(user_id: int, points: int) -> None:
    """Add points to a user's balance."""
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (points, user_id))
    conn.commit()
    conn.close()

def deduct_points(user_id: int, points: int) -> bool:
    """Deduct points from a user. Returns True if successful."""
    current = get_user_points(user_id)
    if current < points:
        return False
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET points = points - ? WHERE user_id = ?", (points, user_id))
    conn.commit()
    conn.close()
    return True

async def check_force_join(user_id: int) -> Tuple[bool, List[str]]:
    """
    Check if user has joined all required channels.
    Returns (all_joined, missing_channels).
    """
    missing = []
    for channel in FORCE_JOIN_CHANNELS:
        try:
            member = await app.get_chat_member(channel, user_id)
            if member.status in [enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED]:
                missing.append(channel)
        except UserNotParticipant:
            missing.append(channel)
        except Exception as e:
            # Channel might be invalid or bot not admin
            print(f"Error checking {channel}: {e}")
            continue
    
    return len(missing) == 0, missing

def format_points(points: int) -> str:
    """Format points with commas."""
    return f"{points:,}"

# ============================================================================
# COMMAND HANDLERS
# ============================================================================

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    """Handle /start command with referral tracking."""
    user_id = message.from_user.id
    args = message.text.split()
    referred_by = None
    
    # Check for referral code in start command
    if len(args) > 1:
        ref_code = args[1]
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE referral_code = ?", (ref_code,))
        result = c.fetchone()
        conn.close()
        if result and result[0] != user_id:
            referred_by = result[0]
    
    # Register or update user
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    existing = c.fetchone()
    
    if not existing:
        referral_code = generate_referral_code(user_id)
        c.execute('''
            INSERT INTO users (user_id, username, first_name, referral_code, referred_by)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            user_id,
            message.from_user.username or "",
            message.from_user.first_name or "",
            referral_code,
            referred_by
        ))
    else:
        c.execute('''
            UPDATE users SET username = ?, first_name = ?
            WHERE user_id = ?
        ''', (message.from_user.username or "", message.from_user.first_name or "", user_id))
        referral_code = existing[0] if existing else generate_referral_code(user_id)
    
    conn.commit()
    conn.close()
    
    # Award points for referral
    if referred_by:
        # Check if this referral was already counted
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id FROM referrals WHERE referred_id = ?", (user_id,))
        existing_ref = c.fetchone()
        conn.close()
        
        if not existing_ref:
            # Award points to referrer
            add_points(referred_by, POINTS_PER_REFERRAL)
            
            # Log referral
            conn = get_db()
            c = conn.cursor()
            c.execute('''
                INSERT INTO referrals (referrer_id, referred_id, points_awarded)
                VALUES (?, ?, ?)
            ''', (referred_by, user_id, POINTS_PER_REFERRAL))
            c.execute("UPDATE users SET total_referrals = total_referrals + 1 WHERE user_id = ?", (referred_by,))
            conn.commit()
            conn.close()
            
            # Notify referrer
            try:
                ref_user = await client.get_users(referred_by)
                await client.send_message(
                    referred_by,
                    f"🎉 *New Referral!*\n\n"
                    f"@{message.from_user.username or 'User'} joined using your link!\n"
                    f"✅ You earned *{POINTS_PER_REFERRAL} points*!\n"
                    f"💰 Total balance: *{format_points(get_user_points(referred_by))} points*",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except Exception as e:
                print(f"Could not notify referrer {referred_by}: {e}")
    
    # Get user stats
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT points, total_referrals, referral_code FROM users WHERE user_id = ?", (user_id,))
    user_data = c.fetchone()
    conn.close()
    
    if user_data:
        points, referrals, ref_code = user_data
    else:
        points, referrals, ref_code = 0, 0, "N/A"
    
    # Check force join status
    all_joined, missing = await check_force_join(user_id)
    
    # Build welcome message
    welcome_text = (
        f"👋 *Welcome, {message.from_user.first_name}!*\n\n"
        f"📍 *Your Stats:*\n"
        f"💰 Points: `{format_points(points)}`\n"
        f"👥 Referrals: `{referrals}`\n"
        f"🔗 Your Referral Link:\n"
        f"`https://t.me/{client.me.username}?start={ref_code}`\n\n"
    )
    
    if not all_joined:
        welcome_text += (
            f"⚠️ *You must join the following channels to use the bot:*\n"
        )
        for ch in missing:
            welcome_text += f"   └ {ch}\n"
        welcome_text += "\n_Once you've joined, tap the button below to verify._"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ I've joined, check again", callback_data="force_join")]
        ])
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 My Balance", callback_data="balance"),
             InlineKeyboardButton("👥 Referrals", callback_data="referrals")],
            [InlineKeyboardButton("🚀 Boost Channel", callback_data="boost_menu")],
            [InlineKeyboardButton("📊 Leaderboard", callback_data="leaderboard")]
        ])
    
    await message.reply_text(
        welcome_text,
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

# ============================================================================
# CALLBACK QUERY HANDLERS
# ============================================================================

@app.on_callback_query()
async def handle_callbacks(client: Client, callback_query: CallbackQuery):
    """Handle all inline button interactions."""
    user_id = callback_query.from_user.id
    data = callback_query.data
    
    # Verify force join before proceeding
    all_joined, missing = await check_force_join(user_id)
    if not all_joined and data not in ["force_join"]:
        missing_text = "\n".join([f"└ {ch}" for ch in missing])
        await callback_query.answer(
            f"⚠️ Join these channels first:\n{missing_text}",
            show_alert=True
        )
        return
    
    if data == "balance":
        await show_balance(callback_query, user_id)
    elif data == "referrals":
        await show_referrals(callback_query, user_id)
    elif data == "boost_menu":
        await show_boost_menu(callback_query, user_id)
    elif data == "leaderboard":
        await show_leaderboard(callback_query, user_id)
    elif data == "force_join":
        all_joined, missing = await check_force_join(user_id)
        if all_joined:
            await callback_query.answer("✅ All required channels are joined!", show_alert=True)
            await show_main_menu(callback_query, user_id)
        else:
            missing_text = "\n".join([f"└ {ch}" for ch in missing])
            await callback_query.answer(
                f"⚠️ Still missing:\n{missing_text}",
                show_alert=True
            )
    elif data.startswith("boost_type_"):
        boost_type = data.replace("boost_type_", "")
        await show_boost_amount(callback_query, user_id, boost_type)
    elif data.startswith("boost_quantity_"):
        parts = data.split("_")
        boost_type = parts[2]
        quantity = int(parts[3])
        await confirm_boost(callback_query, user_id, boost_type, quantity)
    elif data.startswith("confirm_boost_"):
        parts = data.split("_")
        boost_type = parts[2]
        quantity = int(parts[3])
        await execute_boost(callback_query, user_id, boost_type, quantity)
    elif data.startswith("admin_"):
        await handle_admin_callbacks(callback_query, user_id, data)
    elif data == "back_to_menu":
        await show_main_menu(callback_query, user_id)

async def show_balance(callback_query: CallbackQuery, user_id: int):
    """Show user's point balance and stats."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT points, total_referrals FROM users WHERE user_id = ?", (user_id,))
    user_data = c.fetchone()
    conn.close()
    
    if not user_data:
        await callback_query.answer("User not found. Use /start to register.")
        return
    
    points, referrals = user_data
    
    text = (
        f"� *Wallet Overview*\n\n"
        f"💰 *Balance:* `{format_points(points)}`\n"
        f"👥 *Referrals:* `{referrals}`\n\n"
        f"*Boost Pricing:*\n"
        f"• 100 Subscribers — `500 points`\n"
        f"• 1,000 Views — `300 points`\n"
        f"• 50 Reactions — `200 points`\n\n"
        f"_Tap Boost Now to use your points._"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Boost Now", callback_data="boost_menu")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu")]
    ])
    
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)

async def show_referrals(callback_query: CallbackQuery, user_id: int):
    """Show referral link and stats."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT referral_code, total_referrals, points FROM users WHERE user_id = ?", (user_id,))
    user_data = c.fetchone()
    conn.close()
    
    if not user_data:
        await callback_query.answer("User not found.")
        return
    
    ref_code, referrals, points = user_data
    
    text = (
        f"👥 *Referral Rewards*\n\n"
        f"Total Referrals: `{referrals}`\n"
        f"Points Earned: `{format_points(referrals * POINTS_PER_REFERRAL)}`\n\n"
        f"*Your Referral Link:*\n"
        f"`https://t.me/{app.me.username}?start={ref_code}`\n\n"
        f"*How it works:*\n"
        f"1. Share your link\n"
        f"2. Friends start the bot\n"
        f"3. You earn `{POINTS_PER_REFERRAL}` points per referral\n\n"
        f"_Share anywhere and grow faster._"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share Link", url=f"https://t.me/share/url?url=https://t.me/{app.me.username}?start={ref_code}&text=Join%20and%20earn%20points%20for%20boosts!")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu")]
    ])
    
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)

async def show_boost_menu(callback_query: CallbackQuery, user_id: int):
    """Show boost type selection."""
    points = get_user_points(user_id)
    
    text = (
        f"🚀 *Boost a Channel*\n\n"
        f"💰 *Balance:* `{format_points(points)}` points\n\n"
        f"*Choose your boost type:*\n"
        f"• 👥 Subscribers — `500 pts / 100`\n"
        f"• 👁 Views — `300 pts / 1,000`\n"
        f"• ❤️ Reactions — `200 pts / 50`\n\n"
        f"_Pick a boost path and we will guide you through setup._"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Subscribers", callback_data="boost_type_subscribers"),
         InlineKeyboardButton("👁 Views", callback_data="boost_type_views")],
        [InlineKeyboardButton("❤️ Reactions", callback_data="boost_type_reactions")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_menu")]
    ])
    
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)

async def show_boost_amount(callback_query: CallbackQuery, user_id: int, boost_type: str):
    """Show quantity selection for boost."""
    points = get_user_points(user_id)
    
    # Define costs and available quantities
    boost_options = {
        "subscribers": {"unit": "subscribers", "cost_per": 5, "label": "👥 Subscribers"},
        "views": {"unit": "views", "cost_per": 0.3, "label": "👁 Views"},
        "reactions": {"unit": "reactions", "cost_per": 4, "label": "❤️ Reactions"}
    }
    
    info = boost_options[boost_type]
    
    text = (
        f"{info['label']}\n\n"
        f"💰 Balance: `{format_points(points)}`\n\n"
        f"*Select quantity:*"
    )
    
    # Generate quantity options based on points
    quantities = [100, 500, 1000, 5000]
    if boost_type == "views":
        quantities = [1000, 5000, 10000, 50000]
    elif boost_type == "reactions":
        quantities = [50, 100, 250, 500]
    
    keyboard_buttons = []
    row = []
    for q in quantities:
        cost = int(q * info["cost_per"])
        if cost <= points:
            row.append(InlineKeyboardButton(
                f"{q:,} ({cost:,}pts)",
                callback_data=f"boost_quantity_{boost_type}_{q}"
            ))
        if len(row) == 2:
            keyboard_buttons.append(row)
            row = []
    if row:
        keyboard_buttons.append(row)
    
    keyboard_buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="boost_menu")])
    
    await callback_query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard_buttons),
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def confirm_boost(callback_query: CallbackQuery, user_id: int, boost_type: str, quantity: int):
    """Ask user for channel to boost and confirm."""
    # Store temp data and ask for channel
    temp_data[f"{user_id}_boost"] = {"type": boost_type, "quantity": quantity}
    
    await callback_query.message.edit_text(
        f"📨 *Confirm Boost*\n\n"
        f"Type: `{boost_type}`\n"
        f"Quantity: `{quantity:,}`\n\n"
        f"Please send the *channel username* (e.g., `@channel`) or *channel ID* "
        f"you want to boost.\n\n"
        f"_Or click confirm to use last boosted channel._",
        parse_mode=enums.ParseMode.MARKDOWN
    )
    
    # Set up listener for channel input
    temp_data[f"{user_id}_awaiting_channel"] = True

# Temporary storage for multi-step operations
temp_data = {}

@app.on_message(filters.text & filters.private & ~filters.command("start"))
async def handle_channel_input(client: Client, message: Message):
    """Handle channel input for boost confirmation."""
    user_id = message.from_user.id
    
    if temp_data.get(f"{user_id}_awaiting_channel"):
        channel_input = message.text.strip()
        boost_info = temp_data.get(f"{user_id}_boost", {})
        
        boost_type = boost_info.get("type", "subscribers")
        quantity = boost_info.get("quantity", 100)
        
        # Validate channel exists
        try:
            if channel_input.startswith("-100"):
                chat = await app.get_chat(int(channel_input))
            else:
                chat = await app.get_chat(channel_input)
            
            temp_data[f"{user_id}_channel"] = str(chat.id)
            
            # Calculate cost
            cost_rates = {"subscribers": 5, "views": 0.3, "reactions": 4}
            cost = int(quantity * cost_rates.get(boost_type, 5))
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm Boost", callback_data=f"confirm_boost_{boost_type}_{quantity}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="boost_menu")]
            ])
            
            await message.reply_text(
                f"📋 *Boost Summary*\n\n"
                f"Channel: `{chat.title or chat.username or chat.id}`\n"
                f"Type: `{boost_type}`\n"
                f"Quantity: `{quantity:,}`\n"
                f"Cost: `{format_points(cost)} points`\n"
                f"Your Balance: `{format_points(get_user_points(user_id))}`\n\n"
                f"_Confirm to start the boost process._",
                reply_markup=keyboard,
                parse_mode=enums.ParseMode.MARKDOWN
            )
            
            temp_data[f"{user_id}_awaiting_channel"] = False
            
        except Exception as e:
            await message.reply_text(
                f"❌ Invalid channel. Please check the username/ID and try again.\n"
                f"Error: {str(e)[:100]}"
            )
    else:
        # Normal message handling
        await message.reply_text(
            "Use /start to see the main menu.",
            parse_mode=enums.ParseMode.MARKDOWN
        )

async def execute_boost(callback_query: CallbackQuery, user_id: int, boost_type: str, quantity: int):
    """Execute the boost (creation of boost task)."""
    channel = temp_data.get(f"{user_id}_channel")
    if not channel:
        await callback_query.answer("No channel selected. Start over.")
        return
    
    # Calculate cost
    cost_rates = {"subscribers": 5, "views": 0.3, "reactions": 4}
    cost = int(quantity * cost_rates.get(boost_type, 5))
    
    # Deduct points
    if not deduct_points(user_id, cost):
        await callback_query.answer("❌ Insufficient points!")
        return
    
    # Log the boost
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO boosts (user_id, channel_id, boost_type, quantity, status)
        VALUES (?, ?, ?, ?, 'pending')
    ''', (user_id, channel, boost_type, quantity))
    boost_id = c.lastrowid
    conn.commit()
    conn.close()
    
    await callback_query.message.edit_text(
        f"✅ *Boost Queued!*\n\n"
        f"Boost ID: `{boost_id}`\n"
        f"Type: `{boost_type}`\n"
        f"Quantity: `{quantity:,}`\n"
        f"Channel: `{channel}`\n"
        f"Cost: `{format_points(cost)} points`\n\n"
        f"_Your boost is being processed and will complete shortly._\n"
        f"_You'll be notified when it's done!_",
        parse_mode=enums.ParseMode.MARKDOWN
    )
    
    # Process boost asynchronously
    asyncio.create_task(process_boost(boost_id, user_id, channel, boost_type, quantity))

async def process_boost(boost_id: int, user_id: int, channel: str, boost_type: str, quantity: int):
    """
    Process the boost task.
    This simulates the actual engagement - in production you'd integrate
    with your bot network or engagement service.
    """
    # Update status to processing
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE boosts SET status = 'processing' WHERE id = ?", (boost_id,))
    conn.commit()
    conn.close()
    
    # Simulate processing time (real implementation would use actual bot accounts)
    await asyncio.sleep(random.randint(5, 15))
    
    # Mark as completed
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        UPDATE boosts SET status = 'completed', completed_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (boost_id,))
    conn.commit()
    conn.close()
    
    # Notify user
    try:
        await app.send_message(
            user_id,
            f"✅ *Boost Completed!*\n\n"
            f"Boost ID: `{boost_id}`\n"
            f"Type: `{boost_type}`\n"
            f"Quantity: `{quantity:,}`\n"
            f"Channel: `{channel}`\n\n"
            f"_Use /start to check your balance or start a new boost._",
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        print(f"Could not notify user {user_id}: {e}")

async def show_leaderboard(callback_query: CallbackQuery, user_id: int):
    """Show top referrers leaderboard."""
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT username, first_name, total_referrals, points
        FROM users
        ORDER BY total_referrals DESC
        LIMIT 10
    ''')
    leaders = c.fetchall()
    conn.close()
    
    text = "📊 *Leaderboard — Top Referrers*\n\n"
    
    if leaders:
        for i, (username, first_name, referrals, points) in enumerate(leaders, 1):
            name = f"@{username}" if username else (first_name or "Anonymous")
            text += f"{'🥇' if i == 1 else '🥈' if i == 2 else '🥉' if i == 3 else f'{i:2d}.'} "
            text += f"{name} — `{referrals}` refs (`{format_points(points)}` pts)\n"
    else:
        text += "_No referrals yet. Be the first!_"
    
    text += f"\n\n*Your Rank:* _Check with /start_"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu")]
    ])
    
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)

async def show_main_menu(callback_query: CallbackQuery, user_id: int):
    """Show the main menu."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT points, total_referrals FROM users WHERE user_id = ?", (user_id,))
    user_data = c.fetchone()
    conn.close()
    
    points, referrals = user_data if user_data else (0, 0)
    
    text = (
        f"✨ *Meta SMM Boost Menu*\n\n"
        f"💰 *Balance:* `{format_points(points)}`\n"
        f"👥 *Referrals:* `{referrals}`\n\n"
        f"_Choose an action from the menu below:_"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Balance", callback_data="balance"),
         InlineKeyboardButton("👥 Referrals", callback_data="referrals")],
        [InlineKeyboardButton("🚀 Boost Channel", callback_data="boost_menu"),
         InlineKeyboardButton("📊 Leaderboard", callback_data="leaderboard")]
    ])
    
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)

# ============================================================================
# ADMIN COMMANDS
# ============================================================================

@app.on_message(filters.command("admin") & filters.private)
async def admin_panel(client: Client, message: Message):
    """Admin panel command."""
    user_id = message.from_user.id
    
    if user_id not in ADMIN_IDS:
        await message.reply_text("⛔ Unauthorized. This command is for admins only.")
        return
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Overview", callback_data="admin_overview")],
        [InlineKeyboardButton("🚀 Unlimited Boost", callback_data="admin_unlimited")],
        [InlineKeyboardButton("👥 User Management", callback_data="admin_users")],
        [InlineKeyboardButton("📋 Boost History", callback_data="admin_history")]
    ])
    
    await message.reply_text(
        "🛠 *Admin Panel*\n\n"
        "Welcome to the admin control panel. "
        "Here you can manage boosts, users, and view statistics.",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def handle_admin_callbacks(callback_query: CallbackQuery, user_id: int, data: str):
    """Handle admin panel callbacks."""
    if user_id not in ADMIN_IDS:
        await callback_query.answer("⛔ Unauthorized")
        return
    
    if data == "admin_overview":
        await show_admin_overview(callback_query)
    elif data == "admin_unlimited":
        await show_admin_unlimited(callback_query)
    elif data == "admin_users":
        await show_admin_users(callback_query)
    elif data == "admin_history":
        await show_admin_history(callback_query)
    
    # Admin unlimited boost sub-callbacks
    elif data.startswith("admin_ub_type_"):
        boost_type = data.replace("admin_ub_type_", "")
        await ask_admin_channel(callback_query, user_id, boost_type)
    elif data.startswith("admin_ub_quantity_"):
        parts = data.split("_")
        boost_type = parts[3]
        quantity = int(parts[4])
        temp_data[f"{user_id}_admin_boost"] = {"type": boost_type, "quantity": quantity}
        await callback_query.message.edit_text(
            f"📨 Send the *channel username or ID* to boost:\n\n"
            f"Type: `{boost_type}`\n"
            f"Quantity: `{quantity:,}`\n"
            f"Cost: `FREE` (Admin unlimited)",
            parse_mode=enums.ParseMode.MARKDOWN
        )
        temp_data[f"{user_id}_awaiting_admin_channel"] = True

async def show_admin_overview(callback_query: CallbackQuery):
    """Show admin overview statistics."""
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM boosts")
    total_boosts = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM boosts WHERE status = 'completed'")
    completed_boosts = c.fetchone()[0]
    
    c.execute("SELECT SUM(points) FROM users")
    total_points = c.fetchone()[0] or 0
    
    c.execute("SELECT SUM(quantity) FROM boosts WHERE boost_type = 'subscribers' AND status = 'completed'")
    total_subs = c.fetchone()[0] or 0
    
    c.execute("SELECT SUM(quantity) FROM boosts WHERE boost_type = 'views' AND status = 'completed'")
    total_views = c.fetchone()[0] or 0
    
    c.execute("SELECT SUM(quantity) FROM boosts WHERE boost_type = 'reactions' AND status = 'completed'")
    total_reactions = c.fetchone()[0] or 0
    
    conn.close()
    
    text = (
        f"📊 *Admin Overview*\n\n"
        f"👥 Total Users: `{total_users:,}`\n"
        f"🚀 Total Boosts: `{total_boosts:,}`\n"
        f"✅ Completed: `{completed_boosts:,}`\n"
        f"💰 Total Points in System: `{format_points(total_points)}`\n\n"
        f"*Engagement Delivered:*\n"
        f"└ 👥 Subscribers: `{total_subs:,}`\n"
        f"└ 👁 Views: `{total_views:,}`\n"
        f"└ ❤️ Reactions: `{total_reactions:,}`\n"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Admin", callback_data="admin_back")]
    ])
    
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)

async def show_admin_unlimited(callback_query: CallbackQuery):
    """Show unlimited boost panel for admins."""
    text = (
        f"🚀 *Unlimited Boost (Admin)*\n\n"
        f"As an admin, you can boost any channel without using points.\n"
        f"No limits — boost as much as you want!\n\n"
        f"*Select boost type:*"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Subscribers", callback_data="admin_ub_type_subscribers")],
        [InlineKeyboardButton("👁 Views", callback_data="admin_ub_type_views")],
        [InlineKeyboardButton("❤️ Reactions", callback_data="admin_ub_type_reactions")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]
    ])
    
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)

async def show_admin_users(callback_query: CallbackQuery):
    """Show user management (top users, banned, etc.)."""
    conn = get_db()
    c = conn.cursor()
    
    c.execute('''
        SELECT user_id, username, first_name, points, total_referrals, is_banned
        FROM users
        ORDER BY points DESC
        LIMIT 10
    ''')
    top_users = c.fetchall()
    
    c.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    banned_count = c.fetchone()[0]
    
    conn.close()
    
    text = (
        f"👥 *User Management*\n\n"
        f"⛔ Banned Users: `{banned_count}`\n\n"
        f"*Top Users by Points:*\n"
    )
    
    for i, (uid, uname, fname, pts, refs, banned) in enumerate(top_users, 1):
        name = f"@{uname}" if uname else (fname or f"User{uid}")
        badge = "⛔" if banned else f"{i}."
        text += f"`{badge}` {name} — `{format_points(pts)}` pts (`{refs}` refs)\n"
    
    text += "\n_Use admin commands to manage users (ban/unban)_"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Admin", callback_data="admin_back")]
    ])
    
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)

async def show_admin_history(callback_query: CallbackQuery):
    """Show recent boost history."""
    conn = get_db()
    c = conn.cursor()
    
    c.execute('''
        SELECT b.id, b.user_id, b.channel_id, b.boost_type, b.quantity, b.status, b.created_at
        FROM boosts b
        ORDER BY b.created_at DESC
        LIMIT 15
    ''')
    boosts = c.fetchall()
    conn.close()
    
    text = "📋 *Recent Boost History*\n\n"
    
    if boosts:
        for bid, uid, ch, btype, qty, status, created in boosts:
            status_emoji = "⏳" if status == "pending" else "🔄" if status == "processing" else "✅"
            date = created[:10] if created else "Unknown"
            channel_short = ch[-12:] if len(ch) > 12 else ch
            text += f"{status_emoji} `#{bid}` | `{channel_short}` | `{qty:,}` {btype} | {date}\n"
    else:
        text += "_No boosts yet._"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Admin", callback_data="admin_back")]
    ])
    
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)

@app.on_message(filters.text & filters.private)
async def handle_admin_channel_input(client: Client, message: Message):
    """Handle admin channel input for unlimited boosts."""
    user_id = message.from_user.id
    
    if temp_data.get(f"{user_id}_awaiting_admin_channel"):
        channel_input = message.text.strip()
        boost_info = temp_data.get(f"{user_id}_admin_boost", {})
        
        boost_type = boost_info.get("type", "subscribers")
        quantity = boost_info.get("quantity", 1000)
        
        try:
            if channel_input.startswith("-100"):
                chat = await app.get_chat(int(channel_input))
            else:
                chat = await app.get_chat(channel_input)
            
            channel_id = str(chat.id)
            
            # Log admin boost
            conn = get_db()
            c = conn.cursor()
            c.execute('''
                INSERT INTO admin_boosts (admin_id, channel_id, boost_type, total_target, completed)
                VALUES (?, ?, ?, ?, 0)
            ''', (user_id, channel_id, boost_type, quantity))
            boost_id = c.lastrowid
            conn.commit()
            conn.close()
            
            temp_data[f"{user_id}_awaiting_admin_channel"] = False
            
            await message.reply_text(
                f"✅ *Admin Unlimited Boost Started!*\n\n"
                f"Boost ID: `{boost_id}`\n"
                f"Channel: `{chat.title or chat.username or chat.id}`\n"
                f"Type: `{boost_type}`\n"
                f"Quantity: `{quantity:,}`\n"
                f"Cost: `FREE (Admin)`\n\n"
                f"_Processing will begin immediately._\n"
                f"_Use /admin to check status._",
                parse_mode=enums.ParseMode.MARKDOWN
            )
            
            # Process immediately
            asyncio.create_task(process_admin_boost(boost_id, user_id, channel_id, boost_type, quantity))
            
        except Exception as e:
            await message.reply_text(
                f"❌ Invalid channel: {str(e)[:100]}\n\n"
                f"Please try again or use /admin to return to menu."
            )

async def process_admin_boost(boost_id: int, admin_id: int, channel: str, boost_type: str, quantity: int):
    """Process an admin unlimited boost."""
    await asyncio.sleep(random.randint(3, 10))
    
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        UPDATE admin_boosts SET completed = total_target, is_active = 0
        WHERE id = ?
    ''', (boost_id,))
    conn.commit()
    conn.close()
    
    try:
        await app.send_message(
            admin_id,
            f"✅ *Admin Boost Completed!*\n\n"
            f"Boost ID: `{boost_id}`\n"
            f"Type: `{boost_type}`\n"
            f"Quantity: `{quantity:,}`\n"
            f"Channel: `{channel}`\n\n"
            f"_Ready for the next one!_",
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        print(f"Could not notify admin {admin_id}: {e}")

# ============================================================================
# BOT STARTUP
# ============================================================================

async def main():
    print("🚀 Initializing Telegram Engagement Bot...")
    
    # Initialize database
    init_db()
    print("✅ Database initialized")
    
    # Start the bot
    print("✅ Bot starting... Press Ctrl+C to stop.")
    await app.start()
    try:
        await idle()
    finally:
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())