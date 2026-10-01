import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.models import Borrower, ReportingPeriod, Tenant


DEMO_TENANT_ID = uuid.uuid5(uuid.NAMESPACE_URL, "rag-credit:tenant:demo")
DEMO_BORROWER_ID = uuid.uuid5(
    uuid.NAMESPACE_URL,
    f"rag-credit:borrower:{DEMO_TENANT_ID}:example-corp",
)
DEMO_PRIOR_PERIOD_ID = uuid.uuid5(
    uuid.NAMESPACE_URL,
    f"rag-credit:period:{DEMO_BORROWER_ID}:2025-Q1",
)
DEMO_CURRENT_PERIOD_ID = uuid.uuid5(
    uuid.NAMESPACE_URL,
    f"rag-credit:period:{DEMO_BORROWER_ID}:2025-Q2",
)


@dataclass(frozen=True)
class DemoScope:
    tenant_id: uuid.UUID
    borrower_id: uuid.UUID
    prior_period_id: uuid.UUID
    current_period_id: uuid.UUID


def seed_demo_scope(session: Session) -> DemoScope:
    """Create the deterministic demo tenant, borrower, and periods if absent."""
    try:
        tenant = session.get(Tenant, DEMO_TENANT_ID)
        if tenant is None:
            tenant = Tenant(id=DEMO_TENANT_ID, name="Demo Tenant")
            session.add(tenant)
            session.flush()

        borrower = session.get(Borrower, DEMO_BORROWER_ID)
        if borrower is None:
            borrower = Borrower(
                id=DEMO_BORROWER_ID,
                tenant_id=DEMO_TENANT_ID,
                legal_name="Example Corp",
                external_id="example-corp",
            )
            session.add(borrower)
            session.flush()

        _add_period_if_missing(
            session,
            period_id=DEMO_PRIOR_PERIOD_ID,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 3, 31),
            fiscal_quarter=1,
            label="Q1 2025",
        )
        _add_period_if_missing(
            session,
            period_id=DEMO_CURRENT_PERIOD_ID,
            period_start=date(2025, 4, 1),
            period_end=date(2025, 6, 30),
            fiscal_quarter=2,
            label="Q2 2025",
        )
        session.commit()
    except Exception:
        session.rollback()
        raise

    return DemoScope(
        tenant_id=DEMO_TENANT_ID,
        borrower_id=DEMO_BORROWER_ID,
        prior_period_id=DEMO_PRIOR_PERIOD_ID,
        current_period_id=DEMO_CURRENT_PERIOD_ID,
    )


def _add_period_if_missing(
    session: Session,
    *,
    period_id: uuid.UUID,
    period_start: date,
    period_end: date,
    fiscal_quarter: int,
    label: str,
) -> None:
    if session.get(ReportingPeriod, period_id) is not None:
        return

    session.add(
        ReportingPeriod(
            id=period_id,
            tenant_id=DEMO_TENANT_ID,
            borrower_id=DEMO_BORROWER_ID,
            period_type="quarter",
            period_start=period_start,
            period_end=period_end,
            fiscal_year=2025,
            fiscal_quarter=fiscal_quarter,
            label=label,
        )
    )
    session.flush()


def main() -> None:
    from app.database import SessionLocal

    with SessionLocal() as session:
        scope = seed_demo_scope(session)

    print(f"Demo tenant: {scope.tenant_id}")
    print(f"Example Corp: {scope.borrower_id}")
    print(f"Prior period: {scope.prior_period_id}")
    print(f"Current period: {scope.current_period_id}")


if __name__ == "__main__":
    main()
