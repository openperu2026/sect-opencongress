"""Read-only diagnostic for the 2026-09-08 org-lookup incident.

The 2026-08-31 bicameral migration (bd7dc34) started scoping
find_organization by parent_org_id without auditing whether existing
Organization rows actually satisfy the new invariant (a standing committee's
parent_org_id must equal the org_id of whichever chamber "Cámara de
Diputados" / "Senado de la República" resolves to today). This script never
writes anything -- it only reports what it finds, so a human can decide what
Phase 3's data-repair step actually needs to fix.

Checks:
1. Duplicate/multiple "Cámara"-type rows (would mean legacy FKs and today's
   fresh lookups can resolve to two different org_ids for what should be one
   canonical chamber).
2. Every COMMITTEE-type org with a non-null parent_org_id: does that parent
   match one of the two chambers as resolved *today* by the exact same
   find_organization() production code uses?
3. Every ADMINISTRATIVE/COMMITTEE-type org whose name suggests a joint,
   whole-Congress body (Comisión Permanente, anything containing "Bicameral")
   but has a non-null parent_org_id -- these should be top-level (NULL
   parent) per CHAMBER_LABEL_TO_ORG_NAME["Congreso"] = None.

Usage: python scripts/diagnose_org_parent_scoping.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend import TypeOrganization
from backend.config import settings
from backend.database import models as db_models
from backend.database.crud.pipeline_core import find_organization

JOINT_ENTITY_NAME_HINTS = ("comisión permanente", "bicameral", "ética parlamentaria")


def main() -> int:
    engine = create_engine(settings.DB_URL)
    DBSession = sessionmaker(bind=engine)

    with DBSession() as db:
        # 1. Duplicate/multiple chamber rows.
        chambers = (
            db.execute(
                select(db_models.Organization).where(
                    db_models.Organization.org_type == TypeOrganization.CHAMBER.value
                )
            )
            .scalars()
            .all()
        )
        logger.info(f"Found {len(chambers)} 'Cámara'-type row(s):")
        by_name: dict[str, list[int]] = {}
        for c in chambers:
            by_name.setdefault(c.org_name, []).append(c.org_id)
            logger.info(
                f"  org_id={c.org_id} org_name={c.org_name!r} parent_org_id={c.parent_org_id}"
            )
        for name, org_ids in by_name.items():
            if len(org_ids) > 1:
                logger.warning(
                    f"DUPLICATE chamber name {name!r}: org_ids={org_ids} -- "
                    "legacy FKs and fresh lookups may resolve to different rows"
                )

        # 2. Resolve today's canonical chamber org_ids exactly as production does.
        diputados = find_organization(
            db, org_name="Cámara de Diputados", org_type=TypeOrganization.CHAMBER
        )
        senado = find_organization(
            db, org_name="Senado de la República", org_type=TypeOrganization.CHAMBER
        )
        diputados_id = diputados.org_id if diputados else None
        senado_id = senado.org_id if senado else None
        logger.info(
            f"find_organization resolves today: Cámara de Diputados -> org_id={diputados_id}, "
            f"Senado de la República -> org_id={senado_id}"
        )
        canonical_ids = {oid for oid in (diputados_id, senado_id) if oid is not None}

        # 3. Every committee with a non-null parent: does it match a canonical chamber?
        committees = (
            db.execute(
                select(db_models.Organization).where(
                    db_models.Organization.org_type == TypeOrganization.COMMITTEE.value,
                    db_models.Organization.parent_org_id.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        mismatched = [c for c in committees if c.parent_org_id not in canonical_ids]
        logger.info(
            f"Checked {len(committees)} COMMITTEE row(s) with a non-null parent; "
            f"{len(mismatched)} have a parent_org_id that doesn't match either "
            "chamber resolved above:"
        )
        for c in mismatched:
            logger.warning(
                f"  org_id={c.org_id} org_name={c.org_name!r} "
                f"stored parent_org_id={c.parent_org_id} (expected one of {canonical_ids})"
            )

        # 4. Joint-entity-looking orgs that are NOT top-level.
        candidates = (
            db.execute(
                select(db_models.Organization).where(
                    db_models.Organization.org_type.in_(
                        [
                            TypeOrganization.COMMITTEE.value,
                            TypeOrganization.ADMINISTRATIVE.value,
                        ]
                    ),
                    db_models.Organization.parent_org_id.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        joint_but_scoped = [
            c
            for c in candidates
            if any(hint in c.org_name.lower() for hint in JOINT_ENTITY_NAME_HINTS)
        ]
        logger.info(
            f"Found {len(joint_but_scoped)} org(s) whose name suggests a joint/"
            "whole-Congress body but which have a non-null parent_org_id "
            "(should be NULL, per CHAMBER_LABEL_TO_ORG_NAME['Congreso'] = None):"
        )
        for c in joint_but_scoped:
            logger.warning(
                f"  org_id={c.org_id} org_name={c.org_name!r} org_type={c.org_type} "
                f"parent_org_id={c.parent_org_id}"
            )

    logger.info(
        "Diagnostic complete. Read-only -- nothing was written. Use these "
        "findings to scope Phase 3's data-repair step (see "
        "~/.claude/plans/validated-petting-planet.md)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
