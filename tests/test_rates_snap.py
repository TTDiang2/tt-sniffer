import json
from datetime import date

from app.collectors import rates_snap

PAYLOAD = {
    "tradeDate": "10/20/2026",
    "reportType": "Final",
    "settlements": [
        {"month": "OCT 26", "settle": "96.1150"},
        {"month": "NOV 26", "settle": "96.0800"},
        {"month": "DEC 26", "settle": "95.9250"},
    ],
}


def test_parse_settlements_front_next():
    day, front, nxt = rates_snap.parse_settlements(PAYLOAD)
    assert day == date(2026, 10, 20)
    assert front == 96.1150
    assert nxt == 96.0800


def test_parse_settlements_skips_blank_rows():
    payload = {
        "tradeDate": "10/20/2026",
        "settlements": [
            {"month": "OCT 26", "settle": "-"},
            {"month": "NOV 26", "settle": "96.0800"},
            {"month": "DEC 26", "settle": "95.9250"},
        ],
    }
    day, front, nxt = rates_snap.parse_settlements(payload)
    assert day == date(2026, 10, 20)
    assert (front, nxt) == (96.08, 95.925)


def test_snapshot_appends_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setenv("TTSNIFFER_HOME", str(tmp_path))
    monkeypatch.setattr(rates_snap, "fetch_settlements", lambda _: PAYLOAD)

    rec = rates_snap.snapshot(date(2026, 10, 20))
    assert rec["date"] == "2026-10-20"
    assert rec["settle_front"] == 96.115
    assert rec["settle_next"] == 96.08
    assert rec["implied_front"] == 3.885
    assert set(rec) == {"date", "settle_front", "settle_next",
                        "implied_front", "ts_snap"}

    path = tmp_path / "rates_snap.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    rates_snap.snapshot(date(2026, 10, 20))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    line = json.loads(path.read_text(encoding="utf-8").strip())
    assert line["date"] == "2026-10-20"


def test_append_snapshot_keeps_consecutive_days(tmp_path, monkeypatch):
    monkeypatch.setenv("TTSNIFFER_HOME", str(tmp_path))
    monkeypatch.setattr(rates_snap, "fetch_settlements", lambda _: PAYLOAD)
    path = tmp_path / "rates_snap.jsonl"
    rates_snap.append_snapshot({"date": "2026-10-19", "settle_front": 96.1,
                                "settle_next": 96.0, "implied_front": 3.9,
                                "ts_snap": "2026-10-20T05:10:00+08:00"}, path)
    rates_snap.append_snapshot({"date": "2026-10-20", "settle_front": 96.115,
                                "settle_next": 96.08, "implied_front": 3.885,
                                "ts_snap": "2026-10-21T05:10:00+08:00"}, path)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
