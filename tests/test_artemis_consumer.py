import json
import sys
import threading
from types import SimpleNamespace

import pytest

from app import artemis_consumer
from app.schemas.update import parse_artemis_message


class SessionStub:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class ConnectionStub:
    def __init__(self):
        self.acknowledged = []
        self.transport = SimpleNamespace(set_ssl=self._set_ssl)
        self.ssl_settings = None

    def _set_ssl(self, **settings):
        self.ssl_settings = settings

    def ack(self, **kwargs):
        self.acknowledged.append(kwargs)


def frame(body='{"new_pillar": {"pillars": "NO"}}'):
    return SimpleNamespace(
        body=body, headers={"message-id": "message-1", "ack": "ack-1"}
    )


def test_listener_acknowledges_only_after_success(monkeypatch):
    connection = ConnectionStub()
    stopped = threading.Event()
    monkeypatch.setattr(artemis_consumer, "_session", SessionStub)

    def processed(body, message_id, db):
        assert body == frame().body
        assert message_id == "message-1"
        assert not connection.acknowledged

    monkeypatch.setattr(artemis_consumer, "process_artemis_message", processed)
    artemis_consumer.UpdateListener(connection, stopped).on_message(frame())
    assert connection.acknowledged == [{"id": "ack-1", "subscription": "omi-updates"}]
    assert not stopped.is_set()


def test_listener_records_invalid_message_before_ack(monkeypatch):
    connection = ConnectionStub()
    stopped = threading.Event()
    sequence = []
    monkeypatch.setattr(artemis_consumer, "_session", SessionStub)

    def invalid(*args):
        raise ValueError("invalid document")

    def recorded(body, message_id, reason, db):
        sequence.append((message_id, reason))
        assert not connection.acknowledged

    monkeypatch.setattr(artemis_consumer, "process_artemis_message", invalid)
    monkeypatch.setattr(artemis_consumer, "record_invalid_message", recorded)
    artemis_consumer.UpdateListener(connection, stopped).on_message(frame())
    assert sequence == [("message-1", "invalid document")]
    assert len(connection.acknowledged) == 1


def test_listener_leaves_transient_failure_unacknowledged(monkeypatch):
    connection = ConnectionStub()
    stopped = threading.Event()
    monkeypatch.setattr(artemis_consumer, "_session", SessionStub)

    def unavailable(*args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(artemis_consumer, "process_artemis_message", unavailable)
    artemis_consumer.UpdateListener(connection, stopped).on_message(frame())
    assert stopped.is_set()
    assert not connection.acknowledged


def test_listener_reconnects_when_acknowledgement_fails(monkeypatch):
    connection = ConnectionStub()
    stopped = threading.Event()
    monkeypatch.setattr(artemis_consumer, "_session", SessionStub)
    monkeypatch.setattr(artemis_consumer, "process_artemis_message", lambda *_: None)

    def failed_ack(**kwargs):
        raise ConnectionError("broker disconnected")

    connection.ack = failed_ack
    artemis_consumer.UpdateListener(connection, stopped).on_message(frame())
    assert stopped.is_set()


def test_listener_does_not_ack_without_message_id():
    connection = ConnectionStub()
    stopped = threading.Event()
    artemis_consumer.UpdateListener(connection, stopped).on_message(
        SimpleNamespace(body=json.dumps({}), headers={})
    )
    assert stopped.is_set()
    assert not connection.acknowledged


def test_broker_connection_uses_tls_and_heartbeats(monkeypatch):
    created = []

    def connection_factory(**settings):
        created.append(settings)
        return ConnectionStub()

    monkeypatch.setattr(artemis_consumer.stomp, "Connection", connection_factory)
    monkeypatch.setenv("ARTEMIS_CA_CERT", "/certs/ca.pem")
    monkeypatch.setenv("ARTEMIS_CLIENT_CERT", "/certs/client.pem")
    monkeypatch.setenv("ARTEMIS_CLIENT_KEY", "/certs/client.key")
    connection = artemis_consumer._connection("broker", 61614)
    assert created == [
        {"host_and_ports": [("broker", 61614)], "heartbeats": (10000, 10000)}
    ]
    assert connection.ssl_settings["ca_certs"] == "/certs/ca.pem"
    assert connection.ssl_settings["key_file"] == "/certs/client.key"


def test_broker_connection_rejects_incomplete_tls(monkeypatch):
    monkeypatch.setattr(
        artemis_consumer.stomp, "Connection", lambda **_: ConnectionStub()
    )
    monkeypatch.delenv("ARTEMIS_CA_CERT", raising=False)
    monkeypatch.setenv("ARTEMIS_CLIENT_CERT", "/certs/client.pem")
    monkeypatch.setenv("ARTEMIS_CLIENT_KEY", "/certs/client.key")
    with pytest.raises(ValueError, match="ARTEMIS_CA_CERT"):
        artemis_consumer._connection("broker", 61614)


def test_consumer_subscribes_with_individual_acknowledgement(monkeypatch):
    stopped = threading.Event()
    calls = []

    class BrokerConnection:
        def set_listener(self, name, listener):
            calls.append(("listener", name))

        def connect(self, user, password, wait):
            calls.append(("connect", user, wait))

        def subscribe(self, **kwargs):
            calls.append(("subscribe", kwargs))
            stopped.set()

        def is_connected(self):
            return True

        def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr(artemis_consumer, "_connection", lambda *_: BrokerConnection())
    monkeypatch.setenv("ARTEMIS_HOST", "broker")
    monkeypatch.setenv("ARTEMIS_PORT", "61613")
    monkeypatch.setenv("ARTEMIS_USER", "omi")
    artemis_consumer.run_consumer("int.ibechange.update::tue-updates-int", stopped)
    assert (
        "subscribe",
        {
            "destination": "/queue/int.ibechange.update::tue-updates-int",
            "id": "omi-updates",
            "ack": "client-individual",
        },
    ) in calls
    assert calls[-1] == ("disconnect",)


def test_invalid_reason_does_not_include_private_field_values():
    try:
        parse_artemis_message(
            {"new_user": {"email": "private@example.org", "attributes": {}}}
        )
    except ValueError as error:
        reason = artemis_consumer._invalid_reason(error)
    assert "private@example.org" not in reason
    assert "uuid" in reason


def test_invalid_reason_handles_json_and_unknown_event_errors():
    with pytest.raises(json.JSONDecodeError) as captured:
        json.loads("{")
    assert artemis_consumer._invalid_reason(captured.value) == "Invalid JSON"
    assert (
        artemis_consumer._invalid_reason(ValueError("Unknown Artemis event: other"))
        == "Unknown Artemis event"
    )


def test_session_uses_application_engine(monkeypatch):
    from app import dependencies

    sentinel = object()
    monkeypatch.setattr(dependencies, "engine", sentinel)
    monkeypatch.setattr(artemis_consumer, "Session", lambda engine: engine)
    assert artemis_consumer._session() is sentinel


def test_listener_marks_broker_disconnection_and_error():
    stopped = threading.Event()
    listener = artemis_consumer.UpdateListener(ConnectionStub(), stopped)
    listener.on_error(frame())
    assert stopped.is_set()
    stopped.clear()
    listener.on_disconnected()
    assert stopped.is_set()


def test_listener_uses_message_id_when_ack_header_is_absent(monkeypatch):
    connection = ConnectionStub()
    stopped = threading.Event()
    monkeypatch.setattr(artemis_consumer, "_session", SessionStub)
    monkeypatch.setattr(artemis_consumer, "process_artemis_message", lambda *_: True)
    message = frame()
    message.headers.pop("ack")
    artemis_consumer.UpdateListener(connection, stopped).on_message(message)
    assert connection.acknowledged == [
        {"id": "message-1", "subscription": "omi-updates"}
    ]


def test_listener_keeps_invalid_message_unacknowledged_when_recording_fails(
    monkeypatch,
):
    connection = ConnectionStub()
    stopped = threading.Event()
    monkeypatch.setattr(artemis_consumer, "_session", SessionStub)

    def invalid(*args):
        raise ValueError("invalid document")

    def failed_record(*args):
        raise OSError("database unavailable")

    monkeypatch.setattr(artemis_consumer, "process_artemis_message", invalid)
    monkeypatch.setattr(artemis_consumer, "record_invalid_message", failed_record)
    artemis_consumer.UpdateListener(connection, stopped).on_message(frame())
    assert stopped.is_set()
    assert connection.acknowledged == []


def test_broker_connection_without_tls_and_incomplete_client_pair(monkeypatch):
    monkeypatch.setattr(
        artemis_consumer.stomp, "Connection", lambda **_: ConnectionStub()
    )
    for name in ("ARTEMIS_CA_CERT", "ARTEMIS_CLIENT_CERT", "ARTEMIS_CLIENT_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert artemis_consumer._connection("broker", 61613).ssl_settings is None
    monkeypatch.setenv("ARTEMIS_CLIENT_CERT", "/certs/client.pem")
    with pytest.raises(ValueError, match="Both ARTEMIS_CLIENT_CERT"):
        artemis_consumer._connection("broker", 61613)
    monkeypatch.delenv("ARTEMIS_CLIENT_CERT")
    monkeypatch.setenv("ARTEMIS_CA_CERT", "/certs/ca.pem")
    assert artemis_consumer._connection("broker", 61613).ssl_settings == {
        "for_hosts": [("broker", 61613)],
        "ca_certs": "/certs/ca.pem",
        "ssl_version": artemis_consumer.ssl.PROTOCOL_TLS_CLIENT,
    }


def test_consumer_retries_after_connection_failure(monkeypatch):
    stopped = threading.Event()
    attempts = []

    class FailedConnection:
        def set_listener(self, name, listener):
            attempts.append(name)

        def connect(self, user, password, wait):
            raise ConnectionError("broker unavailable")

        def is_connected(self):
            return False

    monkeypatch.setattr(artemis_consumer, "_connection", lambda *_: FailedConnection())
    monkeypatch.setattr(artemis_consumer, "sleep", lambda _: stopped.set())
    artemis_consumer.run_consumer("updates", stopped)
    assert attempts == ["omi-updates"]


def test_consumer_waits_for_disconnect_before_shutdown(monkeypatch):
    calls = []

    class StopAfterOneWait:
        def __init__(self):
            self.calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls > 2

    class WaitOnce:
        def wait(self, timeout):
            assert timeout == 1
            calls.append("wait")
            return False

    class BrokerConnection:
        def set_listener(self, name, listener):
            pass

        def connect(self, user, password, wait):
            pass

        def subscribe(self, **kwargs):
            pass

        def is_connected(self):
            return True

        def disconnect(self):
            calls.append("disconnect")

    monkeypatch.setattr(artemis_consumer.threading, "Event", WaitOnce)
    monkeypatch.setattr(artemis_consumer, "_connection", lambda *_: BrokerConnection())
    artemis_consumer.run_consumer("updates", StopAfterOneWait())
    assert calls == ["wait", "disconnect"]


def test_consumer_main_reads_queue_and_checks_required_setting(monkeypatch):
    calls = []
    monkeypatch.setattr(artemis_consumer, "load_dotenv", lambda: None)
    monkeypatch.setattr(artemis_consumer.signal, "signal", lambda *args: None)
    monkeypatch.setattr(
        artemis_consumer, "run_consumer", lambda queue, stopped: calls.append(queue)
    )
    monkeypatch.setattr(sys, "argv", ["consumer", "--queue", "test-updates"])
    artemis_consumer.main()
    assert calls == ["test-updates"]
    monkeypatch.delenv("ARTEMIS_QUEUE", raising=False)
    monkeypatch.setattr(sys, "argv", ["consumer"])
    with pytest.raises(SystemExit):
        artemis_consumer.main()
