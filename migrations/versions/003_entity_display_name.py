"""Add per-token display_name + options overrides to token_entities.

Guests see an entity's HA friendly_name by default. This column lets one token
call it something else ("Sophia's Room") without touching Home Assistant, and
lets two tokens word the same entity differently.

Nullable: NULL display_name means "fall back to friendly_name, then entity_id".

`options` is a JSON blob of per-entity display toggles (e.g. hiding a light's
brightness slider). It is a blob rather than a column-per-toggle because a second
toggle showed up before the first shipped, and each one is presentation-only —
nothing in the security path reads it.

Revision ID: 003
Revises: 002
Create Date: 2026-09-19
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(token_entities)")]
    if "display_name" not in cols:
        op.execute("ALTER TABLE token_entities ADD COLUMN display_name TEXT")
    if "options" not in cols:
        op.execute("ALTER TABLE token_entities ADD COLUMN options TEXT")


def downgrade() -> None:
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(token_entities)")]
    if "display_name" in cols:
        op.execute("ALTER TABLE token_entities DROP COLUMN display_name")
    if "options" in cols:
        op.execute("ALTER TABLE token_entities DROP COLUMN options")
