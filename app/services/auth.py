import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DBSession

from app.core.config import settings
from app.core.errors import Problem
from app.models import LoginAttempt, Membership, Organization, Session, User, now

hasher = PasswordHasher()  # argon2-cffi defaults to Argon2id.
DUMMY_HASH = hasher.hash("dummy-password-for-constant-work")


def digest(token: str):
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    organization_id: UUID
    role: str
    email: str
    organization_name: str
    currency: str

    def require(self, *roles):
        if self.role not in roles:
            raise Problem("forbidden", "Your role does not permit this action.", 403)


def principal_for(db: DBSession, token: str | None, csrf: str | None = None) -> Principal:
    if not token:
        raise Problem("unauthenticated", "Please sign in.", 401)
    row = db.execute(
        select(Session, Membership, User, Organization)
        .join(
            Membership,
            (Membership.user_id == Session.user_id)
            & (Membership.organization_id == Session.organization_id),
        )
        .join(User, User.id == Session.user_id)
        .join(Organization, Organization.id == Session.organization_id)
        .where(Session.token_hash == digest(token), Session.expires_at > func.now())
    ).first()
    if not row:
        raise Problem("unauthenticated", "Your session has expired. Please sign in.", 401)
    session, membership, user, org = row
    if csrf is not None and not secrets.compare_digest(session.csrf_hash, digest(csrf)):
        raise Problem("csrf", "The session CSRF token is invalid. Reload the page.", 403)
    return Principal(user.id, org.id, membership.role, user.email, org.name, org.currency)


def new_session(db, user_id, organization_id, csrf):
    token = secrets.token_urlsafe(32)
    db.add(
        Session(
            user_id=user_id,
            organization_id=organization_id,
            token_hash=digest(token),
            csrf_hash=digest(csrf),
            expires_at=now() + timedelta(hours=settings().session_hours),
        )
    )
    db.commit()
    return token


def register(db, data, csrf):
    if db.scalar(select(User.id).where(User.email == str(data.email).lower())):
        raise Problem("email_taken", "This email is already registered.", 409)
    org = Organization(name=data.organization_name, currency=data.currency)
    user = User(email=str(data.email).lower(), password_hash=hasher.hash(data.password))
    db.add_all([org, user])
    db.flush()
    db.add(Membership(organization_id=org.id, user_id=user.id, role="ADMIN"))
    db.flush()
    return new_session(db, user.id, org.id, csrf)


def login(db, data, csrf, ip):
    # Persist limits before any authentication error; no Redis dependency for sign-in.
    for identifier in ("email:" + str(data.email).lower(), "ip:" + ip):
        key = digest(identifier)
        start = now()
        old = db.scalar(select(LoginAttempt).where(LoginAttempt.key == key).with_for_update())
        if not old:
            db.execute(
                insert(LoginAttempt)
                .values(key=key, attempts=0, window_start=start)
                .on_conflict_do_nothing()
            )
            old = db.scalar(select(LoginAttempt).where(LoginAttempt.key == key).with_for_update())
        if old.window_start < start - timedelta(minutes=15):
            old.attempts, old.window_start = 0, start
        limit = 10 if identifier.startswith("email:") else 50
        if old.attempts >= limit:
            db.commit()
            raise Problem(
                "rate_limited", "Too many sign-in attempts. Try again in 15 minutes.", 429
            )
        old.attempts += 1
        db.commit()
    user = db.scalar(select(User).where(User.email == str(data.email).lower()))
    try:
        hasher.verify(user.password_hash if user else DUMMY_HASH, data.password)
    except (VerificationError, InvalidHashError):
        raise Problem("invalid_credentials", "Email or password is incorrect.", 401) from None
    if user is None:
        raise Problem("invalid_credentials", "Email or password is incorrect.", 401)
    membership = db.scalar(
        select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at)
    )
    if not membership:
        raise Problem("forbidden", "No organization membership is available.", 403)
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hasher.hash(data.password)
    return new_session(db, user.id, membership.organization_id, csrf)


def logout(db, token):
    db.execute(delete(Session).where(Session.token_hash == digest(token)))
    db.commit()
