"""Release observable rewards and fit a participant's own stream transactionally."""

import hashlib
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import text
from sqlmodel import Session, select

from app.models.event import Event
from app.models.policy_decision import PolicyDecision
from app.models.policy_learning import (
    PolicyCheckpoint,
    PolicyLearner,
    PolicyTrainingUpdate,
    PolicyTransition,
)
from app.rl.learner import ConstrainedConservativeDoubleDQN, TrainingConfig
from app.services.rl_observations import _identity
from app.services.rl_policy import PolicyArtifact
from app.utils.helpers import _aware_utc, _to_naive_utc


def learning_mode() -> str:
    mode = os.getenv("POLICY_LEARNING_MODE", "frozen")
    if mode not in {"frozen", "online"}:
        raise ValueError("POLICY_LEARNING_MODE must be frozen or online")
    return mode


def lock_user(db: Session, user_id) -> None:
    """Use the same transaction lock for serving, feedback and training workers."""
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": str(user_id)},
    )


def _device(decision: PolicyDecision) -> str:
    return (
        "wearer"
        if decision.policy_version.startswith("con_cddqn-wearer-")
        else "nonwearer"
    )


def _base_version(decision: PolicyDecision) -> str:
    return decision.policy_version.split("-online-")[0].removesuffix("-omi2x2-v1")


def record_next_state(db: Session, current: PolicyDecision) -> None:
    """Attach the preceding action to the actual next observed decision epoch."""
    previous = db.exec(
        select(PolicyDecision)
        .where(
            PolicyDecision.user_id == current.user_id,
            PolicyDecision.superseded_by == None,
            PolicyDecision.decided_at < current.decided_at,
        )
        .order_by(PolicyDecision.decided_at.desc())
    ).first()
    if (
        previous is None
        or db.exec(
            select(PolicyTransition).where(
                PolicyTransition.decision_id == previous.id,
            )
        ).first()
    ):
        return
    zone = ZoneInfo(os.getenv("POLICY_TIMEZONE", "Europe/Berlin"))
    same_episode = (
        _aware_utc(previous.decided_at).astimezone(zone).date()
        == _aware_utc(current.decided_at).astimezone(zone).date()
        and _base_version(previous) == _base_version(current)
        and _device(previous) == _device(current)
    )
    db.add(
        PolicyTransition(
            user_id=previous.user_id,
            decision_id=previous.id,
            next_decision_id=current.id if same_episode else None,
            device=_device(previous),
            base_version=_base_version(previous),
            state=previous.state,
            next_state=current.state if same_episode else previous.state,
            action=previous.action,
            safe_mask=previous.safe_mask,
            next_safe_mask=current.safe_mask if same_episode else [True, False],
            elapsed_min=(
                _aware_utc(current.decided_at) - _aware_utc(previous.decided_at)
            ).total_seconds()
            / 60,
            bootstrap=int(same_episode),
        )
    )
    db.flush()


def close_episode(db: Session, user_id, now: datetime) -> None:
    """Close a completed/disabled/empty queue without inventing a future observation."""
    latest = db.exec(
        select(PolicyDecision)
        .where(
            PolicyDecision.user_id == user_id,
            PolicyDecision.superseded_by == None,
            PolicyDecision.decided_at < now,
        )
        .order_by(PolicyDecision.decided_at.desc())
    ).first()
    if (
        latest is None
        or db.exec(
            select(PolicyTransition).where(
                PolicyTransition.decision_id == latest.id,
            )
        ).first()
    ):
        return
    db.add(
        PolicyTransition(
            user_id=user_id,
            decision_id=latest.id,
            device=_device(latest),
            base_version=_base_version(latest),
            state=latest.state,
            next_state=latest.state,
            action=latest.action,
            safe_mask=latest.safe_mask,
            next_safe_mask=[True, False],
            elapsed_min=(
                _aware_utc(now) - _aware_utc(latest.decided_at)
            ).total_seconds()
            / 60,
            bootstrap=0,
        )
    )
    db.flush()


def _release(
    db: Session, transition: PolicyTransition, now: datetime, metadata: dict
) -> None:
    decision = db.get(PolicyDecision, transition.decision_id)
    if now < _aware_utc(decision.decided_at) + timedelta(
        minutes=transition.elapsed_min
    ):
        return
    engagement, miss = 0.0, 0.0
    if decision.action:
        sent = (
            db.get(Event, decision.outcome_event_id)
            if decision.outcome_event_id
            else None
        )
        if (
            sent is None
            or sent.is_deleted
            or sent.is_virtual
            or sent.event_name != "notification_sent"
            or _aware_utc(sent.created_at) > now
            or _aware_utc(sent.event_start_time) > now
        ):
            timeout = float(metadata.get("delivery_timeout_min", 60))
            if now >= _aware_utc(decision.decided_at) + timedelta(minutes=timeout):
                # An unconfirmed plan is not evidence of an unopened delivery.
                transition.status = "undelivered"
                transition.released_at = now
            return
        end = _aware_utc(sent.event_start_time) + timedelta(
            minutes=float(metadata["response_window_min"])
        )
        if now < end:
            return
        opens = db.exec(
            select(Event)
            .where(
                Event.user_id == decision.user_id,
                Event.event_name == "notification_opened",
                Event.event_start_time >= sent.event_start_time,
                Event.event_start_time < _to_naive_utc(end),
                Event.created_at <= now,
                Event.is_deleted == False,
                Event.is_virtual == False,
            )
            .order_by(Event.event_start_time, Event.id)
        ).all()
        used = set(
            db.exec(
                select(PolicyTransition.opened_event_id).where(
                    PolicyTransition.user_id == decision.user_id,
                    PolicyTransition.opened_event_id != None,
                )
            ).all()
        )
        opened = next(
            (
                row
                for row in opens
                if row.id not in used and _identity(row) == _identity(sent)
            ),
            None,
        )
        transition.outcome_event_id = sent.id
        if opened is None:
            miss = -1.0
        else:
            delay = (
                _aware_utc(opened.event_start_time) - _aware_utc(sent.event_start_time)
            ).total_seconds() / 60
            engagement = (1 - float(metadata["rating_weight"])) * np.exp(
                -float(metadata["delay_decay"]) * min(delay, 60)
            )
            transition.opened_event_id = opened.id
    config = TrainingConfig.from_metadata(metadata)
    transition.objectives = {"engagement": float(engagement), "miss": miss}
    transition.reward = float(
        config.weight_engagement * engagement + config.weight_miss * miss
    )
    transition.status = "released"
    transition.released_at = now


def _replay_row(transition: PolicyTransition) -> dict:
    return {
        key: getattr(transition, key)
        for key in (
            "state",
            "next_state",
            "action",
            "safe_mask",
            "next_safe_mask",
            "elapsed_min",
            "bootstrap",
            "objectives",
        )
    }


def _stream(user_id, artifact):
    return select(PolicyTransition).where(
        PolicyTransition.user_id == user_id,
        PolicyTransition.device == artifact.metadata["device"],
        PolicyTransition.base_version == artifact.version,
    )


def _stored_learner(db, user_id, artifact):
    return db.exec(
        select(PolicyLearner).where(
            PolicyLearner.user_id == user_id,
            PolicyLearner.device == artifact.metadata["device"],
            PolicyLearner.base_version == artifact.version,
        )
    ).first()


def _restore_ledger(db, user_id, artifact, learner):
    cursor = learner.replay_count
    rows = db.exec(
        _stream(user_id, artifact)
        .where(
            PolicyTransition.replay_index
            > max(0, cursor - learner.config.replay_capacity),
            PolicyTransition.replay_index <= cursor,
        )
        .order_by(PolicyTransition.replay_index)
    ).all()
    learner.restore_replay([_replay_row(row) for row in rows])


def restore_policy_at(db: Session, user_id, artifact: PolicyArtifact, at: datetime):
    """Return an independent trainable learner as of the last committed change by at."""
    at = _aware_utc(at)
    lock_user(db, user_id)
    stored = _stored_learner(db, user_id, artifact)
    snapshot = (
        db.exec(
            select(PolicyCheckpoint)
            .where(
                PolicyCheckpoint.learner_id == stored.id,
                PolicyCheckpoint.saved_at <= at,
            )
            .order_by(PolicyCheckpoint.saved_at.desc())
        ).first()
        if stored
        else None
    )
    if snapshot is None:
        raise ValueError("No participant checkpoint at or before the requested time")
    learner = ConstrainedConservativeDoubleDQN(
        artifact, seed=participant_seed(user_id, artifact)
    )
    learner.restore(snapshot.checkpoint)
    _restore_ledger(db, user_id, artifact, learner)
    # Between optimizer snapshots only the replay ring and its update credits change.
    later = db.exec(
        _stream(user_id, artifact)
        .where(
            PolicyTransition.replay_index > learner.replay_count,
            PolicyTransition.consumed_at <= at,
        )
        .order_by(PolicyTransition.replay_index)
    ).all()
    for row in later:
        if row.replay_index != learner.replay_count + 1:
            raise ValueError("Gap in participant replay ledger")
        learner.store([_replay_row(row)])
    learner.owed += len(later) * learner.config.updates_per_epoch
    return learner


def prepare_policy(
    db: Session, user_id, artifact: PolicyArtifact, epoch: datetime
) -> PolicyArtifact:
    """Recover the exact optimizer, fit released rewards once, and serve its revision."""
    mode = learning_mode()
    lock_user(db, user_id)
    epoch = _aware_utc(epoch)
    stored = _stored_learner(db, user_id, artifact) if mode == "online" else None
    if stored and epoch < _aware_utc(stored.updated_at):
        raise ValueError("Policy time precedes the current checkpoint")
    stream = _stream(user_id, artifact)
    pending = db.exec(
        stream.where(PolicyTransition.status == "pending").order_by(
            PolicyTransition.created_at, PolicyTransition.id
        )
    ).all()
    for row in pending:
        _release(db, row, epoch, artifact.metadata)
    db.flush()
    if mode == "frozen":
        return artifact
    if (
        TrainingConfig.from_metadata(artifact.metadata).offline_batch_share
        and not artifact.offline
    ):
        raise ValueError("Online learning requires the shared device replay archive")
    learner = ConstrainedConservativeDoubleDQN(
        artifact, seed=participant_seed(user_id, artifact)
    )
    if stored is None:
        stored = PolicyLearner(
            user_id=user_id,
            device=artifact.metadata["device"],
            base_version=artifact.version,
            checkpoint=learner.checkpoint(),
            started_at=epoch,
            updated_at=epoch,
            policy_timezone=os.getenv("POLICY_TIMEZONE", "Europe/Berlin"),
        )
        db.add(stored)
        db.flush()
        db.add(
            PolicyCheckpoint(
                learner_id=stored.id, saved_at=epoch, checkpoint=stored.checkpoint
            )
        )
    else:
        learner.restore(stored.checkpoint)
    fresh = db.exec(
        stream.where(
            PolicyTransition.status == "released",
            PolicyTransition.released_at <= epoch,
            PolicyTransition.consumed_at == None,
        ).order_by(PolicyTransition.released_at, PolicyTransition.id)
    ).all()
    zone = ZoneInfo(stored.policy_timezone)
    day = (
        epoch.astimezone(zone).date()
        - _aware_utc(stored.started_at).astimezone(zone).date()
    ).days
    previous_refit = learner.refit_day
    cursor = learner.replay_count
    if fresh or learner.refit_due(learner.replay_count, day):
        _restore_ledger(db, user_id, artifact, learner)
        updates = []

        def record_update(details):
            updates.append(dict(details, learner_id=stored.id, fitted_at=epoch))
            if len(updates) == 100:
                db.execute(PolicyTrainingUpdate.__table__.insert(), updates)
                updates.clear()

        learner.learn([_replay_row(row) for row in fresh], day, record_update)
        if updates:
            db.execute(PolicyTrainingUpdate.__table__.insert(), updates)
    for index, row in enumerate(fresh, cursor + 1):
        row.replay_index = index
        row.consumed_at = epoch
    if fresh or learner.refit_day != previous_refit:
        stored.checkpoint = learner.checkpoint()
    if learner.refit_day != previous_refit:
        db.merge(
            PolicyCheckpoint(
                learner_id=stored.id, saved_at=epoch, checkpoint=stored.checkpoint
            )
        )
    stored.updated_at = epoch
    metadata = dict(
        artifact.metadata, learner_revision=learner.revision, learning_day=day
    )
    return PolicyArtifact(metadata, learner.online.weights, learner.online.biases)


def participant_seed(user_id, artifact: PolicyArtifact) -> int:
    """Give each participant a reproducible training stream."""
    key = f"{user_id}:{artifact.metadata.get('seed', 0)}"
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
