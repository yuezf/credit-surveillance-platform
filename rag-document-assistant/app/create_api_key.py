"""Issue a tenant API key from the command line."""

import argparse
import uuid

from app.auth import issue_api_key
from app.database import SessionLocal


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a tenant-scoped API key")
    parser.add_argument("--tenant-id", type=uuid.UUID, required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()

    with SessionLocal() as session:
        api_key = issue_api_key(session, args.tenant_id, args.label)

    print("Save this API key now; it cannot be retrieved from the database later:")
    print(api_key)


if __name__ == "__main__":
    main()
