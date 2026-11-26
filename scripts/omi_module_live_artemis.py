#!/usr/bin/env python3

"""Synchronize CS selections and deliver committed OMI sends to VU or Artemis."""

# --------------------------------- import libraries  ---------------------------------
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------- Configurations  ----------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logging.Formatter.converter = time.gmtime

# ---------------------------------- Define variables ---------------------------------

CS_BASE = os.getenv("CS_BASE_URL", "").strip().rstrip("/")
# Existing INT Cronicle jobs may still supply the pre-RCT Compose service name.
if CS_BASE in ("", "http://cs_module:8000"):
    CS_BASE = "http://cs-rct-cs-api-1:8000"
OMI_BASE = os.getenv("OMI_BASE_URL", "http://172.31.20.104:3200").strip()
EUT_BASE = os.getenv("EUT_PRE_BASE_URL")
EUT_TOKEN = os.getenv("EUT_PRE_TOKEN")
HEADERS_EUT_POST = (
    {
        "Authorization": f"Basic {EUT_TOKEN}",
        "Content-Type": "application/json",
    }
    if EUT_TOKEN
    else {"Content-Type": "application/json"}
)
HEADERS_CS = {"Content-Type": "application/json"}
HEADERS_OMI = {"Content-Type": "application/json"}
CATCH_UP_INTERVENTION_DAYS = 4

ARTEMIS_QUEUE_NOTIFICATION_PLANS = os.getenv(
    "ARTEMIS_QUEUE_NOTIFICATION_PLANS", "omi.notification_plans"
)


def _omi_post(endpoint: str, payload, extra_params: dict | None = None):
    url = build_url(OMI_BASE, endpoint)
    if extra_params:
        payload = {"data": payload, **extra_params}
    logging.info("POST %s bytes=%d", url, len(json.dumps(payload)))
    r = requests.post(url, json=payload, headers=HEADERS_OMI, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"OMI POST {endpoint} failed: {r.status_code} {r.text}")


def _omi_get(path: str, params: dict | None = None):
    url = build_url(OMI_BASE, path)
    logging.info("GET %s params=%s", url, params or {})
    r = requests.get(url, headers=HEADERS_OMI, params=params or {}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"OMI GET {path} failed: {r.status_code} {r.text}")
    skipped = r.headers.get("X-OMI-Infeasible-Participants")
    if skipped:
        logging.warning(
            "OMI skipped new decisions for %s participant(s) with infeasible plans on %s. "
            "See OMI app logs for the plan IDs and reasons.",
            skipped,
            path,
        )
    try:
        return r.json()
    except ValueError as exc:
        raise RuntimeError(f"Invalid JSON from OMI at {path}") from exc


def _cs_get(path: str, params: dict | None = None):
    url = build_url(CS_BASE, path)
    logging.info("GET %s params=%s", url, params or {})
    r = requests.get(url, headers=HEADERS_CS, params=params or {}, timeout=30)
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


def delivery_window(now):
    # The API can commit the next grid epoch. Starting an hour ahead misses it.
    return {
        "start_time": utc_iso(now),
        "end_time": utc_iso(now + timedelta(hours=1)),
        "skip_infeasible": "true",
    }


def set_notification_plans_artemis(**context):
    if not __package__:
        # The broker producer lives in the OMI checkout beside scripts/.
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.services.artemis_producer_service import get_artemis_producer

    params = delivery_window(context.get("now") or datetime.now(timezone.utc))
    plans = _omi_get("/notification_plans", params)
    producer = get_artemis_producer()
    try:
        for user_id, bucket in plans.items():
            for item in bucket["contents"]:
                when = item["send_timestamp"]
                message = {
                    "notification_plan": {
                        "user_id": user_id,
                        "notifications": [item],
                        "start_time": params["start_time"],
                        "end_time": params["end_time"],
                    }
                }
                # One committed send per user/epoch, including retries in overlapping windows.
                key = hashlib.sha256(
                    f"{user_id}:{when}:{item.get('plan_id', '')}".encode()
                ).hexdigest()
                producer.send_message(
                    f"/queue/{ARTEMIS_QUEUE_NOTIFICATION_PLANS}",
                    message,
                    duplicate_id=key,
                )
    finally:
        producer.disconnect()


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
def post_selected_contents(**context):
    data = _cs_get("/selected_contents")
    if not data:
        logging.info("CS returned no participant selections")
        return
    _omi_post("selected_contents", data)
    items_by_date = Counter()
    for plan in data.values():
        items_by_date[plan.get("delivery_date") or "undated"] += len(plan["contents"])
    logging.info(
        "OMI acknowledged CS refresh for %d participants. Content items by delivery date: %s",
        len(data),
        dict(sorted(items_by_date.items())),
    )


def fetch_recommendation_plans(**context):
    return _omi_get(
        "/recommendation_plans",
        delivery_window(context.get("now") or datetime.now(timezone.utc)),
    )


def deliver_http(now):
    if not EUT_BASE:
        raise ValueError("EUT_PRE_BASE_URL is required for VU delivery")
    plans = fetch_recommendation_plans(now=now)
    notifications = _omi_get("/notification_plans", delivery_window(now))
    count = 0
    for bucket in notifications.values():
        for item in bucket["contents"]:
            # OMI may serialize scheduled UTC times without the offset.
            if datetime.fromisoformat(item["send_timestamp"]).tzinfo is None:
                item["send_timestamp"] += "Z"
            count += 1
    if not count:
        logging.info("No notification plans to post to EUT for this delivery window")
        return

    url = build_url(EUT_BASE, "/internal/recommendations/") + "/"
    response = requests.post(
        url,
        json=notifications,
        headers=HEADERS_EUT_POST,
        timeout=30,
        allow_redirects=False,
    )
    if not 200 <= response.status_code < 300:
        raise requests.HTTPError(
            f"EUT POST {url} returned HTTP {response.status_code}", response=response
        )
    logging.info(
        "EUT POST %s returned HTTP %d (notifications=%d)",
        url,
        response.status_code,
        count,
    )
    if plans["recommendation_plans"]:
        requests.post(
            build_url(CS_BASE, "/recommendation_plans"),
            json=plans,
            headers=HEADERS_CS,
            timeout=30,
        ).raise_for_status()


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dag_id",
        type=float,
        choices=(1, 2, 3),
        required=True,
        help="1 imports historical CS selections, 2 and 3 refresh CS and deliver",
    )
    parser.add_argument(
        "--delivery",
        choices=("artemis", "vu"),
        default="vu",
        help="Deliver notifications over VU HTTP (default) or the Artemis queue",
    )
    parser.add_argument(
        "--delivery-only",
        action="store_true",
        help="Poll committed plans between hourly CS refreshes",
    )
    args = parser.parse_args()
    if args.dag_id == 1:
        align_past_selected_content()
        return
    if not args.delivery_only:
        post_selected_contents()
    now = datetime.now(timezone.utc)
    if args.delivery == "artemis":
        set_notification_plans_artemis(now=now)
    else:
        deliver_http(now)


if __name__ == "__main__":
    main()
