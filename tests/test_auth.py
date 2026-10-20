"""§3 / §4 — auth: leaderboard requires team login; passphrases hashed."""

from dbutil import fetch_teams, seed_hunt


def test_leaderboard_requires_team_login(client):
    response = client.get("/leaderboard", follow_redirects=False)
    assert response.status_code == 403


def test_leaderboard_allowed_after_login(client, session_factory):
    seed_hunt(session_factory)
    assert client.post("/team/login", data={"passphrase": "alpha_secret"}).status_code == 200
    assert client.get("/leaderboard").status_code == 200


def test_correct_passphrase_logs_in(client, session_factory):
    seed_hunt(session_factory)
    response = client.post("/team/login", data={"passphrase": "alpha_secret"})
    assert response.status_code == 200
    assert response.json()["redirect"] == "/hunt/dashboard"


def test_wrong_passphrase_rejected(client, session_factory):
    seed_hunt(session_factory)
    response = client.post("/team/login", data={"passphrase": "wrong"})
    assert response.status_code == 401


def test_stored_passphrase_is_hashed_not_plaintext(client, session_factory):
    seed_hunt(session_factory)
    team = fetch_teams(session_factory)[0]
    assert team.passphrase_hash != "alpha_secret"
    assert "alpha_secret" not in team.passphrase_hash


def test_login_is_rate_limited(client, session_factory):
    seed_hunt(session_factory)
    statuses = [
        client.post("/team/login", data={"passphrase": "wrong"}).status_code
        for _ in range(11)
    ]
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429
