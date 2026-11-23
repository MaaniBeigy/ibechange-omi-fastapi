import json
import logging
import sys
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app import policy_worker
from app.models.policy_decision import PolicyDecision
from app.models.recommendation_plan import RecommendationPlan
from app.routers import recommendation_plans as router
from app.schemas.selected_content import SelectedContentCreate
from app.services.artemis_update_service import process_artemis_message
from app.services.rl_policy import PolicyArtifact
from tests.test_policy_plans import _query, database, epoch


def daily_plan(day, **changes):
    return dict(
        dict(
            plan_id="1842.0",
            mission_id="PM3",
            mission_process_id=864536,
            delivery_date=str(day),
            created_at=f"{day}T01:05:12",
            task_scheduled_for=f"{day}T18:00:00",
            contents=[
                {"id": "PRc12", "type": "recommendation", "push_text_id": "PT07"},
                {"id": "PRh1", "type": "hour_reminder"},
                {
                    "id": "PRd5",
                    "type": "day_reminder",
                    "latest_time": f"{day}T19:00:00",
                },
            ],
        ),
        **changes,
    )


def test_daily_plan_worker_retry_and_replacement(database, epoch, monkeypatch):
    client, engine = database
    uid = str(uuid4())

    def post(payload):
        return client.post("/selected_contents/", json={uid: payload})

    day = epoch.date()
    original = daily_plan(day)
    assert post(original).status_code == 200
    assert (
        post(
            daily_plan(day + timedelta(days=1), plan_id="1843.0", contents=[])
        ).status_code
        == 200
    )
    monkeypatch.setattr(
        PolicyArtifact, "greedy_action", lambda self, values, mask: int(mask[1])
    )
    assert policy_worker.run_epoch(engine, epoch) == 1
    item = _query(client, epoch, "notification_plans").json()[uid]["contents"][0]
    assert (item["id"], item["push_text_id"], item["mission_process_id"]) == (
        "PRc12",
        "PT07",
        864536,
    )
    assert (
        _query(client, epoch).json()["recommendation_plans"][0]["plans"][0]["plan_id"]
        == "1842.0"
    )
    with Session(engine) as db:
        event = dict(
            user_uuid=uid,
            process_id=42,
            mission_process_id=864536,
            created_at=epoch.isoformat(),
            properties=dict(
                mission_id="PM3",
                content_type="recommendation",
                content_id="PRc12",
                push_text_id="PT07",
            ),
        )
        process_artemis_message({"notification_sent": event}, "confirmed", db)
    for minutes in (390, 405, 450, 465):
        assert policy_worker.run_epoch(engine, epoch + timedelta(minutes=minutes)) == 1
    with Session(engine) as db:
        rows = db.exec(
            select(RecommendationPlan).order_by(RecommendationPlan.scheduled_for)
        ).all()
        assert [r.content_type for r in rows] == [
            "recommendation",
            "hour_reminder",
            "day_reminder",
        ]
        assert [r.scheduled_for.hour for r in rows] == [8, 14, 15]
        assert [r.scheduled_for.minute for r in rows] == [0, 45, 45]
        assert rows[-1].scheduled_for < rows[-1].deadline
    cancel = daily_plan(
        day, plan_id="1842.1", created_at=f"{day}T18:05:00", contents=[]
    )
    assert post(cancel).status_code == post(cancel).status_code == 200
    assert post(original).status_code == 200
    assert post(dict(original, plan_id="stale")).status_code == 200
    assert post(dict(cancel, plan_id="collision")).status_code == 409
    assert post(dict(original, contents=[])).status_code == 409
    assert policy_worker.run_epoch(engine, epoch + timedelta(days=2)) == 1
    with Session(engine) as db:
        active = db.exec(
            select(RecommendationPlan).where(RecommendationPlan.is_deleted == False)
        ).all()
        assert [row.content_type for row in active] == ["recommendation"]
        decisions = db.exec(select(PolicyDecision)).all()
        assert len(decisions) == 5
        for decision in decisions:
            assert len(decision.q_values) == 2
            assert decision.propensity == decision.action_probabilities[decision.action]
    listed = client.get("/selected_contents/").json()[uid]
    assert {row["plan_id"] for row in listed} == {"1842.0", "1842.1", "1843.0"}


def test_strict_latest_time_and_catalog_contract(database, epoch):
    client, _ = database
    day = epoch.date()
    for path, body in [
        (
            "resources",
            {
                "NRs9": {
                    "missions": ["NM79"],
                    "com_b": "capability",
                    "intervention_types": ["education"],
                }
            },
        ),
        ("recommendations", {"NRc1": {"missions": ["NM79"]}}),
        ("missions", [{"mission_id": "NM79", "weekly_frequency": 7}]),
    ]:
        assert client.post(f"/{path}/", json=body).status_code == 200
    uid = str(uuid4())
    payload = daily_plan(
        day,
        task_scheduled_for=None,
        contents=[{"id": "NRs9", "type": "resource", "latest_time": epoch.isoformat()}],
    )
    assert client.post("/selected_contents/", json={uid: payload}).status_code == 200
    assert _query(client, epoch).status_code == 409


@pytest.mark.parametrize(
    "change",
    [
        {"mission_process_id": None},
        {"delivery_date": None},
        {"task_scheduled_for": None},
        {"contents": [{"id": "R", "type": "resource", "mission_id": "wrong"}]},
        {"task_scheduled_for": "2026-10-25T02:30:00"},
    ],
)
def test_daily_contract_rejects_inconsistent_headers(change):
    with pytest.raises(ValueError):
        SelectedContentCreate.model_validate(daily_plan("2026-10-25", **change))


def test_revised_artemis_events_preserve_feedback_and_mission_status(database):
    from app.models.event import Event
    from app.models.user import MissionContent

    _, engine = database
    uid = str(uuid4())
    payload = dict(
        user_uuid=uid,
        process_id=1,
        mission_process_id=42,
        created_at="2026-11-20T15:00:00Z",
    )
    with Session(engine) as db:
        process_artemis_message(
            {"mission_selected": dict(payload, mission_id="PM3")}, "selected", db
        )
        for event in (
            "notification_rated",
            "notification_saved",
            "notification_unsaved",
            "resource_link_opened",
        ):
            props = dict(
                content_type="resource",
                content_id="R",
                mission_id="PM3",
                push_text_id="PT07",
                route="saved",
            )
            if event == "notification_rated":
                props["rating"] = 2
            event_payload = {event: dict(payload, properties=props)}
            assert process_artemis_message(event_payload, event, db)
            assert not process_artemis_message(event_payload, event, db)
            stored = db.exec(select(Event).where(Event.event_name == event)).one()
            assert stored.characteristics == dict(props, mission_process_id=42)
        process_artemis_message(
            {
                "task_step_done": dict(
                    payload,
                    properties=dict(
                        mission_id="PM3", total_steps=3, finished_at="2026-11-20"
                    ),
                )
            },
            "done",
            db,
        )
        for name, reason in (
            ("mission_changed", "changed"),
            ("mission_not_achieved", "expired"),
        ):
            process_artemis_message(
                {name: dict(payload, mission_id="PM3", end_reason=reason)}, name, db
            )
            assert (
                db.exec(select(Event).where(Event.event_name == name))
                .one()
                .characteristics["end_reason"]
                == reason
            )
        assert db.exec(select(MissionContent)).one().mission_end_time is not None


def test_cronicle_can_load_the_delivery_script_outside_the_omi_checkout(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "scripts/omi_module_live_artemis.py"
    script = shutil.copy(source, tmp_path)
    result = subprocess.run(
        [sys.executable, "-I", script, "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--delivery" in result.stdout


def test_delivery_script_retries_same_committed_send(monkeypatch, caplog):
    from datetime import datetime, timezone
    from unittest.mock import Mock

    from scripts import omi_module_live_artemis as live

    now = datetime(2026, 11, 20, 14, 5, tzinfo=timezone.utc)
    item = {"id": "R", "send_timestamp": "2026-11-20T14:15:00", "type": "resource"}
    uid = str(uuid4())

    def read(path, params):
        assert params["start_time"] <= "2026-11-20T14:15:00Z" <= params["end_time"]
        if path == "/notification_plans":
            return {uid: {"contents": [item]}}
        return {"recommendation_plans": [{"user_id": uid}]}

    monkeypatch.setattr(live, "_omi_get", read)
    producer = Mock()
    monkeypatch.setattr(
        "app.services.artemis_producer_service.get_artemis_producer", lambda: producer
    )
    for delay in (0, 1):
        live.set_notification_plans_artemis(now=now + timedelta(minutes=delay))
    first, second = producer.send_message.call_args_list
    assert first.kwargs == second.kwargs and first.kwargs["duplicate_id"]
    assert first.args[1]["notification_plan"]["notifications"] == [item]
    monkeypatch.setattr(live, "EUT_BASE", "http://vu.invalid")
    post = Mock()
    post.return_value.status_code = 200
    monkeypatch.setattr(live.requests, "post", post)
    with caplog.at_level(logging.INFO):
        live.deliver_http(now)
    assert [entry.args[0].split("/")[-1] for entry in post.call_args_list] == [
        "recommendation_plans",
        "recommendations",
    ]
    url = "http://vu.invalid/internal/recommendations"
    post.assert_called_with(
        url,
        json={uid: {"contents": [dict(item, send_timestamp="2026-11-20T14:15:00Z")]}},
        headers=live.HEADERS_EUT_POST,
        timeout=30,
        allow_redirects=False,
    )
    assert f"EUT POST {url} returned HTTP 200 (notifications=1)" in caplog.messages
    assert uid not in caplog.text


@pytest.mark.parametrize("status", [302, 503])
def test_eut_unsuccessful_post_and_empty_window(monkeypatch, caplog, status):
    from datetime import datetime, timezone
    from unittest.mock import Mock

    from scripts import omi_module_live_artemis as live

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(live, "EUT_BASE", "https://vu.invalid")
    monkeypatch.setattr(
        live, "fetch_recommendation_plans", lambda now: {"recommendation_plans": []}
    )
    monkeypatch.setattr(live, "_omi_get", lambda *args: {})
    post = Mock()
    post.return_value.status_code = status
    monkeypatch.setattr(live.requests, "post", post)
    caplog.set_level(logging.INFO)
    live.deliver_http(now)
    post.assert_not_called()
    assert "No notification plans" in caplog.text

    timestamp = "2026-10-21T14:47:22.063Z"
    monkeypatch.setattr(
        live,
        "_omi_get",
        lambda *args: {"user": {"contents": [{"send_timestamp": timestamp}]}},
    )
    with pytest.raises(live.requests.HTTPError, match=f"HTTP {status}"):
        live.deliver_http(now)
    assert (
        post.call_args.kwargs["json"]["user"]["contents"][0]["send_timestamp"]
        == timestamp
    )
    assert not any(message.startswith("EUT POST") for message in caplog.messages)


@pytest.mark.parametrize(
    "options, expected", [([], "vu"), (["--delivery", "artemis"], "artemis")]
)
def test_delivery_cli_routes_to_one_destination(monkeypatch, options, expected):
    from unittest.mock import Mock

    from scripts import omi_module_live_artemis as live

    http, broker = Mock(), Mock()
    monkeypatch.setattr(
        sys, "argv", ["omi", "--dag_id", "3", "--delivery-only", *options]
    )
    monkeypatch.setattr(live, "deliver_http", http)
    monkeypatch.setattr(live, "set_notification_plans_artemis", broker)
    live.main()
    assert http.call_count == int(expected == "vu")
    assert broker.call_count == int(expected == "artemis")


def test_revise_precommitted_epoch_replaces_the_successor(database, epoch, monkeypatch):
    from datetime import datetime

    from app.models.policy_learning import PolicyTransition
    from app.routers import selected_contents
    from app.services import recommendation_plan_service

    class BeforeSlot(datetime):
        @classmethod
        def now(cls, tz=None):
            return epoch + timedelta(minutes=7)

    monkeypatch.setattr(selected_contents, "datetime", BeforeSlot)
    monkeypatch.setattr(recommendation_plan_service, "datetime", BeforeSlot)
    client, engine = database
    uid = str(uuid4())
    day = epoch.date()
    first = daily_plan(day, contents=[{"id": "A", "type": "resource"}])
    assert client.post("/selected_contents/", json={uid: first}).status_code == 200
    monkeypatch.setattr(
        PolicyArtifact, "greedy_action", lambda self, values, mask: int(not mask[0])
    )
    assert _query(client, epoch).status_code == 200
    slot = epoch + timedelta(minutes=15)
    monkeypatch.setattr(router, "_next_epoch", lambda now: slot)
    monkeypatch.setattr(
        PolicyArtifact, "greedy_action", lambda self, values, mask: int(mask[1])
    )
    assert (
        _query(client, slot).json()["recommendation_plans"][0]["plans"][0]["content_id"]
        == "A"
    )
    revised = daily_plan(
        day,
        plan_id="1842.1",
        created_at=f"{day}T10:05",
        contents=[{"id": "B", "type": "resource"}],
    )
    assert client.post("/selected_contents/", json={uid: revised}).status_code == 200
    response = _query(client, slot, "notification_plans")
    assert [(r["plan_id"], r["id"]) for r in response.json()[uid]["contents"]] == [
        ("1842.1", "B")
    ]
    assert _query(client, slot, "notification_plans").json() == response.json()
    with Session(engine) as db:
        choices = db.exec(
            select(PolicyDecision).where(PolicyDecision.decided_at == slot)
        ).all()
        assert len(choices) == 2
        actual = next(row for row in choices if row.superseded_by is None)
        old = next(row for row in choices if row.superseded_by is not None)
        assert old.superseded_by == actual.plan_id
        link = db.exec(select(PolicyTransition)).one()
        assert link.next_decision_id == actual.id and link.status == "pending"


def test_dated_plan_reports_class_overflow(database, epoch):
    client, engine = database
    uid = str(uuid4())
    payload = daily_plan(epoch.date(), contents=[{"id": "R", "type": "resource"}] * 3)
    assert client.post("/selected_contents/", json={uid: payload}).status_code == 200
    assert policy_worker.run_epoch(engine, epoch) == 0
    assert "class limit" in _query(client, epoch).json()["detail"]


@pytest.mark.parametrize(
    "fields",
    [
        {"created_at": "2026-11-20T12:00Z", "contents": []},
        {"contents": [{"id": "A", "type": "resource"}]},
        {"contents": [{"id": "D", "type": "day_reminder", "mission_id": "M"}]},
    ],
)
def test_legacy_request_cannot_drop_daily_constraints(fields):
    with pytest.raises(ValueError):
        SelectedContentCreate.model_validate(dict(plan_id=uuid4(), **fields))
