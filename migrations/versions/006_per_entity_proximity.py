"""Add a per-entity proximity requirement to token_entities.

0 means "no location needed" — the default, and the state every existing row
starts in. 1 means a command for that one entity is only forwarded when the
guest's browser reports a recent position inside HA's zone.home, so a token can
gate input_button.open_door without also gating the living-room lamp.

This is a column rather than a key in the neighbouring `options` blob on
purpose. That blob is presentation-only and nothing in the command path reads
it; this is read by the command path on every gated tap, and keeping the two
apart is what stops a display toggle from ever being mistaken for a control.

Revision ID: 006
Revises: 005
Create Date: 2026-09-19
"""
from typing import Sequence, Union

from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(token_entities)")]
    if "require_proximity" not in cols:
        op.execute(
            "ALTER TABLE token_entities "
            "ADD COLUMN require_proximity INTEGER NOT NULL DEFAULT 0"
        )


def downgrade() -> None:
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(token_entities)")]
    if "require_proximity" in cols:
        op.execute("ALTER TABLE token_entities DROP COLUMN require_proximity")
