"""
Multi-Sport Stats App (Streamlit + Google Sheets)
------------------------------------------------
Scaffolded for multiple sports with a pluggable registry.

Included sports: Football ✅, Soccer ✅, Lacrosse ✅, Baseball ⚠️, Basketball ✅.

Features
- Create a game (sport + date + opponent + Google Sheet URL/ID)
- Read Roster from first worksheet (or one named "Roster") with columns:
  [Player First Name, Player Last Name, Player Number, Player Position(s)]
- Sport-specific **Log a Stat** form via a SportSpec plugin
- Automatically persist each stat to the game's Google Sheet Log tab
- Resume an existing game from its saved Log tab
- Running event log and sport-specific totals
- Save/refresh totals back to Google Sheets
- CSV roster import to (re)create the "Roster" tab

Run
- streamlit run app.py

Requires in requirements.txt:
streamlit==1.37.0
pandas==2.2.2
gspread==6.1.2
oauth2client==4.1.3
python-dateutil==2.9.0
"""

import os
import re
import time
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Multi-Sport Stats App",
    page_icon="🏅",
    layout="wide",
)
RUNNING_TESTS = os.getenv("PYTEST_RUNNING") == "1"


def main():
    # Main app logic starts here
    if "flash_message" in st.session_state:
        st.success(st.session_state.pop("flash_message"))


# Google Sheets (service account flow only)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# One-time flash (after rerun)
if "flash_message" in st.session_state:
    st.success(st.session_state.pop("flash_message"))

# ---------------------------
# Helpers: Google Sheets (service account)
# ---------------------------
SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive",
]

LOG_COLUMNS = [
    "event_id", "timestamp", "sport", "player_key", "first_name", "last_name", "number", "positions",
    "side", "stat_type", "outcome", "yards", "touchdown", "notes", "on_target", "goal",
    "card", "penalty_minutes", "minutes", "two_point",
]

TRANSIENT_GOOGLE_STATUS_CODES = {429, 500, 502, 503, 504}


def get_gspread_client():
    if "gcp_service_account" not in st.secrets:
        st.error("No Google credentials found. Add them to .streamlit/secrets.toml under [gcp_service_account].")
        st.stop()
    creds_info = dict(st.secrets["gcp_service_account"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_info, SCOPE)
    return gspread.authorize(creds)


def parse_sheet_id_from_url(url_or_id: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9\-_]+)", url_or_id)
    return m.group(1) if m else url_or_id.strip()


def open_sheet(sheet_url_or_id: str):
    gc = get_gspread_client()
    sheet_id = parse_sheet_id_from_url(sheet_url_or_id)
    return gc.open_by_key(sheet_id)


def is_transient_google_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in TRANSIENT_GOOGLE_STATUS_CODES:
        return True

    text = str(exc).upper()
    if "UNAVAILABLE" in text or "RESOURCE_EXHAUSTED" in text or "RATE LIMIT" in text:
        return True
    for code in TRANSIENT_GOOGLE_STATUS_CODES:
        if f"'CODE': {code}" in text or f'"CODE": {code}' in text or f"HTTP {code}" in text:
            return True
    return False


def google_retry(operation, attempts: int = 4, base_delay: float = 1.0):
    last_error = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt == attempts - 1 or not is_transient_google_error(exc):
                raise
            time.sleep(base_delay * (2 ** attempt))
    if last_error:
        raise last_error


def find_roster_ws(sh):
    try:
        return google_retry(lambda: sh.worksheet("Roster"))
    except Exception:
        return google_retry(lambda: sh.get_worksheet(0))


def read_roster_df(sh) -> pd.DataFrame:
    ws = find_roster_ws(sh)
    rows = google_retry(lambda: ws.get_all_records())
    df = pd.DataFrame(rows)
    rename_map = {
        "Player First Name": "first_name",
        "Player Last Name": "last_name",
        "Player Number": "number",
        "Player Position(s)": "positions",
    }
    df = df.rename(columns=rename_map)
    missing = [k for k in rename_map.values() if k not in df.columns]
    if missing:
        st.error(f"Roster is missing columns: {missing}. Expected: {list(rename_map.values())}")
        st.stop()
    df["number"] = pd.to_numeric(df["number"], errors="coerce").astype("Int64")
    df["player_key"] = df.apply(lambda r: f"#{r['number']} {r['first_name']} {r['last_name']}", axis=1)
    return df[["player_key", "first_name", "last_name", "number", "positions"]]


def game_tab_titles(game: Dict[str, Any]):
    stamp = f"{game['sport']} {game['date']} vs {game['opponent']}"
    return f"{stamp} (Totals)", f"{stamp} (Log)"


def column_letter(number: int) -> str:
    letters = ""
    n = number
    while n:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def sheet_cell_value(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return value


def get_worksheet_if_exists(sh, title: str):
    worksheets = google_retry(lambda: sh.worksheets())
    for ws in worksheets:
        if ws.title == title:
            return ws
    return None


def ensure_game_log_worksheet(sh, game: Dict[str, Any]):
    _, log_title = game_tab_titles(game)
    ws = get_worksheet_if_exists(sh, log_title)
    if ws is None:
        ws = google_retry(
            lambda: sh.add_worksheet(
                title=log_title,
                rows="500",
                cols=str(len(LOG_COLUMNS) + 5),
            )
        )
        google_retry(lambda: ws.update([LOG_COLUMNS], range_name="A1"))
        return ws, LOG_COLUMNS.copy()

    all_values = google_retry(lambda: ws.get_all_values())
    headers = list(all_values[0]) if all_values else []

    if not headers:
        headers = LOG_COLUMNS.copy()
        google_retry(lambda: ws.update([headers], range_name="A1"))
        return ws, headers

    missing_headers = [col for col in LOG_COLUMNS if col not in headers]
    if missing_headers:
        headers = headers + missing_headers
        last_col = column_letter(len(headers))
        google_retry(lambda: ws.update([headers], range_name=f"A1:{last_col}1"))

    # Older game logs pre-date event_id. Add stable IDs without deleting/recreating the tab.
    event_id_index = headers.index("event_id")
    data_rows = all_values[1:] if len(all_values) > 1 else []
    if data_rows:
        existing_ids = []
        needs_update = False
        for row in data_rows:
            existing = row[event_id_index].strip() if len(row) > event_id_index else ""
            if not existing:
                existing = uuid.uuid4().hex
                needs_update = True
            existing_ids.append([existing])
        if needs_update:
            event_col = column_letter(event_id_index + 1)
            end_row = len(existing_ids) + 1
            google_retry(
                lambda: ws.update(
                    existing_ids,
                    range_name=f"{event_col}2:{event_col}{end_row}",
                )
            )

    return ws, headers


def normalize_log_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=LOG_COLUMNS)
    normalized = df.copy()
    for col in LOG_COLUMNS:
        if col not in normalized.columns:
            normalized[col] = None
    extras = [col for col in normalized.columns if col not in LOG_COLUMNS]
    return normalized[LOG_COLUMNS + extras]


def load_or_create_game_log(sh, game: Dict[str, Any]) -> pd.DataFrame:
    ws, _ = ensure_game_log_worksheet(sh, game)
    records = google_retry(lambda: ws.get_all_records())
    if not records:
        return pd.DataFrame(columns=LOG_COLUMNS)
    return normalize_log_df(pd.DataFrame(records))


def append_rows_to_game_log(game: Dict[str, Any], rows: List[Dict[str, Any]], attempts: int = 4) -> int:
    if not rows:
        return 0

    sh = google_retry(lambda: open_sheet(game["sheet_id"]))
    ws, headers = ensure_game_log_worksheet(sh, game)
    event_col_index = headers.index("event_id") + 1

    last_error = None
    for attempt in range(attempts):
        try:
            existing_ids = set(
                value.strip()
                for value in google_retry(lambda: ws.col_values(event_col_index))[1:]
                if value and value.strip()
            )
            pending = [row for row in rows if str(row.get("event_id", "")).strip() not in existing_ids]
            if not pending:
                return 0

            values = [
                [sheet_cell_value(row.get(header)) for header in headers]
                for row in pending
            ]
            ws.append_rows(values, value_input_option="RAW")
            return len(pending)
        except Exception as exc:
            last_error = exc
            if attempt == attempts - 1 or not is_transient_google_error(exc):
                raise
            # An append may have succeeded even if the response failed. Re-check event IDs before retrying.
            time.sleep(1.0 * (2 ** attempt))

    if last_error:
        raise last_error
    return 0


def write_df_to_worksheet(sh, title: str, df: pd.DataFrame):
    ws = get_worksheet_if_exists(sh, title)
    rows_needed = max(len(df) + 10, 20)
    cols_needed = max(len(df.columns) + 5, 10)
    if ws is None:
        ws = google_retry(
            lambda: sh.add_worksheet(
                title=title,
                rows=str(rows_needed),
                cols=str(cols_needed),
            )
        )
    else:
        google_retry(lambda: ws.clear())
        google_retry(lambda: ws.resize(rows=rows_needed, cols=cols_needed))

    if len(df.columns) > 0:
        values = [df.columns.tolist()] + df.astype(str).values.tolist()
        google_retry(lambda: ws.update(values, range_name="A1"))
    return ws


# ---------------------------
# SportSpec plugin system
# ---------------------------
class SportSpec:
    name: str = ""
    sides: List[str] = []

    def csv_template(self) -> pd.DataFrame:
        return pd.DataFrame({
            "Player First Name": ["First"],
            "Player Last Name": ["Last"],
            "Player Number": [0],
            "Player Position(s)": ["POS"],
        })

    def build_form(self, roster: pd.DataFrame) -> Dict[str, Any]:
        st.info("Sport not implemented yet. Choose Football, Soccer, or Lacrosse. Additional sports coming soon.")
        st.form_submit_button("Add Stat")
        return {"submitted": False, "new_rows": []}

    def aggregate_totals(self, logs: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame()


# ---------------------------
# Football implementation (fully functional)
# ---------------------------
class FootballSpec(SportSpec):
    name = "Football"
    sides = ["Offense", "Defense"]

    def build_form(self, roster: pd.DataFrame) -> Dict[str, Any]:
        c1, c2, c3 = st.columns([2, 1, 1])
        player_key = c1.selectbox("Player", options=roster["player_key"].tolist(), key="fb_player_select")
        side = c2.radio("Side", options=self.sides, key="fb_side_select", horizontal=True)

        new_rows: List[dict] = []
        with st.form("fb_log_form", clear_on_submit=True):
            yards: Optional[int] = None
            outcome: Optional[str] = None
            touchdown_val: int = 0
            two_point_val: int = 0

            if side == "Offense":
                stat_type = c3.selectbox(
                    "Offensive Stat",
                    options=["Reception", "Run", "Fumble", "Pass", "Field Goal", "Punt", "PAT"],
                    key="fb_stat_off"
                )

                if stat_type in ("Reception", "Run", "Punt"):
                    yards = st.number_input("Yards", value=0, step=1, min_value=-99, max_value=300, key="fb_yards")
                    if stat_type in ("Reception", "Run"):
                        td_flag = st.checkbox("Touchdown", value=False, key="fb_td",
                                              help="Set to 1 if this play scored a TD.")
                        tp_flag = st.checkbox("2-pt Conversion", value=False, key="fb_2pt",
                                              help="Check if this play was a successful 2-point conversion.")
                        touchdown_val = 1 if td_flag else 0
                        two_point_val = 1 if tp_flag else 0

                elif stat_type == "Pass":
                    outcome = st.selectbox(
                        "Pass Outcome",
                        options=["Complete", "Incomplete", "Interception"],
                        key="fb_pass_outcome"
                    )
                    receiver_key = None
                    if outcome == "Complete":
                        yards = st.number_input("Pass Yards (if complete)", value=0, step=1, min_value=-99, max_value=300, key="fb_yards")
                        receiver_options = [pk for pk in roster["player_key"].tolist() if pk != player_key]
                        receiver_key = st.selectbox(
                            "Receiver (to auto-log paired Reception)",
                            options=receiver_options if receiver_options else ["No eligible receivers"],
                            key="fb_receiver"
                        )
                        st.checkbox(
                            "Also log paired Reception for the receiver",
                            value=True,
                            key="fb_pair_reception",
                            help="Creates a Reception for the selected receiver with same yards and flags."
                        )
                        td_flag = st.checkbox("Touchdown", value=False, key="fb_td",
                                              help="Set to 1 if this pass resulted in a TD.")
                        tp_flag = st.checkbox("2-pt Conversion", value=False, key="fb_2pt",
                                              help="Check if this pass was a successful 2-point conversion.")
                        touchdown_val = 1 if td_flag else 0
                        two_point_val = 1 if tp_flag else 0

                elif stat_type == "Field Goal":
                    outcome = st.selectbox("Field Goal Outcome", options=["Made", "Miss"], key="fb_fg_outcome")
                    yards = st.number_input("Attempt Distance (yards)", value=0, step=1, min_value=0, max_value=90, key="fb_yards")

                elif stat_type == "PAT":
                    outcome = st.selectbox("PAT Outcome", options=["Made", "Miss"], key="fb_pat_outcome")

            else:
                stat_type = c3.selectbox(
                    "Defensive Stat",
                    options=[
                        "Forced Fumble", "Fumble Recovery", "Sack", "Interception", "Tackle",
                        "Punt Return", "Kickoff Return"
                    ],
                    key="fb_stat_def"
                )

                if stat_type == "Interception":
                    yards = st.number_input("Interception Return Yards", value=0, step=1, min_value=-99, max_value=300, key="fb_yards")
                    td_flag = st.checkbox("Touchdown", value=False, key="fb_td",
                                          help="Check if the interception was returned for a TD.")
                    touchdown_val = 1 if td_flag else 0
                elif stat_type == "Fumble Recovery":
                    yards = st.number_input("Fumble Recovery Return Yards", value=0, step=1, min_value=-99, max_value=300, key="fb_yards")
                    td_flag = st.checkbox("Touchdown", value=False, key="fb_td",
                                          help="Check if the fumble recovery was returned for a TD.")
                    touchdown_val = 1 if td_flag else 0
                elif stat_type == "Punt Return":
                    yards = st.number_input("Punt Return Yards", value=0, step=1, min_value=-99, max_value=300, key="fb_yards")
                    td_flag = st.checkbox("Touchdown", value=False, key="fb_td",
                                          help="Check if the punt return scored a TD.")
                    touchdown_val = 1 if td_flag else 0
                elif stat_type == "Kickoff Return":
                    yards = st.number_input("Kickoff Return Yards", value=0, step=1, min_value=-99, max_value=300, key="fb_yards")
                    td_flag = st.checkbox("Touchdown", value=False, key="fb_td",
                                          help="Check if the kickoff return scored a TD.")
                    touchdown_val = 1 if td_flag else 0

            notes = st.text_input("Notes (optional)", key="fb_notes")
            submitted = st.form_submit_button("Add Stat")

        if submitted:
            pr = roster.loc[roster["player_key"] == player_key].iloc[0]
            base_row = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "sport": self.name,
                "player_key": player_key,
                "first_name": pr["first_name"],
                "last_name": pr["last_name"],
                "number": int(pr["number"]) if pd.notna(pr["number"]) else None,
                "positions": pr["positions"],
                "side": side,
                "notes": notes.strip(),
            }
            row = base_row | {
                "stat_type": stat_type,
                "outcome": outcome,
                "yards": int(yards) if yards is not None else None,
                "touchdown": int(touchdown_val),
                "two_point": int(two_point_val),
            }
            new_rows.append(row)

            if side == "Offense" and stat_type == "Pass" and outcome == "Complete" and st.session_state.get("fb_pair_reception", False):
                try:
                    rcv = roster.loc[roster["player_key"] == st.session_state.get("fb_receiver")].iloc[0]
                    rcv_row = base_row | {
                        "player_key": rcv["player_key"],
                        "first_name": rcv["first_name"],
                        "last_name": rcv["last_name"],
                        "number": int(rcv["number"]) if pd.notna(rcv["number"]) else None,
                        "positions": rcv["positions"],
                        "side": "Offense",
                        "stat_type": "Reception",
                        "outcome": None,
                        "yards": int(yards) if yards is not None else None,
                        "touchdown": int(touchdown_val),
                        "two_point": int(two_point_val),
                    }
                    new_rows.append(rcv_row)
                except Exception:
                    pass

        return {"submitted": submitted, "new_rows": new_rows}

    def aggregate_totals(self, logs: pd.DataFrame) -> pd.DataFrame:
        df = logs.copy()
        df = df[df["sport"] == self.name]
        if df.empty:
            return pd.DataFrame()
        df["yards"] = pd.to_numeric(df.get("yards", 0), errors="coerce").fillna(0).astype(int)
        df["touchdown"] = pd.to_numeric(df.get("touchdown", 0), errors="coerce").fillna(0).astype(int)
        df["two_point"] = pd.to_numeric(df.get("two_point", 0), errors="coerce").fillna(0).astype(int)

        grouped = []
        for player_key, grp in df.groupby("player_key"):
            row = {
                "player_key": player_key,
                "first_name": grp["first_name"].iloc[0],
                "last_name": grp["last_name"].iloc[0],
                "number": grp["number"].iloc[0],
                "positions": grp["positions"].iloc[0],
            }
            row["Receptions"] = int((grp["stat_type"] == "Reception").sum())
            row["Receiving Yards"] = int(grp.loc[grp["stat_type"] == "Reception", "yards"].sum())
            row["Receiving TDs"] = int(((grp["stat_type"] == "Reception") & (grp["touchdown"] == 1)).sum())

            row["Rush Attempts"] = int((grp["stat_type"] == "Run").sum())
            row["Rushing Yards"] = int(grp.loc[grp["stat_type"] == "Run", "yards"].sum())
            row["Rushing TDs"] = int(((grp["stat_type"] == "Run") & (grp["touchdown"] == 1)).sum())

            row["Punts"] = int((grp["stat_type"] == "Punt").sum())
            row["Punt Yards"] = int(grp.loc[grp["stat_type"] == "Punt", "yards"].sum())
            row["Fumbles"] = int((grp["stat_type"] == "Fumble").sum())

            pass_df = grp[grp["stat_type"] == "Pass"]
            row["Pass Attempts"] = int(len(pass_df))
            row["Pass Completions"] = int((pass_df["outcome"] == "Complete").sum())
            row["Passing Interceptions"] = int((pass_df["outcome"] == "Interception").sum())
            row["Pass Yards"] = int(pass_df.loc[pass_df["outcome"] == "Complete", "yards"].sum())
            row["Passing TDs"] = int(((pass_df["outcome"] == "Complete") & (pass_df["touchdown"] == 1)).sum())

            fg_df = grp[grp["stat_type"] == "Field Goal"]
            row["FG Attempts"] = int(len(fg_df))
            row["FG Made"] = int((fg_df["outcome"] == "Made").sum())
            row["FG Attempt Yards (Total)"] = int(fg_df["yards"].sum())

            pat_df = grp[grp["stat_type"] == "PAT"]
            row["PAT Attempts"] = int(len(pat_df))
            row["PAT Made"] = int((pat_df["outcome"] == "Made").sum())

            row["Forced Fumbles"] = int((grp["stat_type"] == "Forced Fumble").sum())
            row["Sacks"] = int((grp["stat_type"] == "Sack").sum())
            row["Tackles"] = int((grp["stat_type"] == "Tackle").sum())

            interception_df = grp[grp["stat_type"] == "Interception"]
            row["Interceptions"] = int(len(interception_df))
            row["Interception Return Yards"] = int(interception_df["yards"].sum())
            row["Interception Return TDs"] = int((interception_df["touchdown"] == 1).sum())

            fumble_recovery_df = grp[grp["stat_type"] == "Fumble Recovery"]
            row["Fumble Recoveries"] = int(len(fumble_recovery_df))
            row["Fumble Recovery Yards"] = int(fumble_recovery_df["yards"].sum())
            row["Fumble Return TDs"] = int((fumble_recovery_df["touchdown"] == 1).sum())

            punt_return_df = grp[grp["stat_type"] == "Punt Return"]
            row["Punt Returns"] = int(len(punt_return_df))
            row["Punt Return Yards"] = int(punt_return_df["yards"].sum())
            row["Punt Return TDs"] = int((punt_return_df["touchdown"] == 1).sum())

            kickoff_return_df = grp[grp["stat_type"] == "Kickoff Return"]
            row["Kickoff Returns"] = int(len(kickoff_return_df))
            row["Kickoff Return Yards"] = int(kickoff_return_df["yards"].sum())
            row["Kickoff Return TDs"] = int((kickoff_return_df["touchdown"] == 1).sum())

            legacy_return_df = grp[grp["stat_type"] == "Return"]
            legacy_return_yards = int(legacy_return_df["yards"].sum())
            legacy_return_tds = int((legacy_return_df["touchdown"] == 1).sum())

            row["Total Return Yards"] = int(
                row["Interception Return Yards"]
                + row["Fumble Recovery Yards"]
                + row["Punt Return Yards"]
                + row["Kickoff Return Yards"]
                + legacy_return_yards
            )
            row["Return Yards"] = row["Total Return Yards"]
            row["Defensive TDs"] = int(
                row["Interception Return TDs"]
                + row["Fumble Return TDs"]
                + row["Punt Return TDs"]
                + row["Kickoff Return TDs"]
                + legacy_return_tds
            )

            row["2-pt Rushing Conversions"] = int(
                ((grp["stat_type"] == "Run") & (grp["two_point"] == 1)).sum()
            )
            row["2-pt Receiving Conversions"] = int(
                ((grp["stat_type"] == "Reception") & (grp["two_point"] == 1)).sum()
            )
            row["2-pt Conversions"] = int(
                row["2-pt Rushing Conversions"] + row["2-pt Receiving Conversions"]
            )
            row["2-pt Conversion Points"] = int(2 * row["2-pt Conversions"])

            row["Touchdowns (Total)"] = int(
                row["Receiving TDs"] + row["Rushing TDs"] + row["Passing TDs"] + row["Defensive TDs"]
            )
            grouped.append(row)

        return pd.DataFrame(grouped).sort_values(by=["last_name", "first_name"]).reset_index(drop=True)


# ---------------------------
# Soccer implementation
# ---------------------------
class SoccerSpec(SportSpec):
    name = "Soccer"
    sides = ["All"]

    def build_form(self, roster: pd.DataFrame) -> Dict[str, Any]:
        c1, c2 = st.columns([2, 1])
        player_key = c1.selectbox("Player", options=roster["player_key"].tolist(), key="sc_player_select")
        stat_type = c2.selectbox(
            "Stat",
            options=["Shot", "Pass", "Tackle", "Interception", "Save", "Foul"],
            key="sc_stat_type",
        )

        new_rows: List[dict] = []
        with st.form("sc_log_form", clear_on_submit=True):
            on_target = None
            goal = 0
            outcome = None
            receiver_key = None
            resulted_goal = False
            card = "None"

            if stat_type == "Shot":
                on_target = st.selectbox("Shot on goal?", options=["Yes", "No"], index=0, key="sc_shot_on_target") == "Yes"
                goal = 1 if st.checkbox("Goal", value=False, key="sc_shot_goal", help="Check if this shot scored.") else 0
            elif stat_type == "Pass":
                outcome = st.selectbox("Pass Outcome", options=["Complete", "Incomplete"], key="sc_pass_outcome")
                if outcome == "Complete":
                    recv_options = [pk for pk in roster["player_key"].tolist() if pk != player_key]
                    if not recv_options:
                        st.warning("No eligible recipients (only one player in roster).")
                    receiver_key = st.selectbox("Pass Recipient", options=recv_options if recv_options else ["None"], key="sc_receiver")
                    resulted_goal = st.checkbox(
                        "Did this completed pass directly result in a goal (assist)?",
                        value=False,
                        key="sc_pass_goal",
                        help="If checked, we'll credit an Assist to the passer and a Shot+Goal to the recipient."
                    )
            elif stat_type == "Foul":
                card = st.selectbox("Card", options=["None", "Yellow", "Red"], index=0, key="sc_foul_card")

            notes = st.text_input("Notes (optional)", key="sc_notes")
            submitted = st.form_submit_button("Add Stat")

        if submitted:
            pr = roster.loc[roster["player_key"] == player_key].iloc[0]
            base_row = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "sport": self.name,
                "player_key": player_key,
                "first_name": pr["first_name"],
                "last_name": pr["last_name"],
                "number": int(pr["number"]) if pd.notna(pr["number"]) else None,
                "positions": pr["positions"],
                "side": "All",
                "notes": notes.strip(),
            }

            if stat_type == "Shot":
                new_rows.append(base_row | {
                    "stat_type": "Shot",
                    "on_target": int(bool(on_target)) if on_target is not None else None,
                    "goal": int(goal),
                })
            elif stat_type == "Pass":
                new_rows.append(base_row | {"stat_type": "Pass", "outcome": outcome})
                if outcome == "Complete" and receiver_key and receiver_key != player_key and resulted_goal:
                    new_rows.append(base_row | {"stat_type": "Assist"})
                    try:
                        rcv = roster.loc[roster["player_key"] == receiver_key].iloc[0]
                        new_rows.append(base_row | {
                            "player_key": rcv["player_key"],
                            "first_name": rcv["first_name"],
                            "last_name": rcv["last_name"],
                            "number": int(rcv["number"]) if pd.notna(rcv["number"]) else None,
                            "positions": rcv["positions"],
                            "stat_type": "Shot",
                            "on_target": 1,
                            "goal": 1,
                        })
                    except Exception:
                        pass
            elif stat_type in ("Tackle", "Interception", "Save"):
                new_rows.append(base_row | {"stat_type": stat_type})
            elif stat_type == "Foul":
                new_rows.append(base_row | {"stat_type": "Foul", "card": card})

        return {"submitted": submitted, "new_rows": new_rows}

    def aggregate_totals(self, logs: pd.DataFrame) -> pd.DataFrame:
        df = logs.copy()
        df = df[df["sport"] == self.name]
        if df.empty:
            return pd.DataFrame()
        df["on_target"] = pd.to_numeric(df.get("on_target", 0), errors="coerce").fillna(0).astype(int)
        df["goal"] = pd.to_numeric(df.get("goal", 0), errors="coerce").fillna(0).astype(int)
        df["card"] = df.get("card", "None").fillna("None")

        grouped = []
        for player_key, grp in df.groupby("player_key"):
            row = {
                "player_key": player_key,
                "first_name": grp["first_name"].iloc[0],
                "last_name": grp["last_name"].iloc[0],
                "number": grp["number"].iloc[0],
                "positions": grp["positions"].iloc[0],
            }
            row["Shots"] = int((grp["stat_type"] == "Shot").sum())
            row["Shots on Target"] = int(grp.loc[grp["stat_type"] == "Shot", "on_target"].sum())
            row["Goals"] = int(grp.loc[grp["stat_type"] == "Shot", "goal"].sum())
            pass_df = grp[grp["stat_type"] == "Pass"]
            row["Passes Attempted"] = int(len(pass_df))
            row["Passes Completed"] = int((pass_df["outcome"] == "Complete").sum())
            row["Assists"] = int((grp["stat_type"] == "Assist").sum())
            row["Tackles"] = int((grp["stat_type"] == "Tackle").sum())
            row["Interceptions"] = int((grp["stat_type"] == "Interception").sum())
            row["Saves"] = int((grp["stat_type"] == "Save").sum())
            foul_df = grp[grp["stat_type"] == "Foul"]
            row["Fouls"] = int(len(foul_df))
            row["Yellow Cards"] = int((foul_df["card"] == "Yellow").sum())
            row["Red Cards"] = int((foul_df["card"] == "Red").sum())
            row["Pass Completion %"] = round(100.0 * row["Passes Completed"] / row["Passes Attempted"], 1) if row["Passes Attempted"] else 0.0
            grouped.append(row)

        return pd.DataFrame(grouped).sort_values(by=["last_name", "first_name"]).reset_index(drop=True)


# ---------------------------
# Lacrosse implementation
# ---------------------------
class LacrosseSpec(SportSpec):
    name = "Lacrosse"
    sides = ["All"]

    def build_form(self, roster: pd.DataFrame) -> Dict[str, Any]:
        c1, c2 = st.columns([2, 1])
        player_key = c1.selectbox("Player", options=roster["player_key"].tolist(), key="lc_player_select")
        stat_type = c2.selectbox(
            "Stat",
            options=[
                "Goal", "Assist", "Shot", "Ground Ball", "Faceoff", "Takeaway", "Interception",
                "Turnover", "Penalty", "Save", "Goal Allowed", "Goalie Minutes"
            ],
            key="lc_stat_type",
        )

        new_rows: List[dict] = []
        with st.form("lc_log_form", clear_on_submit=True):
            assist_key = None
            on_target = None
            faceoff_result = None
            penalty_minutes = None
            minutes = None

            if stat_type == "Goal":
                assist_opts = [pk for pk in roster["player_key"].tolist() if pk != player_key]
                assist_key = st.selectbox("Assisted by (optional)", options=["None"] + assist_opts, key="lc_assist")
            elif stat_type == "Shot":
                on_target = st.selectbox("Shot on goal?", ["Yes", "No"], key="lc_sog") == "Yes"
            elif stat_type == "Faceoff":
                faceoff_result = st.selectbox("Faceoff Result", ["Win", "Loss"], key="lc_faceoff")
            elif stat_type == "Penalty":
                penalty_minutes = st.number_input("Penalty Minutes", value=1.0, step=0.5, key="lc_penmin")
            elif stat_type == "Goalie Minutes":
                minutes = st.number_input("Minutes Played (Goalie)", value=12.0, step=1.0, key="lc_minutes")

            notes = st.text_input("Notes (optional)", key="lc_notes")
            submitted = st.form_submit_button("Add Stat")

        if submitted:
            pr = roster.loc[roster["player_key"] == player_key].iloc[0]
            base = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "sport": self.name,
                "player_key": player_key,
                "first_name": pr["first_name"],
                "last_name": pr["last_name"],
                "number": int(pr["number"]) if pd.notna(pr["number"]) else None,
                "positions": pr["positions"],
                "side": "All",
                "notes": notes.strip(),
            }

            if stat_type == "Goal":
                new_rows.append(base | {"stat_type": "Goal", "goal": 1})
                new_rows.append(base | {"stat_type": "Shot", "on_target": 1})
                if assist_key and assist_key != "None":
                    try:
                        a = roster.loc[roster["player_key"] == assist_key].iloc[0]
                        new_rows.append(base | {
                            "player_key": a["player_key"],
                            "first_name": a["first_name"],
                            "last_name": a["last_name"],
                            "number": int(a["number"]) if pd.notna(a["number"]) else None,
                            "positions": a["positions"],
                            "stat_type": "Assist",
                        })
                    except Exception:
                        pass
            elif stat_type == "Assist":
                new_rows.append(base | {"stat_type": "Assist"})
            elif stat_type == "Shot":
                new_rows.append(base | {"stat_type": "Shot", "on_target": int(on_target)})
            elif stat_type == "Ground Ball":
                new_rows.append(base | {"stat_type": "Ground Ball"})
            elif stat_type == "Faceoff":
                new_rows.append(base | {"stat_type": "Faceoff", "outcome": faceoff_result})
            elif stat_type in ("Takeaway", "Interception", "Turnover", "Save", "Goal Allowed"):
                new_rows.append(base | {"stat_type": stat_type})
            elif stat_type == "Penalty":
                new_rows.append(base | {"stat_type": "Penalty", "penalty_minutes": penalty_minutes})
            elif stat_type == "Goalie Minutes":
                new_rows.append(base | {"stat_type": "Goalie Minutes", "minutes": minutes})

        return {"submitted": submitted, "new_rows": new_rows}

    def aggregate_totals(self, logs: pd.DataFrame) -> pd.DataFrame:
        df = logs.copy()
        df = df[df["sport"] == self.name]
        if df.empty:
            return pd.DataFrame()

        idx = df.index
        df["on_target"] = pd.to_numeric(
            df["on_target"] if "on_target" in df else pd.Series(0, index=idx),
            errors="coerce"
        ).fillna(0).astype(int)
        df["penalty_minutes"] = pd.to_numeric(
            df["penalty_minutes"] if "penalty_minutes" in df else pd.Series(0.0, index=idx),
            errors="coerce"
        ).fillna(0).astype(float)
        df["minutes"] = pd.to_numeric(
            df["minutes"] if "minutes" in df else pd.Series(0.0, index=idx),
            errors="coerce"
        ).fillna(0).astype(float)

        grouped = []
        for pk, grp in df.groupby("player_key"):
            row = {
                "player_key": pk,
                "first_name": grp["first_name"].iloc[0],
                "last_name": grp["last_name"].iloc[0],
                "number": grp["number"].iloc[0],
                "positions": grp["positions"].iloc[0],
            }
            row["Goals"] = int((grp["stat_type"] == "Goal").sum())
            shots = grp[grp["stat_type"] == "Shot"]
            row["Shots"] = len(shots)
            row["Shots on Goal"] = int(shots["on_target"].sum())
            row["Assists"] = int((grp["stat_type"] == "Assist").sum())
            row["Points"] = row["Goals"] + row["Assists"]
            row["Ground Balls"] = int((grp["stat_type"] == "Ground Ball").sum())
            fo = grp[grp["stat_type"] == "Faceoff"]
            row["Faceoffs Attempted"] = len(fo)
            row["Faceoffs Won"] = int((fo["outcome"] == "Win").sum())
            row["Faceoff %"] = round(100 * row["Faceoffs Won"] / row["Faceoffs Attempted"], 1) if row["Faceoffs Attempted"] else 0.0
            row["Takeaways"] = int((grp["stat_type"] == "Takeaway").sum())
            row["Interceptions"] = int((grp["stat_type"] == "Interception").sum())
            row["Caused Turnovers"] = row["Takeaways"] + row["Interceptions"]
            row["Turnovers"] = int((grp["stat_type"] == "Turnover").sum())
            pen = grp[grp["stat_type"] == "Penalty"]
            row["Penalties"] = len(pen)
            row["Penalty Minutes"] = float(pen["penalty_minutes"].sum()) if not pen.empty else 0.0
            row["Saves"] = int((grp["stat_type"] == "Save").sum())
            row["Goals Allowed"] = int((grp["stat_type"] == "Goal Allowed").sum())
            row["Minutes"] = float(grp.loc[grp["stat_type"] == "Goalie Minutes", "minutes"].sum())
            sog_faced = row["Saves"] + row["Goals Allowed"]
            row["Shots on Goal Faced"] = sog_faced
            row["Save %"] = round(100 * row["Saves"] / sog_faced, 1) if sog_faced else 0.0
            row["GAA"] = round((row["Goals Allowed"] * 48) / row["Minutes"], 2) if row["Minutes"] > 0 else 0.0
            row["Shooting %"] = round(100 * row["Goals"] / row["Shots on Goal"], 1) if row["Shots on Goal"] else 0.0
            row["SOG Rate %"] = round(100 * row["Shots on Goal"] / row["Shots"], 1) if row["Shots"] else 0.0
            grouped.append(row)

        return pd.DataFrame(grouped).sort_values(by=["last_name", "first_name"]).reset_index(drop=True)


# ---------------------------
# Basketball Implementation
# ---------------------------
class BasketballSpec(SportSpec):
    name = "Basketball"
    sides = ["All"]

    def build_form(self, roster: pd.DataFrame) -> Dict[str, Any]:
        c1, c2 = st.columns([2, 1])
        player_key = c1.selectbox("Player", options=roster["player_key"].tolist(), key="bb_player_select")
        stat_type = c2.selectbox(
            "Stat",
            options=[
                "2PT Shot", "3PT Shot", "Free Throw",
                "Rebound", "Assist", "Steal", "Block", "Turnover",
            ],
            key="bb_stat_type",
        )

        new_rows: List[dict] = []
        with st.form("bb_log_form", clear_on_submit=True):
            outcome = None
            if stat_type in ("2PT Shot", "3PT Shot", "Free Throw"):
                outcome = st.selectbox("Result", options=["Made", "Miss"], key="bb_shot_outcome")
            elif stat_type == "Rebound":
                outcome = st.selectbox("Type", options=["Offensive", "Defensive"], key="bb_reb_type")
            notes = st.text_input("Notes (optional)", key="bb_notes")
            submitted = st.form_submit_button("Add Stat")

        if submitted:
            pr = roster.loc[roster["player_key"] == player_key].iloc[0]
            new_rows.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "sport": self.name,
                "player_key": player_key,
                "first_name": pr["first_name"],
                "last_name": pr["last_name"],
                "number": int(pr["number"]) if pd.notna(pr["number"]) else None,
                "positions": pr["positions"],
                "side": "All",
                "stat_type": stat_type,
                "outcome": outcome,
                "yards": None,
                "touchdown": 0,
                "two_point": 0,
                "on_target": None,
                "goal": None,
                "card": None,
                "penalty_minutes": None,
                "minutes": None,
                "notes": notes.strip(),
            })

        return {"submitted": submitted, "new_rows": new_rows}

    def aggregate_totals(self, logs: pd.DataFrame) -> pd.DataFrame:
        df = logs.copy()
        df = df[df["sport"] == self.name]
        if df.empty:
            return pd.DataFrame()

        def _cnt(mask):
            return int(mask.sum())

        rows = []
        for pk, grp in df.groupby("player_key"):
            first = grp.iloc[0]
            two_pa = _cnt(grp["stat_type"] == "2PT Shot")
            two_pm = _cnt((grp["stat_type"] == "2PT Shot") & (grp["outcome"] == "Made"))
            three_pa = _cnt(grp["stat_type"] == "3PT Shot")
            three_pm = _cnt((grp["stat_type"] == "3PT Shot") & (grp["outcome"] == "Made"))
            ft_a = _cnt(grp["stat_type"] == "Free Throw")
            ft_m = _cnt((grp["stat_type"] == "Free Throw") & (grp["outcome"] == "Made"))
            fga = two_pa + three_pa
            fgm = two_pm + three_pm
            pts = (2 * two_pm) + (3 * three_pm) + ft_m
            oreb = _cnt((grp["stat_type"] == "Rebound") & (grp["outcome"] == "Offensive"))
            dreb = _cnt((grp["stat_type"] == "Rebound") & (grp["outcome"] == "Defensive"))
            reb = oreb + dreb
            ast = _cnt(grp["stat_type"] == "Assist")
            stl = _cnt(grp["stat_type"] == "Steal")
            blk = _cnt(grp["stat_type"] == "Block")
            tov = _cnt(grp["stat_type"] == "Turnover")
            fg_pct = round(100.0 * fgm / fga, 1) if fga else 0.0
            tp_pct = round(100.0 * three_pm / three_pa, 1) if three_pa else 0.0
            ft_pct = round(100.0 * ft_m / ft_a, 1) if ft_a else 0.0

            rows.append({
                "player_key": pk,
                "first_name": first["first_name"],
                "last_name": first["last_name"],
                "number": first["number"],
                "positions": first["positions"],
                "PTS": pts,
                "FGM": fgm,
                "FGA": fga,
                "FG%": fg_pct,
                "3PM": three_pm,
                "3PA": three_pa,
                "3P%": tp_pct,
                "FTM": ft_m,
                "FTA": ft_a,
                "FT%": ft_pct,
                "OREB": oreb,
                "DREB": dreb,
                "REB": reb,
                "AST": ast,
                "STL": stl,
                "BLK": blk,
                "TOV": tov,
            })

        return pd.DataFrame(rows).sort_values(by=["last_name", "first_name"]).reset_index(drop=True)


# ---------------------------
# Baseball Implementation
# ---------------------------
class BaseballSpec(SportSpec):
    name = "Baseball"
    sides = ["All"]

    def build_form(self, roster: pd.DataFrame) -> Dict[str, Any]:
        c1, c2 = st.columns([2, 1])
        player_key = c1.selectbox("Player", options=roster["player_key"].tolist(), key="bsb_player_select")
        category = c2.selectbox(
            "Category",
            options=["Batting/Running", "Pitching", "Fielding"],
            key="bsb_category",
        )

        new_rows: List[dict] = []
        with st.form("bsb_log_form", clear_on_submit=True):
            stat_type = None
            outcome = None
            qty: Optional[int | float] = None

            if category == "Batting/Running":
                stat_type = st.selectbox(
                    "Stat",
                    options=["Plate Appearance", "Run", "RBI", "Stolen Base"],
                    key="bsb_bat_stat",
                )
                if stat_type == "Plate Appearance":
                    outcome = st.selectbox(
                        "Result",
                        options=["Single", "Double", "Triple", "Home Run", "Out", "Walk", "Strikeout"],
                        key="bsb_pa_outcome",
                    )
            elif category == "Pitching":
                stat_type = st.selectbox(
                    "Stat",
                    options=[
                        "Outs Recorded (+)", "Earned Runs (+)", "Strikeouts (Pitching +)",
                        "Walks Allowed (+)", "Hits Allowed (+)", "Home Runs Allowed (+)",
                        "Pitch Count (+)", "Win", "Loss", "Save"
                    ],
                    key="bsb_pitch_stat",
                )
                if stat_type.endswith("(+)"):
                    qty = st.number_input("Amount", value=1, min_value=0, step=1, key="bsb_pitch_qty")
            elif category == "Fielding":
                stat_type = st.selectbox("Stat", options=["Putout", "Assist", "Error"], key="bsb_field_stat")

            notes = st.text_input("Notes (optional)", key="bsb_notes")
            submitted = st.form_submit_button("Add Stat")

        if submitted:
            pr = roster.loc[roster["player_key"] == player_key].iloc[0]
            new_rows.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "sport": self.name,
                "player_key": player_key,
                "first_name": pr["first_name"],
                "last_name": pr["last_name"],
                "number": int(pr["number"]) if pd.notna(pr["number"]) else None,
                "positions": pr["positions"],
                "side": "All",
                "stat_type": stat_type,
                "outcome": outcome,
                "yards": qty,
                "touchdown": 0,
                "two_point": 0,
                "on_target": None,
                "goal": None,
                "card": None,
                "penalty_minutes": None,
                "minutes": None,
                "notes": notes.strip(),
            })

        return {"submitted": submitted, "new_rows": new_rows}

    def aggregate_totals(self, logs: pd.DataFrame) -> pd.DataFrame:
        df = logs.copy()
        df = df[df["sport"] == self.name]
        if df.empty:
            return pd.DataFrame()

        def _sum_qty(g: pd.DataFrame, stat: str) -> int:
            s = g.loc[g["stat_type"] == stat, "yards"]
            return int(pd.to_numeric(s, errors="coerce").fillna(0).sum())

        rows = []
        for pk, grp in df.groupby("player_key"):
            first = grp.iloc[0]
            pa = grp[grp["stat_type"] == "Plate Appearance"]
            singles = (pa["outcome"] == "Single").sum()
            doubles = (pa["outcome"] == "Double").sum()
            triples = (pa["outcome"] == "Triple").sum()
            homers = (pa["outcome"] == "Home Run").sum()
            outs_pa = (pa["outcome"] == "Out").sum()
            walks = (pa["outcome"] == "Walk").sum()
            strikeouts_bat = (pa["outcome"] == "Strikeout").sum()
            hits = int(singles + doubles + triples + homers)
            ab = int(singles + doubles + triples + homers + outs_pa + strikeouts_bat)
            tb = int(singles + 2 * doubles + 3 * triples + 4 * homers)
            runs = int((grp["stat_type"] == "Run").sum())
            rbi = int((grp["stat_type"] == "RBI").sum())
            sb = int((grp["stat_type"] == "Stolen Base").sum())
            avg = round(hits / ab, 3) if ab else 0.000
            obp_den = ab + walks
            obp = round((hits + walks) / obp_den, 3) if obp_den else 0.000
            slg = round(tb / ab, 3) if ab else 0.000

            outs = _sum_qty(grp, "Outs Recorded (+)")
            ip = outs / 3.0
            er = _sum_qty(grp, "Earned Runs (+)")
            k_pitch = _sum_qty(grp, "Strikeouts (Pitching +)")
            bb_pitch = _sum_qty(grp, "Walks Allowed (+)")
            h_allowed = _sum_qty(grp, "Hits Allowed (+)")
            hr_allowed = _sum_qty(grp, "Home Runs Allowed (+)")
            pc = _sum_qty(grp, "Pitch Count (+)")
            w = int((grp["stat_type"] == "Win").sum())
            l = int((grp["stat_type"] == "Loss").sum())
            sv = int((grp["stat_type"] == "Save").sum())
            era = round((er * 9.0) / ip, 2) if ip > 0 else 0.00

            po = int((grp["stat_type"] == "Putout").sum())
            a = int((grp["stat_type"] == "Assist").sum())
            e = int((grp["stat_type"] == "Error").sum())
            fpct_den = po + a + e
            fpct = round((po + a) / fpct_den, 3) if fpct_den else 0.000

            rows.append({
                "player_key": pk,
                "first_name": first["first_name"],
                "last_name": first["last_name"],
                "number": first["number"],
                "positions": first["positions"],
                "AB": ab, "H": hits, "R": runs, "RBI": rbi, "HR": int(homers),
                "SB": sb, "BB": int(walks), "SO": int(strikeouts_bat),
                "AVG": avg, "OBP": obp, "SLG": slg,
                "IP": round(ip, 2), "ER": er, "K": k_pitch, "BB (P)": bb_pitch,
                "H (P)": h_allowed, "HR (P)": hr_allowed, "W": w, "L": l, "SV": sv,
                "ERA": era, "PC": pc,
                "PO": po, "A": a, "E": e, "FPCT": fpct,
            })

        return pd.DataFrame(rows).sort_values(by=["last_name", "first_name"]).reset_index(drop=True)


# Registry
SPORTS: Dict[str, SportSpec] = {
    "Football": FootballSpec(),
    "Soccer": SoccerSpec(),
    "Lacrosse": LacrosseSpec(),
    "Baseball": BaseballSpec(),
    "Basketball": BasketballSpec(),
}

# ---------------------------
# Session state
# ---------------------------
if "game" not in st.session_state:
    st.session_state.game = None
if "roster" not in st.session_state:
    st.session_state.roster = pd.DataFrame()
if "logs" not in st.session_state:
    st.session_state.logs = pd.DataFrame(columns=LOG_COLUMNS)
if "pending_sync_rows" not in st.session_state:
    st.session_state.pending_sync_rows = []
if "last_sync_error" not in st.session_state:
    st.session_state.last_sync_error = None

# ---------------------------
# Header
# ---------------------------
st.title("🏅 Multi-Sport Stats Collector → Google Sheets")
st.markdown(
    "Create a game → pick a sport → log plays → stats auto-save to your Sheet → refresh totals → export to [Max Preps](https://www.maxpreps.com/)."
)

# ---------------------------
# 1) Create / Resume Game
# ---------------------------
with st.expander("① Create or Resume a Game", expanded=True):
    c0, c1, c2, c3 = st.columns([1.2, 1, 2, 2])
    sport_name = c0.selectbox("Sport", options=list(SPORTS.keys()), index=0, key="sport_selector")
    game_date = c1.date_input("Game Date", value=datetime.today())
    opponent = c2.text_input("Opponent", placeholder="E.g., Wildcats")
    sheet_url = c3.text_input("Google Sheet URL or ID", key="sheet_url_input", placeholder="Paste the sheet URL or ID here…")

    create_btn = st.button("Create Game / Load Roster", type="primary")

    if create_btn:
        try:
            # Protect any locally pending rows before switching/reloading games.
            if st.session_state.pending_sync_rows and st.session_state.game:
                append_rows_to_game_log(st.session_state.game, st.session_state.pending_sync_rows)
                st.session_state.pending_sync_rows = []
                st.session_state.last_sync_error = None

            sh = google_retry(lambda: open_sheet(sheet_url))
            roster_df = read_roster_df(sh)
            new_game = {
                "sport": sport_name,
                "date": game_date.strftime("%Y-%m-%d"),
                "opponent": opponent.strip(),
                "sheet_id": parse_sheet_id_from_url(sheet_url),
            }
            saved_logs = load_or_create_game_log(sh, new_game)

            st.session_state.roster = roster_df
            st.session_state.game = new_game
            st.session_state.logs = saved_logs
            st.session_state.pending_sync_rows = []
            st.session_state.last_sync_error = None

            if saved_logs.empty:
                st.success("Game created and roster loaded. Auto-save is active.")
            else:
                st.success(f"Game resumed with {len(saved_logs)} saved stat rows from Google Sheets.")
        except Exception as e:
            st.error(f"Failed to create/resume game or read roster: {e}")

# ---------------------------
# Show current game/roster and sync state
# ---------------------------
if st.session_state.game:
    g = st.session_state.game
    st.info(f"**Game:** {g['date']} vs {g['opponent']} — **Sport:** {g['sport']}")

    if st.session_state.pending_sync_rows:
        st.warning(
            f"⚠️ {len(st.session_state.pending_sync_rows)} stat row(s) are saved in this session but still waiting to sync to Google Sheets."
        )
        if st.button("Retry Google Sync", type="secondary"):
            try:
                append_rows_to_game_log(g, st.session_state.pending_sync_rows)
                st.session_state.pending_sync_rows = []
                st.session_state.last_sync_error = None
                st.success("Pending stats synced to Google Sheets.")
                st.rerun()
            except Exception as e:
                st.session_state.last_sync_error = str(e)
                st.error(f"Sync retry failed: {e}")
        if st.session_state.last_sync_error:
            st.caption(f"Last sync error: {st.session_state.last_sync_error}")
    else:
        st.caption("✅ Auto-save active — every stat is written to the game Log tab in Google Sheets.")

    with st.expander("Roster (from Google Sheet)"):
        st.dataframe(st.session_state.roster, use_container_width=True)

    with st.expander("Import/Replace Roster (CSV → Google Sheet)", expanded=False):
        st.write("Upload a CSV with headers exactly: **Player First Name, Player Last Name, Player Number, Player Position(s)**. We'll write it to the 'Roster' tab in your Google Sheet.")
        template_df = SPORTS[g['sport']].csv_template()
        st.download_button(
            label="⬇️ Download Roster CSV Template",
            data=template_df.to_csv(index=False).encode("utf-8"),
            file_name=f"{g['sport'].lower()}_roster_template.csv",
            mime="text/csv",
        )
        uploaded = st.file_uploader("Upload roster CSV", type=["csv"], accept_multiple_files=False)

        def upsert_ws_with_df(sh, title: str, df: pd.DataFrame):
            existing = {ws.title: ws for ws in google_retry(lambda: sh.worksheets())}
            if title in existing:
                google_retry(lambda: sh.del_worksheet(existing[title]))
                time.sleep(0.3)
            ws = google_retry(lambda: sh.add_worksheet(title=title, rows=str(len(df) + 10), cols=str(len(df.columns) + 5)))
            google_retry(lambda: ws.update([df.columns.tolist()] + df.astype(str).values.tolist(), range_name="A1"))

        if uploaded is not None:
            try:
                new_roster = pd.read_csv(uploaded)
                expected = [
                    "Player First Name", "Player Last Name", "Player Number", "Player Position(s)"
                ]
                if any(col not in new_roster.columns for col in expected):
                    st.error(f"CSV missing required columns. Expected exactly: {expected}")
                else:
                    new_roster["Player Number"] = pd.to_numeric(new_roster["Player Number"], errors="coerce").astype("Int64")
                    st.dataframe(new_roster, use_container_width=True)
                    if st.button("📤 Write to Google Sheet as 'Roster'", type="primary"):
                        sh = google_retry(lambda: open_sheet(g["sheet_id"]))
                        upsert_ws_with_df(sh, "Roster", new_roster)
                        st.session_state.roster = read_roster_df(sh)
                        st.success("Roster sheet updated from CSV.")
            except Exception as e:
                st.error(f"Failed to process CSV: {e}")

# ---------------------------
# 2) Log a Stat
# ---------------------------
if not st.session_state.game:
    st.warning("Create a game first.")
else:
    st.subheader("② Log a Stat")
    roster = st.session_state.roster
    if roster.empty:
        st.warning("No roster loaded yet.")
    else:
        sport = st.session_state.game["sport"]
        spec = SPORTS[sport]
        result = spec.build_form(roster)
        if result.get("submitted") and result.get("new_rows"):
            new_rows = []
            for raw_row in result["new_rows"]:
                row = dict(raw_row)
                row["event_id"] = row.get("event_id") or uuid.uuid4().hex
                new_rows.append(row)

            current_logs = normalize_log_df(st.session_state.logs)
            st.session_state.logs = normalize_log_df(
                pd.concat([current_logs, pd.DataFrame(new_rows)], ignore_index=True, sort=False)
            )

            pending_by_id = {
                row["event_id"]: row
                for row in list(st.session_state.pending_sync_rows) + new_rows
                if row.get("event_id")
            }
            pending_rows = list(pending_by_id.values())

            synced = False
            try:
                append_rows_to_game_log(st.session_state.game, pending_rows)
                st.session_state.pending_sync_rows = []
                st.session_state.last_sync_error = None
                synced = True
            except Exception as e:
                st.session_state.pending_sync_rows = pending_rows
                st.session_state.last_sync_error = str(e)

            if len(new_rows) == 1:
                a = new_rows[0]
                message = f"✅ Added {a['stat_type']} for {a['first_name']} {a['last_name']}"
            elif len(new_rows) == 2:
                a, b = new_rows[0], new_rows[1]
                message = (
                    f"✅ Added {a['stat_type']} for {a['first_name']} {a['last_name']} and "
                    f"{b['stat_type']} for {b['first_name']} {b['last_name']}"
                )
            else:
                message = f"✅ Added {len(new_rows)} linked stat entries"

            if synced:
                message += " — auto-saved to Google Sheets."
            else:
                message += " — saved in this session; Google sync is pending."
            st.session_state["flash_message"] = message
            st.rerun()

# ---------------------------
# 3) Running Log & Totals
# ---------------------------
if not st.session_state.logs.empty:
    st.subheader("③ Running Event Log")
    display_logs = st.session_state.logs.drop(columns=["event_id"], errors="ignore")
    st.dataframe(display_logs, use_container_width=True)

    st.subheader("④ Player Totals (auto-calculated)")
    sport = st.session_state.game["sport"] if st.session_state.game else "Football"
    totals_df = SPORTS[sport].aggregate_totals(st.session_state.logs)
    if totals_df is not None and not totals_df.empty:
        st.dataframe(totals_df, use_container_width=True)
    else:
        st.info(f"Totals not yet implemented for {sport}.")

    def save_to_google_sheets():
        g = st.session_state.game

        # Reconcile every local event by event_id. Existing events are skipped; missing events are appended.
        all_rows = normalize_log_df(st.session_state.logs).to_dict("records")
        append_rows_to_game_log(g, all_rows)
        st.session_state.pending_sync_rows = []
        st.session_state.last_sync_error = None

        # Totals are derived and can safely be cleared/re-written without risking the event log.
        sh = google_retry(lambda: open_sheet(g["sheet_id"]))
        totals_title, _ = game_tab_titles(g)
        if totals_df is not None and not totals_df.empty:
            write_df_to_worksheet(sh, totals_title, totals_df)

    csave1, _ = st.columns([1.4, 6])
    if csave1.button("💾 Sync / Refresh Totals", type="primary"):
        try:
            save_to_google_sheets()
            st.success("Event log synced and player totals refreshed in Google Sheets.")
        except Exception as e:
            st.error(f"Save failed: {e}")

# ---------------------------
# Footer / Tips
# ---------------------------
st.divider()
st.caption(
    "Tips:\n"
    "1. Create your Google Sheet and share it with the service account email (As Editor)\n"
    "   Service Account - sheets-writer@football-stats-470918.iam.gserviceaccount.com\n"
    "2. Your first tab needs to be your team roster. It needs to be structured with these column Headers exactly: |Player First Name|Player Last Name|Player Number|Player Position(s)\n"
    "3. Every stat now auto-saves to the game Log tab. Re-enter the same sport/date/opponent/sheet to resume after a browser refresh or Streamlit restart.\n"
    "4. Example Google Sheet Setup - https://docs.google.com/spreadsheets/d/1_8dDjSdueDYt-WkKf-NptLskl7BJWeIX7K61nHB171A/edit?usp=sharing\n"
)

if not RUNNING_TESTS:
    main()
