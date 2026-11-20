"""Service for producing and sending messages to Artemis message queue."""

import json
import logging
import os
import ssl
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

import stomp
from pydantic import BaseModel

from app.schemas.artemis_producer import (
    ArtemisNotificationPlan,
    create_notification_plan_message,
)

logger = logging.getLogger(__name__)


class ArtemisProducer:
    """Producer for sending messages to Artemis queue."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        ca_cert: Optional[str] = None,
        client_cert: Optional[str] = None,
        client_key: Optional[str] = None,
    ):
        """Initialize Artemis producer with connection parameters."""
        self.host = host or os.getenv("ARTEMIS_HOST", "localhost")
        self.port = port or int(os.getenv("ARTEMIS_PORT", "61613"))
        self.user = user or os.getenv("ARTEMIS_USER", "artemis")
        self.password = password or os.getenv("ARTEMIS_PASSWORD", "artemis")
        self.ca_cert = ca_cert or os.getenv("ARTEMIS_CA_CERT")
        self.client_cert = client_cert or os.getenv("ARTEMIS_CLIENT_CERT")
        self.client_key = client_key or os.getenv("ARTEMIS_CLIENT_KEY")
        self._connection: Optional[stomp.Connection] = None

        # Validate SSL certificate configuration
        if bool(self.client_cert) != bool(self.client_key):
            raise ValueError(
                "Both ARTEMIS_CLIENT_CERT and ARTEMIS_CLIENT_KEY are required"
            )
        if self.client_cert and not self.ca_cert:
            raise ValueError("ARTEMIS_CA_CERT is required for client certificates")

    def _create_connection(self) -> stomp.Connection:
        """Create and configure STOMP connection with TLS."""
        connection = stomp.Connection(
            host_and_ports=[(self.host, self.port)], heartbeats=(10000, 10000)
        )

        # Configure SSL/TLS
        if self.ca_cert:
            settings: Dict[str, Any] = {
                "for_hosts": [(self.host, self.port)],
                "ca_certs": self.ca_cert,
                "ssl_version": ssl.PROTOCOL_TLS_CLIENT,
            }
            if self.client_cert:
                if not self.client_key:
                    raise ValueError(
                        "ARTEMIS_CLIENT_KEY is required when ARTEMIS_CLIENT_CERT is set"
                    )
                settings.update(cert_file=self.client_cert, key_file=self.client_key)
            connection.transport.set_ssl(**settings)
        elif self.client_cert:
            raise ValueError("ARTEMIS_CA_CERT is required for client certificates")

        return connection

    def connect(self) -> None:
        """Establish connection to Artemis broker."""
        if self._connection is None or not self._connection.is_connected():
            self._connection = self._create_connection()
            try:
                self._connection.connect(self.user, self.password, wait=True)
                logger.info(
                    "Connected to Artemis broker at %s:%s", self.host, self.port
                )
            except Exception as e:
                logger.error("Failed to connect to Artemis: %s", str(e))
                raise

    def disconnect(self) -> None:
        """Disconnect from Artemis broker."""
        if self._connection and self._connection.is_connected():
            self._connection.disconnect()
            logger.info("Disconnected from Artemis broker")

    def send_message(
        self,
        destination: str,
        message: Dict[str, Any],
        persistent: bool = True,
        duplicate_id: str | None = None,
    ) -> str:
        """Send a message to Artemis queue."""
        if self._connection is None or not self._connection.is_connected():
            self.connect()

        message_id = self._generate_message_id()
        body = json.dumps(message, default=str)

        headers = {
            "message-id": message_id,
            "persistent": str(persistent),
            "destination": destination,
        }

        if duplicate_id:
            headers["_AMQ_DUPL_ID"] = duplicate_id
        try:
            self._connection.send(
                body=body,
                destination=destination,
                headers=headers,
            )
            logger.info("Sent message %s to %s", message_id, destination)
            return message_id
        except Exception as e:
            logger.error("Failed to send message to %s: %s", destination, str(e))
            raise

    def _generate_message_id(self) -> str:
        """Generate a unique message ID."""
        import uuid

        return f"omi-{uuid.uuid4()}"

    def send_notification_plan(
        self,
        user_id: UUID,
        notifications: List[Dict],
        start_time: datetime,
        end_time: datetime,
        queue: str = "omi.notification_plans",
    ) -> str:
        """Send notification plan to Artemis queue."""
        message = create_notification_plan_message(
            user_id, notifications, start_time, end_time
        )
        return self.send_message(f"/queue/{queue}", message)

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.disconnect()
        return False


def get_artemis_producer() -> ArtemisProducer:
    """Factory function to create configured Artemis producer."""
    return ArtemisProducer()


def send_notification_plan_to_artemis(
    user_id: UUID,
    notifications: List[Dict],
    start_time: datetime,
    end_time: datetime,
    queue: str = "omi.notification_plans",
) -> str:
    """Convenience function to send notification plan via Artemis."""
    producer = get_artemis_producer()
    message = create_notification_plan_message(
        user_id, notifications, start_time, end_time
    )

    try:
        producer.connect()
        return producer.send_message(f"/queue/{queue}", message)
    finally:
        producer.disconnect()
