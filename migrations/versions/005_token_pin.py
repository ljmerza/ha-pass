"""Add an optional per-token PIN to tokens.

NULL means "no PIN" — the default, and the state every existing token starts in.
A non-NULL value is a bcrypt hash, never the PIN itself: the admin can set,
replace, or clear a PIN but never read one back, so a forgotten PIN is replaced
rather than recovered.

The column doubles as the key material for the guest PIN session cookie (see
app/guest_pin.py), which is why there is no sessions table here — changing or
clearing the PIN changes the hash and invalidates outstanding sessions on its
own, with nothing to expire and nothing to clean up.

Revision ID: 005
Revises: 004
Create Date: 2026-09-19
"""
from typing import Sequence, Union

from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(tokens)")]
    if "pin_hash" not in cols:
        op.execute("ALTER TABLE tokens ADD COLUMN pin_hash TEXT")


def downgrade() -> None:
    cols = [r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(tokens)")]
    if "pin_hash" in cols:
        op.execute("ALTER TABLE tokens DROP COLUMN pin_hash")
