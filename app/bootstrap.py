"""Initialize fresh installations and migrate existing OMI databases once."""

from alembic import command
from alembic.config import Config
from sqlmodel import SQLModel

import app.models


def main() -> None:
    from app.dependencies import engine

    # create_all supplies the pre-Alembic baseline on fresh installations only;
    # Alembic remains responsible for changing existing tables.
    SQLModel.metadata.create_all(engine)
    command.upgrade(Config("alembic.ini"), "head")


if __name__ == "__main__":  # pragma: no cover
    main()
