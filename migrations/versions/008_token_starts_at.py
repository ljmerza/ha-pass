"""Add an optional scheduled start to tokens.

NULL means "active from the moment it was created" — the default, and the state
every existing token starts in. A non-NULL value is the epoch second the link
begins working, so a booking confirmed weeks ahead can be created and sent now
without granting access until check-in.

The column is also the activation switch: "Activate Now" writes NULL back,
which is the same row state a token that was never scheduled has. There is no
separate "activated" flag to keep in step with it.

A start time in the past is never stored — the admin router normalises one to
NULL before the insert — so `starts_at IS NOT NULL AND starts_at > now` and the
simpler `starts_at > now` agree, and a token is pending or it is not.

Revision ID: 008
Revises: 007
Create Date: 2026-09-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(tokens)")]
    if "starts_at" not in cols:
        op.execute("ALTER TABLE tokens ADD COLUMN starts_at INTEGER")


def downgrade() -> None:
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(tokens)")]
    if "starts_at" in cols:
        op.execute("ALTER TABLE tokens DROP COLUMN starts_at")
