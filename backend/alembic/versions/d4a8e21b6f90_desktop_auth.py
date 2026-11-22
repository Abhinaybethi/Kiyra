"""desktop_auth (auth_accounts / auth_otps / auth_sessions)

Revision ID: d4a8e21b6f90
Revises: c7f3a91d2e40
Create Date: 2026-11-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a8e21b6f90'
down_revision: Union[str, Sequence[str], None] = 'c7f3a91d2e40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _exists(bind, table: str) -> bool:
    return table in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    """Upgrade schema (idempotent: startup also runs Base.metadata.create_all)."""
    bind = op.get_bind()

    if not _exists(bind, 'auth_accounts'):
        op.create_table(
            'auth_accounts',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('full_name', sa.String(length=200), nullable=False),
            sa.Column('phone', sa.String(length=32), nullable=False),
            sa.Column('email', sa.String(length=255), nullable=False),
            sa.Column('is_verified', sa.Boolean(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('last_login_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_auth_accounts_email', 'auth_accounts', ['email'], unique=True)

    if not _exists(bind, 'auth_otps'):
        op.create_table(
            'auth_otps',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('email', sa.String(length=255), nullable=False),
            sa.Column('purpose', sa.String(length=20), nullable=False),
            sa.Column('code_hash', sa.String(length=128), nullable=False),
            sa.Column('attempts', sa.Integer(), nullable=False),
            sa.Column('max_attempts', sa.Integer(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('consumed_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_auth_otps_email', 'auth_otps', ['email'])
        op.create_index('ix_auth_otps_created', 'auth_otps', ['created_at'])

    if not _exists(bind, 'auth_sessions'):
        op.create_table(
            'auth_sessions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('token_hash', sa.String(length=128), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('last_seen_at', sa.DateTime(), nullable=True),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['account_id'], ['auth_accounts.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_auth_sessions_token_hash', 'auth_sessions', ['token_hash'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if _exists(bind, 'auth_sessions'):
        op.drop_index('ix_auth_sessions_token_hash', table_name='auth_sessions')
        op.drop_table('auth_sessions')
    if _exists(bind, 'auth_otps'):
        op.drop_index('ix_auth_otps_created', table_name='auth_otps')
        op.drop_index('ix_auth_otps_email', table_name='auth_otps')
        op.drop_table('auth_otps')
    if _exists(bind, 'auth_accounts'):
        op.drop_index('ix_auth_accounts_email', table_name='auth_accounts')
        op.drop_table('auth_accounts')
