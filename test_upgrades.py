"""Tests for the 2026-07-09 upgrade set: boost-token allocator + SGP
haircut pricing, offsite archive backup, and the placed-bets log.

Money-path logic (token allocation, parlay settlement with pushes,
backup signing) gets the same regression floor as the core betting math.
"""
import datetime as dt
import json
import os

import pytest

import backup_archive
import bets_log
import sgp_log
from odds_compare import (
    _attach_parlay_alternatives,
    _build_parlay_rows,
    SGP_ASSUMED_HAIRCUT_PCT,
)


# ── Boost-token allocator ──────────────────────────────────────────────

def _edge(game, market, outcome, dec=1.6, fair=68.0):
    commence = (dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    return {"book": "DraftKings", "sport": "mlb",
            "sport_key": "baseball_mlb", "game": game, "market": market,
            "outcome": outcome, "point": None, "odds_decimal": dec,
            "odds_american": -167, "fair_prob": fair, "ev_pct": 5.0,
            "kelly_pct": 2.0, "commence": commence}


EDGES = [_edge(f"G{i} vs H{i}", "h2h", f"G{i}", fair=70 - i)
         for i in range(5)]


def _boost_promo(**over):
    p = {"label": "30% boost", "book": "DraftKings",
         "promo_type": "straight", "boost_pct": 30,
         "sport": "all", "market": "all"}
    p.update(over)
    return p


def test_allocator_attaches_three_leg_cross_game_alt():
    pp = {"promo": _boost_promo(), "edge": EDGES[0], "copy_idx": 0}
    _attach_parlay_alternatives([pp], EDGES, 500, 500)
    alt = pp.get("alt_parlay")
    assert alt is not None
    assert alt["num_legs"] == 3
    assert len({l["game"] for l in alt["legs"]}) == 3   # cross-game


@pytest.mark.parametrize("promo_over,pp_over", [
    ({"straight_only": True}, {}),          # token restricted to singles
    ({"game_specific": True}, {}),          # game-scoped token
    ({"refund_on_draw": True}, {}),         # draw-insurance rider
    ({"promo_type": "bonus_bet"}, {}),      # not a boost
    ({"boost_pct": None}, {}),              # missing boost
    ({}, {"copy_idx": 1}),                  # duplicate copy of the token
])
def test_allocator_skips_restricted_tokens(promo_over, pp_over):
    pp = {"promo": _boost_promo(**promo_over), "edge": EDGES[0],
          "copy_idx": 0}
    pp.update(pp_over)
    _attach_parlay_alternatives([pp], EDGES, 500, 500)
    assert "alt_parlay" not in pp


# ── Parlay builder: exact legs + SGP haircut pricing ──────────────────

def test_builder_builds_exactly_num_legs():
    promo = _boost_promo(promo_type="parlay", num_legs=3)
    rows = _build_parlay_rows(EDGES, [promo], 500, 500)
    assert rows and rows[0]["legs"] is not None
    # 5 candidates available; the old +3 exploration could pick 4+.
    assert rows[0]["num_legs"] == 3 and len(rows[0]["legs"]) == 3


def test_sgp_defaults_to_two_legs_and_haircut_pricing():
    sgp_edges = [
        _edge("A vs B", "h2h", "A", fair=72.0),
        _edge("A vs B", "totals", "Over", fair=71.0),
        _edge("A vs B", "pts", "LeBron James", fair=70.0),
    ]
    promo = _boost_promo(promo_type="parlay", num_legs=None, sgp=True)
    rows = _build_parlay_rows(sgp_edges, [promo], 500, 500)
    r = rows[0]
    assert r["legs"] is not None and r["num_legs"] == 2
    assert r["sgp_haircut_pct"] == pytest.approx(SGP_ASSUMED_HAIRCUT_PCT)
    # eff price = naive shaved by the haircut; boost applies to eff.
    naive = r["parlay_decimal"]
    expected_eff = 1 + (naive - 1) * (1 - r["sgp_haircut_pct"] / 100)
    assert r["eff_decimal"] == pytest.approx(expected_eff, abs=1e-3)
    assert r["boost_dec"] == pytest.approx(
        1 + (expected_eff - 1) * 1.30, abs=1e-3)


def test_cross_game_rows_have_no_haircut():
    promo = _boost_promo(promo_type="parlay", num_legs=3)
    rows = _build_parlay_rows(EDGES, [promo], 500, 500)
    r = rows[0]
    assert r["sgp_haircut_pct"] == 0
    assert r["eff_decimal"] == pytest.approx(r["parlay_decimal"])


def test_observed_haircut_pct(tmp_path, monkeypatch):
    monkeypatch.setattr(sgp_log, "STORE", str(tmp_path / "slips.jsonl"))
    # No file → default
    assert sgp_log.observed_haircut_pct(default=15.0) == 15.0
    # 10 distinct tickets, each quoted 20% under the naive payout
    for i in range(10):
        sgp_log.log_slip({
            "naive_dec": 6.0, "quoted_dec": 5.0,   # (5-1)/(6-1) → 20% cut
            "legs": [{"game": f"g{i}", "game_date": "20260701",
                      "market": "h2h", "pick": "A", "line": None}],
        })
    assert sgp_log.observed_haircut_pct(default=15.0) == pytest.approx(20.0)
    # Below min_n falls back to default
    assert sgp_log.observed_haircut_pct(default=7.0, min_n=99) == 7.0


# ── Archive backup ─────────────────────────────────────────────────────

def test_backup_skips_when_unconfigured(monkeypatch):
    for k in ("BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET",
              "BACKUP_S3_KEY_ID", "BACKUP_S3_SECRET",
              "AWS_ENDPOINT_URL_S3", "BUCKET_NAME",
              "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)
    result = backup_archive.run_backup(output_dir="/nonexistent")
    assert result["ok"] is False and "skipped" in result


def test_cfg_tigris_fallback_and_precedence(monkeypatch):
    for k in ("BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET",
              "BACKUP_S3_KEY_ID", "BACKUP_S3_SECRET", "BACKUP_S3_REGION"):
        monkeypatch.delenv(k, raising=False)
    # Tigris convention (what `fly storage create` stamps on the app)
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://fly.storage.tigris.dev")
    monkeypatch.setenv("BUCKET_NAME", "arbyx-backups")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "tid")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "tsecret")
    monkeypatch.setenv("AWS_REGION", "auto")
    cfg = backup_archive._cfg()
    assert cfg["bucket"] == "arbyx-backups" and cfg["key_id"] == "tid"
    # Explicit BACKUP_S3_* overrides the fallback
    monkeypatch.setenv("BACKUP_S3_BUCKET", "elsewhere")
    assert backup_archive._cfg()["bucket"] == "elsewhere"


def test_build_backup_tar(tmp_path):
    src = tmp_path / "archive"
    src.mkdir()
    (src / "a.xlsx").write_bytes(b"x" * 10)
    (src / "b.xlsx").write_bytes(b"y" * 10)
    out = tmp_path / "out.tar.gz"
    n = backup_archive.build_backup_tar([str(src), "/does/not/exist"],
                                        str(out))
    assert n == 2 and out.exists() and out.stat().st_size > 0


def test_sigv4_headers_shape_and_stability():
    cfg = {"endpoint": "https://acct.r2.cloudflarestorage.com",
           "bucket": "arbyx", "key_id": "AKID", "secret": "SECRET",
           "region": "auto"}
    now = dt.datetime(2026, 7, 9, 8, 0, 0, tzinfo=dt.timezone.utc)
    h1 = backup_archive.sigv4_put_headers(cfg, "backup.tar.gz",
                                          "e" * 64, now=now)
    h2 = backup_archive.sigv4_put_headers(cfg, "backup.tar.gz",
                                          "e" * 64, now=now)
    assert h1 == h2                                  # deterministic
    assert h1["x-amz-date"] == "20260709T080000Z"
    assert h1["Authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKID/20260709/auto/s3/aws4_request")
    assert "Signature=" in h1["Authorization"]
    # Different payload → different signature
    h3 = backup_archive.sigv4_put_headers(cfg, "backup.tar.gz",
                                          "f" * 64, now=now)
    assert h3["Authorization"] != h1["Authorization"]
    # Different method → different signature (DELETE for retention)
    h4 = backup_archive.sigv4_headers(cfg, "DELETE", "backup.tar.gz",
                                      "e" * 64, now=now)
    assert h4["Authorization"] != h1["Authorization"]


def test_run_backup_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_S3_ENDPOINT", "https://x.example.com")
    monkeypatch.setenv("BACKUP_S3_BUCKET", "b")
    monkeypatch.setenv("BACKUP_S3_KEY_ID", "k")
    monkeypatch.setenv("BACKUP_S3_SECRET", "s")
    out = tmp_path / "output"
    (out / "archive").mkdir(parents=True)
    (out / "archive" / "ev.xlsx").write_bytes(b"data")
    uploads, prunes = [], []
    monkeypatch.setattr(backup_archive, "upload_file",
                        lambda cfg, path, key: uploads.append(key))
    monkeypatch.setattr(backup_archive, "delete_file",
                        lambda cfg, key: prunes.append(key))
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "60")
    result = backup_archive.run_backup(output_dir=str(out),
                                       data_dir=str(tmp_path / "nostate"))
    assert result["ok"] is True and result["files"] == 1
    assert uploads and uploads[0].startswith("arbyx-backup-")
    # Retention pruned exactly the object from 60 days before today's key
    assert prunes == [result["pruned"]]
    up_d = dt.date.fromisoformat(
        uploads[0].removeprefix("arbyx-backup-").removesuffix(".tar.gz"))
    pr_d = dt.date.fromisoformat(
        prunes[0].removeprefix("arbyx-backup-").removesuffix(".tar.gz"))
    assert (up_d - pr_d).days == 60


def test_run_backup_reports_upload_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_S3_ENDPOINT", "https://x.example.com")
    monkeypatch.setenv("BACKUP_S3_BUCKET", "b")
    monkeypatch.setenv("BACKUP_S3_KEY_ID", "k")
    monkeypatch.setenv("BACKUP_S3_SECRET", "s")
    out = tmp_path / "output"
    (out / "archive").mkdir(parents=True)
    (out / "archive" / "ev.xlsx").write_bytes(b"data")

    def boom(cfg, path, key):
        raise RuntimeError("upload HTTP 403")
    monkeypatch.setattr(backup_archive, "upload_file", boom)
    result = backup_archive.run_backup(output_dir=str(out),
                                       data_dir=str(tmp_path / "no"))
    assert result["ok"] is False and "403" in result["error"]


# ── Placed-bets log ────────────────────────────────────────────────────

def _placed(result=None, legs=None, stake=10.0, decimal=2.0, boost=0):
    return {"source": "play", "book": "DraftKings", "stake": stake,
            "decimal": decimal, "boost_pct": boost, "label": "test",
            "legs": legs or [{"game": "A vs B", "game_date": "20260701",
                              "market": "h2h", "pick": "A", "line": None,
                              "decimal": decimal, "pick_key": "k1"}],
            "result": result}


def test_bets_log_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(bets_log, "STORE", str(tmp_path / "bets.jsonl"))
    assert bets_log.log_placed([_placed(), _placed()]) == 2
    loaded = bets_log._load()
    assert len(loaded) == 2
    assert loaded[0]["result"] is None and "ts" in loaded[0]


def test_settle_pnl_straight():
    b = _placed(result="WIN", stake=10, decimal=2.5)
    b["leg_results"] = ["WIN"]
    assert bets_log._settle_pnl(b) == pytest.approx(15.0)
    b = _placed(result="LOSS", stake=10)
    assert bets_log._settle_pnl(b) == -10.0
    b = _placed(result="PUSH", stake=10)
    assert bets_log._settle_pnl(b) == 0.0


def test_settle_pnl_parlay_push_reprices():
    legs = [
        {"decimal": 1.5, "game": "A", "game_date": "20260701"},
        {"decimal": 1.6, "game": "B", "game_date": "20260701"},
        {"decimal": 2.0, "game": "C", "game_date": "20260701"},
    ]
    b = _placed(result="WIN", legs=legs, stake=10,
                decimal=1 + (1.5 * 1.6 * 2.0 - 1) * 1.3, boost=30)
    b["leg_results"] = ["WIN", "PUSH", "WIN"]
    # Push drops the 1.6 leg → 1.5 × 2.0 = 3.0, boost reapplied → 3.6
    assert bets_log._settle_pnl(b) == pytest.approx(10 * 2.6)


def test_grade_pending_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(bets_log, "STORE", str(tmp_path / "bets.jsonl"))
    bets_log.log_placed([_placed(stake=10, decimal=2.0),
                         _placed(stake=5, decimal=1.8)])

    import backtest

    def fake_grade(picks, *a, **k):
        # Each bet grades in its own run_backtest call (one leg each) —
        # key the verdict off the leg's price: the 2.0 bet WINs, 1.8 LOSSes.
        for p in picks:
            p["result"] = "WIN" if p["decimal"] == 2.0 else "LOSS"
        return picks
    monkeypatch.setattr(backtest, "run_backtest",
                        lambda picks: fake_grade(picks))

    stats = bets_log.grade_pending()
    assert stats["graded"] == 2
    s = bets_log.summary(days=7)
    assert s["n_settled"] == 2 and s["wins"] == 1 and s["losses"] == 1
    assert s["pnl"] == pytest.approx(10.0 - 5.0)
    assert s["staked"] == pytest.approx(15.0)
    assert s["by_book"]["DraftKings"]["n"] == 2


def test_mark_placed_flags_sidecar(tmp_path):
    import closing_lines
    d = dt.datetime.now(closing_lines.EASTERN).date()
    path = tmp_path / f"pending_closes_{d.isoformat()}.json"
    path.write_text(json.dumps(
        {"picks": {"k1": {"open": -110}, "k2": {"open": +120}}}))
    n = closing_lines.mark_placed(["k1"], output_dir=str(tmp_path))
    assert n == 1
    payload = json.loads(path.read_text())
    assert payload["picks"]["k1"]["placed"] is True
    assert "placed" not in payload["picks"]["k2"]
