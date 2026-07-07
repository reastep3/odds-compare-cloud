"""Tests for _read_props_from_odds_comparison book attribution.

The "Best Book" column (added 2026-07 to the Odds Comparison sheet) names
the book each edge was actually priced at. Before it existed, the reader
guessed DK-first, which mis-attributed FanDuel-only edges to DraftKings
whenever DK also listed the line — silently breaking the closing-line CLV
lookup (keys include the book). These tests pin both the new behavior and
the legacy fallback.
"""
import datetime as dt

import openpyxl
import pytest

import backtest as bt

_HDR_BASE = ["Rank", "Type", "Sport", "Game", "Market", "Pick", "Line",
             "EV %", "Fair Odds", "Commence"]


def _write_cmp_file(path, header_extra, row_extra):
    """One prop row: NBA points, LeBron James Over 25.5, EV 3.2%."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Odds Comparison"
    ws.append(_HDR_BASE + header_extra)
    ws.append([1, "Points O/U", "NBA", "LAL @ BOS", "Points",
               "LeBron James O", 25.5, 3.2, "+120", "2026-07-06"]
              + row_extra)
    wb.save(path)


def test_best_book_column_wins_over_dk_first(tmp_path):
    p = tmp_path / "ev_edges_test.xlsx"
    # DK also lists the line (-110), but the edge was found at FanDuel (+105)
    _write_cmp_file(p, ["DraftKings", "FanDuel", "Best Book"],
                    ["-110", "+105", "FanDuel"])
    picks = bt._read_props_from_odds_comparison(str(p), dt.date(2026, 7, 6))
    assert len(picks) == 1
    assert picks[0]["book"] == "FanDuel"
    assert picks[0]["odds"] == 105


def test_best_book_draftkings_unchanged(tmp_path):
    p = tmp_path / "ev_edges_test.xlsx"
    _write_cmp_file(p, ["DraftKings", "FanDuel", "Best Book"],
                    ["-110", "+105", "DraftKings"])
    picks = bt._read_props_from_odds_comparison(str(p), dt.date(2026, 7, 6))
    assert len(picks) == 1
    assert picks[0]["book"] == "DraftKings"
    assert picks[0]["odds"] == -110


def test_legacy_file_without_best_book_falls_back_dk_first(tmp_path):
    p = tmp_path / "ev_edges_test.xlsx"
    _write_cmp_file(p, ["DraftKings", "FanDuel"], ["-110", "+105"])
    picks = bt._read_props_from_odds_comparison(str(p), dt.date(2026, 7, 6))
    assert len(picks) == 1
    assert picks[0]["book"] == "DraftKings"
    assert picks[0]["odds"] == -110


def test_best_book_fanduel_with_empty_dk_cell(tmp_path):
    p = tmp_path / "ev_edges_test.xlsx"
    _write_cmp_file(p, ["DraftKings", "FanDuel", "Best Book"],
                    [None, "+105", "FanDuel"])
    picks = bt._read_props_from_odds_comparison(str(p), dt.date(2026, 7, 6))
    assert len(picks) == 1
    assert picks[0]["book"] == "FanDuel"
    assert picks[0]["odds"] == 105
