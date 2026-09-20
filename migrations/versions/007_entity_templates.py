"""Add named, reusable entity selections for the token picker.

An admin who hands out the same set of entities to every guest had to find each
one again in the picker. A template is that selection saved under a name and
replayed into a later token.

Deliberately entity IDs and nothing else. The per-entity overrides
token_entities carries — display_name, the `options` blob, require_proximity —
are decisions about one guest link, not about a reusable set: a name like
"Sophia's Room" belongs to the token it was written for, and require_proximity
is an access control that must be chosen per link rather than arriving silently
with a template. So a template answers "which entities", and the token keeps
answering "and how do they behave".

The list is a JSON blob in one column rather than a join table because nothing
queries across it — it is read whole, written whole, and never filtered on.

`name` is COLLATE NOCASE so "Ground Floor" and "ground floor" are the same
template, which is what an admin typing a name back in expects.

Revision ID: 007
Revises: 006
Create Date: 2026-09-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS entity_templates (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL COLLATE NOCASE UNIQUE,
            entity_ids  TEXT NOT NULL,
            created_at  INTEGER NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS entity_templates")
