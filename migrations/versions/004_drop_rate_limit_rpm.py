"""Drop the unused rate_limit_rpm column from tokens.

The column has been NOT NULL DEFAULT 30 since 001 but nothing has ever read or
written it — guest limits live in app/routers/guest.py. It now also can't
express the limits it appears to configure: the command path enforces a burst
window plus a sustained one, which a single RPM integer has no way to describe.

Revision ID: 004
Revises: 003
Create Date: 2026-09-19
"""
from typing import Sequence, Union

from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(tokens)")]
    if "rate_limit_rpm" in cols:
        op.execute("ALTER TABLE tokens DROP COLUMN rate_limit_rpm")


def downgrade() -> None:
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(tokens)")]
    if "rate_limit_rpm" not in cols:
        op.execute("ALTER TABLE tokens ADD COLUMN rate_limit_rpm INTEGER NOT NULL DEFAULT 30")
