"""
Streamlit page to export a selected Google Sheet totals tab to a MaxPreps-compatible
pipe-delimited .txt import file.

This version **hard-codes** the Stat Supplier ID as the first line of the export:
    669ae75f-4563-494a-8c17-370aaa8539d4

Includes a lightweight unit test for validation and UI improvements:
- Google Sheet tab dropdown (filters to tabs with "Totals" by default)
- Auto-detects likely jersey column and lets you pick from a dropdown
- Case-insensitive fallback in the exporter if the jersey header's casing differs
- Multi-sport support (Football, Baseball, Soccer; Basketball/Lacrosse placeholders)
"""
from __future__ import annotations
import re
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

try:
    import gspread  # type: ignore
except Exception:
    gspread = None

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
    "number": "Jersey",  # common header in your totals
    # Offensive – Rushing
    "Rush Att": "RushingNum",
    "Rush Yds": "RushingYards",
    "Rush Long": "RushingLong",
    # Offensive – Receiving
    "Rec": "ReceivingNum",
    "Rec Yds": "ReceivingYards",
    "Rec Long": "ReceivingLong",
    # Offensive – Passing
    "Pass Cmp": "PassingComp",
    "Pass Att": "PassingAtt",
    "Pass Int": "PassingInt",
    "Pass Yds": "PassingYards",
    "Pass TD": "PassingTD",
    "Pass Long": "PassingLong",
    # Offensive – Fumbles
    "Off Fum": "OffensiveFumbles",
    "Off Fum Lost": "OffensiveFumblesLost",
    # O-Line
    "Pancakes": "PancakeBlocks",
    # Defensive – Tackles
    "Solo Tkl": "Tackles",
    "Ast Tkl": "Assists",
    "Tot Tkl": "TotalTackles",
    "TFL": "TacklesForLoss",
    # Sacks
    "Sacks": "Sacks",
    "Sack Yds Lost": "SacksYardsLost",
    "QB Hurries": "QBHurries",
    # Pass Defense
    "INT": "INTs",
    "INT Yds": "INTYards",
    "Pass Def": "PassesDefensed",
    # Blocks
    "Blk Punt": "BlockedPunts",
    "Blk FG": "BlockedFG",
    # Fumbles
    "Fum Rec": "FumbleRecoveries",
    "FR Yds": "FumbleRecoveryYards",
    "FF": "CausedFumbles",
    # Punt Returns
    "PR": "PuntReturnNum",
    "PR Yds": "PuntReturnYards",
    "PR Long": "PuntReturnLong",
    "PR FC": "PuntReturnFairCatches",
    # Kickoff Returns
    "KR": "KickoffReturnNum",
    "KR Yds": "KickoffReturnYards",
    "KR Long": "KickoffReturnLong",
    # Total Returns
    "Total Return Yds": "TotalReturnYards",
    # Punts
    "Punts": "PuntNum",
    "Punt Yds": "PuntYards",
    "Punt Long": "PuntLong",
    "Punt Inside 20": "PuntInside20",
    # Kickoffs
    "Kickoffs": "KickoffNum",
    "KO Yds": "KickoffYards",
    "KO Long": "KickoffLong",
    "KO TB": "KickoffTouchbacks",
    # Scoring
    "TD": "Touchdowns",
    "Rush TD": "RushingTDNum",
    "Rec TD": "ReceivingTDNum",
    "Fum Ret TD": "FumbleReturnedTDNum",
    "INT Ret TD": "IntReturnedTDNum",
    "Punt Ret TD": "PuntReturnedTDNum",
    "KO Ret TD": "KickoffReturnedTDNum",
    "Total TD": "TotalTDNum",
    # PAT Kicks
    "PAT Made": "PATKickingMade",
    "PAT Att": "PATKickingAtt",
    "PAT Pts": "PATKickingPoints",
    # Conversions
    "PAT Rush": "PATRushingNum",
    "PAT Rec": "PATReceivingNum",
    "Conv Pts": "TotalConversionPoints",
    # Field Goals
    "FG Made": "FGMade",
    "FG Att": "FGAttempted",
    "FG Long": "FGLong",
    # Safeties
    "Safeties": "Safeties",
    # Points
    "Pts": "TotalPoints",
}

# ------------------ Helpers ------------------

def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\"'()]", "", name)
    if not name.lower().endswith(".txt"):
        name += ".txt"
    return name


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
    """Return the actual DF column whose lowercase matches name.lower().
    If not found, return the original name so caller can error.
    """
    target = name.strip().lower()
    for c in df.columns:
        if str(c).strip().lower() == target:
            return c
    return name


def guess_jersey_column(columns: List[str]) -> Optional[str]:
    candidates = [
        "jersey", "number", "player number", "no", "num", "player #", "player_num", "player_number",
    ]
    lowered = {c.lower(): c for c in columns}
    for key in candidates:
        if key in lowered:
            return lowered[key]
    # Heuristic: any column containing 'jersey' or 'number'
    for c in columns:
        cl = c.lower()
        if "jersey" in cl or "number" in cl or cl in ("#", "no", "num"):
            return c
    return None


def choose_fields_to_include(df: pd.DataFrame, field_map: Dict[str, str]) -> List[str]:
    included: List[str] = []
    for sheet_col, mp_field in field_map.items():
        if mp_field == "Jersey":
            continue
        if sheet_col in df.columns:
            has_any = df[sheet_col].apply(lambda v: not pd.isna(v) and str(v).strip() != "").any()
            if has_any:
                included.append(mp_field)
    order = [f for f in MAXPREPS_FIELDS if f != "Jersey"]
    included_sorted = [f for f in order if f in included]
    extras = [f for f in included if f not in included_sorted]
    return included_sorted + extras


def build_maxpreps_txt(df: pd.DataFrame, field_map: Dict[str, str], jersey_column_name: str = "Jersey") -> str:
    supplier_id = "669ae75f-4563-494a-8c17-370aaa8539d4"
    # Case-insensitive resolve for jersey column
    jersey_column_resolved = resolve_column_name_case_insensitive(df, jersey_column_name)
    if jersey_column_resolved not in df.columns:
        raise ValueError(
            f"Missing required jersey column: '{jersey_column_name}'. Available columns: {list(df.columns)}"
        )

    reverse_map: Dict[str, str] = {}
    for sheet_col, mp_field in field_map.items():
        # Resolve each map key case-insensitively as well
        if sheet_col in df.columns:
            reverse_map[mp_field] = sheet_col
        else:
            resolved = resolve_column_name_case_insensitive(df, sheet_col)
            if resolved in df.columns:
                reverse_map[mp_field] = resolved

    included_fields = choose_fields_to_include(df, field_map)
    header_fields = ["Jersey"] + included_fields

    lines: List[str] = [supplier_id, "|".join(header_fields)]

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

# ------------------ UI ------------------
st.title("Export to MaxPreps (.txt)")
st.markdown(
    "Generates a MaxPreps import file based on sport selection"
)

# --- Sport registry ---
SPORT_FIELDS: Dict[str, List[str]] = {
    # Always exclude "Jersey" here; we'll prepend it automatically in the header
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
        # Batting / Baserunning
        "AtBats","Runs","Singles","Doubles","Triples","HomeRuns","Hits","RunsBattedIn",
        "SacrificeFly","SacrificeBunt","BaseOnBalls","StruckOut","HitByPitch","ReachedOnError",
        "FieldersChoice","LeftOnBase","GrandSlams",
        # Baserunning
        "StolenBase","StolenBaseAttempts",
        # Fielding
        "PutOuts","Assists","Errors","DoublePlays","TriplePlays",
        # Catcher stats
        "StolenBaseAttemptsCatcher","CaughtStealing","PassedBalls",
        # Pitching
        "Start","Win","Loss","Save","Appearances","CompleteGame","ShutOut","NoHitter","PerfectGame",
        "InningsPitched","PartialInningPitched","BattersFaced","RunsAgainst","EarnedRuns","HitsAgainst",
        "DoublesAgainst","TriplesAgainst","HomeRunsAgainst","SacrificeFlyPitcher","SacrificeBuntPitcher",
        "BaseOnBallsAgainst","BattersStruckOut","HitBatter","Balks","WildPitches","NumberOfPitches",
        "PickOffs","StolenBasesPitcher",
    ],
    "Basketball": [],
    "Soccer": [
        # Field (outfield) stats
        "FieldMinutesPlayed",
        "Goals","Assists","Shots","ShotsOnGoal","Steals",
        "PenaltyKicksMade","PenaltyKicksAttempted","CornerKicks",
        "GameWinningGoal","YellowCards","RedCards",
        # Goaltending stats
        "MinutesPlayed","OvertimeMinutesPlayed","GoalsAgainst","Saves",
        "OpponentShotsOnGoal","OpponentPenaltyKickSaves","OpponentPenaltyKickAttempts",
        "ShutOuts","Win","Loss","Tie",
    ],
    "Lacrosse": [],
}

# Default per-sport sheet->MaxPreps mapping (Football prefilled; others seeded)
DEFAULT_FIELD_MAP_BY_SPORT: Dict[str, Dict[str, str]] = {
    "Football": DEFAULT_FIELD_MAP,
    "Baseball": {
        # Roster
        "Jersey": "Jersey",
        "number": "Jersey",
        # Batting
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
        # Baserunning
        "SB": "StolenBase",
        "SBA": "StolenBaseAttempts",
        # Fielding
        "PO": "PutOuts",
        "A": "Assists",
        "E": "Errors",
        "DP": "DoublePlays",
        "TP": "TriplePlays",
        # Catcher
        "C SBA": "StolenBaseAttemptsCatcher",
        "CS": "CaughtStealing",
        "PB": "PassedBalls",
        # Pitching
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
        # Starter mappings – tweak to match your sheet headers
        "Jersey": "Jersey",
        "number": "Jersey",
        # Field stats
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
        # Goalkeeper stats
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

# The field map used for the selected sport (editable below)
CURRENT_DEFAULT_MAP = DEFAULT_FIELD_MAP_BY_SPORT.get(sport, {})

# --- UI: Load source data ---
st.header("Load your totals")
source_choice = st.radio(
    "How do you want to load the totals?",
    ["Google Sheet", "Upload CSV/Excel"],
    horizontal=True,
)

sheet_url: Optional[str] = None
worksheet_name: Optional[str] = None
uploaded_df: Optional[pd.DataFrame] = None

if source_choice == "Google Sheet":
    if gspread is None:
        st.error("gspread is not installed in this environment. Please `pip install gspread`.")
    sheet_url = st.text_input(
        "Google Sheet URL",
        placeholder="https://docs.google.com/spreadsheets/d/.../edit#gid=...",
        help="Paste the same sheet you save your game logs/totals to.",
    )

    worksheet_name = None
    if sheet_url and gspread is not None:
        try:
            sa = gspread.service_account_from_dict(st.secrets["gcp_service_account"])  # type: ignore
            sh = sa.open_by_url(sheet_url)
            titles = [ws.title for ws in sh.worksheets()]
            only_totals = st.checkbox("Show only tabs that look like Totals", value=True)
            filtered = [t for t in titles if ("total" in t.lower())] if only_totals else titles
            if not filtered:
                st.info("No tabs matching 'Totals' found. Uncheck the box to see all tabs.")
                filtered = titles
            worksheet_name = st.selectbox("Choose worksheet (tab)", options=filtered)

            if worksheet_name:
                ws = sh.worksheet(worksheet_name)
                rows = ws.get_all_records()
                uploaded_df = pd.DataFrame(rows)
                st.success(f"Loaded {len(uploaded_df)} rows from '{worksheet_name}'.")
                with st.expander("Preview columns", expanded=False):
                    st.write(list(uploaded_df.columns))
        except Exception as e:
            st.warning(f"Unable to read sheet or list tabs: {e}")
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

# Prepare jersey guess before rendering the form
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
    # Allow override of the field list for the selected sport
    field_list_text = st.text_area(
        "Fields (excluding 'Jersey'), one per line in the desired order",
        value="\n".join(SPORT_FIELDS.get(sport, [])),
        height=160,
        help="Paste the exact MaxPreps field names for this sport if you want to override the defaults.",
    )
    # Update registry for current run
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

    default_filename = st.text_input(
        "Output filename",
        value=f"maxpreps_{sport.lower()}_import",
        help="We'll sanitize this and ensure it ends with .txt",
    )

    submitted = st.form_submit_button("Build .txt")

if submitted:
    if uploaded_df is None or uploaded_df.empty:
        st.error("No data loaded. Load a totals tab above first (Google Sheet or file upload).")
    else:
        # Build mapping
        edited_map: Dict[str, str] = {}
        for _, r in mapping_editor.iterrows():
            sheet_col = str(r.get("Sheet Column", "")).strip()
            mp_field = str(r.get("MaxPreps Field", "")).strip()
            if sheet_col and mp_field:
                edited_map[sheet_col] = mp_field

        # Compute included fields respecting the declared list for the sport
        def choose_fields_to_include_for_sport(df: pd.DataFrame, field_map: Dict[str, str], declared: List[str]) -> List[str]:
            included: List[str] = []
            for sheet_col, mp_field in field_map.items():
                if mp_field == "Jersey":
                    continue
                if sheet_col in df.columns:
                    has_any = df[sheet_col].apply(lambda v: not pd.isna(v) and str(v).strip() != "").any()
                    if has_any and (not declared or mp_field in declared):
                        included.append(mp_field)
            order = declared if declared else [f for f in MAXPREPS_FIELDS if f != "Jersey"]
            included_sorted = [f for f in order if f in included]
            extras = [f for f in included if f not in included_sorted]
            return included_sorted + extras

        declared_fields = SPORT_FIELDS.get(sport, [])
        try:
            # Build using a wrapper that pins the included order
            def _build(df=uploaded_df, fmap=edited_map, jersey=jersey_col):
                # Rebuild reverse_map respecting case-insensitive names
                reverse_map: Dict[str, str] = {}
                for sheet_col, mp_field in fmap.items():
                    if sheet_col in df.columns:
                        reverse_map[mp_field] = sheet_col
                    else:
                        resolved = resolve_column_name_case_insensitive(df, sheet_col)
                        if resolved in df.columns:
                            reverse_map[mp_field] = resolved
                # Compute included set
                included = choose_fields_to_include_for_sport(df, fmap, declared_fields)
                header_fields = ["Jersey"] + included
                lines: List[str] = ["669ae75f-4563-494a-8c17-370aaa8539d4", "|".join(header_fields)]
                jersey_col_res = resolve_column_name_case_insensitive(df, jersey)
                if jersey_col_res not in df.columns:
                    raise ValueError(f"Missing required jersey column: '{jersey}'. Available columns: {list(df.columns)}")
                for _, row in df.iterrows():
                    jersey_val = coerce_str_no_nan(row[jersey_col_res])
                    if jersey_val == "":
                        continue
                    values: List[str] = [jersey_val]
                    for mpf in included:
                        scol = reverse_map.get(mpf)
                        val = coerce_str_no_nan(row[scol]) if scol else ""
                        values.append(val)
                    if any(v != "" for v in values[1:]):
                        lines.append("|".join(values))
                return "\n".join(lines) + "\n"

            txt = _build()
        except Exception as e:
            st.error(str(e))
        else:
            fname = sanitize_filename(default_filename)
            st.success(f"MaxPreps {sport} import file generated.")
            st.download_button("Download .txt", data=txt.encode("utf-8"), file_name=fname, mime="text/plain")

            # Preview the first 25 lines of the generated file
            with st.expander("Preview (first 25 lines)"):
                preview_lines: List[str] = []
                for i, line in enumerate(txt.splitlines()):
                    if i >= 25:
                        break
                    preview_lines.append(line)
                st.code("\n".join(preview_lines), language="text")

# --- Developer Tools ---
with st.expander("Developer tools", expanded=False):
    if st.button("Run internal test"):
        def _test_build_maxpreps_txt():
            sample = pd.DataFrame([
                {"Jersey": "12", "Rush Att": 5, "Rush Yds": 42},
                {"Jersey": "10", "Rush Att": 3, "Rush Yds": 12},
            ])
            txt = build_maxpreps_txt(sample, DEFAULT_FIELD_MAP)
            lines = txt.strip().splitlines()
            assert lines[0] == "669ae75f-4563-494a-8c17-370aaa8539d4"
            header = lines[1].split("|")
            assert header[0] == "Jersey"
            assert "RushingNum" in header and "RushingYards" in header
            assert lines[2].startswith("12|")
            return True
        _test_build_maxpreps_txt()
        st.success("Internal unit test passed.")

