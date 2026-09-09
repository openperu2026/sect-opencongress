"""Backfill for Bills, Motions, and Congresistas whose organization/
congresista/membership links were silently skipped by a bug in how the
processing pipeline resolved an entity's parent chamber.

The processing loops for these three raw tables mark a row fully processed
right after their linking loop finishes, even if that loop skipped some
links along the way (a skipped link only does `continue`, it never blocks
the row's own `processed = True`). So once a row has been marked processed,
any link that failed to resolve at the time stays missing forever -- the
normal incremental pipeline only reprocesses rows where `processed` is
still False, and never revisits an already-processed row to check whether
something inside it needs fixing.

This script re-derives, for every already-processed row, exactly what
organization/congresista links it *should* have (using the same functions
and resolution logic the live pipeline uses) and compares that against
what's actually stored. A row whose expected links are now resolvable but
aren't yet recorded gets flagged, and (with --apply) has its `processed`
flag reset to False so the next normal pipeline run fills in the missing
links through the regular code path -- nothing here inserts links directly.

Note: this is unrelated to organization/committee definitions themselves
(RawCommittee/RawOrganization) -- that processing loop raises and retries
the whole batch on any single failure, so it doesn't have this gap, and a
prior audit already confirmed the Organization table itself is consistent.

Defaults to a dry run (report only). Pass --apply to actually write.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend import TypeOrganization
from backend.config import directories, settings
from backend.database import models as db_models
from backend.database.crud import pipeline_core as crud_core
from backend.database.raw_models import RawBill, RawCongresista, RawMotion
from backend.process.bills import process_bill, process_bill_organizations
from backend.process.congresistas import (
    get_cong_data,
    process_cong_memberships,
    process_profile_content,
)
from backend.process.motions import process_motion, process_motion_organizations
from backend.process.utils import (
    find_organization_schema,
    replace_www,
    split_and_sort_name,
)


def _resolve_org(db, *, chamber, org_schema):
    """Mirror _process_legislative_documents'/​_process_congresistas' org
    resolution exactly, so "missing" here means the same thing it would mean
    live."""
    if org_schema.org_type == TypeOrganization.CHAMBER:
        return crud_core.find_organization(
            db,
            org_name=org_schema.org_name,
            org_type=org_schema.org_type,
            parent_org_id=None,
        )
    return crud_core.find_organization_with_congreso_fallback(
        db,
        org_name=org_schema.org_name,
        org_type=org_schema.org_type,
        own_parent_org_id=chamber.org_id if chamber else None,
    )


def _check_bills_or_motions(
    db, *, raw_model, process_fn, process_orgs_fn, org_table, cong_table, label
):
    rows = (
        db.execute(
            select(raw_model).where(
                raw_model.last_update.is_(True), raw_model.processed.is_(True)
            )
        )
        .scalars()
        .all()
    )
    logger.info(f"Checking {len(rows)} already-processed Raw{label} row(s)")

    flagged = []
    for raw_row in rows:
        try:
            schema_obj, cong_rels, steps = process_fn(raw_row)
            orgs = process_orgs_fn(raw_row, steps)
        except Exception as exc:
            logger.warning(
                f"Raw{label} id={raw_row.id}: failed to re-derive ({exc}) -- skipping"
            )
            continue

        chamber_schema = find_organization_schema(
            orgs, org_type=TypeOrganization.CHAMBER.value
        )
        chamber = None
        if chamber_schema is not None:
            chamber = crud_core.find_organization(
                db,
                org_name=chamber_schema.org_name,
                org_type=TypeOrganization.CHAMBER.value,
            )

        missing_orgs = []
        for org_schema in orgs:
            resolved = _resolve_org(db, chamber=chamber, org_schema=org_schema)
            if resolved is None:
                continue  # still wouldn't resolve today -- not something this backfill can fix
            exists = db.execute(
                select(org_table.org_id).where(
                    org_table.__table__.c[f"{label.lower()}_id"] == schema_obj.id,
                    org_table.org_id == resolved.org_id,
                )
            ).first()
            if exists is None:
                missing_orgs.append(org_schema.org_name)

        missing_congs = []
        for cong_rel in cong_rels:
            cong = crud_core.find_congresista(
                db,
                name=split_and_sort_name(cong_rel.nombre)[0],
                website=replace_www(cong_rel.web_page),
            )
            if cong is None:
                continue
            exists = db.execute(
                select(cong_table.person_id).where(
                    cong_table.__table__.c[f"{label.lower()}_id"] == schema_obj.id,
                    cong_table.person_id == cong.id,
                )
            ).first()
            if exists is None:
                missing_congs.append(cong_rel.nombre)

        if missing_orgs or missing_congs:
            flagged.append((raw_row, missing_orgs, missing_congs))

    for raw_row, missing_orgs, missing_congs in flagged:
        logger.warning(
            f"Raw{label} id={raw_row.id}: missing orgs={missing_orgs} missing_congresistas={missing_congs}"
        )

    return flagged


def _check_congresistas(db):
    dict_cong_data = get_cong_data(
        directories.PROCESSED_DATA / "cong_info_2021_2026.json"
    )
    dict_cong_data_current = get_cong_data(
        directories.PROCESSED_DATA / "cong_info_2026_2031.json", leg_period="2026-2031"
    )

    rows = (
        db.execute(
            select(RawCongresista).where(
                RawCongresista.last_update.is_(True), RawCongresista.processed.is_(True)
            )
        )
        .scalars()
        .all()
    )
    logger.info(f"Checking {len(rows)} already-processed RawCongresista row(s)")

    flagged = []
    for raw_cong in rows:
        try:
            cong_schema, org_schemas, profile_memberships = process_profile_content(
                raw_cong, dict_cong_data, dict_cong_data_current=dict_cong_data_current
            )
        except Exception as exc:
            logger.warning(
                f"RawCongresista id={raw_cong.id}: failed to re-derive ({exc}) -- skipping"
            )
            continue

        cong = crud_core.find_congresista(
            db,
            name=cong_schema.full_name,
            website=cong_schema.website,
            congresista_id=cong_schema.congresista_id,
        )
        if cong is None:
            continue  # not even the base row exists yet -- not this backfill's job

        chamber_org_id = None
        for org_schema in org_schemas:
            if org_schema.org_type == TypeOrganization.CHAMBER:
                match = crud_core.find_organization(
                    db,
                    org_name=org_schema.org_name,
                    org_type=org_schema.org_type,
                    parent_org_id=None,
                )
                chamber_org_id = match.org_id if match else None

        memberships = list(profile_memberships)
        if raw_cong.memberships_content:
            memberships.extend(process_cong_memberships(raw_cong, cong_schema))

        missing = []
        for ms in memberships:
            if ms.org_type in (TypeOrganization.CHAMBER, TypeOrganization.PARTY):
                org = crud_core.find_organization(
                    db, org_name=ms.org_name, org_type=ms.org_type, parent_org_id=None
                )
            else:
                org = crud_core.find_organization_with_congreso_fallback(
                    db,
                    org_name=ms.org_name,
                    org_type=ms.org_type,
                    own_parent_org_id=chamber_org_id,
                )
            if org is None:
                continue
            exists = db.execute(
                select(db_models.Membership.id).where(
                    db_models.Membership.person_id == cong.id,
                    db_models.Membership.org_id == org.org_id,
                )
            ).first()
            if exists is None:
                missing.append(ms.org_name)

        if missing:
            flagged.append((raw_cong, missing))

    for raw_cong, missing in flagged:
        logger.warning(
            f"RawCongresista id={raw_cong.id}: missing memberships={missing}"
        )

    return flagged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true", help="Write changes (default: dry run)"
    )
    args = parser.parse_args()

    engine = create_engine(settings.DB_URL)
    DBSession = sessionmaker(bind=engine)

    with DBSession() as db:
        bill_flags = _check_bills_or_motions(
            db,
            raw_model=RawBill,
            process_fn=process_bill,
            process_orgs_fn=process_bill_organizations,
            org_table=db_models.BillOrganization,
            cong_table=db_models.BillCongresistas,
            label="Bill",
        )
        motion_flags = _check_bills_or_motions(
            db,
            raw_model=RawMotion,
            process_fn=process_motion,
            process_orgs_fn=process_motion_organizations,
            org_table=db_models.MotionOrganization,
            cong_table=db_models.MotionCongresistas,
            label="Motion",
        )
        cong_flags = _check_congresistas(db)

        total = len(bill_flags) + len(motion_flags) + len(cong_flags)
        logger.info(
            f"Summary: {len(bill_flags)} Bill(s), {len(motion_flags)} Motion(s), "
            f"{len(cong_flags)} Congresista(s) flagged for reprocessing (total {total})"
        )

        if args.apply:
            for raw_row, _, _ in bill_flags:
                raw_row.processed = False
            for raw_row, _, _ in motion_flags:
                raw_row.processed = False
            for raw_cong, _ in cong_flags:
                raw_cong.processed = False
            db.commit()
            logger.info(
                f"Applied: reset {total} row(s) to processed=False -- "
                "the next normal pipeline run will fill in the missing links."
            )
        else:
            logger.info("Dry run -- re-run with --apply to write changes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
