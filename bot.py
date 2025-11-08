# bot.py
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

import os, asyncio, logging, discord
from typing import Literal
import os
import asyncio
import aiosqlite
from discord import app_commands
from discord.ext import commands
import gspread
from google.oauth2.service_account import Credentials
import random
from datetime import datetime

BLOODBORNE_RED = 0x7A0A0A  # deep, grim red
DIVIDER = "┄┄┄┄┄┄┄┄┄┄"      # thin gothic line

HUNTER_QUOTES = [
    "A hunter is never alone.",
    "Fear the old blood.",
    "We are born of the blood, made men by the blood.",
    "May you find your worth in the waking world.",
    "Tonight, Gehrman joins the hunt.",
    "The moon is close. It will be a long hunt tonight."
]

SPREADSHEET_ID = "1XBU-RPTLbsomlxjdc14SdkwWpn0lPa0lenTWUZjoaMs"
BASE_SHEET_TITLE = "Character Sheet"  # template tab to duplicate
GOOGLE_CREDS_FILE = os.getenv("GOOGLE_CREDENTIALS_JSON")  # path to service account json

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("yakbot")

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # for !prime (text) command

class BloodborneCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Google Sheets client can be created here (sync)
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
        self.gc = gspread.authorize(creds)
        self.sh = self.gc.open_by_key(SPREADSHEET_ID)

        self.db_path = "bb_cache.sqlite"
        self.ready = False  # will flip in cog_load()

    async def cog_load(self):
        """Async hook called when the cog is loaded (discord.py 2.x)."""
        await self._init_db()
        self.ready = True

    async def _init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
            CREATE TABLE IF NOT EXISTS builds (
              user_id TEXT PRIMARY KEY,
              vitality INTEGER,
              endurance INTEGER,
              strength INTEGER,
              skill INTEGER,
              bloodtinge INTEGER,
              arcane INTEGER,
              results_json TEXT,       -- JSON of {label:value}
              sheet_tab TEXT           -- u_<discord_id>
            )
            """)
            await db.commit()
        self.ready = True

    # ---------- helpers ----------

    def _user_tab_name(self, user_id: int) -> str:
        return f"u_{user_id}"

    def _ensure_user_tab(self, user_id: int):
        """
        Make sure the user has a personal worksheet cloned from BASE_SHEET_TITLE.
        """
        tab_name = self._user_tab_name(user_id)
        try:
            ws = self.sh.worksheet(tab_name)
            return ws  # already exists
        except gspread.exceptions.WorksheetNotFound:
            pass

        # Get the base/template worksheet to duplicate
        base_ws = self.sh.worksheet(BASE_SHEET_TITLE)
        duplicated = self.sh.duplicate_sheet(
            source_sheet_id=base_ws.id,
            new_sheet_name=tab_name,
            insert_sheet_index=None
        )
        # Re-fetch as a Worksheet object
        return self.sh.worksheet(tab_name)

    def _write_inputs(self, ws, vit, end, strn, skl, bld, arc):
        # L8-L13 in the same order
        values = [[vit], [end], [strn], [skl], [bld], [arc]]
        ws.update("L8:L13", values, value_input_option="USER_ENTERED")

    def _read_outputs(self, ws):
        # Labels M2:M13, Values O2:O13
        labels = [row[0] for row in ws.get("M2:M13")]
        values = [row[0] for row in ws.get("O2:O13")]
        # Normalize lengths if needed
        pairs = []
        for i in range(min(len(labels), len(values))):
            pairs.append((labels[i], values[i]))
        return pairs

    async def _save_cache(self, user_id: int, stats, pairs, tab_name):
        import json
        (vit, end, strn, skl, bld, arc) = stats
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
            INSERT INTO builds(user_id, vitality, endurance, strength, skill, bloodtinge, arcane, results_json, sheet_tab)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              vitality=excluded.vitality,
              endurance=excluded.endurance,
              strength=excluded.strength,
              skill=excluded.skill,
              bloodtinge=excluded.bloodtinge,
              arcane=excluded.arcane,
              results_json=excluded.results_json,
              sheet_tab=excluded.sheet_tab
            """, (str(user_id), vit, end, strn, skl, bld, arc, json.dumps(dict(pairs)), tab_name))
            await db.commit()

    async def _load_cache(self, user_id: int):
        import json
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT vitality,endurance,strength,skill,bloodtinge,arcane,results_json,sheet_tab FROM builds WHERE user_id=?", (str(user_id),)) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        vit, end, strn, skl, bld, arc, res_json, tab = row
        return {
            "inputs": (vit, end, strn, skl, bld, arc),
            "results": json.loads(res_json),
            "tab": tab
        }

    def _embed_from_results(self, author: discord.abc.User, inputs, pairs):
        def _norm_label(label: str) -> str:
            # Remove trailing colon, trim spaces, lowercase for stable lookups
            base = label.split(":", 1)[0].strip()
            return base.casefold()

        vit, end, strn, skl, bld, arc = inputs

        emoji_map = {
            # keys MUST be normalized with _norm_label(...) as below
            _norm_label("Health"): "❤️",
            _norm_label("Health (Phantom)"): "👻",
            _norm_label("Stamina"): "⚡",
            _norm_label("Stamina/Second"): "🔁",
            _norm_label("Discovery"): "🔍",
            _norm_label("Defense"): "🛡️",
            _norm_label("Slow Poison Resist"): "☠️",
            _norm_label("Rapid Poison Resist"): "💀",
            _norm_label("Frenzy Resist"): "🧠",
            _norm_label("Beasthood"): "🐺",
            _norm_label("Max Vials"): "💉",
            _norm_label("Max Bullets"): "🔫",
        }

        em = discord.Embed(
            title="🩸 Bloodborne — Hunter Build",
            description=f"{DIVIDER}\nStats calculated from your **Character Sheet** sheet.\n{DIVIDER}",
            color=BLOODBORNE_RED
        )
        em.timestamp = datetime.utcnow()

        em.set_author(name=author.display_name, icon_url=author.display_avatar.url)

        # Inputs (stacked, easier to scan)
        em.add_field(
            name="🎯 Input Attributes",
            value=(
                f"**Vitality:** `{vit}`\n"
                f"**Endurance:** `{end}`\n"
                f"**Strength:** `{strn}`\n"
                f"**Skill:** `{skl}`\n"
                f"**Bloodtinge:** `{bld}`\n"
                f"**Arcane:** `{arc}`"
            ),
            inline=False
        )

        # Format outputs with emoji and split into columns
        unknown_labels = []
        formatted = []
        for raw_label, val in pairs:
            key = _norm_label(raw_label)   # <— normalize “Health:” → “health”
            emoji = emoji_map.get(key)
            if not emoji:
                unknown_labels.append(raw_label)
                emoji = "▫️"               # graceful fallback
            # Show the label exactly as it appears in the sheet (colons and all)
            formatted.append(f"{emoji} **{raw_label}**: `{val}`")

        mid = (len(formatted) + 1) // 2
        left = "\n".join(formatted[:mid]) or "—"
        right = "\n".join(formatted[mid:]) or "—"

        em.add_field(name="🩸 Hunter Stats", value=left, inline=True)
        em.add_field(name="\u200b", value=right, inline=True)

        # Optional: print unknowns once so you can extend the map if the sheet changes
        if unknown_labels:
            print("[BB] Unmapped labels from sheet:", unknown_labels)

        em.set_footer(text=random.choice(HUNTER_QUOTES))
        return em

    # ---------- slash commands ----------

    @app_commands.command(name="bb_set", description="Compute your Bloodborne build from your stats.")
    @app_commands.describe(
        vitality="Vitality (int)",
        endurance="Endurance (int)",
        strength="Strength (int)",
        skill="Skill (int)",
        bloodtinge="Bloodtinge (int)",
        arcane="Arcane (int)"
    )
    async def bb_set(
        self,
        interaction: discord.Interaction,
        vitality: int,
        endurance: int,
        strength: int,
        skill: int,
        bloodtinge: int,
        arcane: int
    ):
        await interaction.response.defer(ephemeral=False, thinking=True)
        if not self.ready:
            return await interaction.followup.send("DB is still initializing. Try again in a moment.")

        # 1) Ensure user tab
        ws = self._ensure_user_tab(interaction.user.id)

        # 2) Write inputs
        self._write_inputs(ws, vitality, endurance, strength, skill, bloodtinge, arcane)

        # 3) Read outputs
        pairs = self._read_outputs(ws)

        # 4) Save cache
        await self._save_cache(
            user_id=interaction.user.id,
            stats=(vitality, endurance, strength, skill, bloodtinge, arcane),
            pairs=pairs,
            tab_name=ws.title
        )

        # 5) Pretty embed
        embed = self._embed_from_results(interaction.user, (vitality, endurance, strength, skill, bloodtinge, arcane), pairs)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="bb_show", description="Show your last computed Bloodborne build.")
    @app_commands.describe(fresh="Recompute from Sheets instead of showing cached result.")
    async def bb_show(self, interaction: discord.Interaction, fresh: bool=False):
        await interaction.response.defer(ephemeral=False, thinking=True)
        if not self.ready:
            return await interaction.followup.send("DB is still initializing. Try again in a moment.")

        cached = await self._load_cache(interaction.user.id)
        if not cached and not fresh:
            return await interaction.followup.send("No cached build yet. Use `/bb_set` first.")

        if fresh or not cached:
            # Force re-pull from their tab
            ws = self._ensure_user_tab(interaction.user.id)
            # Read inputs from L8-L13 to display in the embed
            inputs = [int(v[0]) for v in ws.get("L8:L13")]
            pairs = self._read_outputs(ws)
            await self._save_cache(interaction.user.id, tuple(inputs), pairs, ws.title)
        else:
            inputs = cached["inputs"]
            pairs = list(cached["results"].items())

        embed = self._embed_from_results(interaction.user, inputs, pairs)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="bb_delete", description="Delete your personal sheet tab & cached build.")
    async def bb_delete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cached = await self._load_cache(interaction.user.id)
        if cached:
            # Try delete worksheet
            try:
                ws = self.sh.worksheet(cached["tab"])
                self.sh.del_worksheet(ws)
            except Exception:
                pass
            # Delete cache row
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM builds WHERE user_id=?", (str(interaction.user.id),))
                await db.commit()

            return await interaction.followup.send("Your Bloodborne sheet tab and cache have been deleted.")
        else:
            return await interaction.followup.send("No personal tab/cache found.")

# ---------- Admin/owner checks ----------
def owner_or_admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild and interaction.user.guild_permissions.administrator:
            return True
        try:
            return await interaction.client.is_owner(interaction.user)
        except Exception:
            return False
    return app_commands.check(predicate)

def owner_or_admin_ctx(ctx: commands.Context) -> bool:
    if ctx.guild and ctx.author.guild_permissions.administrator:
        return True
    return ctx.bot.is_owner(ctx.author)

# ---------- Debug helper ----------
def dump_tree(tree: app_commands.CommandTree, where: str):
    cmds = tree.get_commands()
    names = ", ".join(f"/{getattr(c, 'qualified_name', getattr(c, 'name', str(c)))}" for c in cmds) or "—none—"
    log.info(f"[{where}] Local CommandTree has {len(cmds)}: {names}")

# ---------- Bootstrap prefix command (!prime) ----------
class AdminBootstrap(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="prime", help="(Owner/Admin) One-time: copy local commands into THIS guild and sync.")
    @commands.check(owner_or_admin_ctx)
    async def prime(self, ctx: commands.Context):
        if ctx.guild is None:
            return await ctx.reply("Run this in a server channel.")
        tree = self.bot.tree
        dump_tree(tree, "before prime")
        await ctx.reply("Copying local definitions to this guild and syncing…", mention_author=False)
        try:
            tree.clear_commands(guild=ctx.guild)   # DO NOT await
            tree.copy_global_to(guild=ctx.guild)
            synced = await tree.sync(guild=ctx.guild)
            names = ", ".join(f"/{getattr(c, 'qualified_name', getattr(c, 'name', str(c)))}" for c in synced) or "—none—"
            await ctx.send(f"✅ Bootstrapped {len(synced)} commands to **{ctx.guild.name}**: {names}")
            fetched = await tree.fetch_commands(guild=ctx.guild)
            names_f = ", ".join(f"/{getattr(c, 'qualified_name', getattr(c, 'name', str(c)))}" for c in fetched) or "—none—"
            await ctx.send(f"📥 Discord reports {len(fetched)} guild commands installed: {names_f}")
        except Exception as e:
            await ctx.send(f"❌ Bootstrap sync failed: `{e}`")

# ---------- Admin slash: /bb_sync ----------
class AdminSync(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="bb_sync", description="Register/refresh slash commands.")
    @app_commands.describe(scope="guild (fast), global (slow), or copy_global_to_guild")
    @owner_or_admin_check()
    async def bb_sync(self, interaction: discord.Interaction,
                      scope: Literal["guild", "global", "copy_global_to_guild"] = "guild"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        tree = interaction.client.tree
        try:
            if scope == "guild":
                if not interaction.guild:
                    return await interaction.followup.send("Run in a server for guild sync.")
                synced = await tree.sync(guild=interaction.guild)
                return await interaction.followup.send(f"✅ Synced {len(synced)} commands to this guild.")
            elif scope == "copy_global_to_guild":
                if not interaction.guild:
                    return await interaction.followup.send("Run in a server.")
                tree.copy_global_to(guild=interaction.guild)
                synced = await tree.sync(guild=interaction.guild)
                return await interaction.followup.send(f"✅ Copied globals and synced {len(synced)} commands to this guild.")
            else:
                synced = await tree.sync()
                return await interaction.followup.send(f"🌍 Globally synced {len(synced)} commands. (Propagation can take minutes.)")
        except Exception as e:
            return await interaction.followup.send(f"❌ Sync failed: `{e}`")

# ---------- Simple slash: /ping ----------
class Health(commands.Cog):
    def __init__(self, bot): self.bot = bot
    @app_commands.command(name="ping", description="Health check")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message("Pong! 🩸")

# ---------- Bot subclass ----------
class YakBot(commands.Bot):
    async def setup_hook(self):
        # *** IMPORTANT: add cogs WITH slash commands here ***
        await self.add_cog(AdminBootstrap(self))  # !prime
        await self.add_cog(AdminSync(self))       # /bb_sync
        await self.add_cog(Health(self))          # /ping
        await self.add_cog(BloodborneCog(self))  # add this line

        dump_tree(self.tree, "after setup_hook")  # should show /ping and /bb_sync

        # Optional one-time bootstrap without !prime:
        # If you prefer env-triggered sync once, set SYNC_ON_BOOT=1 and DEV_GUILD_ID.
        if os.getenv("SYNC_ON_BOOT") == "1":
            gid = int(os.getenv("DEV_GUILD_ID", "0"))
            if gid:
                log.info(f"[bootstrap] Syncing to guild {gid} …")
                synced = await self.tree.sync(guild=discord.Object(id=gid))
                log.info(f"[bootstrap] Synced {len(synced)} commands to guild.")
            else:
                log.info("[bootstrap] Global sync …")
                synced = await self.tree.sync()
                log.info(f"[bootstrap] Synced {len(synced)} global commands.")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")

async def main():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set.")
    bot = YakBot(command_prefix="!", intents=INTENTS)
    await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
