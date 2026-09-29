#!/usr/bin/env python3

"""Pipeline for notification plans of users based on updates and selected_content.

This version uses Artemis message queue for notification_plans only.
Recommendation plans are internal and used to generate notification plans.
"""

# --------------------------------- import libraries  ---------------------------------
from __future__ import annotations

import json
import logging
import time
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID
import requests
import stomp
import ssl
import uuid

# ---------------------------------- Configurations  ----------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logging.Formatter.converter = time.gmtime  # enforce UTC)

# ---------------------------------- Define variables ---------------------------------

CS_BASE = os.getenv("CS_BASE_URL", "http://cs_module:8000")
OMI_BASE = os.getenv("OMI_BASE_URL", "http://localhost:3200")
EUT_BASE = os.getenv("EUT_PRE_BASE_URL")
EUT_TOKEN = os.getenv("EUT_PRE_TOKEN")
HEADERS_EUT = {"Authorization": f"Basic {EUT_TOKEN}"} if EUT_TOKEN else {}
HEADERS_EUT_POST = {
    "Authorization": f"Basic {EUT_TOKEN}",
    "Content-Type": "application/json",
} if EUT_TOKEN else {"Content-Type": "application/json"}
HEADERS_CS = {"Content-Type": "application/json"}
HEADERS_OMI = {"Content-Type": "application/json"}
CATCH_UP_INTERVENTION_DAYS = 4

# Artemis configuration - all values must be provided via environment variables
ARTEMIS_HOST = os.getenv("ARTEMIS_HOST")
ARTEMIS_PORT = int(os.getenv("ARTEMIS_PORT", "61616"))
ARTEMIS_USER = os.getenv("ARTEMIS_USER")
ARTEMIS_PASSWORD = os.getenv("ARTEMIS_PASSWORD")
ARTEMIS_CA_CERT = os.getenv("ARTEMIS_CA_CERT")
ARTEMIS_QUEUE_NOTIFICATION_PLANS = os.getenv("ARTEMIS_QUEUE_NOTIFICATION_PLANS", "omi.notification_plans")

# Validate required Artemis configuration
if not all([ARTEMIS_HOST, ARTEMIS_USER, ARTEMIS_PASSWORD]):
    raise ValueError("ARTEMIS_HOST, ARTEMIS_USER, and ARTEMIS_PASSWORD environment variables are required")


# ---------------------------------- Artemis Producer Helper -------------------------

class ArtemisProducer:
    """Helper class to send messages to Artemis queue."""
    
    def __init__(self):
        self.connection = None
        self.message_count = 0
        self._setup_connection()
    
    def _setup_connection(self):
        """Setup STOMP connection with SSL."""
        self.connection = stomp.Connection(
            host_and_ports=[(ARTEMIS_HOST, ARTEMIS_PORT)],
            heartbeats=(10000, 10000)
        )
        
        # Configure SSL if CA certificate is provided
        if ARTEMIS_CA_CERT:
            settings = {
                "for_hosts": [(ARTEMIS_HOST, ARTEMIS_PORT)],
                "ca_certs": ARTEMIS_CA_CERT,
                "ssl_version": ssl.PROTOCOL_TLS_CLIENT,
            }
            self.connection.transport.set_ssl(**settings)
    
    def connect(self):
        """Connect to Artemis broker."""
        if not self.connection or not self.connection.is_connected():
            self._setup_connection()
            self.connection.connect(ARTEMIS_USER, ARTEMIS_PASSWORD, wait=True)
            logging.info("Connected to Artemis broker at %s:%s", ARTEMIS_HOST, ARTEMIS_PORT)
    
    def disconnect(self):
        """Disconnect from Artemis broker."""
        if self.connection and self.connection.is_connected():
            self.connection.disconnect()
            logging.info("Disconnected from Artemis broker")
    
    def send_message(self, destination, message_body, message_type="notification_plan"):
        """Send a message to Artemis queue."""
        self.connect()
        
        message_id = f"omi-{uuid.uuid4()}"
        body = json.dumps(message_body, default=str)
        
        headers = {
            "message-id": message_id,
            "persistent": "true",
            "type": message_type,
        }
        
        try:
            self.connection.send(
                body=body,
                destination=destination,
                headers=headers,
            )
            self.message_count += 1
            logging.info("Sent %s message %s to %s (total: %d)", message_type, message_id, destination, self.message_count)
            return message_id
        except Exception as e:
            logging.error("Failed to send %s message to %s: %s", message_type, destination, str(e))
            self.disconnect()
            raise

# Global producer instance
artemis_producer = ArtemisProducer()


# ---------------------------------- Original Helper Functions ------------------------

def _eut_get(path: str, params: dict | None = None):
    if not EUT_BASE:
        return None
    url = build_url(EUT_BASE, path)
    logging.info("GET %s params=%s", url, params or {})
    r = requests.get(url, headers=HEADERS_EUT, params=params or {})
    if r.status_code != 200:
        raise RuntimeError(f"EUT GET {path} failed: {r.status_code} {r.text}")
    try:
        return r.json()
    except ValueError as exc:
        raise RuntimeError(f"Invalid JSON from EUT at {path}") from exc


def _omi_post(endpoint: str, payload, extra_params: dict | None = None):
    url = build_url(OMI_BASE, endpoint)
    if extra_params:
        payload = {"data": payload, **extra_params}
    logging.info("POST %s bytes=%d", url, len(json.dumps(payload)))
    r = requests.post(url, json=payload, headers=HEADERS_OMI)
    if r.status_code not in (200, 201):
        logging.error(
            "Failed payload that caused error: %s", json.dumps(payload, indent=2)
        )
        raise RuntimeError(f"OMI POST {endpoint} failed: {r.status_code} {r.text}")


def _omi_get(path: str, params: dict | None = None):
    url = build_url(OMI_BASE, path)
    logging.info("GET %s params=%s", url, params or {})
    r = requests.get(url, headers=HEADERS_OMI, params=params or {})
    if r.status_code != 200:
        raise RuntimeError(f"OMI GET {path} failed: {r.status_code} {r.text}")
    try:
        return r.json()
    except ValueError as exc:
        raise RuntimeError(f"Invalid JSON from OMI at {path}") from exc


def _cs_get(path: str, params: dict | None = None):
    url = build_url(CS_BASE, path)
    logging.info("GET %s params=%s", url, params or {})
    r = requests.get(url, headers=HEADERS_CS, params=params or {})
    if r.status_code != 200:
        raise RuntimeError(f"CS GET {path} failed: {r.status_code} {r.text}")
    try:
        return r.json()
    except ValueError as exc:
        raise RuntimeError(f"Invalid JSON from CS at {path}") from exc


def utc_iso(dt: datetime) -> str:
    """
    Return an RFC 3339 string in UTC with trailing 'Z' and seconds precision.
    Will never lie: it first converts to UTC.
    """
    if dt.tzinfo is None:
        raise ValueError("Naive datetime received - timezone info required")
    return (
        dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def naive_utc_iso(dt: datetime) -> str:
    """
    Return a UTC timestamp formatted without timezone info (naive),
    e.g. '2025-08-11T00:00:00'. Input must be timezone-aware.
    """
    if dt.tzinfo is None:
        raise ValueError("Naive datetime received - timezone info required")
    dt_utc_naive = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt_utc_naive.strftime("%Y-%m-%dT%H:%M:%S")


def build_url(base: str, path: str) -> str:
    base = base.rstrip("/")
    path = "/" + path.lstrip("/")
    return (base + path).rstrip("/")


# ---------------------------------- Modified Function for Artemis --------------------

def set_notification_plans_artemis(**context):
    """Fetch notification plans and emit to Artemis queue instead of posting to EUT.
    
    This is the ONLY function that broadcasts to Artemis.
    Recommendation plans are internal to OMI and used to generate notification plans.
    """
    start = (datetime.now(timezone.utc) + timedelta(hours=1)).astimezone(timezone.utc)
    end = (start + timedelta(hours=1)).astimezone(timezone.utc)
    params = {"start_time": naive_utc_iso(start), "end_time": naive_utc_iso(end)}
    
    data = _omi_get("/notification_plans", params=params)
    logging.info("Retrieved notification_plans payload:")
    logging.info(json.dumps(data, indent=2))

    if not data or (isinstance(data, dict) and len(data) == 0):
        logging.info("No notification plans to emit to Artemis; skipping.")
        return

    for user_id, bucket in data.items():
        if not user_id or not bucket or not bucket.get("contents"):
            continue
        
        artemis_message = {
            "notification_plan": {
                "user_id": str(user_id),
                "notifications": bucket.get("contents", []),
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
            }
        }
        
        try:
            artemis_producer.send_message(
                f"/queue/{ARTEMIS_QUEUE_NOTIFICATION_PLANS}",
                artemis_message,
                "notification_plan"
            )
            logging.info("Successfully emitted notification plan for user %s to Artemis", user_id)
        except Exception as e:
            logging.error("Failed to emit notification plan for user %s: %s", user_id, str(e))
            raise


# ────────────────────────────────────────────────────────────────────────
# Phase 2: Alignment of past updates
# ────────────────────────────────────────────────────────────────────────
def align_past_updates():
    cursor = datetime(2025, 5, 1, tzinfo=timezone.utc)
    stop_at = datetime.now(timezone.utc) - timedelta(days=CATCH_UP_INTERVENTION_DAYS)
    while cursor < stop_at:
        window_end = min(cursor + timedelta(days=2), stop_at)
        _fetch_updates_window(cursor, window_end)
        cursor = window_end


# ────────────────────────────────────────────────────────────────────────
# Alignment of past selected contents
# ────────────────────────────────────────────────────────────────────────
def align_past_selected_content():
    cursor = datetime(2025, 5, 1, tzinfo=timezone.utc)
    stop_at = datetime.now(timezone.utc) - timedelta(days=CATCH_UP_INTERVENTION_DAYS)
    while cursor < stop_at:
        window_end = min(cursor + timedelta(days=2), stop_at)
        params = {
            "start_time": utc_iso(cursor),
            "end_time": utc_iso(window_end),
        }
        data = _cs_get("/selected_contents", params=params)
        if data:
            _omi_post("selected_contents", data)
        else:
            logging.info(
                "No selected contents for window %s -> %s",
                params["start_time"],
                params["end_time"],
            )
        cursor = window_end


# ────────────────────────────────────────────────────────────────────────
# Original update window helper
# ────────────────────────────────────────────────────────────────────────
def _fetch_updates_window(start_dt: datetime, end_dt: datetime):
    params = {
        "start_date": utc_iso(start_dt),
        "end_date": utc_iso(end_dt),
    }
    data = _eut_get("/updates", params=params)
    if data:
        all_user_ids = set()
        sections = [
            "new_users",
            "disabled_users",
            "user_feedback",
            "health_habit_assessments",
            "new_missions_and_contents",
            "escalation_level",
        ]
        for key in sections:
            section = data.get(key, {})
            if isinstance(section, dict):
                all_user_ids.update(section.keys())

        invalid_ids = []
        for uid in all_user_ids:
            try:
                UUID(uid)
            except ValueError:
                invalid_ids.append(uid)
                logging.warning("Skipping invalid user_id: %s", uid)

        for uid in invalid_ids:
            for key in sections:
                section = data.get(key, {})
                if isinstance(section, dict) and uid in section:
                    del section[uid]

        if any(len(v) > 0 for v in data.values() if isinstance(v, dict)):
            _omi_post("updates", data)
        else:
            logging.info(
                "No valid updates after filtering for window %s -> %s",
                params["start_date"],
                params["end_date"],
            )
    else:
        logging.info(
            "No updates for window %s -> %s", params["start_date"], params["end_date"]
        )


# ────────────────────────────────────────────────────────────────────────
# Phase 3 & 4: hourly update
# ────────────────────────────────────────────────────────────────────────
def hourly_update(**context):
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=1)
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    logging.info("INTERVAL %s -> %s", start.isoformat(), end.isoformat())
    _fetch_updates_window(start, end)


def post_selected_contents(**context):
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    end = start + timedelta(hours=1)
    data = _cs_get("/selected_contents")
    if data:
        _omi_post("selected_contents", data)
    logging.info("Retrieved and posted selected_contents payload:")
    logging.info(json.dumps(data, indent=2))


def fetch_recommendation_plans(**context):
    """Fetch recommendation plans from OMI - INTERNAL ONLY."""
    start = (datetime.now(timezone.utc) + timedelta(hours=1)).astimezone(timezone.utc)
    end = (start + timedelta(hours=1)).astimezone(timezone.utc)
    params = {"start_time": naive_utc_iso(start), "end_time": naive_utc_iso(end)}
    data = _omi_get("/recommendation_plans", params=params)
    logging.info("Retrieved recommendation_plans payload (INTERNAL):")
    logging.info(json.dumps(data, indent=2))


# =============================================================================
# ENTRYPOINT
# =============================================================================

if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dag_id",
        required=True,
        type=float,
        help="DAG ID: 1, 2, or 3",
    )

    args = parser.parse_args()

    try:
        if args.dag_id == 1:
            align_past_updates()
            align_past_selected_content()

        elif args.dag_id == 2:
            NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            BACKFILL_START = NOW - timedelta(days=CATCH_UP_INTERVENTION_DAYS)
            BACKFILL_END = NOW - timedelta(hours=1)

            hourly_update()
            post_selected_contents()
            fetch_recommendation_plans()  # INTERNAL
            set_notification_plans_artemis()  # BROADCAST to Artemis

        elif args.dag_id == 3:
            hourly_update()
            post_selected_contents()
            fetch_recommendation_plans()  # INTERNAL
            set_notification_plans_artemis()  # BROADCAST to Artemis
        else:
            raise ValueError(f"Invalid DAG ID: {args.dag_id}")

        artemis_producer.disconnect()

        if artemis_producer.message_count > 0:
            logging.info("Artemis broadcast summary: %d notification_plan messages sent", artemis_producer.message_count)
        else:
            logging.info("No Artemis messages broadcast")

    except Exception:
        artemis_producer.disconnect()
        raise
