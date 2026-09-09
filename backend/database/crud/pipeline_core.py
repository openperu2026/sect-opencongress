from __future__ import annotations

from sqlalchemy import select, or_, func
from sqlalchemy.orm import Session
from datetime import datetime, date
from dataclasses import dataclass
from typing import Type
from enum import Enum

from loguru import logger

from backend import TypeOrganization
from backend.config import settings
from backend.core.constants import WHOLE_CONGRESS_ORG_NAME
from backend.process.utils import normalize_name
from backend.database import models as db_models
from backend.process import schema
from backend.database.raw_models import ScraperRun


@dataclass
class ProcessStats:
    processed: int = 0
    skipped: int = 0
    errors: int = 0


@dataclass
class ScraperStats:
    start_time: datetime
    end_time: datetime
    scrapped: int = 0


MEMBERSHIP_MODELS = {
    TypeOrganization.BANCADA.value: db_models.BancadaMembership,
    TypeOrganization.PARTY.value: db_models.PartyMembership,
    TypeOrganization.CHAMBER.value: db_models.ChamberMembership,
    TypeOrganization.COMMITTEE.value: db_models.CommitteeMembership,
    TypeOrganization.ADMINISTRATIVE.value: db_models.AdminMembership,
}


def _enum_value(value: Enum | str) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _given_name_first(name: str) -> str:
    """
    Vote-roster/roll `full_name` values are transcribed as
    "SURNAME(S), GIVEN NAME(S)" (see system_prompt_bills.md Section 2b),
    but `Congresista.full_name` is stored "GIVEN NAME(S) SURNAME(S)".
    Jaro-Winkler is order-sensitive, so comparing the two as-is scores a
    correct match as low as ~0.70-0.76 -- well under the 0.9 threshold --
    purely because the words are reversed, not because the name is wrong.
    Reordering on the comma before the fuzzy comparison fixed 91% of the
    production name-match failure rate measured on 2026-08-18 (6.87% ->
    0.63% occurrence-weighted, verified against the full 75,905-occurrence
    dataset). Names without a comma are returned unchanged.
    """
    if "," not in name:
        return name
    surname, given = name.split(",", 1)
    return f"{given.strip()} {surname.strip()}"


def find_congresista(
    db: Session,
    name: str,
    website: str | None = None,
    *,
    congresista_id: int | None = None,
    threshold: float = 0.9,
) -> db_models.Congresista | None:
    """
    Find a congressperson using congresista_id, website, aliases, or fuzzy
    name matching.

    Matching is attempted in the following order:

    0. congresista_id exact match, when provided -- a stable, national,
       cross-term/cross-chamber person identifier mined from bill/motion
       firmantes data (confirmed live 2026-09-03: the same congresistaId
       persists across a legacy Diputados term and a later Senado term for
       the same reelected person). This is the only genuinely reliable
       signal in this cascade -- everything below it is a heuristic.
    1. Website exact match.
    2. Known alias exact match.
    3. Canonical full-name fuzzy match (Jaro-Winkler), after reordering a
       "SURNAME, GIVEN" input to "GIVEN SURNAME" (see `_given_name_first`).

    Args:
        db (Session): Active SQLAlchemy database session.
        name (str): Name of the congressperson.
        website (str | None, optional): Congressperson website URL.
        congresista_id (int | None, optional): Cross-term person identifier,
            when known (see backend/process/schema.py::Congresista's
            docstring for provenance). Tried first, before every other
            signal.
        threshold (float, optional): Minimum Jaro-Winkler similarity.
            Defaults to 0.9.

    Returns:
        db_models.Congresista | None: The matching congressperson if found;
        otherwise, None.
    """

    # 0. congresista_id (most reliable -- try first)
    if congresista_id is not None:
        by_congresista_id = db.scalar(
            select(db_models.Congresista).where(
                db_models.Congresista.congresista_id == congresista_id
            )
        )
        if by_congresista_id is not None:
            return by_congresista_id

    # 1. Website
    if website:
        by_web = db.scalar(
            select(db_models.Congresista).where(
                db_models.Congresista.website == website.strip()
            )
        )

        if by_web is not None:
            return by_web

    normalized_name = normalize_name(name, sort_tokens=True)

    if not normalized_name:
        return None

    # Known alias
    by_alias = db.scalar(
        select(db_models.Congresista)
        .join(db_models.CongresistaAlias)
        .where(db_models.CongresistaAlias.name == normalized_name)
    )

    if by_alias is not None:
        return by_alias

    # Fuzzy canonical name (preserve word order for Jaro-Winkler comparison,
    # after reordering "SURNAME, GIVEN" input to match full_name's own
    # "GIVEN SURNAME" order -- see _given_name_first)
    normalized_name_unsorted = normalize_name(
        _given_name_first(name), sort_tokens=False
    )
    score = func.jarowinkler(
        func.unaccent(func.lower(db_models.Congresista.full_name)),
        func.unaccent(normalized_name_unsorted),
    )

    stmt = (
        select(db_models.Congresista)
        .where(score >= threshold)
        .order_by(
            score.desc(),
            db_models.Congresista.id.asc(),
        )
        .limit(1)
    )

    return db.scalar(stmt)


def save_alias(
    db: Session,
    congresista: db_models.Congresista,
    raw_name: str,
) -> bool:
    """Create an alias if it does not already exist.

    Returns:
        True if a new alias was created, otherwise False.
    """
    normalized = normalize_name(raw_name)

    if not normalized:
        return False

    exists = db.scalar(
        select(db_models.CongresistaAlias.id).where(
            db_models.CongresistaAlias.congresista_id == congresista.id,
            db_models.CongresistaAlias.name == normalized,
        )
    )

    if exists is None:
        db.add(
            db_models.CongresistaAlias(
                congresista_id=congresista.id,
                name=normalized,
            )
        )
        return True

    return False


# Sentinel default for find_organization's parent_org_id: distinguishes
# "caller doesn't care about parent, don't scope" (this sentinel) from
# "caller wants a genuinely top-level org" (an explicit parent_org_id=None,
# which now correctly compiles to a parent_org_id IS NULL filter). Before
# this, None was overloaded to mean unscoped-search, so a caller couldn't
# ask for a NULL-parent org without accidentally matching same-named orgs
# under a real parent. Only chambers and parties are genuinely NULL-parent
# today (joint/bicameral bodies are parented under WHOLE_CONGRESS_ORG_NAME,
# see find_organization_with_congreso_fallback) -- NULL-parent joint-entity
# rows may still exist transitionally from before that convention.
_UNSCOPED = object()


def find_organization(
    db: Session,
    org_name: str,
    org_type: TypeOrganization | str,
    threshold: float = 0.9,
    parent_org_id: int | None | object = _UNSCOPED,
) -> db_models.Organization | None:
    """
    Find the closest organization by fuzzy name match and organization type.

    parent_org_id: optionally scope the match to organizations under a specific
    parent. Needed because Organization.org_uniq is (org_name, org_type,
    parent_org_id) — two orgs can share a name+type under different parents
    (e.g. a same-named committee under each chamber). Omit to search
    unscoped (any parent). Pass an explicit org_id to require that parent, or
    explicit None to require a NULL parent (genuinely top-level orgs, e.g.
    joint/bicameral entities and chambers/parties themselves).
    """

    if isinstance(org_type, str):
        org_type = TypeOrganization(org_type)

    normalized_name = org_name.strip().lower()

    score = func.jarowinkler(
        func.unaccent(func.lower(db_models.Organization.org_name)),
        func.unaccent(normalized_name),
    )

    filters = [
        db_models.Organization.org_type == org_type,
        score >= threshold,
    ]
    if parent_org_id is not _UNSCOPED:
        filters.append(db_models.Organization.parent_org_id == parent_org_id)

    stmt = (
        select(db_models.Organization)
        .where(*filters)
        .order_by(
            score.desc(),
            db_models.Organization.org_id.asc(),
        )
        .limit(1)
    )

    return db.scalar(stmt)


def find_organization_with_congreso_fallback(
    db: Session,
    org_name: str,
    org_type: TypeOrganization | str,
    *,
    own_parent_org_id: int | None,
) -> db_models.Organization | None:
    """Look up an ADMINISTRATIVE/COMMITTEE organization scoped to its
    caller-resolved own parent (e.g. a congresista's or bill's own chamber),
    falling back to WHOLE_CONGRESS_ORG_NAME (the parent for joint/bicameral
    bodies like "Comisión Permanente" and "Comisión Bicameral de
    Presupuesto...", see CHAMBER_LABEL_TO_ORG_NAME) and finally to a bare
    NULL parent (some joint-entity rows created before that convention was
    unified may still be NULL-parent transitionally) when the scoped lookup
    misses.

    Never falls all the way back to an unscoped search: an unscoped retry
    could cross-match a *different* chamber's same-named per-chamber org
    (see test_find_organization_parent_org_id_scoping).
    """
    org = find_organization(
        db, org_name=org_name, org_type=org_type, parent_org_id=own_parent_org_id
    )
    if org is not None:
        return org

    congreso = find_organization(
        db, org_name=WHOLE_CONGRESS_ORG_NAME, org_type=TypeOrganization.CHAMBER
    )
    if congreso is not None:
        org = find_organization(
            db, org_name=org_name, org_type=org_type, parent_org_id=congreso.org_id
        )
        if org is not None:
            return org

    return find_organization(
        db, org_name=org_name, org_type=org_type, parent_org_id=None
    )


def find_active_bancada_for_person(
    db: Session, person_id: int, at_date: date | datetime
) -> db_models.Organization | None:
    if isinstance(at_date, datetime):
        at_date = at_date.date()

    return db.scalar(
        select(db_models.Organization)
        .join(
            db_models.Membership,
            db_models.Membership.org_id == db_models.Organization.org_id,
        )
        .where(
            db_models.Membership.person_id == person_id,
            db_models.Membership.org_type == TypeOrganization.BANCADA.value,
            db_models.Membership.start_date <= at_date,
            or_(
                db_models.Membership.end_date.is_(None),
                db_models.Membership.end_date >= at_date,
            ),
        )
        .order_by(db_models.Membership.start_date.desc())
        .limit(1)
    )


def _upsert_model(
    db: Session,
    *,
    existing: db_models.Congresista
    | db_models.Organization
    | db_models.Membership
    | db_models.Ley,
    model: Type[db_models.Congresista]
    | Type[db_models.Organization]
    | Type[db_models.Membership]
    | Type[db_models.Ley],
    payload: dict,
    fields_set: set[str] | None = None,
) -> (
    db_models.Congresista
    | db_models.Organization
    | db_models.Membership
    | db_models.Ley
):
    if existing is None:
        obj = model(**payload)
        db.add(obj)
        db.flush()
        return obj

    # Coalesce, don't blindly overwrite: a matched source can legitimately
    # carry less data than what's already stored (e.g. the 2026-2031
    # chamber congresista scrape has no dni/gender/first_name/last_name at
    # all) -- a None in the payload must never clobber an existing value.
    # Found 2026-09: this exact gap silently wiped dni/gender/first_name/
    # last_name for every reelected congresista matched against their
    # pre-existing legacy row.
    #
    # fields_set is the exception: a caller can pass the source schema's
    # model_fields_set to assert "this field is None on purpose" (e.g. a
    # committee reclassified as top-level, parent_org_id should become
    # NULL) rather than "this field simply wasn't populated by this
    # source" -- without it, a legitimate None could never be written once
    # a row already has a non-null value.
    for key, value in payload.items():
        if value is not None:
            setattr(existing, key, value)
        elif fields_set is not None and key in fields_set:
            setattr(existing, key, None)

    db.flush()
    return existing


def upsert_congresista(
    db: Session, schema: schema.Congresista
) -> db_models.Congresista:
    existing = find_congresista(
        db,
        schema.full_name,
        schema.website,
        congresista_id=schema.congresista_id,
    )
    payload = schema.model_dump()

    return _upsert_model(
        db,
        existing=existing,
        model=db_models.Congresista,
        payload=payload,
    )


class MatchTier(str, Enum):
    """How confidently an incoming Organization schema matches an existing
    row, most to least confident. Drives whether upsert_organization is
    willing to overwrite identity fields (org_name, parent_org_id) on that
    row -- see resolve_organization_match."""

    EXACT = "exact"
    FUZZY_CORROBORATED = "fuzzy_corroborated"
    FUZZY_UNCORROBORATED = "fuzzy_uncorroborated"
    NO_MATCH = "no_match"


def _organization_match_corroborates(
    incoming: schema.Organization, existing: db_models.Organization
) -> bool:
    """A fuzzy name match is only as trustworthy as some second, independent
    signal agreeing with it. org_link (the source page URL) and org_subtype
    are both scraped/derived independently of the name text itself, so
    either one matching is evidence this is genuinely the same organization
    re-scraped, not a coincidentally-similar different one. Either side
    being unset counts as "no evidence," never as a match."""
    if incoming.org_link and existing.org_link:
        if incoming.org_link.strip() == existing.org_link.strip():
            return True
    if incoming.org_subtype is not None and existing.org_subtype:
        if _enum_value(incoming.org_subtype) == existing.org_subtype:
            return True
    return False


def resolve_organization_match(
    db: Session, schema: schema.Organization, parent_id: int | None
) -> tuple[MatchTier, db_models.Organization | None]:
    """Classify how confidently `schema` matches an existing Organization
    row scoped to `parent_id` (the org_uniq-consistent parent already
    resolved by the caller).

    A slug/natural-key column was deliberately not introduced for this --
    it would only move the fuzzy-matching problem to "map scraped text to a
    slug" without eliminating it, and committees do get legitimately
    renamed by Congress over time, so "identity = frozen name" isn't fully
    correct either. This tiering is the alternative: trust an exact match
    fully, trust a fuzzy match only with corroborating evidence, and never
    silently guess otherwise.
    """
    match = find_organization(
        db, schema.org_name, schema.org_type, parent_org_id=parent_id
    )
    if match is None:
        return MatchTier.NO_MATCH, None

    if schema.org_name.strip().lower() == match.org_name.strip().lower():
        return MatchTier.EXACT, match

    if _organization_match_corroborates(schema, match):
        return MatchTier.FUZZY_CORROBORATED, match

    return MatchTier.FUZZY_UNCORROBORATED, match


def upsert_organization(
    db: Session, schema: schema.Organization
) -> db_models.Organization:
    payload = schema.model_dump()
    fields_set = set(schema.model_fields_set)

    parent_name = payload.pop("parent_org_name", None)
    parent_type = payload.pop("parent_org_type", None)

    parent_id = None
    if parent_name and parent_type:
        parent = find_organization(
            db,
            org_name=parent_name,
            org_type=parent_type,
        )

        if parent is None:
            raise ValueError(
                f"Parent organization not found: {parent_name} ({parent_type})"
            )

        parent_id = parent.org_id

    payload["parent_org_id"] = parent_id
    # parent_org_id is always authoritatively resolved above (a real parent
    # org_id, or None for a genuinely top-level org like a joint/bicameral
    # entity) -- always treat it as explicitly set so a corrected NULL
    # parent actually gets written to an existing row, rather than silently
    # coalesced away by _upsert_model's None-skip rule.
    fields_set.add("parent_org_id")

    payload["org_type"] = _enum_value(payload["org_type"])
    if payload.get("org_subtype") is not None:
        payload["org_subtype"] = _enum_value(payload["org_subtype"])

    tier, existing = resolve_organization_match(db, schema, parent_id)

    if tier == MatchTier.FUZZY_UNCORROBORATED:
        logger.warning(
            f"[org-upsert] fuzzy match with no corroborating signal: incoming "
            f"org_name={schema.org_name!r} org_type={schema.org_type} "
            f"parent_org_id={parent_id} matched existing org_id={existing.org_id} "
            f"org_name={existing.org_name!r} -- "
            + (
                "identity fields (org_name, parent_org_id) not overwritten "
                "(ORG_UPSERT_STRICT_IDENTITY=True)."
                if settings.ORG_UPSERT_STRICT_IDENTITY
                else "would NOT overwrite identity fields if "
                "ORG_UPSERT_STRICT_IDENTITY were enabled (shadow mode)."
            )
        )
        if settings.ORG_UPSERT_STRICT_IDENTITY:
            # Don't touch identity fields on an unconfirmed match -- only
            # non-identity fields (dates, subtype/link if newly learned)
            # get updated. Still the same row, never a duplicate insert.
            payload.pop("org_name", None)
            payload.pop("parent_org_id", None)
            fields_set.discard("parent_org_id")

    return _upsert_model(
        db,
        existing=existing,
        model=db_models.Organization,
        payload=payload,
        fields_set=fields_set,
    )


def membership_exists(
    db: Session,
    *,
    person_id: int,
    org_id: int,
    leg_period: str | Enum,
    org_type: str | TypeOrganization,
) -> bool:
    """True if ANY Membership row already exists for this (person, org,
    leg_period, org_type), regardless of start_date/end_date/role -- unlike
    upsert_membership's own existing-row lookup, which is keyed on the
    exact start_date/end_date/role too (so it can't answer "has this
    person ever had this membership before", only "does this exact
    date/role combination already exist").
    """
    return (
        db.scalars(
            select(db_models.Membership.id).where(
                db_models.Membership.person_id == person_id,
                db_models.Membership.org_id == org_id,
                db_models.Membership.leg_period == _enum_value(leg_period),
                db_models.Membership.org_type == _enum_value(org_type),
            )
        ).first()
        is not None
    )


def upsert_membership(
    db: Session,
    *,
    person_id: int,
    org_id: int,
    leg_period: str,
    org_type: str | TypeOrganization,
    role: str,
    start_date: date,
    end_date: date,
    extra_fields: dict | None = None,
) -> db_models.Membership:
    org_type_value = _enum_value(org_type)
    role_value = _enum_value(role)
    leg_period_value = _enum_value(leg_period)
    model = MEMBERSHIP_MODELS[org_type_value]

    payload = {
        "person_id": person_id,
        "org_id": org_id,
        "leg_period": leg_period_value,
        "org_type": org_type_value,
        "role": role_value,
        "start_date": start_date,
        "end_date": end_date,
    }

    if extra_fields:
        payload.update(extra_fields)

    existing = db.scalars(
        select(db_models.Membership).where(
            db_models.Membership.person_id == person_id,
            db_models.Membership.org_id == org_id,
            db_models.Membership.leg_period == leg_period_value,
            db_models.Membership.org_type == org_type_value,
            db_models.Membership.role == role_value,
            db_models.Membership.start_date == start_date,
            db_models.Membership.end_date == end_date,
        )
    ).first()

    return _upsert_model(
        db,
        existing=existing,
        model=model,
        payload=payload,
    )


def upsert_ley(db: Session, schema: schema.Ley) -> db_models.Ley:
    payload = {
        "id": schema.id,
        "title": schema.title,
        "bill_id": schema.bill_id,
    }

    existing = db.get(db_models.Ley, schema.id)

    return _upsert_model(db, existing=existing, model=db_models.Ley, payload=payload)


def upsert_scraper_run(db: Session, scraper_name: str, stats: ScraperStats):
    obj = ScraperRun(
        scraper_name=scraper_name,
        start_time=stats.start_time,
        end_time=stats.end_time,
        scraped_rows=stats.scrapped,
    )

    db.add(obj)
    db.commit()
