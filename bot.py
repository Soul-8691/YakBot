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

    # ===== Gear data sources =====

    def _get_rune_choices(self) -> list[str]:
        """Rune names from 'Rune Data'!A2:A67."""
        ws = self.sh.worksheet("Rune Data")
        col = ws.get("A2:A67")
        return [r[0] for r in col if r and r[0]]

    def _get_oath_choices(self) -> list[str]:
        """Oath names from 'Rune Data'!A68:A73."""
        ws = self.sh.worksheet("Rune Data")
        col = ws.get("A68:A73")
        return [r[0] for r in col if r and r[0]]

    def _get_armor_choices(self) -> list[str]:
        """Armor piece names from 'Armor Data'!A2:A131."""
        ws = self.sh.worksheet("Armor Data")
        col = ws.get("A2:A131")
        return [r[0] for r in col if r and r[0]]


    # ===== Set gear on a user's personal worksheet =====

    def _set_gear(self, ws, rune1: str | None, rune2: str | None, rune3: str | None,
                oath: str | None, head: str | None, chest: str | None,
                arms: str | None, legs: str | None):
        """
        Write runes/oath/armor to Q2:Q9 on the user's worksheet.
        Q2-4 = runes 1..3, Q5=oath, Q6-9=head,chest,arms,legs (in that order).
        Accepts None to "leave as-is" (writes current cell back).
        """
        # Read existing values so None leaves the current value untouched.
        current = ws.get("Q2:Q9")  # 8x1 (or [] if empty)
        def cur(i):
            try:
                return current[i][0]
            except Exception:
                return ""

        values = [
            [rune1 if rune1 is not None else cur(0)],
            [rune2 if rune2 is not None else cur(1)],
            [rune3 if rune3 is not None else cur(2)],
            [oath  if oath  is not None else cur(3)],
            [head  if head  is not None else cur(4)],
            [chest if chest is not None else cur(5)],
            [arms  if arms  is not None else cur(6)],
            [legs  if legs  is not None else cur(7)],
        ]
        ws.update("Q2:Q9", values, value_input_option="USER_ENTERED")

    def _emoji_for_header(self, name: str) -> str:
        n = (name or "").lower()
        # common patterns for stat columns
        if "base" in n: return "📘"
        if "bonus" in n or "mod" in n: return "➕"
        if "total" in n or "final" in n: return "🏁"
        if "def" in n or "defense" in n: return "🛡️"
        if "res" in n or "resist" in n: return "🧪"
        if "dmg" in n or "damage" in n: return "🗡️"
        return "📊"

    def _emoji_for_row(self, name: str) -> str:
        n = (name or "").lower()
        # broad Bloodborne-ish mapping; falls back to a dot if no match
        if "physical" in n: return "⚔️"
        if "blood" in n: return "🩸"
        if "arcane" in n: return "✨"
        if "fire" in n: return "🔥"
        if "bolt" in n or "thunder" in n: return "⚡"
        if "poison" in n: return "☠️"
        if "frenzy" in n: return "🌀"
        if "beast" in n: return "🐺"
        if "hp" in n or "vital" in n: return "❤️"
        if "stamina" in n or "endur" in n: return "💨"
        if "def" in n: return "🛡️"
        if "resist" in n: return "🧪"
        return "•"

    def _format_table_embed_sections(self, headers: list[str], row_names: list[str], data: list[list[str]]):
        """
        Build 3 inline embed fields, one for each header column in U3:W3.
        Values are rendered as emoji-labeled rows using names from T4:T11.
        Returns: list[tuple[name, value, inline_bool]]
        """
        # ensure exactly 3 columns
        cols = 3
        for i in range(len(data)):
            data[i] = (data[i] + [""] * cols)[:cols]
        if len(headers) < cols:
            headers = headers + [f"Col {i+1}" for i in range(len(headers), cols)]

        # compose each column text as lines
        fields = []
        for j in range(cols):
            h_emoji = self._emoji_for_header(headers[j])
            lines = []
            for r, rn in enumerate(row_names):
                emoji = self._emoji_for_row(rn)
                cell = data[r][j] if r < len(data) and j < len(data[r]) else ""
                # Format: ":emoji: Row Name — Value"
                line = f"{emoji} {rn}: **{cell}**" if cell != "" else f"{emoji} {rn}: —"
                lines.append(line)
            value = "\n".join(lines)
            fields.append((f"{h_emoji} {headers[j]}", value, True))
        return fields

    # ===== Read the computed table and format it for an embed =====

    def _read_gear_table(self, ws):
        """
        Returns headers (list[str]), row_names (list[str]), data (list[list[str]])
        from U3:W3 (headers), T4:T11 (row names), U4:W11 (table).
        """
        headers_row = []
        headers_row.append(ws.get("T2")[0][0])
        headers_row.append(ws.get("V2")[0][0])
        headers_row.append(ws.get("W2")[0][0])
        headers_row = [headers_row]
        headers = headers_row[0] if headers_row else []
        row_names_col = ws.get("T4:T11") or []
        row_names = [r[0] if r else "" for r in row_names_col]
        data = ws.get("U4:W11") or []
        # Normalize row lengths to exactly 3 columns
        data = [row + [""] * (3 - len(row)) for row in data]
        return headers, row_names, data

    def _format_table_monospace(self, headers: list[str], row_names: list[str], data: list[list[str]]) -> str:
        """
        Builds a neat monospace table as a code block for Discord embeds.
        """
        # Compute column widths
        name_w = max([len("Name")] + [len(n or "") for n in row_names])
        col_w = [0, 0, 0]
        for j in range(3):
            col_w[j] = max(len(headers[j] if len(headers) > j else f"Col{j+1}"),
                        *[len((r[j] if len(r) > j else "") or "") for r in data])

        # Header
        h_line = f"{'Name'.ljust(name_w)} | " + " | ".join([(headers[i] if i < len(headers) else f'Col{i+1}').ljust(col_w[i]) for i in range(3)])
        sep = "-" * len(h_line)

        # Rows
        rows = []
        for i, rn in enumerate(row_names):
            cells = data[i] if i < len(data) else ["", "", ""]
            row = f"{(rn or '').ljust(name_w)} | " + " | ".join([(cells[j] if j < len(cells) else "").ljust(col_w[j]) for j in range(3)])
            rows.append(row)

        body = "\n".join(rows)
        return f"```\n{h_line}\n{sep}\n{body}\n```"

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

    # import gspread at top if not already:
    # import gspread

    def _get_origin_choices(self) -> list[str]:
        """Read Origin names from 'Origin Data' B1:J1."""
        ws = self.sh.worksheet("Origin Data")
        row = ws.get("B1:J1")
        if not row:
            return []
        return [c for c in row[0] if c]

    def _set_origin(self, ws, origin: str):
        """
        Write the chosen Origin into the user's worksheet (cell J2, which may be merged with K2).
        `ws` is the user's personal worksheet, passed in from _ensure_user_tab().
        """
        import gspread
        try:
            # Use a 2D list in case the J2:K2 range is merged; this avoids 400 API errors.
            ws.update("J2:K2", [[origin, ""]], value_input_option="USER_ENTERED")
        except gspread.exceptions.APIError:
            # Fallback if single-cell write is needed (e.g. unmerged cell)
            ws.update_acell("J2", origin)

    def _read_level_and_origin(self, ws) -> tuple[str | None, str | None]:
        """Return (Level, Origin) from the user's worksheet."""
        try:
            level = ws.acell("J5").value
        except Exception:
            level = None
        try:
            origin = ws.acell("J2").value
        except Exception:
            origin = None
        return level, origin

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

    def _embed_from_results(self, author: discord.abc.User, inputs, pairs, level: str | None = None, origin: str | None = None):
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

        details = []
        if origin:
            details.append(f"**Origin:** {origin}")
        if level:
            details.append(f"**Level:** {level}")
        details_line = (" • ".join(details)) if details else "Stats calculated from your **Character Sheet** sheet."

        em = discord.Embed(
            title="🩸 Bloodborne — Hunter Build",
            description=f"{DIVIDER}\n{details_line}\n{DIVIDER}",
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

    @app_commands.command(
        name="bb_gear",
        description="Choose runes, oath, and armor; shows your selected inputs and computed gear table."
    )
    @app_commands.describe(
        rune1="Rune slot 1",
        rune2="Rune slot 2",
        rune3="Rune slot 3",
        oath="Oath",
        head="Head armor",
        chest="Chest armor",
        arms="Arm/Glove armor",
        legs="Leg armor"
    )
    async def bb_gear(
        self,
        interaction: discord.Interaction,
        rune1: str | None = None,
        rune2: str | None = None,
        rune3: str | None = None,
        oath: str | None = None,
        head: str | None = None,
        chest: str | None = None,
        arms: str | None = None,
        legs: str | None = None
    ):
        await interaction.response.defer(ephemeral=False)

        # Player's personal worksheet
        ws = self._ensure_user_tab(interaction.user.id)

        # Validate against sheet-driven choices
        runes_valid = set(self._get_rune_choices())
        oaths_valid = set(self._get_oath_choices())
        armor_valid = set(self._get_armor_choices())

        bad = []
        if rune1 is not None and rune1 not in runes_valid: bad.append(f"Rune1: {rune1}")
        if rune2 is not None and rune2 not in runes_valid: bad.append(f"Rune2: {rune2}")
        if rune3 is not None and rune3 not in runes_valid: bad.append(f"Rune3: {rune3}")
        if oath  is not None and oath  not in oaths_valid: bad.append(f"Oath: {oath}")
        if head  is not None and head  not in armor_valid: bad.append(f"Head: {head}")
        if chest is not None and chest not in armor_valid: bad.append(f"Chest: {chest}")
        if arms  is not None and arms  not in armor_valid: bad.append(f"Arms: {arms}")
        if legs  is not None and legs  not in armor_valid: bad.append(f"Legs: {legs}")

        if bad:
            await interaction.followup.send(
                "Some choices aren't valid:\n• " + "\n• ".join(bad),
                ephemeral=True
            )
            return

        # Write to Q2:Q9 (None = keep existing)
        self._set_gear(ws, rune1, rune2, rune3, oath, head, chest, arms, legs)

        # Re-read inputs so we show what’s actually set in the sheet
        q_values = ws.get("Q2:Q9")
        q_values = [q[0] if q else "" for q in q_values]
        rune1, rune2, rune3, oath, head, chest, arms, legs = (q_values + [""] * 8)[:8]

        # Compute table sections for embed fields
        headers, row_names, data = self._read_gear_table(ws)
        sections = self._format_table_embed_sections(headers, row_names, data)

        # Build a compact, emoji-forward embed
        em = discord.Embed(
            title="🩸 Bloodborne — Runes, Oath & Armor",
            description="Your selections and computed effects:",
            color=0x8A0303
        )

        # Selections block with emojis
        selections = (
            f"🧿 **Runes:** {rune1 or '—'} • {rune2 or '—'} • {rune3 or '—'}\n"
            f"🗳️ **Oath:** {oath or '—'}\n"
            f"🪖 **Head:** {head or '—'}\n"
            f"🛡️ **Chest:** {chest or '—'}\n"
            f"🧤 **Arms:** {arms or '—'}\n"
            f"🥾 **Legs:** {legs or '—'}"
        )
        user = interaction.user  # this gives you the Discord user object
        em = discord.Embed(
            title="🩸 Bloodborne — Runes, Oath & Armor",
            description="Your selections and computed effects:",
            color=0x8A0303
        )
        em.set_author(name=f"{user.display_name}", icon_url=user.display_avatar.url)
        em.add_field(name="Loadout", value=selections, inline=False)

        # Add the three computed columns as inline fields
        for name, value, inline in sections:
            # Prevent empty field bodies
            em.add_field(name=name, value=value or "—", inline=inline)

        await interaction.followup.send(embed=em)

    @bb_gear.autocomplete("rune1")
    @bb_gear.autocomplete("rune2")
    @bb_gear.autocomplete("rune3")
    async def bb_gear_rune_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = self._get_rune_choices()
        cur = (current or "").lower()
        filtered = [c for c in choices if cur in c.lower()] if cur else choices
        return [app_commands.Choice(name=c, value=c) for c in filtered[:25]]

    @bb_gear.autocomplete("oath")
    async def bb_gear_oath_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = self._get_oath_choices()
        cur = (current or "").lower()
        filtered = [c for c in choices if cur in c.lower()] if cur else choices
        return [app_commands.Choice(name=c, value=c) for c in filtered[:25]]

    @bb_gear.autocomplete("head")
    @bb_gear.autocomplete("chest")
    @bb_gear.autocomplete("arms")
    @bb_gear.autocomplete("legs")
    async def bb_gear_armor_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = self._get_armor_choices()
        cur = (current or "").lower()
        filtered = [c for c in choices if cur in c.lower()] if cur else choices
        return [app_commands.Choice(name=c, value=c) for c in filtered[:25]]

    @app_commands.command(name="bb_set", description="Compute your Bloodborne build from your stats.")
    async def bb_set(
        self,
        interaction: discord.Interaction,
        vitality: int,
        endurance: int,
        strength: int,
        skill: int,
        bloodtinge: int,
        arcane: int,
        origin: str | None = None
    ):
        await interaction.response.defer()

        # 1. Ensure user's personal worksheet
        ws = self._ensure_user_tab(interaction.user.id)

        # 2. If an origin was chosen, write it to Character Sheet
        if origin:
            self._set_origin(ws, origin)

        # 3. Write input stats
        ws.update("L8:L13", [[vitality], [endurance], [strength], [skill], [bloodtinge], [arcane]])

        # 4. Read output values from O2:O13 (your existing helper)
        pairs = self._read_outputs(ws)  # ← THIS must come before using 'pairs'

        # 5. Read Level & Origin from Character Sheet
        level, origin_from_sheet = self._read_level_and_origin(ws)

        # 6. Build and send the embed
        embed = self._embed_from_results(
            interaction.user,
            (vitality, endurance, strength, skill, bloodtinge, arcane),
            pairs,
            level=level,
            origin=origin_from_sheet
        )

        await interaction.followup.send(embed=embed)

    @bb_set.autocomplete("origin")
    async def bb_set_origin_autocomplete(self, interaction: discord.Interaction, current: str):
        # Pull choices from the sheet and filter by user input
        choices = self._get_origin_choices()
        current_lower = (current or "").lower()
        filtered = [c for c in choices if current_lower in c.lower()] if current_lower else choices
        # Discord allows up to 25 items in autocomplete
        return [app_commands.Choice(name=o, value=o) for o in filtered[:25]]

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
            ws = self._ensure_user_tab(interaction.user.id)
            inputs = [int(v[0]) for v in ws.get("L8:L13")]
            pairs = self._read_outputs(ws)
            await self._save_cache(interaction.user.id, tuple(inputs), pairs, ws.title)
        else:
            ws = self._ensure_user_tab(interaction.user.id)  # ensure we can read J2/J5 even if using cache
            inputs = cached["inputs"]
            pairs = list(cached["results"].items())

        level, origin_from_sheet = self._read_level_and_origin(ws)
        embed = self._embed_from_results(interaction.user, inputs, pairs, level=level, origin=origin_from_sheet)
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
