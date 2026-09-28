"""Exercise timestamp and import helpers used by both update paths."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from app.models.user import User
from app.utils import helpers


def test_type_coercion_and_short_variable_rows():
    assert helpers._coerce_by_type("value", None) == "value"
    assert helpers._coerce_by_type("7", "integer") == 7
    assert helpers._coerce_by_type("1.5", "double") == 1.5
    assert helpers._coerce_by_type("bad", "int") is None
    assert helpers._coerce_by_type(object(), "float") is None
    assert helpers._combine_vars_row(
        [SimpleNamespace(name="age", type="int"), SimpleNamespace(name="unused")],
        ["42"],
    ) == {"age": 42}


def test_helper_time_conversions_cover_naive_and_offset_inputs():
    offset = timezone(timedelta(hours=2))
    aware = datetime(2026, 9, 28, 9, 30, tzinfo=offset)
    naive = datetime(2026, 9, 28, 7, 30)
    assert helpers._to_naive_utc(aware) == naive
    assert helpers._to_naive_utc(naive) == naive
    assert helpers._aware_utc(naive) == aware.astimezone(timezone.utc)
    assert helpers._iso_to_utc_aware("2026-09-28T09:30:00+02:00") == aware.astimezone(
        timezone.utc
    )
    assert helpers._iso_to_utc_aware("2026-09-28T07:30:00") == aware.astimezone(
        timezone.utc
    )
    assert helpers._minutes_since_midnight(aware) == 570
    assert helpers._to_utc(None) is None
    assert helpers._to_utc(naive) == aware.astimezone(timezone.utc)
    assert helpers._parse_iso_z("2026-09-28T09:30:00+02:00") == aware.astimezone(
        timezone.utc
    )
    assert helpers._parse_iso_z("2026-09-28T07:30:00") == aware.astimezone(timezone.utc)
    assert helpers._iso_utc(None) is None
    assert helpers._iso_utc(naive) == "2026-09-28T07:30:00Z"
    assert helpers._iso_utc(aware) == "2026-09-28T07:30:00Z"


def test_invalid_uuid_is_ignored():
    assert helpers._parse_uuid_or_none("bad") is None
    user_id = uuid4()
    assert helpers._parse_uuid_or_none(str(user_id)) == user_id


def test_ensure_user_accepts_existing_query_or_creates_new_one():
    user_id = uuid4()
    user = User(id=user_id)

    class FakeDatabase:
        def __init__(self, direct=None, queried=None):
            self.direct = direct
            self.queried = queried
            self.added = []
            self.flushed = False

        def get(self, model, identity):
            return self.direct

        def exec(self, statement):
            return SimpleNamespace(first=lambda: self.queried)

        def add(self, row):
            self.added.append(row)

        def flush(self):
            self.flushed = True

    direct = FakeDatabase(direct=user)
    assert helpers._ensure_user(direct, user_id) is user
    assert direct.added == []
    queried = FakeDatabase(queried=user)
    assert helpers._ensure_user(queried, user_id) is user
    assert queried.added == []
    missing = FakeDatabase()
    created = helpers._ensure_user(missing, user_id)
    assert created.id == user_id
    assert missing.added == [created]
    assert missing.flushed
