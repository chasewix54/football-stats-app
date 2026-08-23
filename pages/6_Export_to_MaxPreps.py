"""
Streamlit page to export one, several, or all Google Sheet game totals tabs to
MaxPreps-compatible pipe-delimited .txt import files.

The Stat Supplier ID is hard-coded as the first line of every export:
    669ae75f-4563-494a-8c17-370aaa8539d4

Google Sheet mode supports:
- Selecting individual game Totals tabs or all game Totals tabs for the selected sport
- Exporting one .txt per game (multiple games are packaged in a .zip)
- Building combined season totals for reconciliation/reference
- Friendly game labels while preserving the underlying worksheet names

Also supports CSV/Excel uploads and editable field mappings.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

try:
    import gspread  # type: ignore
except Exception:
    gspread = None

SUPPLIER_ID = "669ae75f-4563-494a-8c17-370aaa8539d4"

# ------------------ Football canonical fields (used for ordering fallback) ------------------
MAXPREPS_FIELDS: List[str] = [
    "Jersey","RushingNum","RushingYards","RushingLong","ReceivingNum","ReceivingYards","ReceivingLong",
    "PassingComp","PassingAtt","PassingInt","PassingYards","PassingTD","PassingLong","OffensiveFumbles",
    "OffensiveFumblesLost","PancakeBlocks","Tackles","Assists","TotalTackles","TacklesForLoss","Sacks",
    "SacksYardsLost","QBHurries","INTs","INTYards","PassesDefensed","BlockedPunts","BlockedFG",
    "FumbleRecoveries","FumbleRecoveryYards","CausedFumbles","PuntReturnNum","PuntReturnYards",
    "PuntReturnLong","PuntReturnFairCatches","KickoffReturnNum","KickoffReturnYards","KickoffReturnLong",
    "TotalReturnYards","PuntNum","PuntYards","PuntLong","PuntInside20","KickoffNum","KickoffYards",
    "KickoffLong","KickoffTouchbacks","Touchdowns","RushingTDNum","ReceivingTDNum","FumbleReturnedTDNum",
    "IntReturnedTDNum","PuntReturnedTDNum","KickoffReturnedTDNum","TotalTDNum","PATKickingMade",
    "PATKickingAtt","PATKickingPoints","PATRushingNum","PATReceivingNum","TotalConversionPoints","FGMade",
    "FGAttempted","FGLong","Safeties","TotalPoints"
]

# ------------------ Default football mapping ------------------
DEFAULT_FIELD_MAP: Dict[str, str] = {
    # Roster
    "Jersey": "Jersey",
    "number": "Jersey",

    # Offensive – Rushing
    "Rush Attempts": "RushingNum",
    "Rushing Yards": "RushingYards",
    "Rushing TDs": "RushingTDNum",

    # Offensive – Receiving
    "Receptions": "ReceivingNum",
    "Receiving Yards": "ReceivingYards",
    "Receiving TDs": "ReceivingTDNum",

    # Offensive – Passing
    "Pass Completions": "PassingComp",
    "Pass Attempts": "PassingAtt",
    "Passing Interceptions": "PassingInt",
    "Pass Yards": "PassingYards",
    "Passing TDs": "PassingTD",

    # Offensive – Fumbles
    "Fumbles": "OffensiveFumbles",

    # Defensive
    "Tackles": "TotalTackles",
    "Sacks": "Sacks",
    "Interceptions": "INTs",
    "Interception Return Yards": "INTYards",
    "Interception Return TDs": "IntReturnedTDNum",
    "Forced Fumbles": "CausedFumbles",
    "Fumble Recoveries": "FumbleRecoveries",
    "Fumble Recovery Yards": "FumbleRecoveryYards",
    "Fumble Return TDs": "FumbleReturnedTDNum",

    # Punt Returns
    "Punt Returns": "PuntReturnNum",
    "Punt Return Yards": "PuntReturnYards",
    "Punt Return TDs": "PuntReturnedTDNum",

    # Kickoff Returns
    "Kickoff Returns": "KickoffReturnNum",
    "Kickoff Return Yards": "KickoffReturnYards",
    "Kickoff Return TDs": "KickoffReturnedTDNum",
    "Total Return Yards": "TotalReturnYards",

    # Punting
    "Punts": "PuntNum",
    "Punt Yards": "PuntYards",

    # PAT Kicking
    "PAT Made": "PATKickingMade",
    "PAT Attempts": "PATKickingAtt",

    # Two-point conversions
    "2-pt Rushing Conversions": "PATRushingNum",
    "2-pt Receiving Conversions": "PATReceivingNum",
    "2-pt Conversion Points": "TotalConversionPoints",

    # Field Goals
    "FG Made": "FGMade",
    "FG Attempts": "FGAttempted",
}

# ------------------ Helpers ------------------

def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\"'()]", "", name).strip()
    if not name.lower().endswith(".txt"):
        name += ".txt"
    return name


def sanitize_zip_filename(name: str) -> str:
    name = re.sub(r"[\"'()]", "", name).strip()
    if not name.lower().endswith(".zip"):
        name += ".zip"
    return name


def slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_")
    return value.lower() or "game"


def coerce_str_no_nan(x) -> str:
    if pd.isna(x):
        return ""
    if isinstance(x, str):
        return x.strip()
    try:
        if float(x).is_integer():
            return str(int(float(x)))
        return str(x)
    except Exception:
        return str(x)


def resolve_column_name_case_insensitive(df: pd.DataFrame, name: str) -> str:
    """Return the actual DataFrame column whose lowercase matches name.lower()."""
    target = name.strip().lower()
    for c in df.columns:
        if str(c).strip().lower() == target:
            return c
    return name


def guess_jersey_column(columns: List[str]) -> Optional[str]:
    candidates = [
        "jersey", "number", "player number", "no", "num", "player #", "player_num", "player_number",
    ]
    lowered = {str(c).lower(): c for c in columns}
    for key in candidates:
        if key in lowered:
            return lowered[key]
    for c in columns:
        cl = str(c).lower()
        if "jersey" in cl or "number" in cl or cl in ("#", "no", "num"):
            return c
    return None


def resolve_jersey_column(df: pd.DataFrame, preferred: str) -> str:
    resolved = resolve_column_name_case_insensitive(df, preferred)
    if resolved in df.columns:
        return resolved
    guessed = guess_jersey_column([str(c) for c in df.columns])
    if guessed and guessed in df.columns:
        return guessed
    raise ValueError(
        f"Missing required jersey column: '{preferred}'. Available columns: {list(df.columns)}"
    )


GAME_TOTALS_RE = re.compile(
    r"^(?P<sport>.+?)\s+(?P<date>\d{4}-\d{2}-\d{2})\s+vs\s+(?P<opponent>.+?)\s+\(Totals\)$",
    re.IGNORECASE,
)


def parse_game_tab(title: str) -> Optional[Dict[str, str]]:
    match = GAME_TOTALS_RE.match(title.strip())
    return match.groupdict() if match else None


def format_game_tab_label(title: str) -> str:
    parsed = parse_game_tab(title)
    if not parsed:
        return title
    try:
        date_label = datetime.strptime(parsed["date"], "%Y-%m-%d").strftime("%b %d, %Y")
    except ValueError:
        date_label = parsed["date"]
    return f"{date_label} — {parsed['opponent']}"


def is_totals_tab_for_sport(title: str, sport: str) -> bool:
    parsed = parse_game_tab(title)
    return bool(parsed and parsed["sport"].strip().lower() == sport.strip().lower())


def game_tab_sort_key(title: str):
    parsed = parse_game_tab(title)
    if parsed:
        return parsed["date"], parsed["opponent"].lower()
    return "", title.lower()


def game_export_filename(title: str, sport: str) -> str:
    parsed = parse_game_tab(title)
    if parsed:
        return sanitize_filename(
            f"maxpreps_{sport.lower()}_{parsed['date']}_vs_{slugify(parsed['opponent'])}"
        )
    return sanitize_filename(f"maxpreps_{sport.lower()}_{slugify(title)}")


def choose_fields_to_include(df: pd.DataFrame, field_map: Dict[str, str]) -> List[str]:
    included: List[str] = []
    for sheet_col, mp_field in field_map.items():
        if mp_field == "Jersey":
            continue
        resolved = resolve_column_name_case_insensitive(df, sheet_col)
        if resolved in df.columns:
            has_any = df[resolved].apply(lambda v: not pd.isna(v) and str(v).strip() != "").any()
            if has_any:
                included.append(mp_field)
    order = [f for f in MAXPREPS_FIELDS if f != "Jersey"]
    included_sorted = [f for f in order if f in included]
    extras = [f for f in included if f not in included_sorted]
    return included_sorted + extras


def choose_fields_to_include_for_sport(
    df: pd.DataFrame,
    field_map: Dict[str, str],
    declared: List[str],
) -> List[str]:
    included: List[str] = []
    for sheet_col, mp_field in field_map.items():
        if mp_field == "Jersey":
            continue
        resolved = resolve_column_name_case_insensitive(df, sheet_col)
        if resolved in df.columns:
            has_any = df[resolved].apply(lambda v: not pd.isna(v) and str(v).strip() != "").any()
            if has_any and (not declared or mp_field in declared):
                included.append(mp_field)
    order = declared if declared else [f for f in MAXPREPS_FIELDS if f != "Jersey"]
    included_sorted = [f for f in order if f in included]
    extras = [f for f in included if f not in included_sorted]
    return included_sorted + extras


def build_maxpreps_txt(
    df: pd.DataFrame,
    field_map: Dict[str, str],
    jersey_column_name: str = "Jersey",
) -> str:
    jersey_column_resolved = resolve_jersey_column(df, jersey_column_name)

    reverse_map: Dict[str, str] = {}
    for sheet_col, mp_field in field_map.items():
        resolved = resolve_column_name_case_insensitive(df, sheet_col)
        if resolved in df.columns:
            reverse_map[mp_field] = resolved

    included_fields = choose_fields_to_include(df, field_map)
    header_fields = ["Jersey"] + included_fields
    lines: List[str] = [SUPPLIER_ID, "|".join(header_fields)]

    for _, row in df.iterrows():
        jersey_val = coerce_str_no_nan(row[jersey_column_resolved])
        if jersey_val == "":
            continue
        values: List[str] = [jersey_val]
        for mp_field in included_fields:
            sheet_col = reverse_map.get(mp_field)
            val = coerce_str_no_nan(row[sheet_col]) if sheet_col else ""
            values.append(val)
        if any(v != "" for v in values[1:]):
            lines.append("|".join(values))

    return "\n".join(lines) + "\n"


def build_maxpreps_txt_for_sport(
    df: pd.DataFrame,
    field_map: Dict[str, str],
    jersey_column_name: str,
    declared_fields: List[str],
) -> str:
    jersey_col_resolved = resolve_jersey_column(df, jersey_column_name)

    reverse_map: Dict[str, str] = {}
    for sheet_col, mp_field in field_map.items():
        resolved = resolve_column_name_case_insensitive(df, sheet_col)
        if resolved in df.columns:
            reverse_map[mp_field] = resolved

    included = choose_fields_to_include_for_sport(df, field_map, declared_fields)
    header_fields = ["Jersey"] + included
    lines: List[str] = [SUPPLIER_ID, "|".join(header_fields)]

    for _, row in df.iterrows():
        jersey_val = coerce_str_no_nan(row[jersey_col_resolved])
        if jersey_val == "":
            continue
        values: List[str] = [jersey_val]
        for mp_field in included:
            sheet_col = reverse_map.get(mp_field)
            val = coerce_str_no_nan(row[sheet_col]) if sheet_col else ""
            values.append(val)
        if any(v != "" for v in values[1:]):
            lines.append("|".join(values))

    return "\n".join(lines) + "\n"


def combine_game_totals(
    game_frames: Dict[str, pd.DataFrame],
    field_map: Dict[str, str],
    jersey_column_name: str,
) -> pd.DataFrame:
    """Sum mapped stat columns by jersey across selected game Totals tabs."""
    stat_columns: List[str] = []
    for sheet_col, mp_field in field_map.items():
        if mp_field != "Jersey" and sheet_col not in stat_columns:
            stat_columns.append(sheet_col)

    accumulated: Dict[str, Dict[str, object]] = {}

    for _, df in game_frames.items():
        if df is None or df.empty:
            continue
        jersey_actual = resolve_jersey_column(df, jersey_column_name)

        resolved_stats: Dict[str, str] = {}
        for sheet_col in stat_columns:
            actual = resolve_column_name_case_insensitive(df, sheet_col)
            if actual in df.columns:
                resolved_stats[sheet_col] = actual

        for _, row in df.iterrows():
            jersey = coerce_str_no_nan(row[jersey_actual])
            if not jersey:
                continue
            bucket = accumulated.setdefault(jersey, {jersey_column_name: jersey})

            for sheet_col, actual in resolved_stats.items():
                raw = row[actual]
                if pd.isna(raw) or str(raw).strip() == "":
                    continue
                numeric = pd.to_numeric(pd.Series([raw]), errors="coerce").iloc[0]
                if pd.notna(numeric):
                    bucket[sheet_col] = float(bucket.get(sheet_col, 0) or 0) + float(numeric)
                elif sheet_col not in bucket:
                    bucket[sheet_col] = raw

    if not accumulated:
        return pd.DataFrame(columns=[jersey_column_name] + stat_columns)

    combined = pd.DataFrame(list(accumulated.values()))
    for col in [jersey_column_name] + stat_columns:
        if col not in combined.columns:
            combined[col] = 0 if col != jersey_column_name else ""
    combined = combined[[jersey_column_name] + stat_columns]

    numeric_sort = pd.to_numeric(combined[jersey_column_name], errors="coerce")
    combined = combined.assign(_jersey_sort=numeric_sort)
    combined = combined.sort_values(
        by=["_jersey_sort", jersey_column_name],
        na_position="last",
    ).drop(columns=["_jersey_sort"]).reset_index(drop=True)
    return combined


def make_zip(files: Dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for filename, text in files.items():
            zf.writestr(filename, text.encode("utf-8"))
    buffer.seek(0)
    return buffer.getvalue()


@st.cache_data(ttl=60, show_spinner=False)
def list_google_sheet_titles(sheet_url: str) -> List[str]:
    if gspread is None:
        return []
    sa = gspread.service_account_from_dict(st.secrets["gcp_service_account"])  # type: ignore
    sh = sa.open_by_url(sheet_url)
    return [ws.title for ws in sh.worksheets()]


@st.cache_data(ttl=60, show_spinner=False)
def load_google_game_frames(sheet_url: str, worksheet_names: tuple[str, ...]) -> Dict[str, pd.DataFrame]:
    if gspread is None:
        return {}
    sa = gspread.service_account_from_dict(st.secrets["gcp_service_account"])  # type: ignore
    sh = sa.open_by_url(sheet_url)
    frames: Dict[str, pd.DataFrame] = {}
    for worksheet_name in worksheet_names:
        ws = sh.worksheet(worksheet_name)
        frames[worksheet_name] = pd.DataFrame(ws.get_all_records())
    return frames


# ------------------ UI ------------------
st.title("Export to MaxPreps (.txt)")
st.markdown(
    "Select one game, several games, or every game in the season from the same Google Sheet."
)

# --- Sport registry ---
SPORT_FIELDS: Dict[str, List[str]] = {
    "Football": [
        "RushingNum","RushingYards","RushingLong",
        "ReceivingNum","ReceivingYards","ReceivingLong",
        "PassingComp","PassingAtt","PassingInt","PassingYards","PassingTD","PassingLong",
        "OffensiveFumbles","OffensiveFumblesLost",
        "PancakeBlocks",
        "Tackles","Assists","TotalTackles","TacklesForLoss",
        "Sacks","SacksYardsLost","QBHurries",
        "INTs","INTYards","PassesDefensed",
        "BlockedPunts","BlockedFG",
        "FumbleRecoveries","FumbleRecoveryYards","CausedFumbles",
        "PuntReturnNum","PuntReturnYards","PuntReturnLong","PuntReturnFairCatches",
        "KickoffReturnNum","KickoffReturnYards","KickoffReturnLong",
        "TotalReturnYards",
        "PuntNum","PuntYards","PuntLong","PuntInside20",
        "KickoffNum","KickoffYards","KickoffLong","KickoffTouchbacks",
        "Touchdowns","RushingTDNum","ReceivingTDNum","FumbleReturnedTDNum","IntReturnedTDNum","PuntReturnedTDNum","KickoffReturnedTDNum","TotalTDNum",
        "PATKickingMade","PATKickingAtt","PATKickingPoints",
        "PATRushingNum","PATReceivingNum","TotalConversionPoints",
        "FGMade","FGAttempted","FGLong",
        "Safeties",
        "TotalPoints",
    ],
    "Baseball": [
        "AtBats","Runs","Singles","Doubles","Triples","HomeRuns","Hits","RunsBattedIn",
        "SacrificeFly","SacrificeBunt","BaseOnBalls","StruckOut","HitByPitch","ReachedOnError",
        "FieldersChoice","LeftOnBase","GrandSlams",
        "StolenBase","StolenBaseAttempts",
        "PutOuts","Assists","Errors","DoublePlays","TriplePlays",
        "StolenBaseAttemptsCatcher","CaughtStealing","PassedBalls",
        "Start","Win","Loss","Save","Appearances","CompleteGame","ShutOut","NoHitter","PerfectGame",
        "InningsPitched","PartialInningPitched","BattersFaced","RunsAgainst","EarnedRuns","HitsAgainst",
        "DoublesAgainst","TriplesAgainst","HomeRunsAgainst","SacrificeFlyPitcher","SacrificeBuntPitcher",
        "BaseOnBallsAgainst","BattersStruckOut","HitBatter","Balks","WildPitches","NumberOfPitches",
        "PickOffs","StolenBasesPitcher",
    ],
    "Basketball": [],
    "Soccer": [
        "FieldMinutesPlayed",
        "Goals","Assists","Shots","ShotsOnGoal","Steals",
        "PenaltyKicksMade","PenaltyKicksAttempted","CornerKicks",
        "GameWinningGoal","YellowCards","RedCards",
        "MinutesPlayed","OvertimeMinutesPlayed","GoalsAgainst","Saves",
        "OpponentShotsOnGoal","OpponentPenaltyKickSaves","OpponentPenaltyKickAttempts",
        "ShutOuts","Win","Loss","Tie",
    ],
    "Lacrosse": [],
}

# Default per-sport sheet->MaxPreps mapping
DEFAULT_FIELD_MAP_BY_SPORT: Dict[str, Dict[str, str]] = {
    "Football": DEFAULT_FIELD_MAP,
    "Baseball": {
        "Jersey": "Jersey",
        "number": "Jersey",
        "AB": "AtBats",
        "R": "Runs",
        "1B": "Singles",
        "2B": "Doubles",
        "3B": "Triples",
        "HR": "HomeRuns",
        "H": "Hits",
        "RBI": "RunsBattedIn",
        "SF": "SacrificeFly",
        "SAC": "SacrificeBunt",
        "BB": "BaseOnBalls",
        "SO": "StruckOut",
        "HBP": "HitByPitch",
        "ROE": "ReachedOnError",
        "FC": "FieldersChoice",
        "LOB": "LeftOnBase",
        "Grand Slams": "GrandSlams",
        "SB": "StolenBase",
        "SBA": "StolenBaseAttempts",
        "PO": "PutOuts",
        "A": "Assists",
        "E": "Errors",
        "DP": "DoublePlays",
        "TP": "TriplePlays",
        "C SBA": "StolenBaseAttemptsCatcher",
        "CS": "CaughtStealing",
        "PB": "PassedBalls",
        "GS": "Start",
        "W": "Win",
        "L": "Loss",
        "SV": "Save",
        "APP": "Appearances",
        "CG": "CompleteGame",
        "SHO": "ShutOut",
        "NH": "NoHitter",
        "PG": "PerfectGame",
        "IP": "InningsPitched",
        "IP.Part": "PartialInningPitched",
        "BF": "BattersFaced",
        "RA": "RunsAgainst",
        "ER": "EarnedRuns",
        "H Allowed": "HitsAgainst",
        "2B Allowed": "DoublesAgainst",
        "3B Allowed": "TriplesAgainst",
        "HR Allowed": "HomeRunsAgainst",
        "SF Pitcher": "SacrificeFlyPitcher",
        "SAC Pitcher": "SacrificeBuntPitcher",
        "BB Allowed": "BaseOnBallsAgainst",
        "K": "BattersStruckOut",
        "HB": "HitBatter",
        "BK": "Balks",
        "WP": "WildPitches",
        "NP": "NumberOfPitches",
        "Pickoffs": "PickOffs",
        "SB Against Pitcher": "StolenBasesPitcher",
    },
    "Basketball": {},
    "Soccer": {
        "Jersey": "Jersey",
        "number": "Jersey",
        "Minutes": "FieldMinutesPlayed",
        "Goals": "Goals",
        "Assists": "Assists",
        "Shots": "Shots",
        "Shots on Goal": "ShotsOnGoal",
        "Steals": "Steals",
        "PK Made": "PenaltyKicksMade",
        "PK Att": "PenaltyKicksAttempted",
        "Corner Kicks": "CornerKicks",
        "GWG": "GameWinningGoal",
        "YC": "YellowCards",
        "RC": "RedCards",
        "GK Minutes": "MinutesPlayed",
        "OT Minutes": "OvertimeMinutesPlayed",
        "Goals Against": "GoalsAgainst",
        "Saves": "Saves",
        "Opp Shots on Goal": "OpponentShotsOnGoal",
        "Opp PK Saves": "OpponentPenaltyKickSaves",
        "Opp PK Att": "OpponentPenaltyKickAttempts",
        "Shutouts": "ShutOuts",
        "Win": "Win",
        "Loss": "Loss",
        "Tie": "Tie",
    },
    "Lacrosse": {},
}

# --- UI: Sport selection ---
st.header("Select sport")
sport = st.selectbox("Sport", options=list(SPORT_FIELDS.keys()), index=0)
CURRENT_DEFAULT_MAP = DEFAULT_FIELD_MAP_BY_SPORT.get(sport, {})

# --- UI: Load source data ---
st.header("Load your totals")
source_choice = st.radio(
    "How do you want to load the totals?",
    ["Google Sheet", "Upload CSV/Excel"],
    horizontal=True,
)

sheet_url: Optional[str] = None
uploaded_df: Optional[pd.DataFrame] = None
game_frames: Dict[str, pd.DataFrame] = {}
selected_game_tabs: List[str] = []
export_mode = "Individual game files"

if source_choice == "Google Sheet":
    if gspread is None:
        st.error("gspread is not installed in this environment. Please `pip install gspread`.")

    sheet_url = st.text_input(
        "Google Sheet URL",
        placeholder="https://docs.google.com/spreadsheets/d/.../edit#gid=...",
        help="Paste the same season sheet you use to save your game logs and totals.",
    )

    if sheet_url and gspread is not None:
        try:
            titles = list_google_sheet_titles(sheet_url)
            game_tabs = sorted(
                [title for title in titles if is_totals_tab_for_sport(title, sport)],
                key=game_tab_sort_key,
            )

            if not game_tabs:
                st.info(f"No {sport} game Totals tabs were found in this sheet yet.")
            else:
                selection_mode = st.radio(
                    "Which games do you want to export?",
                    ["Choose game(s)", "All games"],
                    horizontal=True,
                    key=f"game_selection_mode_{sport}",
                )

                if selection_mode == "All games":
                    selected_game_tabs = game_tabs
                    st.caption(f"All {len(game_tabs)} {sport} game(s) selected.")
                else:
                    selected_game_tabs = st.multiselect(
                        "Choose game(s)",
                        options=game_tabs,
                        default=game_tabs[-1:],
                        format_func=format_game_tab_label,
                        key=f"selected_game_tabs_{sport}",
                        help="Select one game for a normal export, or select several to export them together.",
                    )

                if selected_game_tabs:
                    game_frames = load_google_game_frames(sheet_url, tuple(selected_game_tabs))
                    nonempty_frames = [df for df in game_frames.values() if df is not None and not df.empty]
                    if nonempty_frames:
                        uploaded_df = pd.concat(nonempty_frames, ignore_index=True, sort=False)
                    total_rows = sum(len(df) for df in game_frames.values())
                    st.success(
                        f"Loaded {len(game_frames)} game(s) with {total_rows} player-total rows."
                    )

                    with st.expander("Selected games", expanded=False):
                        for title in selected_game_tabs:
                            st.write(f"• {format_game_tab_label(title)}")

                    if len(selected_game_tabs) > 1:
                        export_mode = st.radio(
                            "Export mode",
                            ["Individual game files", "Combined season totals (reconciliation)"],
                            horizontal=True,
                            help=(
                                "Individual game files creates one MaxPreps file per game. "
                                "Combined season totals sums the selected games by jersey and is intended for reconciliation/reference."
                            ),
                        )
                        if export_mode.startswith("Combined"):
                            st.warning(
                                "Combined season totals are for reconciliation/reference. "
                                "Do not upload the combined file as the stats for a single game."
                            )

                    with st.expander("Preview columns", expanded=False):
                        st.write(list(uploaded_df.columns) if uploaded_df is not None else [])
        except Exception as e:
            st.warning(f"Unable to read sheet or game tabs: {e}")
else:
    up = st.file_uploader("Upload totals CSV or Excel", type=["csv", "xlsx", "xls"])
    if up is not None:
        try:
            if up.name.lower().endswith(".csv"):
                uploaded_df = pd.read_csv(up)
            else:
                uploaded_df = pd.read_excel(up)
            st.success(f"Loaded {len(uploaded_df)} rows from file '{up.name}'.")
            with st.expander("Preview columns", expanded=False):
                st.write(list(uploaded_df.columns))
        except Exception as e:
            st.error(f"Failed to read file: {e}")

st.divider()

columns_available: List[str] = list(uploaded_df.columns) if uploaded_df is not None else []
jersey_guess: Optional[str] = guess_jersey_column(columns_available) if columns_available else None

# --- Export form ---
with st.form("export_form"):
    st.subheader("Export Settings")

    if columns_available:
        default_index = 0
        if jersey_guess and jersey_guess in columns_available:
            default_index = columns_available.index(jersey_guess)
        jersey_col = st.selectbox(
            "Jersey column (choose from loaded sheet)",
            options=columns_available,
            index=default_index,
            help="Pick the column that contains jersey numbers. If you need leading zeros, store them as text in your sheet.",
        )
    else:
        jersey_col = st.text_input(
            "Jersey column header",
            value="Jersey",
            help="Will be matched case-insensitively when data is loaded.",
        )

    st.markdown("**Declared MaxPreps fields for** " + sport)
    field_list_text = st.text_area(
        "Fields (excluding 'Jersey'), one per line in the desired order",
        value="\n".join(SPORT_FIELDS.get(sport, [])),
        height=160,
        help="Paste the exact MaxPreps field names for this sport if you want to override the defaults.",
    )
    SPORT_FIELDS[sport] = [f.strip() for f in field_list_text.splitlines() if f.strip()]

    st.markdown("**Field Mapping** – map your sheet columns to MaxPreps fields.")
    default_map_rows = (
        [{"Sheet Column": k, "MaxPreps Field": v} for k, v in CURRENT_DEFAULT_MAP.items()]
        if CURRENT_DEFAULT_MAP else [{"Sheet Column": "Jersey", "MaxPreps Field": "Jersey"}]
    )
    mapping_editor = st.data_editor(
        pd.DataFrame(default_map_rows),
        num_rows="dynamic",
        use_container_width=True,
        key="mapping_editor",
    )

    if source_choice == "Google Sheet" and selected_game_tabs:
        if len(selected_game_tabs) == 1:
            default_output_name = game_export_filename(selected_game_tabs[0], sport).removesuffix(".txt")
        elif export_mode.startswith("Combined"):
            default_output_name = f"maxpreps_{sport.lower()}_combined_season_totals"
        else:
            default_output_name = f"maxpreps_{sport.lower()}_selected_games"
    else:
        default_output_name = f"maxpreps_{sport.lower()}_import"

    default_filename = st.text_input(
        "Output filename",
        value=default_output_name,
        help=(
            "Used for a single/combined .txt export or as the .zip name when exporting multiple individual games. "
            "Individual files inside the zip are named automatically by date and opponent."
        ),
    )

    submitted = st.form_submit_button("Build export")

if submitted:
    if uploaded_df is None or uploaded_df.empty:
        st.error("No data loaded. Select game totals above or upload a totals file first.")
    else:
        edited_map: Dict[str, str] = {}
        for _, row in mapping_editor.iterrows():
            sheet_col = str(row.get("Sheet Column", "")).strip()
            mp_field = str(row.get("MaxPreps Field", "")).strip()
            if sheet_col and mp_field:
                edited_map[sheet_col] = mp_field

        declared_fields = SPORT_FIELDS.get(sport, [])

        try:
            if source_choice == "Google Sheet" and game_frames:
                if len(game_frames) > 1 and export_mode == "Individual game files":
                    generated_files: Dict[str, str] = {}
                    for title, df in game_frames.items():
                        txt = build_maxpreps_txt_for_sport(
                            df,
                            edited_map,
                            jersey_col,
                            declared_fields,
                        )
                        generated_files[game_export_filename(title, sport)] = txt

                    zip_bytes = make_zip(generated_files)
                    zip_name = sanitize_zip_filename(default_filename)
                    st.success(f"Generated {len(generated_files)} MaxPreps game files.")
                    st.download_button(
                        "Download all game files (.zip)",
                        data=zip_bytes,
                        file_name=zip_name,
                        mime="application/zip",
                    )

                    with st.expander("Files included"):
                        for filename in generated_files:
                            st.write(f"• {filename}")

                    first_filename = next(iter(generated_files))
                    with st.expander(f"Preview: {first_filename} (first 25 lines)"):
                        preview = "\n".join(generated_files[first_filename].splitlines()[:25])
                        st.code(preview, language="text")

                elif len(game_frames) > 1 and export_mode.startswith("Combined"):
                    combined_df = combine_game_totals(game_frames, edited_map, jersey_col)
                    txt = build_maxpreps_txt_for_sport(
                        combined_df,
                        edited_map,
                        jersey_col,
                        declared_fields,
                    )
                    fname = sanitize_filename(default_filename)
                    st.success(
                        f"Combined {len(game_frames)} games into one season-total reconciliation file."
                    )
                    st.download_button(
                        "Download combined season totals .txt",
                        data=txt.encode("utf-8"),
                        file_name=fname,
                        mime="text/plain",
                    )
                    with st.expander("Combined totals preview", expanded=False):
                        st.dataframe(combined_df, use_container_width=True)
                    with st.expander("MaxPreps file preview (first 25 lines)"):
                        st.code("\n".join(txt.splitlines()[:25]), language="text")

                else:
                    title, df = next(iter(game_frames.items()))
                    txt = build_maxpreps_txt_for_sport(
                        df,
                        edited_map,
                        jersey_col,
                        declared_fields,
                    )
                    fname = sanitize_filename(default_filename)
                    st.success(f"MaxPreps {sport} file generated for {format_game_tab_label(title)}.")
                    st.download_button(
                        "Download .txt",
                        data=txt.encode("utf-8"),
                        file_name=fname,
                        mime="text/plain",
                    )
                    with st.expander("Preview (first 25 lines)"):
                        st.code("\n".join(txt.splitlines()[:25]), language="text")
            else:
                txt = build_maxpreps_txt_for_sport(
                    uploaded_df,
                    edited_map,
                    jersey_col,
                    declared_fields,
                )
                fname = sanitize_filename(default_filename)
                st.success(f"MaxPreps {sport} import file generated.")
                st.download_button(
                    "Download .txt",
                    data=txt.encode("utf-8"),
                    file_name=fname,
                    mime="text/plain",
                )
                with st.expander("Preview (first 25 lines)"):
                    st.code("\n".join(txt.splitlines()[:25]), language="text")
        except Exception as e:
            st.error(str(e))

# --- Developer Tools ---
with st.expander("Developer tools", expanded=False):
    if st.button("Run internal test"):
        def _test_build_maxpreps_txt():
            sample = pd.DataFrame([
                {
                    "number": 12,
                    "Rush Attempts": 5,
                    "Rushing Yards": 42,
                    "Rushing TDs": 1,
                    "Receptions": 3,
                    "Receiving Yards": 28,
                    "Receiving TDs": 0,
                    "Pass Attempts": 8,
                    "Pass Completions": 5,
                    "Passing Interceptions": 1,
                    "Pass Yards": 67,
                    "Passing TDs": 1,
                    "Tackles": 2,
                    "Sacks": 0,
                    "Interceptions": 1,
                    "Interception Return Yards": 15,
                    "Interception Return TDs": 1,
                    "Forced Fumbles": 1,
                    "Fumble Recoveries": 1,
                    "Fumble Recovery Yards": 4,
                    "Fumble Return TDs": 0,
                    "Punt Returns": 2,
                    "Punt Return Yards": 22,
                    "Punt Return TDs": 0,
                    "Kickoff Returns": 1,
                    "Kickoff Return Yards": 30,
                    "Kickoff Return TDs": 0,
                    "Total Return Yards": 71,
                    "2-pt Rushing Conversions": 1,
                    "2-pt Receiving Conversions": 0,
                    "2-pt Conversion Points": 2,
                },
                {
                    "number": 10,
                    "Rush Attempts": 3,
                    "Rushing Yards": 12,
                    "Rushing TDs": 0,
                    "Receptions": 0,
                    "Receiving Yards": 0,
                    "Receiving TDs": 0,
                    "Pass Attempts": 0,
                    "Pass Completions": 0,
                    "Passing Interceptions": 0,
                    "Pass Yards": 0,
                    "Passing TDs": 0,
                    "Tackles": 0,
                    "Sacks": 0,
                    "Interceptions": 0,
                    "Interception Return Yards": 0,
                    "Interception Return TDs": 0,
                    "Forced Fumbles": 0,
                    "Fumble Recoveries": 0,
                    "Fumble Recovery Yards": 0,
                    "Fumble Return TDs": 0,
                    "Punt Returns": 0,
                    "Punt Return Yards": 0,
                    "Punt Return TDs": 0,
                    "Kickoff Returns": 0,
                    "Kickoff Return Yards": 0,
                    "Kickoff Return TDs": 0,
                    "Total Return Yards": 0,
                    "2-pt Rushing Conversions": 0,
                    "2-pt Receiving Conversions": 0,
                    "2-pt Conversion Points": 0,
                },
            ])

            txt = build_maxpreps_txt(sample, DEFAULT_FIELD_MAP, jersey_column_name="number")
            lines = txt.strip().splitlines()
            header = lines[1].split("|")

            assert lines[0] == SUPPLIER_ID
            assert header[0] == "Jersey"
            assert "RushingNum" in header
            assert "RushingYards" in header
            assert "RushingTDNum" in header
            assert "ReceivingNum" in header
            assert "ReceivingYards" in header
            assert "PassingComp" in header
            assert "PassingAtt" in header
            assert "PassingInt" in header
            assert "PassingYards" in header
            assert "PassingTD" in header
            assert "TotalTackles" in header
            assert "INTs" in header
            assert "INTYards" in header
            assert "IntReturnedTDNum" in header
            assert "FumbleRecoveries" in header
            assert "FumbleRecoveryYards" in header
            assert "PuntReturnNum" in header
            assert "PuntReturnYards" in header
            assert "KickoffReturnNum" in header
            assert "KickoffReturnYards" in header
            assert "TotalReturnYards" in header
            assert "PATRushingNum" in header
            assert "PATReceivingNum" in header
            assert "TotalConversionPoints" in header
            assert lines[2].startswith("12|")
            assert lines[3].startswith("10|")

            # Multi-game helper checks
            assert format_game_tab_label("Football 2026-08-21 vs Agoura (Totals)") == "Aug 21, 2026 — Agoura"

            game_one = pd.DataFrame([
                {"number": 12, "Rush Attempts": 5, "Rushing Yards": 42, "Pass Attempts": 8}
            ])
            game_two = pd.DataFrame([
                {"number": 12, "Rush Attempts": 3, "Rushing Yards": 20, "Pass Attempts": 4}
            ])
            combined = combine_game_totals(
                {"g1": game_one, "g2": game_two},
                DEFAULT_FIELD_MAP,
                "number",
            )
            qb = combined.loc[combined["number"].astype(str) == "12"].iloc[0]
            assert int(qb["Rush Attempts"]) == 8
            assert int(qb["Rushing Yards"]) == 62
            assert int(qb["Pass Attempts"]) == 12

            zip_bytes = make_zip({"a.txt": "hello", "b.txt": "world"})
            assert len(zip_bytes) > 0

            return True

        _test_build_maxpreps_txt()
        st.success("Internal unit test passed.")
