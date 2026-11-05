"""업로드·병합·내보내기 작업 기록."""
from alembic import op
import sqlalchemy as sa

revision = "0003_processing"
down_revision = "0002_cases"
branch_labels = depends_on = None


def upgrade():
    op.create_table("processing_records",
                    sa.Column("id", sa.String(48), primary_key=True),
                    sa.Column("case_id", sa.Integer(), sa.ForeignKey("cases.id"), nullable=False),
                    sa.Column("kind", sa.String(16), nullable=False),
                    sa.Column("payload", sa.JSON(), nullable=False))
    op.create_index("ix_processing_records_case_id", "processing_records", ["case_id"])


def downgrade():
    op.drop_index("ix_processing_records_case_id", table_name="processing_records")
    op.drop_table("processing_records")
