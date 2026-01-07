import discord
from discord.ext import commands
from dotenv import load_dotenv
import os, time, sqlite3, datetime, pytz, re
from collections import defaultdict, deque

# ================= ENV =================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ================= DATABASE =================
db = sqlite3.connect("moderation.db", check_same_thread=False)
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    log_channel_id INTEGER,
    ticket_worker_role_id INTEGER,
    member_role_id INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS guild_features (
    guild_id INTEGER PRIMARY KEY,
    ai_mod INTEGER DEFAULT 1,
    image_spam INTEGER DEFAULT 1,
    raid_detection INTEGER DEFAULT 1,
    auto_role INTEGER DEFAULT 1,
    tickets INTEGER DEFAULT 1
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS tickets (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER,
    opener_id INTEGER,
    claimer_id INTEGER
)
""")

db.commit()

# ================= STATE =================
state = defaultdict(lambda: {
    "raid": False,
    "joins": deque(),
    "activity": defaultdict(deque),
    "recent": defaultdict(deque),
    "images": defaultdict(deque),
    "offences": defaultdict(deque)
})

# ================= HELPERS =================
def uk_time():
    uk = pytz.timezone("Europe/London")
    return datetime.datetime.now(uk).strftime("%Y-%m-%d %H:%M:%S")

def get_config(gid):
    cursor.execute("SELECT * FROM guild_config WHERE guild_id=?", (gid,))
    r = cursor.fetchone()
    if not r:
        return None
    return {"log": r[1], "ticket": r[2], "member": r[3]}

def get_features(gid):
    cursor.execute("SELECT * FROM guild_features WHERE guild_id=?", (gid,))
    r = cursor.fetchone()
    if not r:
        cursor.execute("INSERT INTO guild_features (guild_id) VALUES (?)", (gid,))
        db.commit()
        return get_features(gid)
    return {
        "ai": bool(r[1]),
        "images": bool(r[2]),
        "raid": bool(r[3]),
        "auto": bool(r[4]),
        "tickets": bool(r[5])
    }

async def log_action(guild, title, desc):
    cfg = get_config(guild.id)
    if not cfg:
        return
    ch = guild.get_channel(cfg["log"])
    if ch:
        embed = discord.Embed(
            title=f"{title} | {uk_time()} UK",
            description=desc,
            color=discord.Color.blurple()
        )
        await ch.send(embed=embed)

# ================= AI =================
def ai_score(msg):
    g, u, now = msg.guild.id, msg.author.id, time.time()
    s = state[g]
    score = 0

    s["activity"][u].append(now)
    s["activity"][u] = deque(t for t in s["activity"][u] if now - t < 30)
    if len(s["activity"][u]) > 6:
        score += 2

    s["recent"][u].append(msg.content)
    s["recent"][u] = deque(list(s["recent"][u])[-5:])
    if s["recent"][u].count(msg.content) >= 3:
        score += 3

    if re.search(r"https?://", msg.content):
        score += 2

    if state[g]["raid"]:
        score += 1

    return score

# ================= EVENTS =================
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")

@bot.event
async def on_member_join(member):
    f = get_features(member.guild.id)
    if not f["raid"]:
        return

    now = time.time()
    joins = state[member.guild.id]["joins"]
    joins.append(now)
    state[member.guild.id]["joins"] = deque(t for t in joins if now - t < 60)

    if len(state[member.guild.id]["joins"]) >= 5:
        state[member.guild.id]["raid"] = True
        await log_action(member.guild, "🚨 RAID MODE", "High join rate")

    if f["auto"]:
        cfg = get_config(member.guild.id)
        role = member.guild.get_role(cfg["member"])
        if role:
            await member.add_roles(role)

@bot.event
async def on_message(msg):
    if msg.author.bot or not msg.guild:
        return

    f = get_features(msg.guild.id)

    if f["ai"] and ai_score(msg) >= 4:
        await log_action(msg.guild, "🤖 AI Flag", msg.author.mention)

    if f["images"] and msg.attachments:
        g, u, now = msg.guild.id, msg.author.id, time.time()
        imgs = state[g]["images"][u]
        imgs.append(now)
        state[g]["images"][u] = deque(t for t in imgs if now - t < 600)
        if len(state[g]["images"][u]) >= 5:
            await msg.author.timeout(
                discord.utils.utcnow() + discord.timedelta(minutes=10),
                reason="Image spam"
            )
            state[g]["images"][u].clear()

    await bot.process_commands(msg)

# ================= SETUP =================
@bot.tree.command(name="setup")
async def setup(i: discord.Interaction,
    log_channel: discord.TextChannel,
    ticket_role: discord.Role,
    member_role: discord.Role
):
    if not i.user.guild_permissions.administrator:
        return await i.response.send_message("Admin only", ephemeral=True)

    cursor.execute(
        "INSERT OR REPLACE INTO guild_config VALUES (?, ?, ?, ?)",
        (i.guild.id, log_channel.id, ticket_role.id, member_role.id)
    )
    db.commit()
    await i.response.send_message("✅ Setup complete", ephemeral=True)

# ================= TICKETS =================
@bot.tree.command(name="ticket_open")
async def ticket_open(i: discord.Interaction, reason: str):
    cfg = get_config(i.guild.id)
    role = i.guild.get_role(cfg["ticket"])

    overwrites = {
        i.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        i.user: discord.PermissionOverwrite(view_channel=True),
        role: discord.PermissionOverwrite(view_channel=True)
    }

    ch = await i.guild.create_text_channel(
        f"ticket-{i.user.name}",
        overwrites=overwrites
    )

    cursor.execute(
        "INSERT INTO tickets VALUES (?, ?, ?, NULL)",
        (ch.id, i.guild.id, i.user.id)
    )
    db.commit()

    await ch.send(f"🎟 Ticket opened by {i.user.mention}\nReason: {reason}")
    await i.response.send_message("Ticket created", ephemeral=True)

@bot.tree.command(name="ticket_claim")
async def ticket_claim(i: discord.Interaction):
    cursor.execute("SELECT claimer_id FROM tickets WHERE channel_id=?", (i.channel.id,))
    r = cursor.fetchone()
    if not r or r[0]:
        return await i.response.send_message("Already claimed / not ticket", ephemeral=True)

    cursor.execute(
        "UPDATE tickets SET claimer_id=? WHERE channel_id=?",
        (i.user.id, i.channel.id)
    )
    db.commit()

    await i.channel.send(f"🛠 Claimed by {i.user.mention}")
    await i.response.send_message("Claimed", ephemeral=True)

@bot.tree.command(name="ticket_unclaim")
async def ticket_unclaim(i: discord.Interaction):
    cursor.execute("SELECT claimer_id FROM tickets WHERE channel_id=?", (i.channel.id,))
    r = cursor.fetchone()
    if not r or r[0] != i.user.id:
        return await i.response.send_message("Not yours", ephemeral=True)

    cursor.execute(
        "UPDATE tickets SET claimer_id=NULL WHERE channel_id=?",
        (i.channel.id,)
    )
    db.commit()

    await i.channel.send("🔓 Unclaimed")
    await i.response.send_message("Unclaimed", ephemeral=True)

@bot.tree.command(name="ticket_close")
async def ticket_close(i: discord.Interaction):
    cursor.execute("DELETE FROM tickets WHERE channel_id=?", (i.channel.id,))
    db.commit()
    await i.channel.delete()

# ================= RUN =================
bot.run(TOKEN)
