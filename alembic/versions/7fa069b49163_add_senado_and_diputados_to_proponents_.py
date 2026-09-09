"""add senado and diputados to proponents enum

Revision ID: 7fa069b49163
Revises: e854aca48cca
Create Date: 2026-09-08 23:04:18.229939

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "7fa069b49163"
down_revision: Union[str, Sequence[str], None] = "e854aca48cca"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 2026-2031 bicameral term (backend/core/enums.py Proponents.SENADO /
# Proponents.DIPUTADOS, added in 9751ea0) added these two values to the
# Python enum but never to the Postgres native enum type, so any bill/motion
# proposed by a chamber under the bicameral term fails to insert with
# psycopg.errors.InvalidTextRepresentation.
NEW_PROPONENT_VALUES = (
    "Senado de la República",
    "Cámara de Diputados",
)


def upgrade() -> None:
    """Upgrade schema."""
    for value in NEW_PROPONENT_VALUES:
        op.execute(f"ALTER TYPE proponents ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError(
        "Postgres does not support removing enum values; rolling back the "
        "proponents value additions requires restoring from a pre-migration "
        "backup."
    )
