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
import math

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
_ENEMY_SPREADSHEET_ID = "1KWBYOU-2HtYNpFNDtYfxlPGqxRZgmJGeSabQVsVRNdo"
_ENEMY_SHEET_NAME = "enemydata"
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

    def _enemy_ws(self):
        """Return the 'enemydata' worksheet from the separate spreadsheet."""
        other = self.gc.open_by_key(_ENEMY_SPREADSHEET_ID)
        return other.worksheet(_ENEMY_SHEET_NAME)

    def _get_enemy_choices(self) -> list[tuple[str, int]]:
        """
        Returns [(label, row_index)], where label is 'Name (Location)' and row_index is the actual row (3..296).
        Source:
        Names: A3:A296
        Locations: B3:B296
        """
        ws = self._enemy_ws()
        names = ws.get("A3:A296") or []
        locs  = ws.get("B3:B296") or []
        out = []
        for i in range(max(len(names), len(locs))):
            name = names[i][0] if i < len(names) and names[i] else ""
            loc  = locs[i][0]  if i < len(locs)  and locs[i]  else ""
            if not name:
                continue
            label = f"{name} ({loc})" if loc else name
            row_index = 3 + i  # because A3 is the first
            out.append((label, row_index))
        return out

    def _apply_enemy_matchup(self, ws_player, enemy_row: int):
        """
        For the chosen enemy row (F..L @ row), write those 7 values into T36..T42 on the player's worksheet.
        Mapping: F->T36, G->T37, H->T38, I->T39, J->T40, K->T41, L->T42
        """
        ws_e = self._enemy_ws()
        row = enemy_row
        vals_row = ws_e.get(f"F{row}:L{row}") or [[]]
        vals = vals_row[0] if vals_row else []
        # Ensure 7 values
        vals = (vals + [""] * 7)[:7]
        ws_player.update("T36:T42", [[v] for v in vals], value_input_option="USER_ENTERED")

    def _read_damage_summary(self, ws) -> tuple[str | None, str | None]:
        """
        Returns (Unmitigated PhysAtk U17, Total Final Dmg U43) from the player's worksheet.
        """
        try:
            u17 = ws.acell("U17").value
        except Exception:
            u17 = None
        try:
            u43 = ws.acell("U43").value
        except Exception:
            u43 = None
        return u17, u43

    def _read_matchup_block(self, ws):
        """
        Reads the matchup labels and values from the player's worksheet.
        Labels: Q36:Q42 (7 rows)
        Values: T36:T42 (7 rows)
        Returns: list[tuple[label, value]]
        """
        labels = ws.get("Q36:Q42") or []
        values = ws.get("T36:T42") or []
        # Flatten and pad
        labels = [(r[0] if r else "") for r in labels]
        values = [(r[0] if r else "") for r in values]
        # Ensure 7
        if len(labels) < 7: labels += [""] * (7 - len(labels))
        if len(values) < 7: values += [""] * (7 - len(values))
        return list(zip(labels[:7], values[:7]))

    def _read_enemy_health(self, enemy_row: int) -> float | None:
        """Return the enemy health (column C) as a float, or None if blank/invalid."""
        ws = self._enemy_ws()
        val = None
        try:
            v = ws.acell(f"C{enemy_row}").value
            val = float(v.replace(",", "")) if v else None
        except Exception:
            pass
        return val

    def _compute_h2k(self, total_final_dmg: str | None, enemy_health: float | None) -> int | None:
        """
        Compute the number of hits needed to kill (ceil(health / damage)).
        Returns None if either value is missing or non-numeric.
        """
        if not total_final_dmg or not enemy_health:
            return None
        try:
            dmg = float(str(total_final_dmg).replace(",", ""))
            if dmg <= 0:
                return None
            return math.ceil(enemy_health / dmg)
        except Exception:
            return None

    # ====== Weapon / Gem / Attack choices from "Weapon Data" ======

    def _get_weapon_choices(self) -> list[str]:
        ws = self.sh.worksheet("Weapon Data")
        col = ws.get("A2:A43") or []
        return [r[0] for r in col if r and r[0]]

    def _get_gem_choices(self) -> list[str]:
        ws = self.sh.worksheet("Weapon Data")
        col = ws.get("AJ2:AJ32") or []
        return [r[0] for r in col if r and r[0]]

    def _get_attack_choices(self) -> list[str]:
        """
        Row B45:BN45; return only non-empty cells.
        """
        ws = self.sh.worksheet("Weapon Data")
        row = ws.get("B45:BN45") or [[]]
        cells = row[0] if row else []
        return [c for c in cells if c]

    # ====== Write weapon, gems, attack to the player's worksheet ======

    def _set_weapon_gems_attack(
        self, ws,
        weapon: str | None,
        gems: list[str | None],   # length up to 9, order: G1 Prim, G1 Sec, G1 C/T, G2 Prim, G2 Sec, G2 C/T, G3 Prim, G3 Sec, G3 C/T
        attack: str | None,
        gem_ct_kinds: tuple[str | None, str | None, str | None] | None = None  # ("Curse"/"Tertiary"/None) x 3
    ):
        """
        Writes:
        - Q17 = weapon
        - S21:S29 = 9 gem values (Primary, Secondary, Curse/Tertiary for each Gem 1..3)
        - R19 = attack
        - Q23/Q26/Q29 = "Gem X Curse" or "Gem X Tertiary" depending on gem_ct_kinds

        Passing None for any slot preserves the existing value on the sheet.
        """
        # --- Weapon ---
        if weapon is not None:
            ws.update("Q17", [[weapon]], value_input_option="USER_ENTERED")

        # --- Gems S21:S29 (9 rows) ---
        current_gems = ws.get("S21:S29") or []
        def cur_g(i):
            try:
                return current_gems[i][0]
            except Exception:
                return ""
        padded = (gems + [None] * 9)[:9]
        values = [[(padded[i] if padded[i] is not None else cur_g(i))] for i in range(9)]
        ws.update("S21:S29", values, value_input_option="USER_ENTERED")

        # --- Attack R19 ---
        if attack is not None:
            ws.update("R19", [[attack]], value_input_option="USER_ENTERED")

        # --- Curse/Tertiary labels Q23 (Gem1), Q26 (Gem2), Q29 (Gem3) ---
        if gem_ct_kinds is not None:
            # Read current so None preserves
            current_q = ws.get("Q23:Q29") or []  # this gives Q23..Q29; we’ll only use rows 1,4,7 (0-index 0,3,6) for Q23/Q26/Q29 fetch fallback
            def cur_q_cell(a1):
                # small helper to read single existing value safely
                try:
                    return ws.acell(a1).value
                except Exception:
                    return ""

            g1_kind, g2_kind, g3_kind = gem_ct_kinds

            # Build write list for the three cells (preserve when None)
            if g1_kind is not None:
                ws.update("Q23", [[f"Gem 1 {g1_kind}"]], value_input_option="USER_ENTERED")
            if g2_kind is not None:
                ws.update("Q26", [[f"Gem 2 {g2_kind}"]], value_input_option="USER_ENTERED")
            if g3_kind is not None:
                ws.update("Q29", [[f"Gem 3 {g3_kind}"]], value_input_option="USER_ENTERED")

    # ====== Read damage summary from the player's worksheet ======

    def _read_damage_summary(self, ws) -> tuple[str | None, str | None]:
        """
        Returns (unmitigated_phys_atk_U17, total_final_dmg_U43)
        """
        try:
            u17 = ws.acell("U17").value
        except Exception:
            u17 = None
        try:
            u43 = ws.acell("U43").value
        except Exception:
            u43 = None
        return u17, u43

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

    def _get_head_armor_choices(self) -> list[str]:
        """Armor piece names from 'Armor Data'!A2:A131."""
        ws = self.sh.worksheet("Armor Data")
        col = ws.get("A2:A37")
        return [r[0] for r in col if r and r[0]]

    def _get_chest_armor_choices(self) -> list[str]:
        """Armor piece names from 'Armor Data'!A2:A131."""
        ws = self.sh.worksheet("Armor Data")
        col = ws.get("A38:A72")
        return [r[0] for r in col if r and r[0]]

    def _get_arms_armor_choices(self) -> list[str]:
        """Armor piece names from 'Armor Data'!A2:A131."""
        ws = self.sh.worksheet("Armor Data")
        col = ws.get("A73:A99")
        return [r[0] for r in col if r and r[0]]

    def _get_legs_armor_choices(self) -> list[str]:
        """Armor piece names from 'Armor Data'!A2:A131."""
        ws = self.sh.worksheet("Armor Data")
        col = ws.get("A100:A131")
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
        name="bb_matchup",
        description="Pick an enemy to apply matchup values and show your damage & H2K."
    )
    @app_commands.describe(
        enemy="Select an enemy (from enemydata A3:A296 with its location)."
    )
    async def bb_matchup(
        self,
        interaction: discord.Interaction,
        enemy: str
    ):
        await interaction.response.defer(ephemeral=False)
        ws_player = self._ensure_user_tab(interaction.user.id)

        # Parse row index
        try:
            row_index = int(enemy)
        except Exception:
            await interaction.followup.send("Couldn't parse the selected enemy. Please pick from the suggestions.", ephemeral=True)
            return

        # Apply matchup (F..L -> T36..T42)
        self._apply_enemy_matchup(ws_player, row_index)

        # Fetch enemy info
        ws_e = self._enemy_ws()
        name_loc = ws_e.get(f"A{row_index}:B{row_index}") or [[]]
        enemy_name = (name_loc[0][0] if name_loc and name_loc[0] else "") or "Unknown"
        enemy_loc  = (name_loc[0][1] if name_loc and len(name_loc[0]) > 1 else "")
        label = f"{enemy_name} ({enemy_loc})" if enemy_loc else enemy_name

        # --- NEW: get enemy health & compute hits to kill ---
        enemy_health = self._read_enemy_health(row_index)

        # Damage from player sheet
        phys_unmitigated, total_final = self._read_damage_summary(ws_player)

        # Calculate hits-to-kill
        h2k = self._compute_h2k(total_final, enemy_health)

        # Read matchup (Q36–Q42, T36–T42)
        matchup = self._read_matchup_block(ws_player)
        matchup_lines = [f"{lab or '—'}: **{val or '—'}**" for lab, val in matchup]
        matchup_md = "\n".join(matchup_lines) if matchup_lines else "—"

        # Build embed
        user = interaction.user
        em = discord.Embed(
            title=f"🩸 {user.display_name}'s Matchup",
            description=f"Applied **{label}** matchup values (T36–T42).",
            color=0x8A0303
        )
        em.add_field(name="Matchup", value=matchup_md, inline=False)
        em.add_field(
            name="Damage",
            value=(
                f"🎯 **Total Final Dmg (U43):** {total_final or '—'}"
            ),
            inline=False
        )

        # Show H2K if both values exist
        if enemy_health is not None and h2k is not None:
            em.add_field(
                name="H2K (Hits to Kill)",
                value=f"❤️ Enemy Health: **{int(enemy_health)}**\n💥 Hits Required: **{h2k}**",
                inline=False
            )
        else:
            em.add_field(name="H2K (Hits to Kill)", value="—", inline=False)

        await interaction.followup.send(embed=em)

    @bb_matchup.autocomplete("enemy")
    async def bb_matchup_enemy_autocomplete(self, interaction: discord.Interaction, current: str):
        # Produce up to 25 matching "Name (Location)" labels, with value = row index (string)
        cur = (current or "").lower()
        items = self._get_enemy_choices()
        if cur:
            items = [(label, row) for (label, row) in items if cur in label.lower()]
        items = items[:25]
        return [app_commands.Choice(name=label, value=str(row)) for (label, row) in items]

    @app_commands.command(
        name="bb_weapon",
        description="Choose weapon, gems, and an attack; shows PhysAtk & Total Final Dmg."
    )
    @app_commands.describe(
        weapon="Weapon (Weapon Data!A2:A43)",
        attack="Attack (non-empty from Weapon Data!B45:BN45)",

        gem1_primary="Gem 1 Primary (Weapon Data!AJ2:AJ32)",
        gem1_secondary="Gem 1 Secondary",
        gem1_ct_value="Gem 1 Curse/Tertiary value",
        gem1_ct_kind="Gem 1: choose whether the 3rd slot is Curse or Tertiary",

        gem2_primary="Gem 2 Primary",
        gem2_secondary="Gem 2 Secondary",
        gem2_ct_value="Gem 2 Curse/Tertiary value",
        gem2_ct_kind="Gem 2: choose whether the 3rd slot is Curse or Tertiary",

        gem3_primary="Gem 3 Primary",
        gem3_secondary="Gem 3 Secondary",
        gem3_ct_value="Gem 3 Curse/Tertiary value",
        gem3_ct_kind="Gem 3: choose whether the 3rd slot is Curse or Tertiary",
    )
    @app_commands.choices(
        gem1_ct_kind=[
            app_commands.Choice(name="Curse", value="Curse"),
            app_commands.Choice(name="Tertiary", value="Tertiary"),
        ],
        gem2_ct_kind=[
            app_commands.Choice(name="Curse", value="Curse"),
            app_commands.Choice(name="Tertiary", value="Tertiary"),
        ],
        gem3_ct_kind=[
            app_commands.Choice(name="Curse", value="Curse"),
            app_commands.Choice(name="Tertiary", value="Tertiary"),
        ],
    )
    async def bb_weapon(
        self,
        interaction: discord.Interaction,
        weapon: str | None = None,
        attack: str | None = None,

        gem1_primary: str | None = None,
        gem1_secondary: str | None = None,
        gem1_ct_value: str | None = None,
        gem1_ct_kind: app_commands.Choice[str] | None = None,

        gem2_primary: str | None = None,
        gem2_secondary: str | None = None,
        gem2_ct_value: str | None = None,
        gem2_ct_kind: app_commands.Choice[str] | None = None,

        gem3_primary: str | None = None,
        gem3_secondary: str | None = None,
        gem3_ct_value: str | None = None,
        gem3_ct_kind: app_commands.Choice[str] | None = None,
    ):
        await interaction.response.defer(ephemeral=False)

        ws = self._ensure_user_tab(interaction.user.id)

        # ---- Validate against sheet lists ----
        weapons_valid = set(self._get_weapon_choices())
        gems_valid = set(self._get_gem_choices())
        attacks_valid = set(self._get_attack_choices())

        bad = []
        if weapon is not None and weapon not in weapons_valid: bad.append(f"Weapon: {weapon}")
        if attack is not None and attack not in attacks_valid: bad.append(f"Attack: {attack}")

        gem_inputs_flat = [
            gem1_primary, gem1_secondary, gem1_ct_value,
            gem2_primary, gem2_secondary, gem2_ct_value,
            gem3_primary, gem3_secondary, gem3_ct_value,
        ]
        labels_for_validation = [
            "Gem 1 Primary", "Gem 1 Secondary", "Gem 1 Curse/Tertiary",
            "Gem 2 Primary", "Gem 2 Secondary", "Gem 2 Curse/Tertiary",
            "Gem 3 Primary", "Gem 3 Secondary", "Gem 3 Curse/Tertiary",
        ]
        for val, label in zip(gem_inputs_flat, labels_for_validation):
            if val is not None and val not in gems_valid:
                bad.append(f"{label}: {val}")

        if bad:
            await interaction.followup.send(
                "Some choices aren't valid:\n• " + "\n• ".join(bad),
                ephemeral=True
            )
            return

        # ---- Write updates (includes Curse/Tertiary labels to Q23/Q26/Q29) ----
        ct_tuple = (
            gem1_ct_kind.value if gem1_ct_kind else None,
            gem2_ct_kind.value if gem2_ct_kind else None,
            gem3_ct_kind.value if gem3_ct_kind else None,
        )
        self._set_weapon_gems_attack(ws, weapon, gem_inputs_flat, attack, gem_ct_kinds=ct_tuple)

        # ---- Re-read what's on the sheet (truth source) ----
        q17 = ws.acell("Q17").value  # weapon
        r19 = ws.acell("R19").value  # attack
        s21_s29 = ws.get("S21:S29") or []
        gems_now = [(r[0] if r else "") for r in s21_s29]
        if len(gems_now) < 9:
            gems_now += [""] * (9 - len(gems_now))

        # Read the label cells to show the actual stored kind names
        q23 = (ws.acell("Q23").value or "").lower()  # Gem 1 Curse/Tertiary
        q26 = (ws.acell("Q26").value or "").lower()  # Gem 2 Curse/Tertiary
        q29 = (ws.acell("Q29").value or "").lower()  # Gem 3 Curse/Tertiary

        def _kind_from_cell(val: str, fallback: str) -> str:
            if "curse" in val:
                return "Curse"
            if "tertiary" in val:
                return "Tertiary"
            return fallback

        g1_kind = _kind_from_cell(q23, "Curse/Tertiary")
        g2_kind = _kind_from_cell(q26, "Curse/Tertiary")
        g3_kind = _kind_from_cell(q29, "Curse/Tertiary")

        # ---- Damage summary outputs ----
        phys_unmitigated, total_final = self._read_damage_summary(ws)

        # ---- Build embed ----
        user = interaction.user
        em = discord.Embed(
            title=f"🩸 {user.display_name}'s Weapon & Gems",
            description="Selections applied to your sheet; current damage below.",
            color=0x8A0303
        )

        selections = (
            f"🗡️ **R-Hand Weapon:** {q17 or '—'}\n"
            f"💥 **Attack:** {r19 or '—'}\n"
            f"💎 **Gems:**\n"
            f"• Gem 1 Primary: {gems_now[0] or '—'}\n"
            f"• Gem 1 Secondary: {gems_now[1] or '—'}\n"
            f"• Gem 1 {g1_kind}: {gems_now[2] or '—'}\n"
            f"• Gem 2 Primary: {gems_now[3] or '—'}\n"
            f"• Gem 2 Secondary: {gems_now[4] or '—'}\n"
            f"• Gem 2 {g2_kind}: {gems_now[5] or '—'}\n"
            f"• Gem 3 Primary: {gems_now[6] or '—'}\n"
            f"• Gem 3 Secondary: {gems_now[7] or '—'}\n"
            f"• Gem 3 {g3_kind}: {gems_now[8] or '—'}"
        )
        em.add_field(name="Loadout", value=selections, inline=False)

        dmg_md = (
            f"⚔️ **Unmitigated PhysAtk (U17):** {phys_unmitigated or '—'}"
        )
        em.add_field(name="Damage", value=dmg_md, inline=False)

        await interaction.followup.send(embed=em)

    # ---- Weapon ----
    @bb_weapon.autocomplete("weapon")
    async def bb_weapon_weapon_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = self._get_weapon_choices()
        cur = (current or "").lower()
        filtered = [c for c in choices if cur in c.lower()] if cur else choices
        return [app_commands.Choice(name=c, value=c) for c in filtered[:25]]

    # ---- Attack ----
    @bb_weapon.autocomplete("attack")
    async def bb_weapon_attack_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = self._get_attack_choices()
        cur = (current or "").lower()
        filtered = [c for c in choices if cur in c.lower()] if cur else choices
        return [app_commands.Choice(name=c, value=c) for c in filtered[:25]]

    # ---- Gems: every gem value field pulls from the same list ----
    @bb_weapon.autocomplete("gem1_primary")
    @bb_weapon.autocomplete("gem1_secondary")
    @bb_weapon.autocomplete("gem1_ct_value")
    @bb_weapon.autocomplete("gem2_primary")
    @bb_weapon.autocomplete("gem2_secondary")
    @bb_weapon.autocomplete("gem2_ct_value")
    @bb_weapon.autocomplete("gem3_primary")
    @bb_weapon.autocomplete("gem3_secondary")
    @bb_weapon.autocomplete("gem3_ct_value")
    async def bb_weapon_gem_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = self._get_gem_choices()
        cur = (current or "").lower()
        filtered = [c for c in choices if cur in c.lower()] if cur else choices
        return [app_commands.Choice(name=c, value=c) for c in filtered[:25]]

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
        head_armor_valid = set(self._get_head_armor_choices())
        chest_armor_valid = set(self._get_chest_armor_choices())
        arms_armor_valid = set(self._get_arms_armor_choices())
        legs_armor_valid = set(self._get_legs_armor_choices())

        bad = []
        if rune1 is not None and rune1 not in runes_valid: bad.append(f"Rune1: {rune1}")
        if rune2 is not None and rune2 not in runes_valid: bad.append(f"Rune2: {rune2}")
        if rune3 is not None and rune3 not in runes_valid: bad.append(f"Rune3: {rune3}")
        if oath  is not None and oath  not in oaths_valid: bad.append(f"Oath: {oath}")
        if head  is not None and head  not in head_armor_valid: bad.append(f"Head: {head}")
        if chest is not None and chest not in chest_armor_valid: bad.append(f"Chest: {chest}")
        if arms  is not None and arms  not in arms_armor_valid: bad.append(f"Arms: {arms}")
        if legs  is not None and legs  not in legs_armor_valid: bad.append(f"Legs: {legs}")

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
    async def bb_gear_armor_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = self._get_head_armor_choices()
        cur = (current or "").lower()
        filtered = [c for c in choices if cur in c.lower()] if cur else choices
        return [app_commands.Choice(name=c, value=c) for c in filtered[:25]]
    
    @bb_gear.autocomplete("chest")
    async def bb_gear_armor_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = self._get_chest_armor_choices()
        cur = (current or "").lower()
        filtered = [c for c in choices if cur in c.lower()] if cur else choices
        return [app_commands.Choice(name=c, value=c) for c in filtered[:25]]
    
    @bb_gear.autocomplete("arms")
    async def bb_gear_armor_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = self._get_arms_armor_choices()
        cur = (current or "").lower()
        filtered = [c for c in choices if cur in c.lower()] if cur else choices
        return [app_commands.Choice(name=c, value=c) for c in filtered[:25]]
    
    @bb_gear.autocomplete("legs")
    async def bb_gear_armor_autocomplete(self, interaction: discord.Interaction, current: str):
        choices = self._get_legs_armor_choices()
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
