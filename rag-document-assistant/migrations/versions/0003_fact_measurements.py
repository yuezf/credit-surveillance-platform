"""Record financial-fact measurement definitions without guessing legacy values."""

from alembic import op
import sqlalchemy as sa


revision = "0003_fact_measurements"
down_revision = "0002_tenant_api_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("financial_facts", sa.Column("measurement_basis", sa.String(20)))
    op.add_column("financial_facts", sa.Column("measurement_start", sa.Date()))
    op.add_column("financial_facts", sa.Column("measurement_end", sa.Date()))
    op.create_check_constraint(
        "measurement_definition",
        "financial_facts",
        "(measurement_basis IS NULL AND measurement_start IS NULL "
        "AND measurement_end IS NULL) OR "
        "(measurement_basis IS NOT NULL AND measurement_end IS NOT NULL AND ("
        "(measurement_basis = 'point_in_time' AND measurement_start IS NULL) OR "
        "(measurement_basis = 'ttm' AND measurement_start IS NOT NULL "
        "AND measurement_start <= measurement_end)))",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_financial_facts_measurement_definition"), "financial_facts", type_="check")
    op.drop_column("financial_facts", "measurement_end")
    op.drop_column("financial_facts", "measurement_start")
    op.drop_column("financial_facts", "measurement_basis")
