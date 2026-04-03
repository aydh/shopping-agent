"""add_not_found_to_products

Revision ID: 4054b9e11b62
Revises: 7202881a0dca
Create Date: 2026-04-03 17:47:42.643354

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '4054b9e11b62'
down_revision: Union[str, Sequence[str], None] = '7202881a0dca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('products', sa.Column('not_found', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('products', 'not_found')
