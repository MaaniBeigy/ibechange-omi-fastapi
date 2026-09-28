import argparse
import json
import logging
import os
import signal
import ssl
import threading
from time import sleep

import stomp
from dotenv import load_dotenv
from pydantic import ValidationError
from sqlmodel import Session

from app.services.artemis_update_service import (
    process_artemis_message,
    record_invalid_message,
)

logger = logging.getLogger(__name__)


def _invalid_reason(error: Exception) -> str:
    """Describe validation failures without storing message field values."""
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['type']}"
            for item in error.errors()
        )
    if isinstance(error, json.JSONDecodeError):
        return "Invalid JSON"
    if str(error).startswith("Unknown Artemis event:"):
        return "Unknown Artemis event"
    return str(error)


def _session() -> Session:
    """Open a session after environment settings are loaded."""
    from app.dependencies import engine

    return Session(engine)


class UpdateListener(stomp.ConnectionListener):
    """Write broker messages to the database before acknowledging them."""

    def __init__(self, connection, stopped: threading.Event):
        self.connection = connection
        self.stopped = stopped

    def on_disconnected(self):
        self.stopped.set()

    def on_error(self, frame):
        logger.error("Artemis broker reported an error")
        self.stopped.set()

    def on_message(self, frame):
        message_id = frame.headers.get("message-id")
        ack_id = frame.headers.get("ack", message_id)
        if not message_id or not ack_id:
            logger.error("Artemis delivered a message without an acknowledgement ID")
            self.stopped.set()
            return
        try:
            with _session() as db:
                process_artemis_message(frame.body, message_id, db)
        except (ValueError, ValidationError, json.JSONDecodeError) as error:
            try:
                with _session() as db:
                    record_invalid_message(
                        frame.body, message_id, _invalid_reason(error), db
                    )
            except Exception:
                logger.exception(
                    "Could not record invalid Artemis message %s", message_id
                )
                self.stopped.set()
                return
        except Exception:
            logger.exception("Could not process Artemis message %s", message_id)
            self.stopped.set()
            return
        try:
            self.connection.ack(id=ack_id, subscription="omi-updates")
        except Exception:
            logger.exception("Could not acknowledge Artemis message %s", message_id)
            self.stopped.set()


def _connection(host: str, port: int):
    """Build the STOMP connection with the configured TLS settings."""
    connection = stomp.Connection(
        host_and_ports=[(host, port)], heartbeats=(10000, 10000)
    )
    ca_cert = os.getenv("ARTEMIS_CA_CERT")
    client_cert = os.getenv("ARTEMIS_CLIENT_CERT")
    client_key = os.getenv("ARTEMIS_CLIENT_KEY")
    if bool(client_cert) != bool(client_key):
        raise ValueError("Both ARTEMIS_CLIENT_CERT and ARTEMIS_CLIENT_KEY are required")
    if ca_cert:
        settings = {
            "for_hosts": [(host, port)],
            "ca_certs": ca_cert,
            "ssl_version": ssl.PROTOCOL_TLS_CLIENT,
        }
        if client_cert:
            settings.update(cert_file=client_cert, key_file=client_key)
        connection.transport.set_ssl(**settings)
    elif client_cert:
        raise ValueError("ARTEMIS_CA_CERT is required for client certificates")
    return connection


def run_consumer(queue: str, stopped: threading.Event) -> None:
    """Consume the configured queue until shutdown, reconnecting after a break."""
    host = os.getenv("ARTEMIS_HOST", "localhost")
    port = int(os.getenv("ARTEMIS_PORT", "61613"))
    user = os.getenv("ARTEMIS_USER", "artemis")
    password = os.getenv("ARTEMIS_PASSWORD", "artemis")
    destination = f"/queue/{queue}"
    while not stopped.is_set():
        disconnected = threading.Event()
        connection = _connection(host, port)
        connection.set_listener("omi-updates", UpdateListener(connection, disconnected))
        try:
            connection.connect(user, password, wait=True)
            connection.subscribe(
                destination=destination, id="omi-updates", ack="client-individual"
            )
            logger.info("Consuming Artemis destination %s", destination)
            while not stopped.is_set() and not disconnected.wait(1):
                pass
        except Exception:
            logger.exception("Artemis connection failed")
        finally:
            if connection.is_connected():
                connection.disconnect()
        if not stopped.is_set():
            sleep(5)


def main() -> None:
    """Run the Artemis consumer as a separate application process."""
    load_dotenv()
    parser = argparse.ArgumentParser(description="Consume Artemis updates for OMI")
    parser.add_argument("--queue", default=os.getenv("ARTEMIS_QUEUE"), required=False)
    args = parser.parse_args()
    if not args.queue:
        parser.error("--queue or ARTEMIS_QUEUE is required")
    logging.basicConfig(level=logging.INFO)
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    run_consumer(args.queue, stopped)


if __name__ == "__main__":  # pragma: no cover
    main()
