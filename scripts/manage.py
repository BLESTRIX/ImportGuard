"""Small operational commands. Run with: uv run python -m scripts.manage --help."""

import argparse
import getpass
import os
from uuid import UUID

from pydantic import EmailStr, TypeAdapter
from sqlalchemy import select

from app.core.config import settings
from app.db.session import session_factory
from app.models import ImportJob, Membership, Organization, User
from app.schemas.requests import Register
from app.services.auth import hasher, register
from app.services.imports import cleanup_orphans
from app.services.processing import SimulatedWorkerCrash, discover, run_job


def add_member(db, org_id, email, role, password):
    email = str(TypeAdapter(EmailStr).validate_python(email)).lower()
    if not 12 <= len(password) <= 128:
        raise SystemExit("Use a password of 12–128 characters.")
    if not db.get(Organization, org_id):
        raise SystemExit("Organization not found.")
    user = db.scalar(select(User).where(User.email == email))
    if user:
        raise SystemExit("Email already exists. Refusing to change an existing account.")
    user = User(email=email, password_hash=hasher.hash(password))
    db.add(user)
    db.flush()
    db.add(Membership(organization_id=org_id, user_id=user.id, role=role))
    db.commit()
    print(f"Created {role} member {email} in {org_id}")


def seed(db):
    if settings().app_env == "production" or not settings().demo_seed_enabled:
        raise SystemExit(
            "Set DEMO_SEED_ENABLED=true explicitly in development to create demo accounts."
        )
    password = "ImportGuard-demo-2026!"
    for org_name, prefix in (("North Market", "north"), ("South Supply", "south")):
        email = f"admin@{prefix}.example.com"
        user = db.scalar(select(User).where(User.email == email))
        if user:
            print(f"Skipping existing demo organization for {email}")
            continue
        register(
            db,
            Register(email=email, password=password, organization_name=org_name, currency="USD"),
            "demo-seed-csrf",
        )
        user = db.scalar(select(User).where(User.email == email))
        membership = db.scalar(select(Membership).where(Membership.user_id == user.id))
        for role in ("EDITOR", "VIEWER"):
            add_member(
                db,
                membership.organization_id,
                f"{role.lower()}@{prefix}.example.com",
                role,
                password,
            )
        print(f"Demo org {org_name}: {membership.organization_id}; admin: {email}")
    print(f"Development-only demo password: {password}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed", help="Create two development organizations and all three roles")
    member = commands.add_parser("add-member", help="Create an additional organization member")
    member.add_argument("organization_id", type=UUID)
    member.add_argument("email")
    member.add_argument("--role", choices=["ADMIN", "EDITOR", "VIEWER"], default="VIEWER")
    work = commands.add_parser(
        "work-once", help="Process available PostgreSQL jobs without a broker"
    )
    work.add_argument("--job-id", type=UUID, help="Process only this job")
    commands.add_parser(
        "cleanup-orphans", help="Clean unreferenced storage reservations older than 24 hours"
    )
    failure = commands.add_parser("failpoint", help="Arm a one-shot development recovery failpoint")
    failure.add_argument("import_id", type=UUID)
    failure.add_argument("name", choices=["crash_after_batch", "apply_after_first_write"])
    args = parser.parse_args()
    with session_factory()() as db:
        if args.command == "seed":
            seed(db)
        elif args.command == "add-member":
            password = os.environ.get("IMPORTGUARD_MEMBER_PASSWORD") or getpass.getpass(
                "New member password: "
            )
            add_member(db, args.organization_id, args.email, args.role, password)
        elif args.command == "cleanup-orphans":
            print(f"Cleaned {cleanup_orphans(db)} orphan objects")
        elif args.command == "work-once":
            pending = [args.job_id] if args.job_id else discover(db)
            db.rollback()
            for job_id in pending:
                try:
                    run_job(job_id)
                except SimulatedWorkerCrash as exc:
                    print(str(exc))
                    raise SystemExit(75) from None
            print(f"Processed {len(pending)} available job wake-ups")
        elif args.command == "failpoint":
            if not settings().enable_failure_injection or settings().app_env == "production":
                raise SystemExit("Set ENABLE_FAILURE_INJECTION=true in development first.")
            item = db.get(ImportJob, args.import_id)
            if not item:
                raise SystemExit("Import not found.")
            if item.status in ("APPLIED", "APPLYING", "CANCELED"):
                raise SystemExit("Cannot arm a failpoint in this state.")
            item.failpoint = args.name
            db.commit()
            print(f"Armed {args.name} for import {item.id}")


if __name__ == "__main__":
    main()
