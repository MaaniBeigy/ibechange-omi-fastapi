"""Run real decision epochs and delayed-feedback learning independently of HTTP."""

import logging
import signal
import threading
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models.policy_learning import PolicyLearner, PolicyTransition
from app.models.user import User
from app.services.policy_learning_service import (
    close_episode,
    lock_user,
    prepare_policy,
)
from app.services.recommendation_plan_service import (
    current_queue,
    generate_recommendation_plan,
)
from app.services.rl_policy import InfeasibleQueueError, load_policy_artifacts

logger = logging.getLogger(__name__)


def run_epoch(engine, epoch: datetime) -> int:
    """Commit users independently so an infeasible queue cannot stop the cohort."""
    policies = load_policy_artifacts()
    versions = [policy.version for policy in policies.values()]
    with Session(engine) as db:
        user_ids = db.exec(select(User.id).order_by(User.id)).all()
    completed = 0
    for user_id in user_ids:
        with Session(engine) as db:
            try:
                lock_user(db, user_id)
                # HTTP may already have prepared a later epoch while this worker
                # was processing the rest of the cohort. Never move a stream back.
                if db.exec(
                    select(PolicyLearner.id)
                    .where(
                        PolicyLearner.user_id == user_id,
                        PolicyLearner.updated_at > epoch,
                        PolicyLearner.base_version.in_(versions),
                    )
                    .limit(1)
                ).first():
                    continue
                user = db.get(User, user_id)
                current = current_queue(db, user_id, epoch)
                feasible = True
                if current is not None and not user.is_deleted:
                    try:
                        generate_recommendation_plan(current, epoch, db)
                    except InfeasibleQueueError as error:
                        feasible = False
                        logger.warning(
                            "Infeasible queue for %s at %s: %s", user_id, epoch, error
                        )
                close_episode(db, user_id, epoch)
                wearer = (user.characteristics or {}).get("attributes", {}).get(
                    "wearable"
                ) == "yes"
                prepare_policy(
                    db, user_id, policies["wearer" if wearer else "nonwearer"], epoch
                )
                previous_device = policies["nonwearer" if wearer else "wearer"]
                if db.exec(
                    select(PolicyTransition.id)
                    .where(
                        PolicyTransition.user_id == user_id,
                        PolicyTransition.base_version == previous_device.version,
                    )
                    .limit(1)
                ).first():
                    prepare_policy(db, user_id, previous_device, epoch)
                db.commit()
                completed += int(feasible)
            except Exception:
                db.rollback()
                logger.exception(
                    "Policy epoch failed for user %s at %s", user_id, epoch
                )
    return completed


def run_worker(engine, stopped: threading.Event) -> None:
    """Wake at each grid boundary; missed epochs are never simulated retrospectively."""
    grid_seconds = int(load_policy_artifacts()["nonwearer"].metadata["grid_min"]) * 60
    previous = None
    while not stopped.is_set():
        now = datetime.now(timezone.utc)
        stamp = int(now.timestamp())
        epoch = datetime.fromtimestamp(stamp - stamp % grid_seconds, timezone.utc)
        if epoch != previous:
            # On a mid-epoch restart, wait for the next boundary to avoid backdating sends.
            if (now - epoch).total_seconds() < 1:
                run_epoch(engine, epoch)
            previous = epoch
        wait = grid_seconds - now.timestamp() % grid_seconds
        stopped.wait(min(wait, 30))


def main() -> None:
    from app.dependencies import engine
    from app.services.policy_learning_service import learning_mode

    learning_mode()
    load_policy_artifacts()
    logging.basicConfig(level=logging.INFO)
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    run_worker(engine, stopped)


if __name__ == "__main__":  # pragma: no cover
    main()
