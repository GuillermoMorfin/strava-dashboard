from strava_dashboard import cli, db


def test_demo_command_writes_database(tmp_path, capsys):
    path = tmp_path / "demo.duckdb"
    assert cli.main(["demo", "--db", str(path)]) == 0
    con = db.connect(path, read_only=True)
    assert len(db.load_activities(con)) > 50
    assert len(db.load_hr_zones(con)) == 5
    assert "strava-dash dashboard" in capsys.readouterr().out


def test_sync_without_credentials_fails_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: cls()))
    assert cli.main(["sync"]) == 1
    assert "STRAVA_CLIENT_ID" in capsys.readouterr().err


def test_sync_before_auth_fails_cleanly(monkeypatch, tmp_path, capsys):
    settings = cli.Settings(
        client_id="1",
        client_secret="s",
        token_path=tmp_path / "missing.json",
        db_path=tmp_path / "x.duckdb",
    )
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: settings))
    assert cli.main(["sync"]) == 1
    assert "strava-dash auth" in capsys.readouterr().err
