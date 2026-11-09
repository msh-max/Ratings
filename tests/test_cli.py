"""CLI end to end with --fake: run, no-op resume, warm start, budget abort, every --help."""

from __future__ import annotations

import json
import sqlite3

import pytest
from click.testing import CliRunner

from ksa_restaurants.cli import main

COMMANDS = ["discover", "estimate", "fetch", "audit", "export", "report", "run", "smoke"]


@pytest.mark.parametrize("cmd", [[]] + [[c] for c in COMMANDS])
def test_help(cmd):
    res = CliRunner().invoke(main, cmd + ["--help"])
    assert res.exit_code == 0 and "Usage" in res.output


def _calls(db):
    with sqlite3.connect(db) as conn:
        return dict(conn.execute("SELECT run_id, SUM(calls) FROM api_calls GROUP BY run_id").fetchall())


def test_fake_run_resume_and_warm_start(tmp_path):
    db, out, sdir = str(tmp_path / "f.sqlite"), str(tmp_path / "exports"), str(tmp_path / "summary")
    args = ["--fake", "--db", db]
    tail = ["--out", out, "--summary-dir", sdir, "--audit-cells", "30"]
    r = CliRunner().invoke(main, args + ["--run-id", "2026-10-02T0100", "run"] + tail)
    assert r.exit_code == 0, r.output
    assert "Places found this run" in r.output and "no previous run" in r.output
    first = _calls(db)["2026-10-02T0100"]
    r = CliRunner().invoke(main, args + ["--run-id", "2026-10-02T0100", "run"] + tail)
    assert r.exit_code == 0 and _calls(db)["2026-10-02T0100"] == first  # no-op resume
    r = CliRunner().invoke(main, args + ["--run-id", "2026-11-02T0100", "run"] + tail)
    assert r.exit_code == 0, r.output
    assert "vs previous run       : 2026-10-02T0100" in r.output
    assert _calls(db)["2026-11-02T0100"] < first  # warm start
    s = json.loads((tmp_path / "summary" / "latest.json").read_text("utf-8"))
    assert s["month_over_month"]["new"] > 0 and s["run"]["run_id"] == "2026-11-02T0100"
    for cmd in (["report"], ["estimate"], ["export", "--out", out, "--summary-dir", sdir]):
        r = CliRunner().invoke(main, args + cmd)
        assert r.exit_code == 0, r.output


def test_budget_abort_exit_code_and_step_by_step(tmp_path):
    db = str(tmp_path / "b.sqlite")
    args = ["--fake", "--db", db, "--run-id", "x1"]
    r = CliRunner().invoke(main, args + ["--max-budget-usd", "0.5", "run", "--out", str(tmp_path / "e"),
                                         "--summary-dir", str(tmp_path / "s")])
    assert r.exit_code == 2 and "BUDGET ABORT" in r.output
    r = CliRunner().invoke(main, args + ["fetch"])
    assert r.exit_code == 0, r.output
    r = CliRunner().invoke(main, args + ["audit", "--audit-cells", "10"])
    assert r.exit_code == 0 and "recall 1.0000" in r.output
    r = CliRunner().invoke(main, args + ["export", "--out", str(tmp_path / "e"), "--summary-dir", str(tmp_path / "s")])
    assert r.exit_code == 0 and "(completed" in r.output


def test_smoke_fake(tmp_path):
    r = CliRunner().invoke(main, ["--fake", "--db", str(tmp_path / "s.sqlite"), "smoke", "--out",
                                  str(tmp_path / "e"), "--summary-dir", str(tmp_path / "s")])
    assert r.exit_code == 0, r.output
    assert "Est. cost" in r.output
