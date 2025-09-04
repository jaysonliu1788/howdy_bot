# bot.py
import os
import re
import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

# Optional extras - may not be installed; bot will fallback gracefully.
try:
    import language_tool_python
except Exception:
    language_tool_python = None

try:
    import openai
except Exception:
    openai = None

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # optional
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID") or 0)

if OPENAI_API_KEY and openai:
    openai.api_key = OPENAI_API_KEY

# Simple profanity filter list (extend as needed)
PROFANITY = {
    "badword1", "badword2"  # placeholder - replace with the words you want blocked
}

def contains_profanity(text: str) -> bool:
    text_l = text.lower()
    # naive check; add boundaries as needed
    for w in PROFANITY:
        if re.search(rf"\b{re.escape(w)}\b", text_l):
            return True
    return False

# minimal content-safe check - optional more checks or third-party moderation
async def is_content_safe(text: str) -> bool:
    if contains_profanity(text):
        return False
    # if OpenAI moderation desired & configured, call it:
    if OPENAI_API_KEY and openai:
        try:
            resp = openai.Moderation.create(input=text)
            results = resp["results"][0]
            if results["flagged"]:
                return False
        except Exception:
            # if moderation fails, fallback to local checks
            pass
    return True

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Simple persistent storage for chatbot history (sqlite)
DB_PATH = "bot_memory.sqlite"
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute(
    """CREATE TABLE IF NOT EXISTS history (
           channel_id INTEGER,
           role TEXT,
           content TEXT,
           created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
       )"""
)
conn.commit()

def save_message_to_history(channel_id: int, role: str, content: str):
    c.execute(
        "INSERT INTO history (channel_id, role, content) VALUES (?, ?, ?)",
        (channel_id, role, content),
    )
    conn.commit()

def load_history_for_channel(channel_id: int, limit: int = 12):
    c.execute(
        "SELECT role, content FROM history WHERE channel_id = ? ORDER BY created_at DESC LIMIT ?",
        (channel_id, limit),
    )
    rows = c.fetchall()
    # return oldest-first
    return list(reversed(rows))

async def run_language_tool_check(text: str, is_article: bool = False) -> str:
    """
    Use language_tool_python if available. If not, do a simple fallback (basic fixes)
    """
    if language_tool_python:
        try:
            tool = language_tool_python.LanguageTool("en-US")
            matches = tool.check(text)
            new_text = language_tool_python.utils.correct(text, matches)
            # Optionally improve wording if is_article; LanguageTool doesn't rewrite style much.
            return new_text
        except Exception:
            pass

    # Fallback naive "fixes": fix double spaces, simple punctuation spacing, capitalize sentences
    s = re.sub(r"\s+", " ", text).strip()
    s = re.sub(r"\s+([?.!,])", r"\1", s)
    # capitalize sentence starts (very naive)
    parts = re.split("([.?!]\s+)", s)
    s = "".join(p.capitalize() for p in parts)
    return s

# Helper to fetch a message by ID or link
async def fetch_message_from_identifier(interaction: discord.Interaction, identifier: Optional[str]) -> Optional[discord.Message]:
    # If identifier is None, try to get referenced message if command was used as a reply (context menu covers this better)
    if identifier:
        # try message link pattern
        m = re.search(r"/channels/\d+/(\d+)/(\d+)$", identifier)
        try:
            if m:
                guild_id = int(m.group(1))
                channel_id = int(m.group(1))  # sometimes different formats; try robust parse
            # try numeric id fallback
            if identifier.isdigit():
                # search in current channel
                try:
                    msg = await interaction.channel.fetch_message(int(identifier))
                    return msg
                except Exception:
                    pass
            # try message link like https://discord.com/channels/guild_id/channel_id/message_id
            link_match = re.search(r"channels/(\d+)/(\d+)/(\d+)", identifier)
            if link_match:
                ch_id = int(link_match.group(2))
                msg_id = int(link_match.group(3))
                channel = interaction.guild.get_channel(ch_id) or await interaction.client.fetch_channel(ch_id)
                return await channel.fetch_message(msg_id)
        except Exception:
            pass
    # No identifier or failed: try to use the replied message if present (works for context menu or when button used)
    if interaction.data and "resolved" in interaction.data:
        # sometimes slash commands don't include resolved messages; skip
        pass
    # Last resort: try to get the last message before interaction in the channel
    try:
        async for m in interaction.channel.history(limit=10, before=interaction.message.created_at if interaction.message else None):
            # choose first non-bot message
            if not m.author.bot:
                return m
    except Exception:
        pass
    return None

# --- Context menu for message editing (works when you right-click a message -> Apps -> Edit with AI)
@tree.context_menu(name="Edit message (AI)")
async def context_edit(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(thinking=True)
    if message.author.bot:
        await interaction.followup.send("I won't edit bot messages.")
        return

    if not await is_content_safe(message.content):
        await interaction.followup.send("The message contains disallowed content and cannot be edited.")
        return

    repaired = await run_language_tool_check(message.content, is_article=False)

    # Send the "ChatGPT style" confirmation
    reply_text = f"Sure! I'll edit that message for typos and grammar and post the improved version below."
    await interaction.followup.send(reply_text)
    # post improved text as a reply
    await interaction.channel.send(f"**Edited (by {interaction.user.display_name}):**\n{repaired}")

# /edit slash command (message_id or link optional)
@tree.command(name="edit", description="Edit a message for typos/grammar. Use as context menu or provide a message link/ID.")
@app_commands.describe(message_identifier="Message link or ID (optional). If omitted, the bot will try to infer a recent message.")
async def slash_edit(interaction: discord.Interaction, message_identifier: Optional[str] = None):
    await interaction.response.defer(thinking=True)
    msg = await fetch_message_from_identifier(interaction, message_identifier)
    if not msg:
        await interaction.followup.send("Couldn't find the target message. You can use the message context menu (right-click message -> Apps -> Edit message (AI)) or provide a message link/ID.")
        return
    if msg.author.bot:
        await interaction.followup.send("I won't edit bot messages.")
        return
    if not await is_content_safe(msg.content):
        await interaction.followup.send("The target message contains disallowed content and cannot be edited.")
        return
    repaired = await run_language_tool_check(msg.content, is_article=False)
    await interaction.followup.send(f"Sure! I'll edit that message for typos and grammar and post the improved version below.")
    await interaction.channel.send(f"**Edited (by {interaction.user.display_name}):**\n{repaired}")

# Context menu + slash for editarticle (longer rewrite)
@tree.context_menu(name="Edit article (AI)")
async def context_edit_article(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(thinking=True)
    if message.author.bot:
        await interaction.followup.send("I won't edit bot messages.")
        return
    if not await is_content_safe(message.content):
        await interaction.followup.send("The message contains disallowed content and cannot be edited.")
        return
    repaired = await run_language_tool_check(message.content, is_article=True)
    await interaction.followup.send("I'll rewrite the article for clarity, flow, and grammar and post it below.")
    await interaction.channel.send(f"**Article rewrite (by {interaction.user.display_name}):**\n{repaired}")

@tree.command(name="editarticle", description="Rewrite an article for clarity and grammar. Use as context menu or give message link/ID.")
@app_commands.describe(message_identifier="Message link or ID (optional).")
async def slash_edit_article(interaction: discord.Interaction, message_identifier: Optional[str] = None):
    await interaction.response.defer(thinking=True)
    msg = await fetch_message_from_identifier(interaction, message_identifier)
    if not msg:
        await interaction.followup.send("Couldn't find the target message. Use the message context menu or provide a message link/ID.")
        return
    if msg.author.bot:
        await interaction.followup.send("I won't edit bot messages.")
        return
    if not await is_content_safe(msg.content):
        await interaction.followup.send("The target message contains disallowed content and cannot be edited.")
        return
    repaired = await run_language_tool_check(msg.content, is_article=True)
    await interaction.followup.send("I'll rewrite the article for clarity, flow, and grammar and post it below.")
    await interaction.channel.send(f"**Article rewrite (by {interaction.user.display_name}):**\n{repaired}")

# Advertise book
@tree.command(name="advertisebook", description="Post the promotional embed for the book.")
async def advertise_book(interaction: discord.Interaction):
    await interaction.response.defer()
    book_url = "https://a.co/d/7cKhbC7"
    embed = discord.Embed(
        title="Recommended Read",
        description="Check out this book — a great pick for readers!",
        url=book_url,
        color=discord.Color.blurple()
    )
    embed.set_thumbnail(url="https://m.media-amazon.com/images/I/51-example.jpg")  # placeholder
    embed.add_field(name="Buy it here", value=book_url)
    embed.set_footer(text="Promoted")
    await interaction.followup.send(embed=embed)

# Advertise logos company
@tree.command(name="advertiselogos", description="Post the promotional embed for logoscompany.store")
async def advertise_logos(interaction: discord.Interaction):
    await interaction.response.defer()
    company_url = "https://logoscompany.store"
    embed = discord.Embed(
        title="LogosCompany — Custom Logos & Branding",
        description="Professional logo design and branding at LogosCompany. Visit their shop for templates and custom design.",
        url=company_url,
        color=discord.Color.green()
    )
    embed.add_field(name="Shop", value=company_url)
    embed.set_footer(text="Promoted")
    await interaction.followup.send(embed=embed)

# Moderation helpers
def make_chatgpt_confirmation(action: str, target: str, reason: Optional[str]):
    r = reason or "no reason provided"
    return f"Sure! I’ll {action} {target} immediately for **{r}**."

@tree.command(name="kick", description="Kick a user. Provide a reason if you want.")
@app_commands.describe(member="Member to kick", reason="Reason for kick (optional)")
async def cmd_kick(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
    # permission checks
    if not interaction.user.guild_permissions.kick_members and interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message("You don't have permission to kick members.", ephemeral=True)
        return
    confirm = make_chatgpt_confirmation("kick", member.display_name, reason)
    await interaction.response.send_message(confirm)
    await asyncio.sleep(1)  # tiny delay to mimic conversational confirmation
    try:
        await member.kick(reason=reason)
        await interaction.channel.send(f"{member.mention} has been kicked. Reason: {reason or 'No reason provided.'}")
    except Exception as e:
        await interaction.channel.send(f"Failed to kick {member.display_name}: {e}")

@tree.command(name="ban", description="Ban a user. Provide a reason if you want.")
@app_commands.describe(member="Member to ban", reason="Reason for ban (optional)")
async def cmd_ban(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
    if not interaction.user.guild_permissions.ban_members and interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message("You don't have permission to ban members.", ephemeral=True)
        return
    confirm = make_chatgpt_confirmation("ban", member.display_name, reason)
    await interaction.response.send_message(confirm)
    await asyncio.sleep(1)
    try:
        await member.ban(reason=reason)
        await interaction.channel.send(f"{member.mention} has been banned. Reason: {reason or 'No reason provided.'}")
    except Exception as e:
        await interaction.channel.send(f"Failed to ban {member.display_name}: {e}")

@tree.command(name="timeout", description="Put a user in timeout for a number of minutes.")
@app_commands.describe(member="Member to timeout", minutes="Duration in minutes (0 to remove timeout)", reason="Reason (optional)")
async def cmd_timeout(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: Optional[str] = None):
    if not interaction.user.guild_permissions.moderate_members and interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message("You don't have permission to timeout members.", ephemeral=True)
        return
    if minutes < 0 or minutes > 40320:  # Discord max ~28 days in minutes (40,320)
        await interaction.response.send_message("Minutes must be between 0 and 40320.", ephemeral=True)
        return
    confirm = make_chatgpt_confirmation(f"timeout for {minutes} minutes", member.display_name, reason)
    await interaction.response.send_message(confirm)
    await asyncio.sleep(1)
    try:
        if minutes == 0:
            await member.timeout(None, reason=reason)
            await interaction.channel.send(f"{member.mention} timeout removed. Reason: {reason or 'No reason provided.'}")
        else:
            until = datetime.utcnow() + timedelta(minutes=minutes)
            await member.timeout(until, reason=reason)
            await interaction.channel.send(f"{member.mention} has been timed out for {minutes} minutes. Reason: {reason or 'No reason provided.'}")
    except Exception as e:
        await interaction.channel.send(f"Failed to timeout {member.display_name}: {e}")

# Chatbot reply behavior: when someone replies to the bot, respond in chat
@bot.event
async def on_message(message: discord.Message):
    # Always let other commands process
    await bot.process_commands(message)
    # Ignore self
    if message.author.bot:
        return
    # If this message is a reply to a bot message, we'll treat as chatbot input
    if message.reference and isinstance(message.reference.resolved, discord.Message):
        ref = message.reference.resolved
        if ref.author.id == bot.user.id:
            # user is talking to the bot
            user_text = message.content
            if not await is_content_safe(user_text):
                await message.channel.send("Sorry — I can't respond to that content.")
                return
            # save to history
            save_message_to_history(message.channel.id, "user", user_text)
            # create assistant prompt
            history = load_history_for_channel(message.channel.id, limit=10)
            system_prompt = "You are a friendly, helpful assistant. Always be polite and avoid profanity."
            if OPENAI_API_KEY and openai:
                # Call OpenAI ChatCompletion (gpt-3.5-turbo-like). This is optional and only runs if OPENAI_API_KEY is provided.
                try:
                    messages = [{"role":"system", "content": system_prompt}]
                    for role, content in history:
                        messages.append({"role": role, "content": content})
                    messages.append({"role":"user", "content": user_text})
                    resp = openai.ChatCompletion.create(model="gpt-3.5-turbo", messages=messages, max_tokens=400)
                    reply_text = resp["choices"][0]["message"]["content"].strip()
                except Exception:
                    reply_text = "Sorry — I'm having trouble accessing the AI service right now. Please try again later."
            else:
                # Fallback basic response generator (naive)
                reply_text = f"You said: {user_text}\n\n(You can enable smarter replies by adding OPENAI_API_KEY to your .env.)"
            # Filter final reply (again)
            if not await is_content_safe(reply_text):
                reply_text = "Sorry — I can't share that content."
            # Save assistant message
            save_message_to_history(message.channel.id, "assistant", reply_text)
            await message.reply(reply_text)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Syncing application commands...")
    # Try to sync globally; could be slow to propagate. You can also restrict to a dev guild for faster updates.
    try:
        await tree.sync()
        print("Commands synced.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

# Run
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing from environment variables (.env).")

bot.run(DISCORD_TOKEN)
