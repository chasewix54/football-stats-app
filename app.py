"""
Multi-Sport Stats App (Streamlit + Google Sheets)
------------------------------------------------
Scaffolded for multiple sports with a pluggable registry.

Included sports (scaffold): Football ✅ (fully implemented), Soccer ⚠️ (coming next),
Baseball ⚠️, Basketball ⚠️, Lacrosse ⚠️.

Features
- Create a game (sport + date + opponent + Google Sheet URL/ID)
- Read Roster from first worksheet (or one named "Roster") with columns:
  [Player First Name, Player Last Name, Player Number, Player Position(s)]
- Sport-specific **Log a Stat** form via a SportSpec plugin
- Running event log and sport-specific totals
- Save back to Google Sheets (two new tabs per game): Totals and Log
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

import re
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

import pandas as pd
import streamlit as st

# Google Sheets (service account flow only)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ---------------------------
# Page config MUST be first Streamlit call
# ---------------------------
st.set_page_config(page_title="Multi-Sport Stats App", page_icon="🏅", layout="wide")

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


def find_roster_ws(sh):
    try:
        return sh.worksheet("Roster")
    except Exception:
        return sh.get_worksheet(0)


def read_roster_df(sh) -> pd.DataFrame:
    ws = find_roster_ws(sh)
    rows = ws.get_all_records()
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

# ---------------------------
# SportSpec plugin system
# ---------------------------
class SportSpec:
    name: str = ""
    sides: List[str] = []  # e.g., ["Offense", "Defense"] or ["All"]

    def csv_template(self) -> pd.DataFrame:
        return pd.DataFrame({
            "Player First Name": ["First"],
            "Player Last Name": ["Last"],
            "Player Number": [0],
            "Player Position(s)": ["POS"],
        })

    def build_form(self, roster: pd.DataFrame) -> Dict[str, Any]:
        st.info("Sport not implemented yet. Choose Football or check back soon.")
        submitted = st.form_submit_button("Add Stat")
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

            if side == "Offense":
                stat_type = c3.selectbox(
                    "Offensive Stat",
                    options=["Reception", "Run", "Fumble", "Pass", "Field Goal", "Punt"],
                    key="fb_stat_off"
                )

                if stat_type in ("Reception", "Run", "Punt"):
                    yards = st.number_input("Yards", value=0, step=1, min_value=-99, max_value=300, key="fb_yards")
                    if stat_type in ("Reception", "Run"):
                        td_flag = st.checkbox("Touchdown", value=False, key="fb_td", help="Set to 1 if this play scored a TD.")
                        touchdown_val = 1 if td_flag else 0

                elif stat_type == "Pass":
                    outcome = st.selectbox("Pass Outcome", options=["Complete", "Incomplete"], key="fb_pass_outcome")
                    receiver_key = None
                    if outcome == "Complete":
                        yards = st.number_input("Pass Yards (if complete)", value=0, step=1, min_value=-99, max_value=300, key="fb_yards")
                        # Receiver list excludes passer (no QB throwing to himself)
                        receiver_options = [pk for pk in roster["player_key"].tolist() if pk != player_key]
                        receiver_key = st.selectbox(
                            "Receiver (to auto-log paired Reception)",
                            options=receiver_options if receiver_options else ["No eligible receivers"],
                            key="fb_receiver"
                        )
                        pair = st.checkbox(
                            "Also log paired Reception for the receiver",
                            value=True,
                            key="fb_pair_reception",
                            help="Creates a Reception for the selected receiver with same yards and TD."
                        )
                        td_flag = st.checkbox("Touchdown", value=False, key="fb_td",
                                              help="Set to 1 if this pass resulted in a TD.")
                        touchdown_val = 1 if td_flag else 0

                elif stat_type == "Field Goal":
                    outcome = st.selectbox("Field Goal Outcome", options=["Made", "Miss"], key="fb_fg_outcome")
                    yards = st.number_input("Attempt Distance (yards)", value=0, step=1, min_value=0, max_value=90, key="fb_yards")
                # Fumble: no yards/TD

            else:
                # Defense
                stat_type = c3.selectbox(
                    "Defensive Stat",
                    options=["Forced Fumble", "Sack", "Interception", "Tackle", "Return"],
                    key="fb_stat_def"
                )

                if stat_type == "Return":
                    yards = st.number_input("Return Yards", value=0, step=1, min_value=-99, max_value=300, key="fb_yards")
                    td_flag = st.checkbox("Touchdown", value=False, key="fb_td", help="Set to 1 if this return scored a TD.")
                    touchdown_val = 1 if td_flag else 0
                elif stat_type == "Interception":
                    td_flag = st.checkbox("Touchdown", value=False, key="fb_td", help="Set to 1 if this interception was returned for a TD.")
                    touchdown_val = 1 if td_flag else 0
                # Forced Fumble, Sack, Tackle: no yards/TD prompt

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
            # primary row
            row = base_row | {
                "stat_type": stat_type,
                "outcome": outcome,
                "yards": int(yards) if yards is not None else None,
                "touchdown": int(touchdown_val),
            }
            new_rows.append(row)

            # paired reception on pass complete
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
        df["yards"] = pd.to_numeric(df["yards"], errors="coerce").fillna(0).astype(int)
        df["touchdown"] = pd.to_numeric(df.get("touchdown", 0), errors="coerce").fillna(0).astype(int)

        grouped = []
        for player_key, grp in df.groupby("player_key"):
            row = {
                "player_key": player_key,
                "first_name": grp["first_name"].iloc[0],
                "last_name": grp["last_name"].iloc[0],
                "number": grp["number"].iloc[0],
                "positions": grp["positions"].iloc[0],
            }
            # Offense
            row["Receptions"] = int((grp["stat_type"] == "Reception").sum())
            row["Receiving Yards"] = int(grp.loc[grp["stat_type"] == "Reception", "yards"].sum())
            row["Receiving TDs"] = int(((grp["stat_type"] == "Reception") & (grp["touchdown"] == 1)).sum())

            row["Rush Attempts"] = int((grp["stat_type"] == "Run").sum())
            row["Rushing Yards"] = int(grp.loc[grp["stat_type"] == "Run", "yards"].sum())
            row["Rushing TDs"] = int(((grp["stat_type"] == "Run") & (grp["touchdown"] == 1)).sum())

            row["Punts"] = int((grp["stat_type"] == "Punt").sum())
            row["Punt Yards"] = int(grp.loc[grp["stat_type"] == "Punt", "yards"].sum())
            row["Fumbles"] = int((grp["stat_type"] == "Fumble").sum())

            # Passing
            pass_df = grp[grp["stat_type"] == "Pass"]
            row["Pass Attempts"] = int(len(pass_df))
            row["Pass Completions"] = int((pass_df["outcome"] == "Complete").sum())
            row["Pass Yards"] = int(pass_df.loc[pass_df["outcome"] == "Complete", "yards"].sum())
            row["Passing TDs"] = int(((pass_df["outcome"] == "Complete") & (pass_df["touchdown"] == 1)).sum())

            # Field Goals
            fg_df = grp[grp["stat_type"] == "Field Goal"]
            row["FG Attempts"] = int(len(fg_df))
            row["FG Made"] = int((fg_df["outcome"] == "Made").sum())
            row["FG Attempt Yards (Total)"] = int(fg_df["yards"].sum())

            # Defense
            row["Forced Fumbles"] = int((grp["stat_type"] == "Forced Fumble").sum())
            row["Sacks"] = int((grp["stat_type"] == "Sack").sum())
            row["Interceptions"] = int((grp["stat_type"] == "Interception").sum())
            row["Tackles"] = int((grp["stat_type"] == "Tackle").sum())
            row["Return Yards"] = int(grp.loc[grp["stat_type"] == "Return", "yards"].sum())
            row["Defensive TDs"] = int(((grp["stat_type"].isin(["Interception", "Return"])) & (grp["touchdown"] == 1)).sum())

            # Total TDs
            row["Touchdowns (Total)"] = int(row["Receiving TDs"] + row["Rushing TDs"] + row["Passing TDs"] + row["Defensive TDs"])

            grouped.append(row)

        totals = pd.DataFrame(grouped).sort_values(by=["last_name", "first_name"]).reset_index(drop=True)
        return totals

# ---------------------------
# Soccer tball implementation (fully functional)
# ---------------------------
class SoccerSpec(SportSpec):
    name = "Soccer"
    sides = ["All"]

    def build_form(self, roster: pd.DataFrame) -> Dict[str, Any]:
        """Soccer has no offense/defense split.
        Supported entries: Shot, Pass, Tackle, Interception, Save, Foul.
        - Shot: on_target (bool), goal (bool)
        - Pass: outcome Complete/Incomplete; if Complete, choose Recipient; optional "Resulted in Goal?"
                 If checked, auto-add two paired rows: (1) Assist for passer, (2) Shot(on_target=True, goal=True) for recipient.
        - Save: goalkeeper save (simple count)
        - Foul: choose card (None / Yellow / Red)
        """
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

            # Save / Tackle / Interception: no extra fields
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
                row = base_row | {
                    "stat_type": "Shot",
                    "on_target": int(bool(on_target)) if on_target is not None else None,
                    "goal": int(goal),
                }
                new_rows.append(row)

            elif stat_type == "Pass":
                row = base_row | {
                    "stat_type": "Pass",
                    "outcome": outcome,
                }
                new_rows.append(row)

                if outcome == "Complete" and receiver_key and receiver_key != player_key:
                    if resulted_goal:
                        assist_row = base_row | {
                            "stat_type": "Assist",
                        }
                        new_rows.append(assist_row)
                        try:
                            rcv = roster.loc[roster["player_key"] == receiver_key].iloc[0]
                            shot_row = base_row | {
                                "player_key": rcv["player_key"],
                                "first_name": rcv["first_name"],
                                "last_name": rcv["last_name"],
                                "number": int(rcv["number"]) if pd.notna(rcv["number"]) else None,
                                "positions": rcv["positions"],
                                "stat_type": "Shot",
                                "on_target": 1,
                                "goal": 1,
                            }
                            new_rows.append(shot_row)
                        except Exception:
                            pass

            elif stat_type in ("Tackle", "Interception", "Save"):
                row = base_row | {
                    "stat_type": stat_type,
                }
                new_rows.append(row)

            elif stat_type == "Foul":
                row = base_row | {
                    "stat_type": "Foul",
                    "card": card,
                }
                new_rows.append(row)

        return {"submitted": submitted, "new_rows": new_rows}

    def aggregate_totals(self, logs: pd.DataFrame) -> pd.DataFrame:
        """
        Aggregate per-player soccer totals.

        Doctests:
        >>> import pandas as _pd
        >>> sample = _pd.DataFrame([
        ...   {"sport":"Soccer","player_key":"#7 A","first_name":"A","last_name":"A","number":7,"positions":"F","stat_type":"Pass","outcome":"Complete","timestamp":"t","notes":""},
        ...   {"sport":"Soccer","player_key":"#7 A","first_name":"A","last_name":"A","number":7,"positions":"F","stat_type":"Assist","timestamp":"t","notes":""},
        ...   {"sport":"Soccer","player_key":"#9 B","first_name":"B","last_name":"B","number":9,"positions":"F","stat_type":"Shot","on_target":1,"goal":1,"timestamp":"t","notes":""},
        ...   {"sport":"Soccer","player_key":"#1 GK","first_name":"GK","last_name":"One","number":1,"positions":"GK","stat_type":"Save","timestamp":"t","notes":""},
        ...   {"sport":"Soccer","player_key":"#6 C","first_name":"C","last_name":"C","number":6,"positions":"M","stat_type":"Tackle","timestamp":"t","notes":""},
        ...   {"sport":"Soccer","player_key":"#5 D","first_name":"D","last_name":"D","number":5,"positions":"D","stat_type":"Interception","timestamp":"t","notes":""},
        ...   {"sport":"Soccer","player_key":"#4 E","first_name":"E","last_name":"E","number":4,"positions":"D","stat_type":"Foul","card":"Yellow","timestamp":"t","notes":""},
        ...   {"sport":"Soccer","player_key":"#3 F","first_name":"F","last_name":"F","number":3,"positions":"D","stat_type":"Foul","card":"Red","timestamp":"t","notes":""},
        ...   {"sport":"Soccer","player_key":"#7 A","first_name":"A","last_name":"A","number":7,"positions":"F","stat_type":"Pass","outcome":"Incomplete","timestamp":"t","notes":""},
        ... ])
        >>> out = SoccerSpec().aggregate_totals(sample)
        >>> int(out.loc[out['player_key']=='#7 A','Passes Attempted'].iloc[0])
        2
        >>> int(out.loc[out['player_key']=='#7 A','Passes Completed'].iloc[0])
        1
        >>> int(out.loc[out['player_key']=='#7 A','Assists'].iloc[0])
        1
        >>> int(out.loc[out['player_key']=='#9 B','Goals'].iloc[0])
        1
        >>> int(out.loc[out['player_key']=='#9 B','Shots on Target'].iloc[0])
        1
        >>> int(out.loc[out['player_key']=='#1 GK','Saves'].iloc[0])
        1
        >>> int(out.loc[out['player_key']=='#6 C','Tackles'].iloc[0])
        1
        >>> int(out.loc[out['player_key']=='#5 D','Interceptions'].iloc[0])
        1
        >>> int(out.loc[out['player_key']=='#4 E','Fouls'].iloc[0])
        1
        >>> int(out.loc[out['player_key']=='#4 E','Yellow Cards'].iloc[0])
        1
        >>> int(out.loc[out['player_key']=='#3 F','Red Cards'].iloc[0])
        1
        """
        df = logs.copy()
        df = df[df["sport"] == self.name]
        if df.empty:
            return pd.DataFrame()
        # Normalize flags
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
            # Shots
            row["Shots"] = int((grp["stat_type"] == "Shot").sum())
            row["Shots on Target"] = int(grp.loc[grp["stat_type"] == "Shot", "on_target"].sum())
            row["Goals"] = int(grp.loc[grp["stat_type"] == "Shot", "goal"].sum())
            # Passes
            pass_df = grp[grp["stat_type"] == "Pass"]
            row["Passes Attempted"] = int(len(pass_df))
            row["Passes Completed"] = int((pass_df["outcome"] == "Complete").sum())
            # Assists
            row["Assists"] = int((grp["stat_type"] == "Assist").sum())
            # Defensive actions
            row["Tackles"] = int((grp["stat_type"] == "Tackle").sum())
            row["Interceptions"] = int((grp["stat_type"] == "Interception").sum())
            # Saves
            row["Saves"] = int((grp["stat_type"] == "Save").sum())
            # Fouls & Cards
            foul_df = grp[grp["stat_type"] == "Foul"]
            row["Fouls"] = int(len(foul_df))
            row["Yellow Cards"] = int((foul_df["card"] == "Yellow").sum())
            row["Red Cards"] = int((foul_df["card"] == "Red").sum())
            # Derived: Pass %
            row["Pass Completion %"] = round(100.0 * row["Passes Completed"] / row["Passes Attempted"], 1) if row["Passes Attempted"] else 0.0

            grouped.append(row)

        totals = pd.DataFrame(grouped).sort_values(by=["last_name", "first_name"]).reset_index(drop=True)
        return totals
    
# ---------------------------
# Lacrosse implementation (New)
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
                "Goal","Assist","Shot","Ground Ball","Faceoff","Takeaway","Interception","Turnover","Penalty","Save","Goal Allowed","Goalie Minutes"
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
                on_target = st.selectbox("Shot on goal?", ["Yes","No"], key="lc_sog") == "Yes"

            elif stat_type == "Faceoff":
                faceoff_result = st.selectbox("Faceoff Result", ["Win","Loss"], key="lc_faceoff")

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
                new_rows.append(base | {"stat_type":"Goal","goal":1})
                new_rows.append(base | {"stat_type":"Shot","on_target":1})
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
                new_rows.append(base | {"stat_type":"Assist"})
            elif stat_type == "Shot":
                new_rows.append(base | {"stat_type":"Shot","on_target":int(on_target)})
            elif stat_type == "Ground Ball":
                new_rows.append(base | {"stat_type":"Ground Ball"})
            elif stat_type == "Faceoff":
                new_rows.append(base | {"stat_type":"Faceoff","outcome":faceoff_result})
            elif stat_type in ("Takeaway","Interception","Turnover","Save","Goal Allowed"):
                new_rows.append(base | {"stat_type":stat_type})
            elif stat_type == "Penalty":
                new_rows.append(base | {"stat_type":"Penalty","penalty_minutes":penalty_minutes})
            elif stat_type == "Goalie Minutes":
                new_rows.append(base | {"stat_type":"Goalie Minutes","minutes":minutes})

        return {"submitted": submitted, "new_rows": new_rows}

    def aggregate_totals(self, logs: pd.DataFrame) -> pd.DataFrame:
        df = logs.copy()
        df = df[df["sport"] == self.name]
        if df.empty:
            return pd.DataFrame()

        df["on_target"] = pd.to_numeric(df.get("on_target",0), errors="coerce").fillna(0).astype(int)
        df["penalty_minutes"] = pd.to_numeric(df.get("penalty_minutes",0), errors="coerce").fillna(0).astype(float)
        df["minutes"] = pd.to_numeric(df.get("minutes",0), errors="coerce").fillna(0).astype(float)

        grouped = []
        for pk, grp in df.groupby("player_key"):
            row = {
                "player_key": pk,
                "first_name": grp["first_name"].iloc[0],
                "last_name": grp["last_name"].iloc[0],
                "number": grp["number"].iloc[0],
                "positions": grp["positions"].iloc[0],
            }
            row["Goals"] = int((grp["stat_type"]=="Goal").sum())
            shots = grp[grp["stat_type"]=="Shot"]
            row["Shots"] = len(shots)
            row["Shots on Goal"] = int(shots["on_target"].sum())
            row["Assists"] = int((grp["stat_type"]=="Assist").sum())
            row["Points"] = row["Goals"] + row["Assists"]
            row["Ground Balls"] = int((grp["stat_type"]=="Ground Ball").sum())
            # Faceoffs
            fo = grp[grp["stat_type"]=="Faceoff"]
            row["Faceoffs Attempted"] = len(fo)
            row["Faceoffs Won"] = int((fo["outcome"]=="Win").sum())
            row["Faceoff %"] = round(100*row["Faceoffs Won"]/row["Faceoffs Attempted"],1) if row["Faceoffs Attempted"] else 0.0
            # Defensive
            row["Takeaways"] = int((grp["stat_type"]=="Takeaway").sum())
            row["Interceptions"] = int((grp["stat_type"]=="Interception").sum())
            row["Caused Turnovers"] = row["Takeaways"] + row["Interceptions"]
            row["Turnovers"] = int((grp["stat_type"]=="Turnover").sum())
            # Penalties
            pen = grp[grp["stat_type"]=="Penalty"]
            row["Penalties"] = len(pen)
            row["Penalty Minutes"] = float(pen["penalty_minutes"].sum()) if not pen.empty else 0.0
            # Goalie
            row["Saves"] = int((grp["stat_type"]=="Save").sum())
            row["Goals Allowed"] = int((grp["stat_type"]=="Goal Allowed").sum())
            row["Minutes"] = float(grp.loc[grp["stat_type"]=="Goalie Minutes","minutes"].sum())
            sog_faced = row["Saves"] + row["Goals Allowed"]
            row["Shots on Goal Faced"] = sog_faced
            row["Save %"] = round(100*row["Saves"]/sog_faced,1) if sog_faced else 0.0
            row["GAA"] = round((row["Goals Allowed"]*48)/row["Minutes"],2) if row["Minutes"]>0 else 0.0
            # Derived shooting
            row["Shooting %"] = round(100*row["Goals"]/row["Shots on Goal"],1) if row["Shots on Goal"] else 0.0
            row["SOG Rate %"] = round(100*row["Shots on Goal"]/row["Shots"],1) if row["Shots"] else 0.0
            grouped.append(row)

        return pd.DataFrame(grouped).sort_values(by=["last_name","first_name"]).reset_index(drop=True)

# ---------------------------
# Placeholder specs (scaffold only)
# ---------------------------
class BaseballSpec(SportSpec):
    name = "Baseball"
    sides = ["All"]

class BaseballSpec(SportSpec):
    name = "Baseball"
    sides = ["All"]

class BasketballSpec(SportSpec):
    name = "Basketball"
    sides = ["All"]

class BaseballSpec(SportSpec):
    name = "Baseball"
    sides = ["All"]

class BasketballSpec(SportSpec):
    name = "Basketball"
    sides = ["All"]

class BaseballSpec(SportSpec):
    name = "Baseball"
    sides = ["All"]

class BasketballSpec(SportSpec):
    name = "Basketball"
    sides = ["All"]

SPORTS: Dict[str, SportSpec] = {
    "Football": FootballSpec(),
    "Soccer": SoccerSpec(),
    "Baseball": BaseballSpec(),
    "Basketball": BasketballSpec(),
    "Lacrosse": LacrosseSpec(),
}

# ---------------------------
# Session state
# ---------------------------
if "game" not in st.session_state:
    st.session_state.game = None
if "roster" not in st.session_state:
    st.session_state.roster = pd.DataFrame()
if "logs" not in st.session_state:
    st.session_state.logs = pd.DataFrame()

# ---------------------------
# Header
# ---------------------------
st.title("🏅 Multi-Sport Stats Collector → Google Sheets")
st.caption("Create a game → pick a sport → log plays → save totals & log back to your Sheet.")

# ---------------------------
# 1) Create a Game (paste an existing Google Sheet URL/ID)
# ---------------------------
with st.expander("① Create a Game", expanded=True):
    c0, c1, c2, c3 = st.columns([1.2, 1, 2, 2])
    sport_name = c0.selectbox("Sport", options=list(SPORTS.keys()), index=0, key="sport_selector")
    game_date = c1.date_input("Game Date", value=datetime.today())
    opponent = c2.text_input("Opponent", placeholder="E.g., Wildcats")
    sheet_url = c3.text_input("Google Sheet URL or ID", key="sheet_url_input", placeholder="Paste the sheet URL or ID here…")

    create_btn = st.button("Create Game / Load Roster", type="primary")

    if create_btn:
        try:
            sh = open_sheet(sheet_url)
            roster_df = read_roster_df(sh)
            st.session_state.roster = roster_df
            st.session_state.game = {
                "sport": sport_name,
                "date": game_date.strftime("%Y-%m-%d"),
                "opponent": opponent.strip(),
                "sheet_id": parse_sheet_id_from_url(sheet_url),
            }
            st.session_state.logs = pd.DataFrame(columns=[
                "timestamp", "sport", "player_key", "first_name", "last_name", "number", "positions",
                "side", "stat_type", "outcome", "yards", "touchdown", "notes"
            ])
            st.success("Game created and roster loaded.")
        except Exception as e:
            st.error(f"Failed to open sheet / read roster: {e}")

# ---------------------------
# Show current game/roster
# ---------------------------
if st.session_state.game:
    g = st.session_state.game
    st.info(f"**Game:** {g['date']} vs {g['opponent']} — **Sport:** {g['sport']}")
    with st.expander("Roster (from Google Sheet)"):
        st.dataframe(st.session_state.roster, use_container_width=True)

    # CSV import
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
            existing = {ws.title: ws for ws in sh.worksheets()}
            if title in existing:
                sh.del_worksheet(existing[title])
                time.sleep(0.3)
            ws = sh.add_worksheet(title=title, rows=str(len(df) + 10), cols=str(len(df.columns) + 5))
            ws.update([df.columns.tolist()] + df.astype(str).values.tolist())

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
                        sh = open_sheet(g["sheet_id"])  # reopen
                        upsert_ws_with_df(sh, "Roster", new_roster)
                        st.session_state.roster = read_roster_df(sh)
                        st.success("Roster sheet updated from CSV.")
            except Exception as e:
                st.error(f"Failed to process CSV: {e}")

# ---------------------------
# 2) Log a Stat (delegated to SportSpec)
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
            # Append rows exactly once (no duplicates)
            st.session_state.logs = pd.concat(
                [st.session_state.logs, pd.DataFrame(result["new_rows"])],
                ignore_index=True
            )
            if len(result["new_rows"]) == 2:
                a, b = result["new_rows"][0], result["new_rows"][1]
                st.session_state["flash_message"] = (
                    f"✅ Added {a['stat_type']} for {a['first_name']} {a['last_name']} and "
                    f"{b['stat_type']} for {b['first_name']} {b['last_name']}"
                )
            else:
                a = result["new_rows"][0]
                st.session_state["flash_message"] = f"✅ Added {a['stat_type']} for {a['first_name']} {a['last_name']}"
            st.rerun()

# ---------------------------
# 3) Running Log & Totals
# ---------------------------
if not st.session_state.logs.empty:
    st.subheader("③ Running Event Log")
    st.dataframe(st.session_state.logs, use_container_width=True)

    st.subheader("④ Player Totals (auto-calculated)")
    sport = st.session_state.game["sport"] if st.session_state.game else "Football"
    totals_df = SPORTS[sport].aggregate_totals(st.session_state.logs)
    if totals_df is not None and not totals_df.empty:
        st.dataframe(totals_df, use_container_width=True)
    else:
        st.info(f"Totals not yet implemented for {sport}.")

    # ---------------------------
    # 4) Save back to Google Sheets
    # ---------------------------
    def save_to_google_sheets():
        g = st.session_state.game
        sh = open_sheet(g["sheet_id"])  # re-open
        stamp = f"{g['sport']} {g['date']} vs {g['opponent']}"

        totals_title = f"{stamp} (Totals)"
        log_title = f"{stamp} (Log)"

        existing = {ws.title: ws for ws in sh.worksheets()}
        for title in (totals_title, log_title):
            if title in existing:
                sh.del_worksheet(existing[title])
                time.sleep(0.4)

        if totals_df is not None and not totals_df.empty:
            ws_totals = sh.add_worksheet(title=totals_title, rows=str(len(totals_df) + 10), cols=str(len(totals_df.columns) + 5))
            ws_totals.update([totals_df.columns.tolist()] + totals_df.astype(str).values.tolist())

        logs_df = st.session_state.logs.copy()
        ws_log = sh.add_worksheet(title=log_title, rows=str(len(logs_df) + 10), cols=str(len(logs_df.columns) + 5))
        ws_log.update([logs_df.columns.tolist()] + logs_df.astype(str).values.tolist())

    csave1, _ = st.columns([1, 6])
    if csave1.button("💾 Save to Google Sheet", type="primary"):
        try:
            save_to_google_sheets()
            st.success("Saved game totals and log to your Google Sheet (two new tabs).")
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
    "3. Example Google Sheet Setup - https://docs.google.com/spreadsheets/d/1_8dDjSdueDYt-WkKf-NptLskl7BJWeIX7K61nHB171A/edit?usp=sharing\n\n"
)
