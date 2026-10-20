"""Small async helpers for the route/integration tests."""

import asyncio

from sqlalchemy.future import select

from app.models import Hunt, HuntItem, Submission, Team
from app.security import hash_passphrase


def run(coro):
    return asyncio.run(coro)


def seed_hunt(
    session_factory,
    *,
    target=None,
    mode="bonus",
    proximity_points=0,
    radius=100.0,
    near_multiplier=2.0,
    points=100,
    bonus_points=0,
    hunt_name="Test Hunt",
    item_name="Test Item",
    with_team=True,
    team_name="Team Alpha",
    passphrase="alpha_secret",
):
    """Create one hunt with one item (and optionally one team). Returns ids."""

    async def _seed():
        async with session_factory() as session:
            hunt = Hunt(name=hunt_name, theme_json="{}")
            session.add(hunt)
            await session.commit()

            item = HuntItem(
                hunt_id=hunt.id,
                name=item_name,
                description="A test item",
                points=points,
                bonus_points=bonus_points,
                required_properties='["shows a thing"]',
                bonus_properties="[]",
                target_lat=target[0] if target else None,
                target_lon=target[1] if target else None,
                radius_meters=radius,
                near_multiplier=near_multiplier,
                proximity_points=proximity_points,
                proximity_mode=mode,
            )
            session.add(item)

            team = None
            if with_team:
                team = Team(name=team_name, passphrase_hash=hash_passphrase(passphrase))
                session.add(team)
            await session.commit()
            return {
                "hunt_id": hunt.id,
                "item_id": item.id,
                "team_id": team.id if team else None,
            }

    return run(_seed())


def fetch_submissions(session_factory):
    """Return all submissions as plain dicts (detached from any session)."""

    async def _fetch():
        async with session_factory() as session:
            rows = (await session.execute(select(Submission))).scalars().all()
            return [
                {
                    "id": s.id,
                    "item_id": s.item_id,
                    "team_id": s.team_id,
                    "verified": s.verified,
                    "points_awarded": s.points_awarded,
                    "captured_at": s.captured_at,
                    "gps_lat": s.gps_lat,
                    "gps_lon": s.gps_lon,
                    "distance_m": s.distance_m,
                    "location_status": s.location_status,
                    "proximity_points_awarded": s.proximity_points_awarded,
                    "live_lat": s.live_lat,
                    "live_lon": s.live_lon,
                    "attempt_number": s.attempt_number,
                }
                for s in rows
            ]

    return run(_fetch())


def fetch_teams(session_factory):
    async def _fetch():
        async with session_factory() as session:
            return (await session.execute(select(Team))).scalars().all()

    return run(_fetch())


def point_north(lat, lon, meters):
    """A point ``meters`` due north of ``(lat, lon)`` (exact under haversine)."""
    import math

    return (lat + math.degrees(meters / 6371000.0), lon)
