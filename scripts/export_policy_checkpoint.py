"""Export a trainable participant checkpoint as of a UTC or offset-qualified time."""

import argparse
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlmodel import Session, select

from app.models.policy_decision import PolicyDecision
from app.models.policy_learning import (
    PolicyLearner,
    PolicyTrainingUpdate,
    PolicyTransition,
)
from app.rl.checkpoint import save_checkpoint
from app.services.policy_learning_service import restore_policy_at
from app.services.rl_policy import load_policy_artifacts


def export_checkpoint(db, user_id, artifact, at, output, include_audit=False):
    if at.tzinfo is None:
        raise ValueError("Checkpoint time requires a UTC offset")
    learner = restore_policy_at(db, user_id, artifact, at)
    stored = db.exec(
        select(PolicyLearner).where(
            PolicyLearner.user_id == user_id,
            PolicyLearner.base_version == artifact.version,
            PolicyLearner.device == artifact.metadata["device"],
        )
    ).one()
    audit = {}
    if include_audit:
        updates = db.exec(
            select(PolicyTrainingUpdate)
            .where(
                PolicyTrainingUpdate.learner_id == stored.id,
                PolicyTrainingUpdate.update_number <= learner.online.updates,
            )
            .order_by(PolicyTrainingUpdate.update_number)
        ).all()
        personal = db.exec(
            select(PolicyTransition)
            .where(
                PolicyTransition.user_id == user_id,
                PolicyTransition.device == stored.device,
                PolicyTransition.base_version == stored.base_version,
                PolicyTransition.replay_index <= learner.replay_count,
            )
            .order_by(PolicyTransition.replay_index)
        ).all()
        decisions = db.exec(
            select(PolicyDecision)
            .where(
                PolicyDecision.user_id == user_id,
                PolicyDecision.decided_at <= at,
                PolicyDecision.policy_version.startswith(
                    stored.base_version + "-", autoescape=True
                ),
            )
            .order_by(PolicyDecision.decided_at, PolicyDecision.id)
        ).all()
        audit = dict(
            training_updates=[u.model_dump(mode="json") for u in updates],
            personal_transitions=[t.model_dump(mode="json") for t in personal],
            decisions=[
                d.model_dump(mode="json", exclude={"outcome_event_id", "superseded_by"})
                for d in decisions
            ],
        )
    save_checkpoint(
        output,
        dict(
            user_id=str(user_id),
            device=artifact.metadata["device"],
            as_of=at.isoformat(),
            started_at=stored.started_at.isoformat(),
            policy_timezone=stored.policy_timezone,
            **audit,
            **learner.portable_checkpoint()
        ),
    )


def main():
    from app.dependencies import engine

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, type=UUID)
    parser.add_argument("--device", required=True, choices=("wearer", "nonwearer"))
    parser.add_argument("--at", required=True, type=datetime.fromisoformat)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--include-audit",
        action="store_true",
        help="Include decisions, consumed transitions, and recorded optimizer updates through the cutoff",
    )
    args = parser.parse_args()
    with Session(engine) as db:
        export_checkpoint(
            db,
            args.user,
            load_policy_artifacts()[args.device],
            args.at,
            args.out,
            args.include_audit,
        )


if __name__ == "__main__":  # pragma: no cover
    main()
