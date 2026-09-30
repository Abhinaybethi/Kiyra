"""session_memory (session_turns / session_memories / session_retrievals)

Revision ID: c7f3a91d2e40
Revises: b8831c6c4c55
Create Date: 2026-09-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7f3a91d2e40'
down_revision: Union[str, Sequence[str], None] = 'b8831c6c4c55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _exists(bind, table: str) -> bool:
    return table in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    """Upgrade schema (idempotent: startup also runs Base.metadata.create_all)."""
    bind = op.get_bind()

    if not _exists(bind, 'session_turns'):
        op.create_table(
            'session_turns',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('session_id', sa.Integer(), nullable=False),
            sa.Column('turn_index', sa.Integer(), nullable=False),
            sa.Column('question', sa.Text(), nullable=False),
            sa.Column('answer', sa.Text(), nullable=False),
            sa.Column('resolved_question', sa.Text(), nullable=True),
            sa.Column('topic', sa.String(length=200), nullable=True),
            sa.Column('entities', sa.JSON(), nullable=True),
            sa.Column('follow_up', sa.Boolean(), nullable=False),
            sa.Column('answered_at', sa.DateTime(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['session_id'], ['interview_sessions.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_session_turns_session', 'session_turns', ['session_id'])

    if not _exists(bind, 'session_memories'):
        op.create_table(
            'session_memories',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('session_id', sa.Integer(), nullable=False),
            sa.Column('active_topic', sa.String(length=200), nullable=False),
            sa.Column('compact_summary', sa.Text(), nullable=False),
            sa.Column('important_entities', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['session_id'], ['interview_sessions.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('session_id'),
        )
        op.create_index('ix_session_memories_session', 'session_memories', ['session_id'])

    if not _exists(bind, 'session_retrievals'):
        op.create_table(
            'session_retrievals',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('session_id', sa.Integer(), nullable=False),
            sa.Column('topic', sa.String(length=200), nullable=True),
            sa.Column('query', sa.Text(), nullable=False),
            sa.Column('context', sa.Text(), nullable=False),
            sa.Column('metrics', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['session_id'], ['interview_sessions.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_session_retrievals_session', 'session_retrievals', ['session_id'])


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if _exists(bind, 'session_retrievals'):
        op.drop_index('ix_session_retrievals_session', table_name='session_retrievals')
        op.drop_table('session_retrievals')
    if _exists(bind, 'session_memories'):
        op.drop_index('ix_session_memories_session', table_name='session_memories')
        op.drop_table('session_memories')
    if _exists(bind, 'session_turns'):
        op.drop_index('ix_session_turns_session', table_name='session_turns')
        op.drop_table('session_turns')
