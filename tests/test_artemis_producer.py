"""Comprehensive unit tests for Artemis producer functionality - Notification Plan only."""

import json
import logging
import ssl
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import stomp

from app.schemas.artemis_producer import (
    ArtemisNotificationPlan,
    create_notification_plan_message,
)
from app.services.artemis_producer_service import (
    ArtemisProducer,
    get_artemis_producer,
    send_notification_plan_to_artemis,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_artemis_connection():
    """Create a mock STOMP connection."""
    mock_conn = MagicMock(spec=stomp.Connection)
    mock_conn.is_connected = MagicMock(return_value=False)
    return mock_conn


@pytest.fixture
def mock_stomp_connection_class(mock_artemis_connection):
    """Mock the stomp.Connection class."""
    with patch("stomp.Connection", return_value=mock_artemis_connection):
        yield mock_artemis_connection


@pytest.fixture
def sample_notification_plan():
    """Create a sample notification plan."""
    user_id = uuid4()
    return ArtemisNotificationPlan(
        user_id=user_id,
        notifications=[
            {"id": "notif_1", "type": "recommendation", "mission": "mission_1"},
            {"id": "notif_2", "type": "reminder", "mission": "mission_2"},
        ],
        start_time=datetime(2025, 9, 29, 10, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2025, 9, 29, 11, 0, 0, tzinfo=timezone.utc),
    )


# =============================================================================
# Schema Tests
# =============================================================================

class TestArtemisNotificationPlan:
    """Tests for ArtemisNotificationPlan schema."""

    def test_valid_notification_plan(self, sample_notification_plan):
        """Test creating a valid notification plan."""
        assert len(sample_notification_plan.notifications) == 2
        assert sample_notification_plan.user_id is not None
        assert sample_notification_plan.start_time is not None
        assert sample_notification_plan.end_time is not None

    def test_notification_plan_empty_notifications(self):
        """Test that empty notifications list is allowed."""
        user_id = uuid4()
        # Empty notifications should be allowed
        plan = ArtemisNotificationPlan(
            user_id=user_id,
            notifications=[],
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        assert len(plan.notifications) == 0

    def test_notification_plan_time_order(self):
        """Test that start_time can be before or after end_time (no validation)."""
        user_id = uuid4()
        # Currently no validation on time order for notification plans
        plan = ArtemisNotificationPlan(
            user_id=user_id,
            notifications=[{"id": "1", "type": "test", "mission": "m1"}],
            start_time=datetime(2025, 9, 29, 10, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2025, 9, 30, 10, 0, 0, tzinfo=timezone.utc),
        )
        assert plan.start_time < plan.end_time


class TestMessageCreators:
    """Tests for message creation functions."""

    def test_create_notification_plan_message(self, sample_notification_plan):
        """Test creating a notification plan message."""
        user_id = sample_notification_plan.user_id
        notifications = sample_notification_plan.notifications
        start_time = sample_notification_plan.start_time
        end_time = sample_notification_plan.end_time
        
        message = create_notification_plan_message(
            user_id, notifications, start_time, end_time
        )
        
        assert "notification_plan" in message
        assert message["notification_plan"]["user_id"] == str(user_id)
        assert len(message["notification_plan"]["notifications"]) == 2
        assert "start_time" in message["notification_plan"]
        assert "end_time" in message["notification_plan"]


# =============================================================================
# ArtemisProducer Tests
# =============================================================================

class TestArtemisProducerInit:
    """Tests for ArtemisProducer initialization."""

    def test_init_with_defaults(self, mock_stomp_connection_class):
        """Test producer initialization with default values."""
        with patch.dict("os.environ", {
            "ARTEMIS_HOST": "localhost",
            "ARTEMIS_PORT": "61613",
            "ARTEMIS_USER": "admin",
            "ARTEMIS_PASSWORD": "password",
        }, clear=True):
            producer = ArtemisProducer()
            assert producer.host == "localhost"
            assert producer.port == 61613
            assert producer.user == "admin"
            assert producer.password == "password"

    def test_init_with_custom_values(self):
        """Test producer initialization with custom values."""
        producer = ArtemisProducer(
            host="custom_host",
            port=61614,
            user="custom_user",
            password="custom_pass",
        )
        assert producer.host == "custom_host"
        assert producer.port == 61614
        assert producer.user == "custom_user"
        assert producer.password == "custom_pass"

    def test_init_with_ssl_certs(self):
        """Test producer initialization with SSL certificates."""
        producer = ArtemisProducer(
            ca_cert="/path/to/ca.crt",
            client_cert="/path/to/client.crt",
            client_key="/path/to/client.key",
        )
        assert producer.ca_cert == "/path/to/ca.crt"
        assert producer.client_cert == "/path/to/client.crt"
        assert producer.client_key == "/path/to/client.key"

    def test_init_missing_client_key_raises(self):
        """Test that missing client_key with client_cert raises error."""
        # The producer checks: if bool(client_cert) != bool(client_key)
        # So if client_cert is set but client_key is not, it should raise
        with pytest.raises(ValueError, match="Both ARTEMIS_CLIENT_CERT and ARTEMIS_CLIENT_KEY are required"):
            ArtemisProducer(
                ca_cert="/path/to/ca.crt",
                client_cert="/path/to/client.crt",
                client_key=None,
            )

    def test_init_client_cert_without_ca_cert_raises(self):
        """Test that client_cert without ca_cert raises error."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="ARTEMIS_CA_CERT is required for client certificates"):
                ArtemisProducer(
                    client_cert="/path/to/client.crt",
                    client_key="/path/to/client.key",
                )


class TestArtemisProducerConnection:
    """Tests for ArtemisProducer connection management."""

    def test_connect_success(self, mock_artemis_connection, mock_stomp_connection_class):
        """Test successful connection to Artemis."""
        # Setup transport mock
        mock_transport = MagicMock()
        mock_artemis_connection.transport = mock_transport
        mock_artemis_connection.is_connected = MagicMock(return_value=False)
        mock_artemis_connection.connect = MagicMock()
        
        producer = ArtemisProducer()
        producer._connection = mock_artemis_connection
        
        with patch.object(producer, "_create_connection", return_value=mock_artemis_connection):
            producer.connect()
        
        mock_artemis_connection.connect.assert_called_once()

    def test_connect_already_connected(self, mock_artemis_connection):
        """Test that connection is not re-established if already connected."""
        mock_artemis_connection.is_connected = MagicMock(return_value=True)
        mock_artemis_connection.connect = MagicMock()
        
        producer = ArtemisProducer()
        producer._connection = mock_artemis_connection
        
        producer.connect()
        
        mock_artemis_connection.connect.assert_not_called()

    def test_disconnect_success(self, mock_artemis_connection):
        """Test successful disconnection from Artemis."""
        mock_artemis_connection.is_connected = MagicMock(return_value=True)
        mock_artemis_connection.disconnect = MagicMock()
        
        producer = ArtemisProducer()
        producer._connection = mock_artemis_connection
        
        producer.disconnect()
        
        mock_artemis_connection.disconnect.assert_called_once()

    def test_disconnect_not_connected(self, mock_artemis_connection):
        """Test that disconnect does nothing if not connected."""
        mock_artemis_connection.is_connected = MagicMock(return_value=False)
        mock_artemis_connection.disconnect = MagicMock()
        
        producer = ArtemisProducer()
        producer._connection = mock_artemis_connection
        
        producer.disconnect()
        
        mock_artemis_connection.disconnect.assert_not_called()

    def test_context_manager_methods_exist(self):
        """Test that context manager methods exist."""
        producer = ArtemisProducer()
        assert hasattr(producer, "__enter__")
        assert hasattr(producer, "__exit__")
        assert callable(producer.__enter__)
        assert callable(producer.__exit__)


class TestArtemisProducerSend:
    """Tests for ArtemisProducer message sending."""

    def test_send_message_success(self, mock_artemis_connection):
        """Test successful message sending."""
        mock_artemis_connection.is_connected = MagicMock(return_value=True)
        mock_artemis_connection.send = MagicMock()
        
        producer = ArtemisProducer()
        producer._connection = mock_artemis_connection
        
        message = {"test": "message"}
        msg_id = producer.send_message("/queue/test", message)
        
        assert msg_id.startswith("omi-")
        mock_artemis_connection.send.assert_called_once()
        call_args = mock_artemis_connection.send.call_args
        assert call_args[1]["destination"] == "/queue/test"
        assert json.loads(call_args[1]["body"]) == message

    def test_send_message_connects_if_not_connected(self, mock_artemis_connection, mock_stomp_connection_class):
        """Test that send_message connects if not already connected."""
        # Setup transport mock
        mock_transport = MagicMock()
        mock_artemis_connection.transport = mock_transport
        mock_artemis_connection.is_connected = MagicMock(return_value=False)
        mock_artemis_connection.connect = MagicMock()
        mock_artemis_connection.send = MagicMock()
        
        producer = ArtemisProducer()
        producer._connection = mock_artemis_connection
        
        with patch.object(producer, "_create_connection", return_value=mock_artemis_connection):
            producer.send_message("/queue/test", {"test": "message"})
        
        mock_artemis_connection.connect.assert_called_once()
        mock_artemis_connection.send.assert_called_once()

    def test_send_message_with_persistent_header(self, mock_artemis_connection):
        """Test that persistent header is included."""
        mock_artemis_connection.is_connected = MagicMock(return_value=True)
        mock_artemis_connection.send = MagicMock()
        
        producer = ArtemisProducer()
        producer._connection = mock_artemis_connection
        
        producer.send_message("/queue/test", {"test": "message"}, persistent=True)
        
        call_args = mock_artemis_connection.send.call_args
        # The persistent header is set as string "True" (Python bool)
        assert call_args[1]["headers"]["persistent"] == "True"

    def test_send_message_with_message_id_header(self, mock_artemis_connection):
        """Test that message-id header is included."""
        mock_artemis_connection.is_connected = MagicMock(return_value=True)
        mock_artemis_connection.send = MagicMock()
        
        producer = ArtemisProducer()
        producer._connection = mock_artemis_connection
        
        msg_id = producer.send_message("/queue/test", {"test": "message"})
        
        call_args = mock_artemis_connection.send.call_args
        assert call_args[1]["headers"]["message-id"] == msg_id

    def test_send_notification_plan(self, mock_artemis_connection):
        """Test sending a notification plan."""
        mock_artemis_connection.is_connected = MagicMock(return_value=True)
        mock_artemis_connection.send = MagicMock()
        
        producer = ArtemisProducer()
        producer._connection = mock_artemis_connection
        
        user_id = uuid4()
        notifications = [
            {"id": "1", "type": "recommendation", "mission": "m1"},
            {"id": "2", "type": "reminder", "mission": "m2"},
        ]
        start = datetime.now(timezone.utc)
        end = start + timedelta(hours=1)
        
        msg_id = producer.send_notification_plan(
            user_id=user_id,
            notifications=notifications,
            start_time=start,
            end_time=end,
        )
        
        assert msg_id.startswith("omi-")
        mock_artemis_connection.send.assert_called_once()


class TestGetArtemisProducer:
    """Tests for get_artemis_producer factory function."""

    def test_returns_producer(self):
        """Test that factory returns an ArtemisProducer instance."""
        producer = get_artemis_producer()
        assert isinstance(producer, ArtemisProducer)


class TestSendNotificationPlanToArtemis:
    """Tests for send_notification_plan_to_artemis convenience function."""

    @patch("app.services.artemis_producer_service.get_artemis_producer")
    def test_send_plan_success(self, mock_get_producer, mock_artemis_connection):
        """Test successful sending via convenience function."""
        mock_producer = MagicMock()
        mock_producer.connect = MagicMock()
        mock_producer.disconnect = MagicMock()
        mock_producer.send_message = MagicMock(return_value="msg-123")
        mock_get_producer.return_value = mock_producer
        
        user_id = uuid4()
        notifications = [
            {
                "id": "notif_1",
                "type": "recommendation",
                "mission": "mission_1",
            }
        ]
        start = datetime.now(timezone.utc)
        end = start + timedelta(hours=1)
        
        msg_id = send_notification_plan_to_artemis(
            user_id=user_id,
            notifications=notifications,
            start_time=start,
            end_time=end,
        )
        
        assert msg_id == "msg-123"
        mock_producer.connect.assert_called_once()
        mock_producer.send_message.assert_called_once()
        mock_producer.disconnect.assert_called_once()

    @patch("app.services.artemis_producer_service.get_artemis_producer")
    def test_send_plan_handles_exception(self, mock_get_producer, mock_artemis_connection):
        """Test that exception is propagated and disconnect is called."""
        mock_producer = MagicMock()
        mock_producer.connect = MagicMock()
        mock_producer.disconnect = MagicMock()
        mock_producer.send_message = MagicMock(side_effect=Exception("Connection failed"))
        mock_get_producer.return_value = mock_producer
        
        user_id = uuid4()
        notifications = []
        start = datetime.now(timezone.utc)
        end = start + timedelta(hours=1)
        
        with pytest.raises(Exception, match="Connection failed"):
            send_notification_plan_to_artemis(
                user_id=user_id,
                notifications=notifications,
                start_time=start,
                end_time=end,
            )
        
        mock_producer.disconnect.assert_called_once()


# =============================================================================
# Serialization Tests
# =============================================================================

class TestMessageSerialization:
    """Tests for message serialization and deserialization."""

    def test_notification_plan_serialization(self, sample_notification_plan):
        """Test that notification plan can be serialized to JSON."""
        json_str = sample_notification_plan.model_dump_json()
        assert isinstance(json_str, str)
        
        # Verify it can be parsed back
        parsed = json.loads(json_str)
        assert parsed["user_id"] == str(sample_notification_plan.user_id)
        assert len(parsed["notifications"]) == 2

    def test_message_contains_correct_keys(self, sample_notification_plan):
        """Test that message contains all required keys."""
        message = create_notification_plan_message(
            sample_notification_plan.user_id,
            sample_notification_plan.notifications,
            sample_notification_plan.start_time,
            sample_notification_plan.end_time,
        )
        
        assert "notification_plan" in message
        plan = message["notification_plan"]
        assert "user_id" in plan
        assert "notifications" in plan
        assert "start_time" in plan
        assert "end_time" in plan


# =============================================================================
# Edge Case Tests
# =============================================================================

class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_future_dates(self):
        """Test that future dates are handled correctly."""
        future_date = datetime(2030, 1, 1, tzinfo=timezone.utc)
        plan = ArtemisNotificationPlan(
            user_id=uuid4(),
            notifications=[{"id": "1", "type": "test", "mission": "m1"}],
            start_time=future_date,
            end_time=future_date + timedelta(hours=1),
        )
        assert plan.start_time == future_date

    def test_none_values_for_optional_fields(self):
        """Test that notification items can have various fields."""
        plan = ArtemisNotificationPlan(
            user_id=uuid4(),
            notifications=[
                {"id": "1", "type": "recommendation"},
                {"id": "2", "mission": "m2"},
                {},  # Empty notification
            ],
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        assert len(plan.notifications) == 3

    def test_message_id_uniqueness(self):
        """Test that generated message IDs are unique."""
        producer = ArtemisProducer()
        
        msg_ids = [producer._generate_message_id() for _ in range(100)]
        
        # All IDs should be unique
        assert len(msg_ids) == len(set(msg_ids))
        
        # All should start with "omi-"
        assert all(msg_id.startswith("omi-") for msg_id in msg_ids)


# =============================================================================
# SSL/TLS Configuration Tests
# =============================================================================

class TestSSLConfiguration:
    """Tests for SSL/TLS configuration in _create_connection."""

    def test_create_connection_with_ca_cert_only(self):
        """Test creating connection with CA cert only (no client certs)."""
        producer = ArtemisProducer(
            host="test_host",
            port=61613,
            ca_cert="/path/to/ca.crt",
        )
        
        with patch("stomp.Connection") as mock_conn_class:
            mock_conn = MagicMock()
            mock_conn_class.return_value = mock_conn
            mock_transport = MagicMock()
            mock_conn.transport = mock_transport
            
            result = producer._create_connection()
            
            assert result == mock_conn
            mock_transport.set_ssl.assert_called_once()
            call_kwargs = mock_transport.set_ssl.call_args[1]
            assert call_kwargs["ca_certs"] == "/path/to/ca.crt"
            assert call_kwargs["ssl_version"] == ssl.PROTOCOL_TLS_CLIENT

    def test_create_connection_with_ca_cert_and_client_certs(self):
        """Test creating connection with CA cert and client certificates."""
        producer = ArtemisProducer(
            host="test_host",
            port=61613,
            ca_cert="/path/to/ca.crt",
            client_cert="/path/to/client.crt",
            client_key="/path/to/client.key",
        )
        
        with patch("stomp.Connection") as mock_conn_class:
            mock_conn = MagicMock()
            mock_conn_class.return_value = mock_conn
            mock_transport = MagicMock()
            mock_conn.transport = mock_transport
            
            result = producer._create_connection()
            
            assert result == mock_conn
            mock_transport.set_ssl.assert_called_once()
            call_kwargs = mock_transport.set_ssl.call_args[1]
            assert call_kwargs["ca_certs"] == "/path/to/ca.crt"
            assert call_kwargs["cert_file"] == "/path/to/client.crt"
            assert call_kwargs["key_file"] == "/path/to/client.key"

    def test_create_connection_with_no_ssl(self):
        """Test creating connection without SSL (no certs)."""
        with patch.dict("os.environ", {}, clear=True):
            producer = ArtemisProducer(
                host="test_host",
                port=61613,
                ca_cert=None,
                client_cert=None,
                client_key=None,
            )
            
            with patch("stomp.Connection") as mock_conn_class:
                mock_conn = MagicMock()
                mock_conn_class.return_value = mock_conn
                mock_transport = MagicMock()
                mock_conn.transport = mock_transport
                
                result = producer._create_connection()
                
                assert result == mock_conn
                # set_ssl should not be called when no certs are provided
                mock_transport.set_ssl.assert_not_called()

    def test_create_connection_with_client_cert_without_ca_cert_raises(self):
        """Test that client_cert without ca_cert raises ValueError during _create_connection."""
        with patch.dict("os.environ", {}, clear=True):
            # This should pass __init__ validation but fail in _create_connection
            # because we're not providing ca_cert
            producer = ArtemisProducer(
                host="test_host",
                port=61613,
                ca_cert=None,
                client_cert=None,
                client_key=None,
            )
            # Set attributes directly to bypass __init__ validation
            producer.client_cert = "/path/to/client.crt"
            producer.client_key = "/path/to/client.key"
            
            with patch("stomp.Connection") as mock_conn_class:
                mock_conn = MagicMock()
                mock_conn_class.return_value = mock_conn
                mock_transport = MagicMock()
                mock_conn.transport = mock_transport
                
                with pytest.raises(ValueError, match="ARTEMIS_CA_CERT is required for client certificates"):
                    producer._create_connection()

    def test_create_connection_with_client_cert_without_client_key_raises(self):
        """Test that client_cert without client_key raises ValueError during _create_connection.
        
        This tests the edge case where __init__ validation is bypassed.
        """
        producer = ArtemisProducer(
            host="test_host",
            port=61613,
            ca_cert="/path/to/ca.crt",
            client_cert=None,
            client_key=None,
        )
        # Set attributes directly to bypass __init__ validation
        producer.client_cert = "/path/to/client.crt"
        producer.client_key = None  # Bypass __init__ check
        
        with patch("stomp.Connection") as mock_conn_class:
            mock_conn = MagicMock()
            mock_conn_class.return_value = mock_conn
            mock_transport = MagicMock()
            mock_conn.transport = mock_transport
            
            with pytest.raises(ValueError, match="ARTEMIS_CLIENT_KEY is required when ARTEMIS_CLIENT_CERT is set"):
                producer._create_connection()

    def test_create_connection_without_ssl(self):
        """Test creating connection without SSL (no certs)."""
        with patch.dict("os.environ", {}, clear=True):
            producer = ArtemisProducer(
                host="test_host",
                port=61613,
                ca_cert=None,
                client_cert=None,
                client_key=None,
            )
            
            with patch("stomp.Connection") as mock_conn_class:
                mock_conn = MagicMock()
                mock_conn_class.return_value = mock_conn
                mock_transport = MagicMock()
                mock_conn.transport = mock_transport
                
                result = producer._create_connection()
                
                assert result == mock_conn
                # set_ssl should not be called when no certs are provided
                mock_transport.set_ssl.assert_not_called()


# =============================================================================
# Connection Exception Handling Tests
# =============================================================================

class TestConnectionExceptionHandling:
    """Tests for connection exception handling."""

    def test_connect_exception_handling(self, caplog):
        """Test that connect properly logs and raises exceptions."""
        producer = ArtemisProducer()
        mock_conn = MagicMock(spec=stomp.Connection)
        mock_conn.connect = MagicMock(side_effect=Exception("Connection failed"))
        
        with patch.object(producer, "_create_connection", return_value=mock_conn):
            with caplog.at_level(logging.ERROR):
                with pytest.raises(Exception, match="Connection failed"):
                    producer.connect()
            
            assert any("Failed to connect to Artemis" in record.message for record in caplog.records)

    def test_connect_logs_success(self, caplog):
        """Test that connect logs success message."""
        producer = ArtemisProducer(host="localhost", port=61613)
        mock_conn = MagicMock(spec=stomp.Connection)
        mock_conn.connect = MagicMock()
        
        with patch.object(producer, "_create_connection", return_value=mock_conn):
            with caplog.at_level(logging.INFO):
                producer.connect()
            
            assert any("Connected to Artemis broker" in record.message for record in caplog.records)


# =============================================================================
# Send Message Exception Handling Tests
# =============================================================================

class TestSendMessageExceptionHandling:
    """Tests for send_message exception handling."""

    def test_send_message_exception_handling(self, caplog):
        """Test that send_message properly logs and raises exceptions."""
        producer = ArtemisProducer()
        mock_conn = MagicMock(spec=stomp.Connection)
        mock_conn.is_connected = MagicMock(return_value=True)
        mock_conn.send = MagicMock(side_effect=Exception("Send failed"))
        producer._connection = mock_conn
        
        with caplog.at_level(logging.ERROR):
            with pytest.raises(Exception, match="Send failed"):
                producer.send_message("/queue/test", {"test": "message"})
        
        # Check that the error was logged
        assert any("Failed to send message to /queue/test" in record.message for record in caplog.records)


# =============================================================================
# Context Manager Tests
# =============================================================================

class TestContextManager:
    """Tests for context manager functionality."""

    def test_context_manager_connects_and_disconnects(self):
        """Test that context manager properly connects and disconnects."""
        producer = ArtemisProducer()
        mock_conn = MagicMock(spec=stomp.Connection)
        mock_conn.connect = MagicMock()
        mock_conn.disconnect = MagicMock()
        
        with patch.object(producer, "_create_connection", return_value=mock_conn):
            with producer:
                mock_conn.connect.assert_called_once()
            
            mock_conn.disconnect.assert_called_once()

    def test_context_manager_returns_producer(self):
        """Test that context manager returns the producer instance."""
        producer = ArtemisProducer()
        mock_conn = MagicMock(spec=stomp.Connection)
        mock_conn.is_connected = MagicMock(return_value=True)
        
        with patch.object(producer, "_create_connection", return_value=mock_conn):
            with producer as p:
                assert p is producer

    def test_context_manager_disconnects_on_exception(self):
        """Test that context manager disconnects even when exception occurs."""
        producer = ArtemisProducer()
        mock_conn = MagicMock(spec=stomp.Connection)
        mock_conn.connect = MagicMock()
        mock_conn.disconnect = MagicMock()
        
        with patch.object(producer, "_create_connection", return_value=mock_conn):
            with pytest.raises(RuntimeError):
                with producer:
                    raise RuntimeError("Test error")
            
            mock_conn.disconnect.assert_called_once()

    def test_enter_calls_connect(self):
        """Test that __enter__ calls connect."""
        producer = ArtemisProducer()
        mock_conn = MagicMock(spec=stomp.Connection)
        mock_conn.connect = MagicMock()
        
        with patch.object(producer, "_create_connection", return_value=mock_conn):
            producer.__enter__()
            
            mock_conn.connect.assert_called_once()

    def test_exit_calls_disconnect(self):
        """Test that __exit__ calls disconnect."""
        producer = ArtemisProducer()
        mock_conn = MagicMock(spec=stomp.Connection)
        mock_conn.disconnect = MagicMock()
        
        producer._connection = mock_conn
        
        producer.__exit__(None, None, None)
        
        mock_conn.disconnect.assert_called_once()

    def test_exit_does_not_suppress_exception(self):
        """Test that __exit__ does not suppress exceptions."""
        producer = ArtemisProducer()
        mock_conn = MagicMock(spec=stomp.Connection)
        mock_conn.disconnect = MagicMock()
        
        producer._connection = mock_conn
        
        result = producer.__exit__(RuntimeError, RuntimeError("test"), None)
        
        # Should return False to not suppress the exception
        assert result is False
