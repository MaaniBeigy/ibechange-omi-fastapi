"""Export a trainable participant checkpoint as of a UTC or offset-qualified time."""

import argparse
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlmodel import Session, select

from app.models.policy_learning import PolicyLearner
from app.rl.checkpoint import save_checkpoint
from app.services.policy_learning_service import restore_policy_at
from app.services.rl_policy import load_policy_artifacts


def export_checkpoint(db, user_id, artifact, at, output):
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
    save_checkpoint(
        output,
        dict(
            user_id=str(user_id),
            device=artifact.metadata["device"],
            as_of=at.isoformat(),
            started_at=stored.started_at.isoformat(),
            policy_timezone=stored.policy_timezone,
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
    args = parser.parse_args()
    with Session(engine) as db:
        export_checkpoint(
            db, args.user, load_policy_artifacts()[args.device], args.at, args.out
        )


if __name__ == "__main__":  # pragma: no cover
    main()
