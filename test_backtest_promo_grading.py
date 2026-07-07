"""Unit tests for the anytime-prop promo graders added to backtest.py.

Before this, HR and anytime-goalscorer promo picks were hard-coded to
MANUAL and silently dropped from every P&L report (they have no game-line
fallback, so the picker always emits one — they were the invisible tail of
the promo program). These graders settle them via box scores with DNP→void
(PUSH) detection. The fetchers are monkeypatched so the suite stays offline.
"""
import backtest as bt


# ── _grade_hr_anytime (MLB Stats API box score) ────────────────────────

def _hr_pick(player="Aaron Judge"):
    return {"game_date": "20260616", "player": player, "pick": player,
            "market": "HR"}


def _gr_mlb():
    return {"home_team": "New York Yankees", "away_team": "Boston Red Sox"}


def test_hr_win(monkeypatch):
    monkeypatch.setattr(bt, "_fetch_mlb_player_stats",
                        lambda *a, **k: {"Aaron Judge": {"HR": 2.0}})
    assert bt._grade_hr_anytime(_hr_pick(), _gr_mlb()) == "WIN"


def test_hr_loss_batted_zero(monkeypatch):
    monkeypatch.setattr(bt, "_fetch_mlb_player_stats",
                        lambda *a, **k: {"Aaron Judge": {"HR": 0.0}})
    assert bt._grade_hr_anytime(_hr_pick(), _gr_mlb()) == "LOSS"


def test_hr_void_on_dnp(monkeypatch):
    # On the roster but did not bat — book voids/refunds → PUSH (P&L 0).
    monkeypatch.setattr(bt, "_fetch_mlb_player_stats",
                        lambda *a, **k: {"Aaron Judge": {bt._DNP_FLAG: True}})
    assert bt._grade_hr_anytime(_hr_pick(), _gr_mlb()) == "PUSH"


def test_hr_void_when_absent_from_boxscore(monkeypatch):
    # Game graded but batter not in the box score at all → treated as void.
    monkeypatch.setattr(bt, "_fetch_mlb_player_stats",
                        lambda *a, **k: {"Some Other Guy": {"HR": 1.0}})
    assert bt._grade_hr_anytime(_hr_pick(), _gr_mlb()) == "PUSH"


def test_hr_no_result_when_game_missing(monkeypatch):
    monkeypatch.setattr(bt, "_fetch_mlb_player_stats", lambda *a, **k: {})
    assert bt._grade_hr_anytime(_hr_pick(), _gr_mlb()) == "NO_RESULT"


# ── _grade_goal_anytime (ESPN World Cup summary) ───────────────────────

def _goal_pick(player="Kylian Mbappe"):
    return {"game_date": "20260616", "player": player, "pick": player,
            "market": "GOAL"}


def _summary(goal_scorers=(), started=(), subbed_in=(), benched=()):
    key_events = [{"type": {"text": "Goal"},
                   "athletesInvolved": [{"displayName": n}]}
                  for n in goal_scorers]
    roster = ([{"athlete": {"displayName": n}, "starter": True} for n in started]
              + [{"athlete": {"displayName": n}, "subbedIn": True} for n in subbed_in]
              + [{"athlete": {"displayName": n}, "starter": False,
                  "subbedIn": False} for n in benched])
    return {"keyEvents": key_events, "rosters": [{"roster": roster}]}


def test_goal_win(monkeypatch):
    monkeypatch.setattr(bt, "_fetch_espn_summary",
                        lambda *a, **k: _summary(goal_scorers=["Kylian Mbappe"],
                                                 started=["Kylian Mbappe"]))
    assert bt._grade_goal_anytime(_goal_pick(), {"event_id": "1"}) == "WIN"


def test_goal_loss_played_no_goal(monkeypatch):
    monkeypatch.setattr(bt, "_fetch_espn_summary",
                        lambda *a, **k: _summary(started=["Kylian Mbappe"]))
    assert bt._grade_goal_anytime(_goal_pick(), {"event_id": "1"}) == "LOSS"


def test_goal_void_when_benched(monkeypatch):
    # Named in the squad but never came on → void (PUSH).
    monkeypatch.setattr(bt, "_fetch_espn_summary",
                        lambda *a, **k: _summary(benched=["Kylian Mbappe"]))
    assert bt._grade_goal_anytime(_goal_pick(), {"event_id": "1"}) == "PUSH"


def test_goal_own_goal_does_not_count(monkeypatch):
    summ = {"keyEvents": [{"type": {"text": "Own Goal"},
                           "athletesInvolved": [{"displayName": "Kylian Mbappe"}]}],
            "rosters": [{"roster": [{"athlete": {"displayName": "Kylian Mbappe"},
                                     "starter": True}]}]}
    monkeypatch.setattr(bt, "_fetch_espn_summary", lambda *a, **k: summ)
    assert bt._grade_goal_anytime(_goal_pick(), {"event_id": "1"}) == "LOSS"


def test_goal_no_result_when_summary_missing(monkeypatch):
    monkeypatch.setattr(bt, "_fetch_espn_summary", lambda *a, **k: {})
    assert bt._grade_goal_anytime(_goal_pick(), {"event_id": "1"}) == "NO_RESULT"


def test_goal_defaults_loss_when_rosters_unavailable(monkeypatch):
    # No rosters to confirm appearance: a non-scoring goalscorer pick (made
    # inside the lineup window) defaults to LOSS, the realistic case.
    monkeypatch.setattr(bt, "_fetch_espn_summary",
                        lambda *a, **k: {"keyEvents": [], "rosters": []})
    assert bt._grade_goal_anytime(_goal_pick(), {"event_id": "1"}) == "LOSS"


# ── Promo odds-band digest section ─────────────────────────────────────

def _band_pick(dec, result="WIN", bet=10.0):
    pnl = round(bet * (dec - 1), 2) if result == "WIN" else (
        0.0 if result == "PUSH" else -bet)
    return {"decimal": dec, "result": result, "pnl": pnl, "bet_amount": bet}


def test_promo_odds_band_edges():
    # decimal 2.0 = +100 (band edge, inclusive on the low side)
    assert bt._promo_odds_band({"decimal": 2.0})  == "≤+100"
    assert bt._promo_odds_band({"decimal": 1.5})  == "≤+100"   # -200
    assert bt._promo_odds_band({"decimal": 2.5})  == "+100..+150"
    assert bt._promo_odds_band({"decimal": 2.51}) == "+150..+300"
    assert bt._promo_odds_band({"decimal": 4.0})  == "+150..+300"
    assert bt._promo_odds_band({"decimal": 4.01}) == "+300..+700"
    assert bt._promo_odds_band({"decimal": 8.0})  == "+300..+700"
    assert bt._promo_odds_band({"decimal": 8.01}) == ">+700"


def test_promo_odds_band_rejects_unusable_decimal():
    assert bt._promo_odds_band({"decimal": None}) is None
    assert bt._promo_odds_band({"decimal": 1.0})  is None
    assert bt._promo_odds_band({})                is None


def test_digest_promo_bands_fixed_order_and_danger_flag():
    picks = (
        [_band_pick(9.0, "LOSS")] * 3          # >+700, all losses → ⚠
        + [_band_pick(3.0, "WIN")]             # +150..+300
        + [_band_pick(3.0, "LOSS")]
        + [_band_pick(5.0, "WIN", bet=5.0)]    # +300..+700, net positive
    )
    block = bt._digest_promo_bands(picks)
    assert "Promos by Odds Band" in block
    lines = [ln for ln in block.splitlines() if ln.startswith("  ")]
    # Fixed band order regardless of group sizes
    assert [ln.split(":")[0].strip() for ln in lines] == [
        "+150..+300", "+300..+700", ">+700"]
    # Danger flag only on the losing danger band
    assert "⚠" in lines[2]
    assert "⚠" not in lines[0] and "⚠" not in lines[1]


def test_digest_promo_bands_empty_when_no_bandable_picks():
    assert bt._digest_promo_bands([]) == ""
    no_dec = {"decimal": None, "result": "LOSS", "pnl": -10.0,
              "bet_amount": 10.0}
    assert bt._digest_promo_bands([no_dec]) == ""
