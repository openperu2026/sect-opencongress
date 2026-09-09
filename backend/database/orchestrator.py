from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import ModuleType
from typing import Type, Callable, Literal
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm
from loguru import logger
from sqlalchemy import create_engine, func, select, or_, update
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import SQLAlchemyError

from backend.config import (
    settings,
    directories,
    log_manager,
)
from backend.database import models as db_models
from backend.database.build_db import drop_step_vote_event_fks
from backend.database.models import Bill, Motion
from backend.database.crud import (
    pipeline_bills as crud_bills,
    pipeline_core as crud_core,
    pipeline_motions as crud_motions,
    pipeline_embeddings as crud_embeddings,
)
from backend.database.crud.pipeline_core import (
    ProcessStats,
    ScraperStats,
    upsert_scraper_run,
)
from backend.database.raw_models import (
    RawBase,
    RawBancada,
    RawBill,
    RawCommittee,
    RawCongresista,
    RawLey,
    RawMotion,
    RawOrganization,
    RawBillDocument,
    ScrapeGapRetry,
)
from backend.process.bancadas import process_bancada
from backend.process.bills import (
    process_bill,
    process_bill_organizations,
    process_bill_text,
)
from backend.process.diff import compute_bill_difference
from backend.process.congresistas import (
    process_cong_memberships,
    process_profile_content,
    get_cong_data,
)
from backend.process.motions import (
    process_motion,
    process_motion_organizations,
    process_motion_text,
)
from backend.process.organizations import (
    process_chambers,
    process_committee,
    process_admin_org,
)
from backend.process.leyes import process_leyes
from backend.process.schema import Membership, Organization
from backend.process.summarization import summarize_bill_from_db
from backend.process.utils import (
    get_current_leg_year,
    find_organization_schema,
    split_and_sort_name,
    replace_www,
    chamber_label_from_id,
)
from backend.process.votes import extract as votes_extract, load as votes_load
from backend.process.votes.config import DEFAULT_MODEL as VOTES_DEFAULT_MODEL
from backend.scrapers.utils import get_last_id
from backend.scrapers.congresista_photos import sync_photo as sync_congresista_photo
from backend import TypeOrganization
from backend.core.constants import (
    CHAMBER_LABEL_TO_ORG_NAME,
    CHAMBER_LABEL_TO_ID_SUFFIX,
    CHAMBER_LABEL_TO_COD_TIPO_PARL,
    LEG_PERIOD_TO_PER_PAR_ID,
)
from backend.core.parsers import (
    get_processable_year_range,
    resolve_processable_leg_periods,
    LEG_PERIOD_RANGES,
)
from backend.core.enums import LegPeriod

# org_type values that are always top-level (parent_org_id=NULL by design) —
# a parent_org_id filter must never be applied when looking these up, since
# they don't have a chamber (or any) parent to scope by. Membership.org_type
# is a TypeOrganization enum member at this stage (backend/process/schema.py),
# not yet converted to its string .value.
_CHAMBER_UNSCOPED_ORG_TYPES = {
    TypeOrganization.CHAMBER,
    TypeOrganization.PARTY,
}


@dataclass(frozen=True)
class _DocumentUploadStats:
    total: int
    succeeded: int
    failed: int


@dataclass(frozen=True)
class _LegislativeDocumentConfig:
    """Everything that differs between _process_bills and _process_motions
    -- both otherwise share identical control flow (query -> chamber
    resolution -> upsert -> steps -> orgs -> congresistas). See
    _process_legislative_documents, the shared implementation both are
    thin wrappers around.

    crud_module + the upsert_*_name fields are resolved via getattr at
    call time (not bound to a function reference here) so that tests
    monkeypatching e.g. crud_bills.upsert_bill still take effect -- a
    frozen dataclass field bound at import time would capture the
    pre-monkeypatch function object instead.
    """

    raw_model: Type[RawBill] | Type[RawMotion]
    clean_model: Type[Bill] | Type[Motion]
    crud_module: ModuleType
    process_fn: Callable
    process_organizations_fn: Callable
    upsert_name: str
    upsert_step_name: str
    upsert_organization_name: str
    upsert_congresista_name: str
    entity_label: str  # "Bill" / "Motion" -- used in Raw{label}/{label}Organization/{label}Congresista warnings
    id_field: str  # "bill_id" / "motion_id" -- used in warning messages
    desc: str  # tqdm progress label
    log_key: str  # "bills" / "motions" -- summary log line prefix


_BILL_DOCUMENT_CONFIG = _LegislativeDocumentConfig(
    raw_model=RawBill,
    clean_model=Bill,
    crud_module=crud_bills,
    process_fn=process_bill,
    process_organizations_fn=process_bill_organizations,
    upsert_name="upsert_bill",
    upsert_step_name="upsert_bill_step",
    upsert_organization_name="upsert_bill_organization",
    upsert_congresista_name="upsert_bill_congresista",
    entity_label="Bill",
    id_field="bill_id",
    desc="Process bills",
    log_key="bills",
)

_MOTION_DOCUMENT_CONFIG = _LegislativeDocumentConfig(
    raw_model=RawMotion,
    clean_model=Motion,
    crud_module=crud_motions,
    process_fn=process_motion,
    process_organizations_fn=process_motion_organizations,
    upsert_name="upsert_motion",
    upsert_step_name="upsert_motion_step",
    upsert_organization_name="upsert_motion_organization",
    upsert_congresista_name="upsert_motion_congresista",
    entity_label="Motion",
    id_field="motion_id",
    desc="Process motions",
    log_key="motions",
)


class OpenPeruOrchestrator:
    """
    End-to-end ETL orchestrator:
      1) scrape raw tables
      2) process raw rows into Pydantic DTOs
      3) load SQLAlchemy models into the clean DB
    """

    def __init__(self, db_url: str = settings.DB_URL, engine=None):
        """Bind to ``db_url`` (or reuse ``engine``), ensure tables exist, and drop step/vote FKs."""
        self.db_engine = engine or create_engine(db_url, pool_pre_ping=True)
        self.DBSession = sessionmaker(
            bind=self.db_engine, autocommit=False, autoflush=False
        )

        # Ensure schemas exist before the pipeline runs.
        db_models.Base.metadata.create_all(self.db_engine)
        drop_step_vote_event_fks(self.db_engine)

    # -----------------------------
    # Public API
    # -----------------------------
    def _recent_raw_exists(
        self,
        raw_model: RawBase,
        days: int = 1,
        *,
        chamber: str | None | Literal["_ANY_"] = "_ANY_",
    ) -> bool:
        """
        Query to check recent changes in a period of time in any RawDB table (default 1 day)

        chamber: scope the "recently scraped" check to one chamber value
        (including None for legacy rows) so scraping Senado doesn't suppress
        the very next Diputados run (or vice versa) for `days`. Default
        "_ANY_" preserves the original table-wide behavior for callers that
        don't pass it (e.g. bills/motions/leyes, which have no chamber
        column at all).
        """
        cutoff = datetime.now() - timedelta(days=days)
        with self.DBSession() as raw_db:
            query = raw_db.query(func.max(raw_model.timestamp))
            if chamber != "_ANY_":
                query = query.filter(raw_model.chamber == chamber)
            last_ts = query.scalar()
            return bool(last_ts and last_ts >= cutoff)

    def _get_approved_ids(self, model: Type[Bill] | Type[Motion]) -> list[str]:
        """Return ids of Bills/Motions whose ``*_approved`` flag is True (never re-scrape these)."""
        with self.DBSession() as db:
            approved_col = (
                model.bill_approved if model is Bill else model.motion_approved
            )
            ids = [
                row[0]
                for row in db.query(model.id).filter(approved_col.is_(True)).all()
            ]
        return ids

    def _get_ids_to_update(
        self,
        raw_model: Type[RawBill] | Type[RawMotion],
        model: Type[Bill] | Type[Motion],
        days: int = 1,
    ) -> list[str] | None:
        """
        Return ids that should be refreshed this day:
          - latest snapshot is older than `max_age_days`
          - latest snapshot is not approved
        """
        cutoff = datetime.now() - timedelta(days=days)

        with self.DBSession() as raw_db:
            latest_rows = raw_db.query(raw_model).filter(raw_model.last_update).all()
            pending_ids: list[str] = []

            approved_ids = self._get_approved_ids(model)

            for row in latest_rows:
                if row.timestamp > cutoff:
                    continue
                if row.id in approved_ids:
                    continue
                pending_ids.append(row.id)

            return pending_ids

    def _get_last_id_scraped(
        self,
        raw_model: Type[RawBill] | Type[RawMotion] | Type[RawLey],
        id_suffix: str | None = None,
    ) -> int:
        """Return the highest numeric id present in ``raw_model`` (0 if empty).

        id_suffix: when given (e.g. "S"/"CD"), scopes to 2026-2031 ids
        ending "-{id_suffix}" and parses the leading zero-padded number.
        When omitted (default, legacy path), scopes to legacy-format ids
        only (those containing "_") before parsing -- without this filter,
        a mixed table (once any new-format id exists) would raise
        IndexError on item.split("_", 1)[1], since new-format ids have no
        underscore. No-op change when only legacy ids exist.
        """
        with self.DBSession() as db:
            ids = db.scalars(select(raw_model.id).distinct()).all()

        if not ids:
            return 0

        if raw_model is RawLey:
            return max(int(item) for item in ids)

        if id_suffix is not None:
            matching = [item for item in ids if item.endswith(f"-{id_suffix}")]
            if not matching:
                return 0
            return max(int(item.split("-", 1)[0]) for item in matching)

        legacy_ids = [item for item in ids if "_" in item]
        if not legacy_ids:
            return 0
        return max(int(item.split("_", 1)[1]) for item in legacy_ids)

    # Number of failed retry attempts a single gap id gets before it's
    # marked permanently skipped (see _find_id_gaps/_record_gap_retry_outcomes).
    # A legitimately-nonexistent id (withdrawn/renumbered legislative item)
    # is indistinguishable from a transient failure by id alone, so this
    # caps the cost of never finding out rather than trying to tell them
    # apart up front.
    MAX_GAP_RETRY_ATTEMPTS = 5

    def _find_id_gaps(
        self,
        raw_model: Type[RawBill] | Type[RawMotion] | Type[RawLey],
        id_suffix: str | None = None,
    ) -> list[int]:
        """Return numeric ids missing between the observed min and max id
        in ``raw_model`` -- e.g. an id that failed to scrape mid-range on a
        prior run and was never retried since only ids ABOVE the watermark
        get picked up by _get_last_id_scraped. Uses the same three
        id-format branches as _get_last_id_scraped (bare/id_suffix-tagged/
        legacy year_number); ids already marked permanently skipped in
        ScrapeGapRetry are excluded.

        Note: the legacy branch (no id_suffix) inherits the same
        single-legislative-year assumption _get_last_id_scraped already
        depends on -- safe today only because _scrape_range hardcodes
        `year = 2021` (see its own "TODO: update this for next congreso"),
        so legacy bills/motions/leyes ids are single-year by construction.
        Revisit this if that TODO is ever addressed.
        """
        with self.DBSession() as db:
            ids = db.scalars(select(raw_model.id).distinct()).all()

        if not ids:
            return []

        if raw_model is RawLey:
            numbers = sorted(int(item) for item in ids)
        elif id_suffix is not None:
            matching = [item for item in ids if item.endswith(f"-{id_suffix}")]
            if not matching:
                return []
            numbers = sorted(int(item.split("-", 1)[0]) for item in matching)
        else:
            legacy_ids = [item for item in ids if "_" in item]
            if not legacy_ids:
                return []
            numbers = sorted(int(item.split("_", 1)[1]) for item in legacy_ids)

        full_range = set(range(numbers[0], numbers[-1] + 1))
        missing = sorted(full_range - set(numbers))
        if not missing:
            return []

        raw_model_name = raw_model.__name__
        with self.DBSession() as db:
            skipped_ids = {
                int(row.gap_id)
                for row in db.query(ScrapeGapRetry).filter(
                    ScrapeGapRetry.raw_model_name == raw_model_name,
                    ScrapeGapRetry.skipped.is_(True),
                )
            }
        return [n for n in missing if n not in skipped_ids]

    def _record_gap_retry_outcomes(
        self,
        raw_model: Type[RawBill] | Type[RawMotion] | Type[RawLey],
        attempted_ids: list[int],
        id_suffix: str | None = None,
    ) -> None:
        """After attempting to re-scrape a batch of gap ids, bump the
        retry count for any id still missing from ``raw_model`` and mark
        it permanently skipped once MAX_GAP_RETRY_ATTEMPTS is reached. An
        id that now exists is left alone -- it's no longer a gap, nothing
        to track.
        """
        if not attempted_ids:
            return

        raw_model_name = raw_model.__name__
        with self.DBSession() as db:
            existing_ids = set(db.scalars(select(raw_model.id).distinct()).all())

            def _id_exists(number: int) -> bool:
                if raw_model is RawLey:
                    return str(number) in existing_ids
                if id_suffix is not None:
                    return any(
                        item.endswith(f"-{id_suffix}")
                        and int(item.split("-", 1)[0]) == number
                        for item in existing_ids
                    )
                return any(
                    "_" in item and int(item.split("_", 1)[1]) == number
                    for item in existing_ids
                )

            for number in attempted_ids:
                if _id_exists(number):
                    continue

                gap_id = str(number)
                row = db.get(ScrapeGapRetry, (raw_model_name, gap_id))
                if row is None:
                    row = ScrapeGapRetry(
                        raw_model_name=raw_model_name,
                        gap_id=gap_id,
                        attempts=0,
                        last_attempt_at=datetime.now(),
                        skipped=False,
                    )
                    db.add(row)
                row.attempts += 1
                row.last_attempt_at = datetime.now()
                if row.attempts >= self.MAX_GAP_RETRY_ATTEMPTS:
                    row.skipped = True
                    logger.warning(
                        f"{raw_model_name} id {gap_id} failed {row.attempts} "
                        "gap-retry attempts -- marking permanently skipped"
                    )
            db.commit()

    def _load_scraper_results(self, scraper_name: str) -> None:
        """Persist a ScraperStats row for ``scraper_name`` and log a one-line summary."""
        stats = self.scraper_results[scraper_name]
        with self.DBSession() as db:
            upsert_scraper_run(db, scraper_name, stats)
        log_manager.console_logger().info(
            f"Results for scraper/{scraper_name}: Time: {(stats.end_time - stats.start_time).seconds}s | Rows scraped: {stats.scrapped}"
        )

    def _log_stage_summary(self, stage: str, stats: ProcessStats) -> None:
        """Log processed/skipped/errors counts for a processing stage."""
        log_manager.console_logger().info(
            f"{stage}: processed={stats.processed}, skipped={stats.skipped}, errors={stats.errors}"
        )

    def run_scrapers(
        self,
        *,
        scrape_bills: bool = True,
        scrape_motions: bool = True,
        scrape_leyes: bool = True,
        scrape_others: bool = True,
        only_current: bool = True,
        scrape_documents: bool = False,
        upload_s3: bool = False,
        leg_period: str | None = None,
    ) -> None:
        """
        Run raw scrapers. Bills/motions scraping requires explicit ranges.

        leg_period: when None (default) or settings.LEG_PERIOD (the current
        term, "2026-2031"), scrape_others runs ONLY the current-period
        chamber-specific congresistas/bancadas/committees/organizations
        scrape; the legacy (through 2021-2026) reference scrape
        is skipped by default -- that data is now historical/stable and
        doesn't need continuous re-scraping. Passing any OTHER explicit
        value (e.g. "2021-2026") flips this: it opts back into the legacy
        reference scrape and skips the current-period one for that run.

        Bills/motions are NOT subject to this asymmetry: their legacy scrape
        always runs regardless of leg_period (old bills/motions can still
        gain new documents/votes/status), and their 2026-2031 chamber-
        specific range scrapers run when leg_period is None or
        settings.LEG_PERIOD, same gating shape as scrape_others' current
        block.

        Leyes scraping is unaffected either way -- there is no bicameral
        concept for leyes.
        """
        console = log_manager.console_logger()
        console.info("Starting scraper pipeline")
        self.scraper_results: dict[str, ScraperStats] = dict()

        if scrape_others:
            from backend.scrapers.bancadas import RawBancadaScraper
            from backend.scrapers.committees import RawCommitteeScraper
            from backend.scrapers.congresistas import RawCongresistasScraper
            from backend.scrapers.organizations import RawOrganizationScraper

            console.info(
                "Running reference scrapers (congresistas, bancadas, committees, organizations)"
            )

            # =================================================================
            # LEGACY (through 2021-2026) -- unicameral www3.congreso.gob.pe.
            # This reference data is now historical/stable, so it is OPT-IN
            # ONLY: it runs when leg_period is explicitly set to something
            # other than the current period (settings.LEG_PERIOD), and is
            # skipped by default. See the "2026-2031 BICAMERAL TERM" block
            # below for the current-period scrape, which runs by default
            # instead of this one, not in addition to it.
            # =================================================================

            if leg_period not in (None, settings.LEG_PERIOD):
                with log_manager.stage("scraper", "congresistas") as stage_logger:
                    if self._recent_raw_exists(RawCongresista, days=1):
                        console.info(
                            "Skipping congresistas scrape: latest raw scrape is within 1 day"
                        )
                        stage_logger.info("Skipped congresistas scraper")
                    else:
                        console.info("Starting congresistas scraper")
                        stage_logger.info("Starting congresistas scraper")
                        cong = RawCongresistasScraper()
                        start_time = datetime.now()
                        cong.get_dict_periodos()
                        scraped_congs = cong.extract_and_load_all(
                            only_current=only_current
                        )
                        end_time = datetime.now()
                        self.scraper_results["congresistas.py"] = ScraperStats(
                            start_time, end_time, len(scraped_congs)
                        )
                        self._load_scraper_results("congresistas.py")

                with log_manager.stage("scraper", "bancadas") as stage_logger:
                    if self._recent_raw_exists(RawBancada, days=1):
                        console.info(
                            "Skipping bancadas scrape: latest raw scrape is within 1 day"
                        )
                        stage_logger.info("Skipped bancadas scraper")
                    else:
                        console.info("Starting bancadas scraper")
                        stage_logger.info("Starting bancadas scraper")
                        banc = RawBancadaScraper()
                        start_time = datetime.now()
                        banc.get_raw_bancadas(only_current=only_current)
                        scraped_banc = banc.add_bancadas_to_db()
                        end_time = datetime.now()
                        self.scraper_results["bancadas.py"] = ScraperStats(
                            start_time, end_time, int(scraped_banc)
                        )
                        self._load_scraper_results("bancadas.py")

                with log_manager.stage("scraper", "committees") as stage_logger:
                    if self._recent_raw_exists(RawCommittee, days=1):
                        console.info(
                            "Skipping committees scrape: latest raw scrape is within 1 day"
                        )
                        stage_logger.info("Skipped committees scraper")
                    else:
                        console.info("Starting committees scraper")
                        stage_logger.info("Starting committees scraper")
                        comm = RawCommitteeScraper()
                        start_time = datetime.now()
                        comm.get_raw_committees(only_current=only_current)
                        comm.add_committees_to_db()
                        scraped_comm = len(comm.committee_list)
                        end_time = datetime.now()
                        self.scraper_results["committees.py"] = ScraperStats(
                            start_time, end_time, scraped_comm
                        )
                        self._load_scraper_results("committees.py")

                with log_manager.stage("scraper", "organizations") as stage_logger:
                    if self._recent_raw_exists(RawOrganization, days=1):
                        console.info(
                            "Skipping organizations scrape: latest raw scrape is within 1 day"
                        )
                        stage_logger.info("Skipped organizations scraper")
                    else:
                        console.info("Starting organizations scraper")
                        stage_logger.info("Starting organizations scraper")
                        org = RawOrganizationScraper()
                        start_time = datetime.now()
                        org.get_raw_organizations(only_current=only_current)
                        scraped_orgs = len(org.organizations_list)
                        org.add_organizations_to_db()
                        end_time = datetime.now()
                        self.scraper_results["organizations.py"] = ScraperStats(
                            start_time, end_time, scraped_orgs
                        )
                        self._load_scraper_results("organizations.py")

            # =================================================================
            # 2026-2031 BICAMERAL TERM -- per-chamber senado/diputados
            # microsites (see RawCongresistasScraper/RawBancadaScraper/
            # RawCommitteeScraper/RawOrganizationScraper's "2026-2031 BICAMERAL
            # TERM" sections). Runs by default (leg_period is None or
            # settings.LEG_PERIOD) INSTEAD OF the legacy block above, not in
            # addition to it -- see the LEGACY block's comment above.
            # =================================================================

            if leg_period in (None, settings.LEG_PERIOD):
                chamber_cong = RawCongresistasScraper()
                chamber_banc = RawBancadaScraper()
                chamber_comm = RawCommitteeScraper()
                chamber_org = RawOrganizationScraper()
                # Bancada membership is derived from the same roster fetched
                # during the congresistas stage below (see
                # RawBancadaScraper.build_chamber_bancada_html) -- cached
                # here per chamber so the bancadas stage can reuse it
                # without a second roster fetch.
                chamber_rosters: dict[str, list[dict]] = {}

                with log_manager.stage(
                    "scraper", "congresistas_chamber"
                ) as stage_logger:
                    console.info("Starting 2026-2031 congresistas scraper")
                    stage_logger.info("Starting 2026-2031 congresistas scraper")
                    start_time = datetime.now()
                    total_scraped = 0

                    for chamber in ("Senadores", "Diputados"):
                        if self._recent_raw_exists(
                            RawCongresista, days=1, chamber=chamber
                        ):
                            console.info(
                                f"Skipping {chamber} congresistas scrape: "
                                "latest raw scrape is within 1 day"
                            )
                            continue
                        roster = chamber_cong.get_chamber_roster(chamber)
                        scraped = chamber_cong.extract_chamber_congresistas(
                            chamber, roster
                        )
                        if scraped:
                            chamber_cong.add_congresistas_to_db()
                            total_scraped += len(scraped)
                            chamber_rosters[chamber] = roster

                    end_time = datetime.now()
                    self.scraper_results["congresistas_chamber.py"] = ScraperStats(
                        start_time, end_time, total_scraped
                    )
                    self._load_scraper_results("congresistas_chamber.py")

                with log_manager.stage("scraper", "bancadas_chamber") as stage_logger:
                    console.info("Starting 2026-2031 bancadas scraper")
                    stage_logger.info("Starting 2026-2031 bancadas scraper")
                    start_time = datetime.now()
                    total_scraped = 0

                    for chamber in ("Senadores", "Diputados"):
                        roster = chamber_rosters.get(chamber)
                        if roster is None:
                            # Congresistas were skipped (recent) or came back
                            # empty this run -- nothing new to derive bancada
                            # membership from.
                            continue
                        if self._recent_raw_exists(RawBancada, days=1, chamber=chamber):
                            continue
                        chamber_banc.get_chamber_bancadas(chamber, roster)
                        chamber_banc.add_bancadas_to_db()
                        total_scraped += len(chamber_banc.bancadas_list)

                    end_time = datetime.now()
                    self.scraper_results["bancadas_chamber.py"] = ScraperStats(
                        start_time, end_time, total_scraped
                    )
                    self._load_scraper_results("bancadas_chamber.py")

                with log_manager.stage("scraper", "committees_chamber") as stage_logger:
                    console.info("Starting 2026-2031 committees scraper")
                    stage_logger.info("Starting 2026-2031 committees scraper")
                    start_time = datetime.now()
                    total_scraped = 0

                    for chamber in ("Senadores", "Diputados"):
                        if self._recent_raw_exists(
                            RawCommittee, days=1, chamber=chamber
                        ):
                            continue
                        chamber_comm.get_chamber_committees(chamber)
                        if chamber_comm.committee_list:
                            chamber_comm.add_committees_to_db()
                            total_scraped += len(chamber_comm.committee_list)

                    if not self._recent_raw_exists(
                        RawCommittee, days=1, chamber="Congreso"
                    ):
                        chamber_comm.get_joint_committees()
                        if chamber_comm.committee_list:
                            chamber_comm.add_committees_to_db()
                            total_scraped += len(chamber_comm.committee_list)

                    end_time = datetime.now()
                    self.scraper_results["committees_chamber.py"] = ScraperStats(
                        start_time, end_time, total_scraped
                    )
                    self._load_scraper_results("committees_chamber.py")

                with log_manager.stage(
                    "scraper", "organizations_chamber"
                ) as stage_logger:
                    console.info("Starting 2026-2031 organizations scraper")
                    stage_logger.info("Starting 2026-2031 organizations scraper")
                    start_time = datetime.now()
                    total_scraped = 0

                    for chamber in ("Senadores", "Diputados"):
                        if not self._recent_raw_exists(
                            RawOrganization, days=1, chamber=chamber
                        ):
                            chamber_org.get_chamber_organizations(chamber)
                            if chamber_org.organizations_list:
                                chamber_org.add_organizations_to_db()
                                total_scraped += len(chamber_org.organizations_list)

                    if not self._recent_raw_exists(
                        RawOrganization, days=1, chamber="Congreso"
                    ):
                        chamber_org.get_joint_comision_permanente()
                        if chamber_org.organizations_list:
                            chamber_org.add_organizations_to_db()
                            total_scraped += len(chamber_org.organizations_list)

                    end_time = datetime.now()
                    self.scraper_results["organizations_chamber.py"] = ScraperStats(
                        start_time, end_time, total_scraped
                    )
                    self._load_scraper_results("organizations_chamber.py")

        # =====================================================================
        # LEGACY (through 2021-2026) -- bills/motions ALWAYS scrape the
        # legacy range regardless of leg_period (unlike scrape_others above:
        # old bills/motions can still gain new documents/votes/status, so
        # this never becomes opt-in). See the "2026-2031 BICAMERAL TERM"
        # blocks below for the current-period scrape, which runs IN
        # ADDITION TO this, not instead.
        # =====================================================================

        if scrape_bills:
            from backend.scrapers.bills import RawBillScraper

            with log_manager.stage("scraper", "bills") as stage_logger:
                console.info("Starting bills scraper")
                stage_logger.info("Starting bills scraper")
                scraper = RawBillScraper()
                new_results = self._scrape_range(
                    scraper=scraper,
                    raw_model=RawBill,
                    scrape_fn=scraper.scrape_bill,
                    buffer_attr="raw_bills",
                    load_fn=scraper.load_raw_bills,
                    flush_every=100,
                    entity_name="Bills",
                )
                pending_results = self._scrape_pending_daily(
                    raw_model=RawBill,
                    model=Bill,
                    scraper=scraper,
                    scrape_fn=scraper.scrape_bill,
                    buffer_attr="raw_bills",
                    load_fn=scraper.load_raw_bills,
                    max_age_days=1,
                    flush_every=100,
                    entity_name="Bills",
                    chamber_scrape_fn=scraper.scrape_chamber_bill,
                )
                self.scraper_results["bills.py"] = ScraperStats(
                    new_results.start_time,
                    pending_results.end_time,
                    new_results.scrapped + pending_results.scrapped,
                )
                self._load_scraper_results("bills.py")

                # =============================================================
                # 2026-2031 BICAMERAL TERM -- independent per-chamber id
                # sequences (confirmed live 2026-09-01). Each chamber gets its
                # OWN ScraperStats entry (not combined) so "chamber X silently
                # stopped scraping" is visible in stats.errors monitoring.
                # =============================================================
                if leg_period in (None, settings.LEG_PERIOD):
                    for chamber in ("Senadores", "Diputados"):
                        chamber_results = self._scrape_chamber_range(
                            scraper=scraper,
                            raw_model=RawBill,
                            scrape_fn=scraper.scrape_chamber_bill,
                            buffer_attr="raw_bills",
                            load_fn=scraper.load_raw_bills,
                            chamber=chamber,
                            flush_every=100,
                            entity_name="Bills",
                        )
                        key = f"bills_chamber_{chamber.lower()}.py"
                        self.scraper_results[key] = chamber_results
                        self._load_scraper_results(key)

        if scrape_motions:
            from backend.scrapers.motions import RawMotionScraper

            with log_manager.stage("scraper", "motions") as stage_logger:
                console.info("Starting motions scraper")
                stage_logger.info("Starting motions scraper")
                scraper = RawMotionScraper()
                new_results = self._scrape_range(
                    scraper=scraper,
                    raw_model=RawMotion,
                    scrape_fn=scraper.scrape_motion,
                    buffer_attr="raw_motions",
                    load_fn=scraper.load_raw_motions,
                    flush_every=100,
                    entity_name="Motions",
                )
                pending_results = self._scrape_pending_daily(
                    raw_model=RawMotion,
                    model=Motion,
                    scraper=scraper,
                    scrape_fn=scraper.scrape_motion,
                    buffer_attr="raw_motions",
                    load_fn=scraper.load_raw_motions,
                    max_age_days=1,
                    flush_every=100,
                    entity_name="Motions",
                    chamber_scrape_fn=scraper.scrape_chamber_motion,
                )
                self.scraper_results["motions.py"] = ScraperStats(
                    new_results.start_time,
                    pending_results.end_time,
                    new_results.scrapped + pending_results.scrapped,
                )
                self._load_scraper_results("motions.py")

                # 2026-2031 BICAMERAL TERM -- see the bills block above for
                # the full rationale (independent per-chamber stats).
                if leg_period in (None, settings.LEG_PERIOD):
                    for chamber in ("Senadores", "Diputados"):
                        chamber_results = self._scrape_chamber_range(
                            scraper=scraper,
                            raw_model=RawMotion,
                            scrape_fn=scraper.scrape_chamber_motion,
                            buffer_attr="raw_motions",
                            load_fn=scraper.load_raw_motions,
                            chamber=chamber,
                            flush_every=100,
                            entity_name="Motions",
                        )
                        key = f"motions_chamber_{chamber.lower()}.py"
                        self.scraper_results[key] = chamber_results
                        self._load_scraper_results(key)

        if scrape_documents:
            with log_manager.stage("scraper", "documents") as stage_logger:
                console.info("Starting document scraper")
                stage_logger.info("Starting document scraper")
                doc_bill_run, doc_motion_run = self._scrape_pending_documents(upload_s3)
                self.scraper_results["bills_documents.py"] = doc_bill_run
                self.scraper_results["motions_documents.py"] = doc_motion_run
                self._load_scraper_results("bills_documents.py")
                self._load_scraper_results("motions_documents.py")

        if scrape_leyes:
            from backend.scrapers.leyes import RawLeyesScraper

            with log_manager.stage("scraper", "leyes") as stage_logger:
                console.info("Starting leyes scraper")
                stage_logger.info("Starting leyes scraper")
                scraper = RawLeyesScraper()
                self.scraper_results["leyes.py"] = self._scrape_range(
                    scraper=scraper,
                    raw_model=RawLey,
                    scrape_fn=scraper.scrape_ley,
                    buffer_attr="raw_leyes",
                    load_fn=scraper.load_raw_leyes,
                    flush_every=100,
                    entity_name="Leyes",
                )
                self._load_scraper_results("leyes.py")

    def run_processing(
        self,
        *,
        process_bills: bool = True,
        process_motions: bool = True,
        process_leyes: bool = True,
        process_others: bool = True,
        process_documents: bool = True,
        process_votes: bool = False,
        bills_limit: int | None = None,
        leyes_limit: int | None = None,
        motions_limit: int | None = None,
        votes_limit: int | None = None,
        votes_max_pages: int = 5,
        votes_model: str = VOTES_DEFAULT_MODEL,
        votes_max_cost_usd: float = 5.0,
        first_load: bool = False,
        leg_period: str | None = None,
        skip_extraction: bool = True,
    ) -> dict[str, ProcessStats]:
        """
        Process raw -> clean tables.

        leg_period: optionally restrict congresistas/bancadas/organizations
        processing to a single legislative period (a LegPeriod value, e.g.
        "2026-2031") instead of all of PROCESSABLE_LEG_PERIODS. Default (None)
        preserves current behavior exactly. Bills/motions/leyes processing is
        not period-gated and ignores this parameter -- chamber for those is
        resolved per-row from the bill/motion's own id (see process_bill_organizations).

        first_load: besides its existing bill-summary/semantic-embedding
        backfill meaning, also controls the initial start_date for
        2026-2031 chamber/party/bancada/admin-org memberships -- when True,
        the FIRST-ever membership recorded for a given (person, org,
        leg_period, org_type) in the 2026-2031 term is seeded with the
        confirmed real term-start date (2026-07-28) rather than whichever
        date we happened to scrape it on. A later re-scrape that finds an
        existing membership (e.g. someone switched bancadas mid-term) is
        unaffected regardless of first_load -- it always uses the date it
        was detected. Legacy (pre-2026) memberships are never affected by
        this flag either way. See _upsert_membership_schema.
        """
        console = log_manager.console_logger()
        console.info("Starting processing pipeline")
        summary: dict[str, ProcessStats] = {}

        if process_others:
            with log_manager.stage("process", "organizations"):
                console.info("Starting organizations processing")
                summary["organizations"] = self._process_organization_definitions(
                    leg_period=leg_period
                )
                summary["bancadas"] = self._process_bancada_definitions(
                    leg_period=leg_period
                )
                self._log_stage_summary("organizations", summary["organizations"])
                self._log_stage_summary("bancadas", summary["bancadas"])

            with log_manager.stage("process", "congresistas"):
                console.info("Starting congresistas processing")
                summary["congresistas"] = self._process_congresistas(
                    leg_period=leg_period, first_load=first_load
                )
                self._log_stage_summary("congresistas", summary["congresistas"])

            with log_manager.stage("process", "memberships"):
                console.info("Starting memberships processing")
                summary["admin_memberships"] = self._process_admin_memberships(
                    leg_period=leg_period, first_load=first_load
                )
                summary["bancada_memberships"] = self._process_bancada_memberships(
                    leg_period=leg_period, first_load=first_load
                )
                self._log_stage_summary(
                    "admin_memberships", summary["admin_memberships"]
                )
                self._log_stage_summary(
                    "bancada_memberships", summary["bancada_memberships"]
                )

        if process_bills:
            with log_manager.stage("process", "bills"):
                console.info("Starting bills processing")
                summary["bills"] = self._process_bills(
                    limit=bills_limit,
                )
                self._log_stage_summary("bills", summary["bills"])

            with log_manager.stage("process", "bills"):
                console.info("Starting populating bill summaries")
                summary["bill_summary"] = self._process_bills_summaries(
                    first_load=first_load
                )
                self._log_stage_summary("bill_summary", summary["bill_summary"])

            if process_documents:
                with log_manager.stage("process", "bill_text"):
                    console.info("Starting bill text processing")
                    summary["bill_text"] = self._process_bill_text(limit=bills_limit)
                    self._log_stage_summary("bill_text", summary["bill_text"])

                with log_manager.stage("process", "bill_differences"):
                    console.info("Starting bill differences processing")
                    summary["bill_differences"] = self._process_bill_differences(
                        limit=bills_limit,
                    )
                    self._log_stage_summary(
                        "bill_differences", summary["bill_differences"]
                    )

        if process_motions:
            with log_manager.stage("process", "motions"):
                console.info("Starting motions processing")
                summary["motions"] = self._process_motions(
                    include_documents=False,
                    limit=motions_limit,
                )
                self._log_stage_summary("motions", summary["motions"])

        if process_leyes:
            with log_manager.stage("process", "leyes"):
                console.info("Starting leyes processing")
                summary["leyes"] = self._process_leyes(limit=leyes_limit)
                self._log_stage_summary("leyes", summary["leyes"])

        if process_votes:
            with log_manager.stage("process", "votes"):
                if not skip_extraction:
                    for kind in ("bill", "motion"):
                        console.info(f"Starting vote extraction ({kind})")
                        key = f"votes_extraction_{kind}"
                        summary[key] = self._process_vote_extraction(
                            kind=kind,
                            model=votes_model,
                            max_pages=votes_max_pages,
                            limit=votes_limit,
                            max_cost_usd=votes_max_cost_usd,
                        )
                        self._log_stage_summary(key, summary[key])

                for kind in ("bill", "motion"):
                    console.info(f"Starting vote load ({kind})")
                    key = f"votes_load_{kind}"
                    summary[key] = self._process_vote_load(
                        kind=kind, model=votes_model, limit=votes_limit
                    )
                    self._log_stage_summary(key, summary[key])
        # Running the semantic search
        with log_manager.stage("process", "semantic_table"):
            console.info("Starting semantic table population")
            summary["semantic"] = self._semantic_table(first_load=first_load)
            self._log_stage_summary("semantic", summary["semantic"])

        return summary

    # -----------------------------
    # Scraping internals
    # -----------------------------
    def _scrape_range(
        self,
        scraper,
        raw_model: Type[RawBill] | Type[RawMotion] | Type[RawLey],
        scrape_fn: Callable[[str, str], None],
        buffer_attr: str,
        load_fn: Callable[[], None],
        flush_every: int = 100,
        entity_name: str = "items",
    ) -> ScraperStats:
        """Scrape ids from (last_scraped+1) up to the remote max, flushing
        every ``flush_every`` rows. Also retries any gap ids found below
        the watermark (see _find_id_gaps) before the forward scan -- a
        gap-retry sub-loop, throttled the same as _scrape_chamber_range's
        forward pass so a first-ever gap backfill can't hammer the live
        site unattended, and capped per-id via ScrapeGapRetry so a
        legitimately-nonexistent id isn't retried forever.
        """
        start = self._get_last_id_scraped(raw_model) + 1
        end = get_last_id(entity_name)
        # TODO: update this for next congreso
        year = 2021

        def _call_scrape_fn(number: int) -> None:
            if entity_name == "Leyes":
                scrape_fn(number)
            else:
                scrape_fn(str(year), str(number))

        start_time = datetime.now()
        count = 0

        gaps = self._find_id_gaps(raw_model)
        if gaps:
            logger.info(f"Retrying {len(gaps)} gap id(s) for {entity_name}: {gaps}")
            for idx, number in enumerate(gaps, start=1):
                try:
                    _call_scrape_fn(number)
                except Exception as exc:
                    logger.warning(
                        f"Gap retry failed for {entity_name} id {number}: {exc}"
                    )

                current_length = len(getattr(scraper, buffer_attr))
                if current_length >= flush_every:
                    count += current_length
                    load_fn()

                if idx % 10 == 0:
                    time.sleep(2)

            # Flush before checking outcomes -- otherwise a successfully
            # re-scraped gap id would still look "missing" to
            # _record_gap_retry_outcomes (it checks the DB, not the
            # in-memory buffer), incorrectly bumping its retry count.
            remaining_gap_buffer = len(getattr(scraper, buffer_attr))
            if remaining_gap_buffer:
                count += remaining_gap_buffer
                load_fn()

            self._record_gap_retry_outcomes(raw_model, gaps)

        logger.info(f"Scraping {entity_name} in range {year}_{start}..{year}_{end}")

        for number in tqdm(range(start, end + 1), desc=entity_name):
            try:
                _call_scrape_fn(number)
            except Exception as exc:
                logger.warning(f"Scrape failed for {entity_name} id {number}: {exc}")
                continue

            current_length = len(getattr(scraper, buffer_attr))

            if current_length >= flush_every:
                count += current_length
                load_fn()

        remaining = len(getattr(scraper, buffer_attr))

        if remaining:
            count += remaining
            load_fn()

        end_time = datetime.now()
        return ScraperStats(start_time, end_time, count)

    def _scrape_chamber_range(
        self,
        scraper,
        raw_model: Type[RawBill] | Type[RawMotion],
        scrape_fn: Callable[[str, int], None],
        buffer_attr: str,
        load_fn: Callable[[], None],
        chamber: str,
        flush_every: int = 100,
        entity_name: str = "Bills",
    ) -> ScraperStats:
        """2026-2031 counterpart to _scrape_range: one independent
        per-chamber id sequence (Senado/Diputados numbers don't share a
        range). ``scrape_fn`` is scraper.scrape_chamber_bill/
        scrape_chamber_motion, taking (chamber, number).

        Throttled (unlike _scrape_range, left untouched): the very first
        run after this ships unattended-backfills the ENTIRE existing
        2026-2031 history for this chamber through a recaptcha-protected
        path (bills specifically, via Playwright) -- _scrape_range's lack
        of throttling is harmless for legacy only because its backlog is
        already caught up in steady state. Mirrors _scrape_pending_daily's
        already-proven interval.
        """
        id_suffix = CHAMBER_LABEL_TO_ID_SUFFIX[chamber]
        start = self._get_last_id_scraped(raw_model, id_suffix=id_suffix) + 1
        end = get_last_id(
            entity_name,
            per_par_id=LEG_PERIOD_TO_PER_PAR_ID["2026-2031"],
            cod_tipo_parl=CHAMBER_LABEL_TO_COD_TIPO_PARL[chamber],
        )

        start_time = datetime.now()
        count = 0

        gaps = self._find_id_gaps(raw_model, id_suffix=id_suffix)
        if gaps:
            logger.info(
                f"Retrying {len(gaps)} gap id(s) for {entity_name} ({chamber}): {gaps}"
            )
            for idx, number in enumerate(gaps, start=1):
                try:
                    scrape_fn(chamber, number)
                except Exception as exc:
                    logger.warning(
                        f"Gap retry failed for {entity_name} ({chamber}) id {number}: {exc}"
                    )

                current_length = len(getattr(scraper, buffer_attr))
                if current_length >= flush_every:
                    count += current_length
                    load_fn()

                if idx % 10 == 0:
                    time.sleep(2)

            # Flush before checking outcomes -- otherwise a successfully
            # re-scraped gap id would still look "missing" to
            # _record_gap_retry_outcomes (it checks the DB, not the
            # in-memory buffer), incorrectly bumping its retry count.
            remaining_gap_buffer = len(getattr(scraper, buffer_attr))
            if remaining_gap_buffer:
                count += remaining_gap_buffer
                load_fn()

            self._record_gap_retry_outcomes(raw_model, gaps, id_suffix=id_suffix)

        logger.info(f"Scraping {entity_name} ({chamber}) in range {start}..{end}")

        for idx, number in enumerate(
            tqdm(range(start, end + 1), desc=f"{entity_name} ({chamber})"), start=1
        ):
            try:
                scrape_fn(chamber, number)
            except Exception as exc:
                logger.warning(
                    f"Scrape failed for {entity_name} ({chamber}) id {number}: {exc}"
                )
                continue

            current_length = len(getattr(scraper, buffer_attr))

            if current_length >= flush_every:
                count += current_length
                load_fn()

            if idx % 10 == 0:
                time.sleep(2)

        remaining = len(getattr(scraper, buffer_attr))

        if remaining:
            count += remaining
            load_fn()

        end_time = datetime.now()
        return ScraperStats(start_time, end_time, count)

    def _scrape_pending_daily(
        self,
        raw_model: Type[RawBill] | Type[RawMotion],
        model: Type[Bill] | Type[Motion],
        scraper,
        scrape_fn: Callable[[str, str], None],
        buffer_attr: str,
        load_fn: Callable[[], None],
        max_age_days: int = 1,
        flush_every: int = 100,
        entity_name: str = "items",
        chamber_scrape_fn: Callable[[str, int], None] | None = None,
    ) -> ScraperStats:
        """Re-scrape rows older than ``max_age_days`` and not yet approved, sleeping briefly every 10 ids.

        chamber_scrape_fn: optional -- when a pending id is new-format
        (2026-2031, detected via chamber_label_from_id), dispatches to this
        instead of the legacy scrape_fn(year, number) path. Reuses the
        existing chamber_label_from_id() rather than reimplementing suffix
        parsing. Legacy-format ids are entirely unaffected (identical to
        today) regardless of whether chamber_scrape_fn is provided.
        """
        pending_ids = self._get_ids_to_update(raw_model, model, max_age_days)
        start_time = datetime.now()
        count = 0

        for idx, item_id in enumerate(
            tqdm(pending_ids, desc=f"Pending {entity_name}"), start=1
        ):
            chamber_label = chamber_label_from_id(item_id)
            if chamber_label is None:
                year, number = item_id.split("_", 1)
                scrape_fn(str(year), str(number))
            elif chamber_scrape_fn is not None:
                number = int(item_id.split("-", 1)[0])
                chamber_scrape_fn(chamber_label, number)
            else:
                logger.warning(
                    f"Skipping pending id {item_id}: chamber-scoped id but "
                    "no chamber_scrape_fn provided"
                )
                continue

            current_length = len(getattr(scraper, buffer_attr))

            if current_length >= flush_every:
                count += current_length
                load_fn()

            if idx % 10 == 0:
                time.sleep(2)

        remaining = len(getattr(scraper, buffer_attr))

        if remaining:
            count += remaining
            load_fn()

        end_time = datetime.now()
        return ScraperStats(start_time, end_time, count)

    @staticmethod
    def _document_identity(document, entity_attr: str) -> tuple[str, str, str]:
        return (
            str(getattr(document, entity_attr)),
            str(document.step_id),
            str(document.file_id),
        )

    def _upload_documents(
        self,
        *,
        scraper,
        documents: list,
        document_kind: str,
        phase: str,
    ) -> _DocumentUploadStats:
        succeeded = 0
        failed = 0

        with ThreadPoolExecutor(max_workers=15) as executor:
            future_to_doc = {
                executor.submit(scraper.upload_s3, document): document
                for document in documents
            }

            for future in tqdm(
                as_completed(future_to_doc),
                total=len(documents),
                desc=f"{document_kind.title()} {phase} S3 uploads",
            ):
                document = future_to_doc[future]
                try:
                    ok = future.result()
                except Exception as exc:
                    ok = False
                    logger.exception(
                        f"Unexpected {document_kind} S3 upload error for "
                        f"step_id={document.step_id} file_id={document.file_id}: {exc}"
                    )

                if ok:
                    succeeded += 1
                else:
                    failed += 1

        stats = _DocumentUploadStats(
            total=len(documents),
            succeeded=succeeded,
            failed=failed,
        )
        logger.info(
            f"{document_kind.title()} {phase} S3 upload complete: "
            f"{stats.succeeded} succeeded, {stats.failed} failed, "
            f"{stats.total} total"
        )
        return stats

    def _scrape_pending_documents(
        self, upload_s3: bool = False
    ) -> tuple[ScraperStats, ScraperStats]:
        """Upload backlogs, scrape missing bill/motion documents, and upload new rows."""
        from backend.scrapers.bills_documents import RawBillDocumentScraper
        from backend.scrapers.motions_documents import RawMotionDocumentScraper

        logger.info("Scraping pending bill and motion documents")

        bill_docs = RawBillDocumentScraper()
        motion_docs = RawMotionDocumentScraper()
        bill_backlog_ids: set[tuple[str, str, str]] = set()
        motion_backlog_ids: set[tuple[str, str, str]] = set()

        if upload_s3:
            bill_backlog = bill_docs.get_docs_pending_s3_upload()
            motion_backlog = motion_docs.get_docs_pending_s3_upload()
            bill_backlog_ids = {
                self._document_identity(document, "bill_id")
                for document in bill_backlog
            }
            motion_backlog_ids = {
                self._document_identity(document, "motion_id")
                for document in motion_backlog
            }
            self._upload_documents(
                scraper=bill_docs,
                documents=bill_backlog,
                document_kind="bill",
                phase="backlog",
            )
            self._upload_documents(
                scraper=motion_docs,
                documents=motion_backlog,
                document_kind="motion",
                phase="backlog",
            )

        start_time = datetime.now()
        count = 0
        bill_ids = bill_docs.get_bills_pending_documents()
        for bill_id in tqdm(bill_ids, desc="Bill documents"):
            bill_docs.get_bill_documents(
                bill_id=bill_id,
                update=False,
                download_local=False,
            )
            count += len(bill_docs.documents)
            bill_docs.load_raw_documents()
        end_time = datetime.now()
        doc_bill_run = ScraperStats(start_time, end_time, count)

        start_time = datetime.now()
        count = 0
        motion_ids = motion_docs.get_motions_pending_documents()
        for motion_id in tqdm(motion_ids, desc="Motion documents"):
            motion_docs.get_motion_documents(
                motion_id=motion_id,
                update=False,
                download_local=False,
            )
            count += len(motion_docs.documents)
            motion_docs.load_raw_documents()
        end_time = datetime.now()
        doc_motion_run = ScraperStats(start_time, end_time, count)

        if upload_s3:
            new_bill_documents = [
                document
                for document in bill_docs.get_docs_pending_s3_upload()
                if self._document_identity(document, "bill_id") not in bill_backlog_ids
            ]
            new_motion_documents = [
                document
                for document in motion_docs.get_docs_pending_s3_upload()
                if self._document_identity(document, "motion_id")
                not in motion_backlog_ids
            ]
            self._upload_documents(
                scraper=bill_docs,
                documents=new_bill_documents,
                document_kind="bill",
                phase="new-document",
            )
            self._upload_documents(
                scraper=motion_docs,
                documents=new_motion_documents,
                document_kind="motion",
                phase="new-document",
            )

        return doc_bill_run, doc_motion_run

    # -----------------------------
    # Processing internals
    # -----------------------------
    def _semantic_table(
        self,
        model_name: str = "intfloat/multilingual-e5-base",
        first_load: bool = False,
    ) -> ProcessStats:
        """Populate semantic_bills. On first_load, rebuild embeddings for every bill;
        otherwise only re-embed bills whose raw content changed or that have no
        semantic_bills rows yet."""
        with self.DBSession() as db:
            if first_load:
                bill_ids = list(db.execute(select(db_models.Bill.id)).scalars().all())
                processed_chunks = crud_embeddings.rebuild_semantic_bills(
                    db, embedding_model_name=model_name
                )
            else:
                changed_ids = (
                    select(db_models.Bill.id)
                    .join(RawBill, db_models.Bill.id == RawBill.id)
                    .where(RawBill.last_update.is_(True), RawBill.changed.is_(True))
                )
                unembedded_ids = select(db_models.Bill.id).where(
                    ~db_models.Bill.id.in_(select(db_models.SemanticBill.bill_id))
                )
                bill_ids = list(
                    db.execute(changed_ids.union(unembedded_ids)).scalars().all()
                )
                processed_chunks = crud_embeddings.bulk_upsert_semantic_bills(
                    db, bill_ids, model_name
                )

            db.commit()

        return ProcessStats(
            processed=len(bill_ids),
            skipped=0,
            errors=0 if processed_chunks or not bill_ids else len(bill_ids),
        )

    def _membership_dates(
        self, membership: Membership, *, seed_override: date | None = None
    ) -> tuple[date, date]:
        """Resolve a membership's start/end, falling back to the legislative-year window (Jul 28 → Jul 28).

        seed_override: when given, used instead of membership.time_stamp as
        the seed for deriving the legislative-year window (still only when
        membership.start_date itself is unset). See _upsert_membership_schema
        for when this is the 2026-2031 term-start date rather than the scrape
        timestamp.
        """
        seed = membership.start_date or seed_override or membership.time_stamp
        leg_year = get_current_leg_year(seed)
        derived_start = date(leg_year, 7, 28)
        derived_end = date(leg_year + 1, 7, 28)

        start = membership.start_date or derived_start
        if isinstance(start, datetime):
            start = start.date()

        end = membership.end_date
        if isinstance(end, datetime):
            end = end.date()
        if end is None or end < start:
            end = derived_end

        return start, end

    def _upsert_organization_with_count(
        self, db, org_schema: Organization
    ) -> tuple[db_models.Organization, bool]:
        """Upsert an organization; second return is True if this was a new insert."""
        # Resolve org_schema's own declared parent (None for top-level orgs like
        # chambers/parties) so the pre-check matches the real org_uniq constraint
        # (org_name, org_type, parent_org_id) — otherwise insert/update counting
        # would misreport once same-named orgs exist under different parents.
        parent_org_id = None
        if org_schema.parent_org_name and org_schema.parent_org_type:
            parent = crud_core.find_organization(
                db,
                org_name=org_schema.parent_org_name,
                org_type=org_schema.parent_org_type,
            )
            parent_org_id = parent.org_id if parent else None

        pre = crud_core.find_organization(
            db,
            org_name=org_schema.org_name,
            org_type=org_schema.org_type,
            parent_org_id=parent_org_id,
        )
        org = crud_core.upsert_organization(db, org_schema)
        return org, pre is None

    def _upsert_membership_schema(
        self,
        db,
        *,
        cong: db_models.Congresista,
        org: db_models.Organization,
        membership: Membership,
        first_load: bool = False,
    ) -> db_models.Membership:
        """Upsert a Membership row linking ``cong`` to ``org`` with derived dates and non-null extras.

        first_load: when True AND membership.leg_period is the 2026-2031
        term AND no Membership row has ever been recorded for this
        (person, org, leg_period, org_type) before, seeds the start-date
        derivation with the confirmed real term-start date (2026-07-28)
        instead of the scrape timestamp -- every congresista's chamber/
        party/bancada/admin membership genuinely began that day, regardless
        of when we happened to scrape their profile. Only the FIRST-ever
        record for a given membership gets this treatment: a later re-scrape
        that finds an existing record (e.g. someone switched bancadas
        mid-term) falls through to the existing timestamp-derived fallback,
        since that membership change genuinely started whenever we detected
        it, not on day one of the term.
        """
        seed_override = None
        if first_load and membership.leg_period == LegPeriod.PERIODO_2026_2031:
            already_exists = crud_core.membership_exists(
                db,
                person_id=cong.id,
                org_id=org.org_id,
                leg_period=membership.leg_period,
                org_type=org.org_type,
            )
            if not already_exists:
                seed_override = next(
                    start
                    for period, start, _ in LEG_PERIOD_RANGES
                    if period == LegPeriod.PERIODO_2026_2031
                )

        start_date, end_date = self._membership_dates(
            membership, seed_override=seed_override
        )
        extra_fields = {
            "condicion": membership.condicion,
            "votes_in_election": membership.votes_in_election,
            "dist_electoral": membership.dist_electoral,
        }
        extra_fields = {k: v for k, v in extra_fields.items() if v is not None}
        return crud_core.upsert_membership(
            db=db,
            person_id=cong.id,
            org_id=org.org_id,
            leg_period=membership.leg_period,
            org_type=org.org_type,
            role=membership.role,
            start_date=start_date,
            end_date=end_date,
            extra_fields=extra_fields,
        )

    def _process_congresistas(
        self, *, leg_period: str | None = None, first_load: bool = False
    ) -> ProcessStats:
        """Process unprocessed RawCongresista rows into Congresista + Organization + Membership records."""
        stats = ProcessStats()
        clean_inserted = 0
        clean_updated = 0
        chamber_tally = {"Diputados": 0, "Senadores": 0, "None": 0}

        CONG_JSON = directories.PROCESSED_DATA / "cong_info_2021_2026.json"
        CONG_JSON_2026 = directories.PROCESSED_DATA / "cong_info_2026_2031.json"

        dict_cong_data = get_cong_data(CONG_JSON)
        # Mined from 2026-2031 bill/motion firmantes (see gen_congresistas_df/
        # get_cong_data's leg_period="2026-2031" docstrings) -- coverage
        # grows as the term progresses, gracefully empty/partial early on.
        dict_cong_data_current = get_cong_data(CONG_JSON_2026, leg_period="2026-2031")
        processable_periods = resolve_processable_leg_periods(leg_period)
        with self.DBSession() as db:
            rows = (
                db.query(RawCongresista)
                .filter(
                    RawCongresista.last_update.is_(True),
                    RawCongresista.processed.is_(False),
                )
                .all()
            )
            for raw_cong in tqdm(rows, desc="Process congresistas"):
                try:
                    if raw_cong.leg_period not in processable_periods:
                        raw_cong.processed = False
                        stats.skipped += 1
                        continue
                    cong_schema, org_schemas, profile_memberships = (
                        process_profile_content(
                            raw_cong,
                            dict_cong_data,
                            dict_cong_data_current=dict_cong_data_current,
                        )
                    )
                    pre = crud_core.find_congresista(
                        db,
                        name=cong_schema.full_name,
                        website=cong_schema.website,
                        congresista_id=cong_schema.congresista_id,
                    )
                    photo_url_changed = (
                        pre is not None and pre.photo_url != cong_schema.photo_url
                    )
                    cong = crud_core.upsert_congresista(db, cong_schema)
                    if pre is None:
                        clean_inserted += 1
                        try:
                            sync_congresista_photo(db, cong)
                        except Exception as photo_exc:
                            logger.warning(
                                f"Photo sync failed for congresista {cong.id}: {photo_exc}"
                            )
                    else:
                        clean_updated += 1
                        if photo_url_changed:
                            try:
                                sync_congresista_photo(db, cong, force=True)
                            except Exception as photo_exc:
                                logger.warning(
                                    f"Photo re-sync failed for congresista {cong.id}: {photo_exc}"
                                )

                    chamber_org_id = None
                    for org_schema in org_schemas:
                        upserted_org, _ = self._upsert_organization_with_count(
                            db, org_schema
                        )
                        if org_schema.org_type == TypeOrganization.CHAMBER:
                            chamber_org_id = upserted_org.org_id

                    memberships = profile_memberships
                    if raw_cong.memberships_content:
                        memberships.extend(
                            process_cong_memberships(raw_cong, cong_schema)
                        )
                    for ms in memberships:
                        # TODO: We need to implement a fuzzy match for finding organization
                        # party_mem/chamber_mem entries are top-level (parent_org_id=NULL
                        # by design, see _CHAMBER_UNSCOPED_ORG_TYPES) and must NOT be
                        # scoped by chamber_org_id — only committee/admin/bancada
                        # memberships (from process_cong_memberships) are actually
                        # children of this congresista's chamber.
                        if ms.org_type in _CHAMBER_UNSCOPED_ORG_TYPES:
                            org = crud_core.find_organization(
                                db=db,
                                org_name=ms.org_name,
                                org_type=ms.org_type,
                                parent_org_id=None,
                            )
                        else:
                            # Committee/admin/bancada memberships are usually
                            # children of this congresista's own chamber, but
                            # some are genuinely joint, whole-Congress bodies
                            # (Comisión Permanente, Comisión Bicameral de
                            # Presupuesto...) parented under
                            # WHOLE_CONGRESS_ORG_NAME instead -- see
                            # CHAMBER_LABEL_TO_ORG_NAME.
                            org = crud_core.find_organization_with_congreso_fallback(
                                db,
                                org_name=ms.org_name,
                                org_type=ms.org_type,
                                own_parent_org_id=chamber_org_id,
                            )
                        if org is None:
                            logger.warning(
                                f"Skipping Membership org_name={ms.org_name} for org_type={ms.org_type} and Congresista={cong.full_name}"
                            )
                            stats.skipped += 1
                            continue
                        self._upsert_membership_schema(
                            db,
                            cong=cong,
                            org=org,
                            membership=ms,
                            first_load=first_load,
                        )

                    raw_cong.processed = True
                    stats.processed += 1
                    chamber_tally[raw_cong.chamber or "None"] = (
                        chamber_tally.get(raw_cong.chamber or "None", 0) + 1
                    )
                    db.commit()
                except Exception as exc:
                    logger.exception(
                        f"Error processing RawCongresista id={raw_cong.id}: {exc}"
                    )
                    db.rollback()
                    stats.errors += 1
        logger.info(
            f"[congresistas] raw_total={len(rows)} processed={stats.processed} skipped={stats.skipped} errors={stats.errors} clean_inserted={clean_inserted} clean_updated={clean_updated} by_chamber={chamber_tally}"
        )
        return stats

    def _process_organization_definitions(
        self, *, leg_period: str | None = None
    ) -> ProcessStats:
        """Load chambers, committees, and admin organizations into the clean Organization table."""
        stats = ProcessStats()
        clean_inserted = 0
        clean_updated = 0
        processable_year_range = get_processable_year_range(leg_period)
        committee_chamber_tally = {
            "Diputados": 0,
            "Senadores": 0,
            "Congreso": 0,
            "None": 0,
        }
        org_chamber_tally = {"Diputados": 0, "Senadores": 0, "Congreso": 0, "None": 0}
        with self.DBSession() as db:
            for org_schema in process_chambers():
                try:
                    _, inserted = self._upsert_organization_with_count(db, org_schema)
                    if inserted:
                        clean_inserted += 1
                    else:
                        clean_updated += 1
                    db.commit()
                except Exception as exc:
                    logger.exception(
                        f"Error processing chamber org_name={org_schema.org_name}: {exc}"
                    )
                    db.rollback()
                    stats.errors += 1

            # Committees
            committees = (
                db.query(RawCommittee)
                .filter(
                    RawCommittee.last_update.is_(True),
                    RawCommittee.processed.is_(False),
                )
                .all()
            )

            for raw_comm in tqdm(committees, desc="Process committees"):
                try:
                    if int(raw_comm.legislative_year) not in processable_year_range:
                        raw_comm.processed = False
                        stats.skipped += 1
                        continue
                    for org_schema in process_committee(raw_comm):
                        _, inserted = self._upsert_organization_with_count(
                            db, org_schema
                        )
                        if inserted:
                            clean_inserted += 1
                        else:
                            clean_updated += 1
                    raw_comm.processed = True
                    stats.processed += 1
                    key = raw_comm.chamber or "None"
                    committee_chamber_tally[key] = (
                        committee_chamber_tally.get(key, 0) + 1
                    )
                    db.commit()
                except Exception as exc:
                    logger.exception(
                        f"Error processing RawCommittee id={raw_comm.id}: {exc}"
                    )
                    db.rollback()
                    stats.errors += 1

            # Administrative organization definitions. RawOrganization is marked
            # processed only after its memberships are loaded.
            organizations = (
                db.query(RawOrganization)
                .filter(
                    RawOrganization.last_update.is_(True),
                    RawOrganization.processed.is_(False),
                )
                .all()
            )
            for raw_org in tqdm(organizations, desc="Process organizations"):
                try:
                    if int(raw_org.legislative_year) not in processable_year_range:
                        raw_org.processed = False
                        stats.skipped += 1
                        continue
                    org_schema, _ = process_admin_org(raw_org)
                    _, inserted = self._upsert_organization_with_count(db, org_schema)
                    if inserted:
                        clean_inserted += 1
                    else:
                        clean_updated += 1
                    stats.processed += 1
                    key = raw_org.chamber or "None"
                    org_chamber_tally[key] = org_chamber_tally.get(key, 0) + 1
                    db.commit()
                except Exception as exc:
                    logger.exception(
                        f"Error processing RawOrganization id={raw_org.id}: {exc}"
                    )
                    db.rollback()
                    stats.errors += 1

        logger.info(
            f"[organization_definitions] raw_committees={len(committees)} raw_orgs={len(organizations)} processed={stats.processed} skipped={stats.skipped} errors={stats.errors} clean_inserted={clean_inserted} clean_updated={clean_updated} committees_by_chamber={committee_chamber_tally} orgs_by_chamber={org_chamber_tally}"
        )
        return stats

    def _process_admin_memberships(
        self, *, leg_period: str | None = None, first_load: bool = False
    ) -> ProcessStats:
        """Link congresistas to admin organizations; marks RawOrganization processed only if all members resolved."""
        stats = ProcessStats()
        processable_year_range = get_processable_year_range(leg_period)
        with self.DBSession() as db:
            organizations = (
                db.query(RawOrganization)
                .filter(
                    RawOrganization.last_update.is_(True),
                    RawOrganization.processed.is_(False),
                )
                .all()
            )
            for raw_org in tqdm(organizations, desc="Process admin memberships"):
                try:
                    if int(raw_org.legislative_year) not in processable_year_range:
                        raw_org.processed = False
                        stats.skipped += 1
                        continue
                    org_schema, membership_list = process_admin_org(raw_org)
                    org, _ = self._upsert_organization_with_count(db, org_schema)
                    missing = False
                    for ms in membership_list:
                        cong = crud_core.find_congresista(
                            db,
                            name=ms.cong_name,
                            website=ms.website,
                        )
                        if cong is None:
                            missing = True
                            stats.skipped += 1
                            continue
                        self._upsert_membership_schema(
                            db,
                            cong=cong,
                            org=org,
                            membership=ms,
                            first_load=first_load,
                        )
                    raw_org.processed = not missing
                    stats.processed += 1
                    db.commit()
                except Exception as exc:
                    logger.exception(
                        f"Error processing RawOrganization memberships id={raw_org.id}: {exc}"
                    )
                    db.rollback()
                    stats.errors += 1

        logger.info(
            f"[admin_memberships] raw_orgs={len(organizations)} processed={stats.processed} skipped={stats.skipped} errors={stats.errors}"
        )
        return stats

    def _process_bancada_definitions(
        self, *, leg_period: str | None = None
    ) -> ProcessStats:
        """Upsert Organization rows for each bancada in a processable legislative period."""
        stats = ProcessStats()
        clean_inserted = 0
        clean_updated = 0
        processable_periods = resolve_processable_leg_periods(leg_period)
        chamber_tally = {"Diputados": 0, "Senadores": 0, "None": 0}
        with self.DBSession() as db:
            rows = (
                db.query(RawBancada)
                .filter(
                    RawBancada.last_update.is_(True), RawBancada.processed.is_(False)
                )
                .all()
            )
            for raw_bancada in tqdm(rows, desc="Process bancada definitions"):
                try:
                    if raw_bancada.legislative_period not in processable_periods:
                        raw_bancada.processed = False
                        stats.skipped += 1
                        continue
                    bancadas, _ = process_bancada(raw_bancada)
                    missing = False
                    for bancada in bancadas:
                        org, inserted = self._upsert_organization_with_count(
                            db, bancada
                        )
                        if inserted:
                            clean_inserted += 1
                        else:
                            clean_updated += 1
                    stats.processed += 1
                    raw_bancada.processed = not missing
                    key = raw_bancada.chamber or "None"
                    chamber_tally[key] = chamber_tally.get(key, 0) + 1
                    db.commit()
                except Exception as exc:
                    logger.exception(
                        f"Error processing RawBancada definitions id={raw_bancada.id}: {exc}"
                    )
                    db.rollback()
                    stats.errors += 1

        logger.info(
            f"[bancada_definitions] raw_total={len(rows)} processed={stats.processed} skipped={stats.skipped} errors={stats.errors} clean_inserted={clean_inserted} clean_updated={clean_updated} by_chamber={chamber_tally}"
        )
        return stats

    def _process_bancada_memberships(
        self, *, leg_period: str | None = None, first_load: bool = False
    ) -> ProcessStats:
        """Link congresistas to bancada organizations; only marks raw processed when every member resolves."""
        stats = ProcessStats()
        processable_periods = resolve_processable_leg_periods(leg_period)
        chamber_tally = {"Diputados": 0, "Senadores": 0, "None": 0}
        with self.DBSession() as db:
            rows = (
                db.query(RawBancada)
                .filter(
                    RawBancada.last_update.is_(True), RawBancada.processed.is_(False)
                )
                .all()
            )
            for raw_bancada in tqdm(rows, desc="Process bancada memberships"):
                try:
                    if raw_bancada.legislative_period not in processable_periods:
                        raw_bancada.processed = False
                        stats.skipped += 1
                        continue
                    _, memberships = process_bancada(raw_bancada)

                    # Resolve this row's chamber org_id once (not per membership) so
                    # same-named bancadas across chambers (confirmed real — Step 0
                    # item 7) don't collide via the unscoped fuzzy match.
                    chamber_org_name = CHAMBER_LABEL_TO_ORG_NAME[raw_bancada.chamber]
                    bancada_parent_org_id = None
                    if chamber_org_name is not None:
                        chamber_org = crud_core.find_organization(
                            db,
                            org_name=chamber_org_name,
                            org_type=TypeOrganization.CHAMBER,
                        )
                        bancada_parent_org_id = (
                            chamber_org.org_id if chamber_org else None
                        )

                    missing = False
                    for ms in memberships:
                        cong = crud_core.find_congresista(
                            db,
                            name=ms.cong_name,
                            website=ms.website,
                        )
                        org = crud_core.find_organization(
                            db,
                            org_name=ms.org_name,
                            org_type=ms.org_type,
                            parent_org_id=bancada_parent_org_id,
                        )
                        if cong is None or org is None:
                            logger.warning(
                                f"Skipping BancadaMembership raw_id={raw_bancada.id}, cong={ms.cong_name}, website={ms.website}, org={ms.org_name}, org_type={ms.org_type}: reference not found"
                            )
                            missing = True
                            stats.skipped += 1
                            continue
                        self._upsert_membership_schema(
                            db,
                            cong=cong,
                            org=org,
                            membership=ms,
                            first_load=first_load,
                        )

                    raw_bancada.processed = not missing
                    stats.processed += 1
                    key = raw_bancada.chamber or "None"
                    chamber_tally[key] = chamber_tally.get(key, 0) + 1
                    db.commit()
                except Exception as exc:
                    logger.exception(
                        f"Error processing RawBancada memberships id={raw_bancada.id}: {exc}"
                    )
                    db.rollback()
                    stats.errors += 1

        logger.info(
            f"[bancada_memberships] raw_total={len(rows)} processed={stats.processed} skipped={stats.skipped} errors={stats.errors} by_chamber={chamber_tally}"
        )
        return stats

    def _process_legislative_documents(
        self,
        *,
        config: _LegislativeDocumentConfig,
        limit: int | None,
        document_hook: Callable[[Session, str, ProcessStats], None] | None = None,
    ) -> ProcessStats:
        """Shared implementation behind _process_bills and _process_motions
        -- both raw document types flow through identical query -> chamber
        resolution -> upsert -> steps -> orgs -> congresistas control flow,
        differing only in which raw/clean models and crud functions apply
        (see config) and, for motions, an optional document-text extraction
        pass after the congresista loop (document_hook).

        The org_type-conditional parent_org_id scoping below (chamber's own
        entry never scoped by its own org_id) is load-bearing, not
        incidental -- this codebase has a recorded history of exactly this
        conditional being dropped when a shared loop mixes a parent org
        with its children. Preserved unchanged from both original
        implementations.
        """
        stats = ProcessStats()
        clean_inserted = 0
        clean_updated = 0
        with self.DBSession() as db:
            query = db.query(config.raw_model).filter(
                config.raw_model.last_update.is_(True),
                config.raw_model.processed.is_(False),
            )
            if limit is not None:
                query = query.limit(limit)
            rows = query.all()

            for raw_row in tqdm(rows, desc=config.desc):
                try:
                    schema_obj, cong_rels, steps = config.process_fn(raw_row)

                    orgs = config.process_organizations_fn(raw_row, steps)
                    chamber_schema = find_organization_schema(
                        orgs,
                        org_type=TypeOrganization.CHAMBER.value,
                    )

                    if chamber_schema is None:
                        logger.warning(
                            f"Skipping Raw{config.entity_label} id={raw_row.id}: chamber relation not generated"
                        )
                        stats.skipped += 1
                        continue
                    chamber = crud_core.find_organization(
                        db,
                        org_name=chamber_schema.org_name,
                        org_type=TypeOrganization.CHAMBER.value,
                    )
                    if chamber is None:
                        logger.warning(
                            f"Skipping Raw{config.entity_label} id={raw_row.id}: {chamber_schema.org_name} organization not found"
                        )
                        stats.skipped += 1
                        continue

                    pre = db.get(config.clean_model, schema_obj.id)
                    entity = getattr(config.crud_module, config.upsert_name)(
                        db, schema_obj
                    )
                    if pre is None:
                        clean_inserted += 1
                    else:
                        clean_updated += 1

                    for step_schema in steps:
                        getattr(config.crud_module, config.upsert_step_name)(
                            db, step_schema
                        )

                    for org_schema in orgs:
                        # The chamber's own entry (org_schema.org_type ==
                        # CHAMBER) must NOT be scoped by chamber.org_id as
                        # its own parent — only committee-type entries are
                        # actually children of this entity's chamber.
                        if org_schema.org_type == TypeOrganization.CHAMBER:
                            org = crud_core.find_organization(
                                db=db,
                                org_name=org_schema.org_name,
                                org_type=org_schema.org_type,
                                parent_org_id=None,
                            )
                        else:
                            # Usually a child of this bill/motion's own
                            # chamber, but some committees are genuinely
                            # joint, whole-Congress bodies (Comisión
                            # Bicameral de Presupuesto...) parented under
                            # WHOLE_CONGRESS_ORG_NAME instead -- see
                            # CHAMBER_LABEL_TO_ORG_NAME.
                            org = crud_core.find_organization_with_congreso_fallback(
                                db,
                                org_name=org_schema.org_name,
                                org_type=org_schema.org_type,
                                own_parent_org_id=chamber.org_id,
                            )
                        if org is None:
                            logger.warning(
                                f"Skipping {config.entity_label}Organization {config.id_field}={entity.id}, org={org_schema.org_name}, org_type={org_schema.org_type}: organization not found"
                            )
                            stats.skipped += 1
                            continue
                        getattr(config.crud_module, config.upsert_organization_name)(
                            db, entity.id, org.org_id, org_schema
                        )

                    for cong_rel in cong_rels:
                        cong = crud_core.find_congresista(
                            db,
                            name=split_and_sort_name(cong_rel.nombre)[0],
                            website=replace_www(cong_rel.web_page),
                        )
                        if cong is None:
                            logger.warning(
                                f"Skipping {config.entity_label}Congresista {config.id_field}={entity.id}, name={cong_rel.nombre}, website={cong_rel.web_page}: congresista not found"
                            )
                            stats.skipped += 1
                            continue

                        bancada = crud_core.find_active_bancada_for_person(
                            db, cong.id, chamber_schema.presentation_date
                        )
                        getattr(config.crud_module, config.upsert_congresista_name)(
                            db,
                            entity.id,
                            cong.id,
                            cong_rel.role_type.value
                            if hasattr(cong_rel.role_type, "value")
                            else cong_rel.role_type,
                            bancada_id=bancada.org_id if bancada else None,
                        )

                    if document_hook is not None:
                        document_hook(db, entity.id, stats)

                    raw_row.processed = True
                    stats.processed += 1
                    db.commit()
                except Exception as exc:
                    # Roll back before touching raw_row's attributes: a prior
                    # iteration's db.commit() expires every object still
                    # attached to this session (expire_on_commit defaults to
                    # True), so accessing raw_row.id while the session is
                    # still in "rollback required" state after a failed
                    # flush fires a reload query and raises
                    # PendingRollbackError instead of logging the real error.
                    db.rollback()
                    logger.exception(
                        f"Error processing Raw{config.entity_label} id={raw_row.id}: {exc}"
                    )
                    stats.errors += 1

        logger.info(
            f"[{config.log_key}] raw_total={len(rows)} processed={stats.processed} skipped={stats.skipped} errors={stats.errors} clean_inserted={clean_inserted} clean_updated={clean_updated}"
        )
        return stats

    def _process_bills(self, *, limit: int | None) -> ProcessStats:
        """Process unprocessed RawBill rows into Bill + steps + org/cong relations."""
        return self._process_legislative_documents(
            config=_BILL_DOCUMENT_CONFIG, limit=limit
        )

    def _process_bills_summaries(
        self,
        *,
        first_load: bool = False,
    ) -> ProcessStats:
        stats = ProcessStats()
        clean_inserted = 0
        clean_updated = 0

        with self.DBSession() as db:
            if first_load:
                stmt = select(Bill.id).where(
                    or_(
                        Bill.summary_oc.is_(None),
                        func.trim(Bill.summary_oc) == "",
                    )
                )
            else:
                stmt = (
                    select(Bill.id)
                    .join(RawBill, Bill.id == RawBill.id)
                    .where(
                        RawBill.last_update.is_(True),
                        RawBill.changed.is_(True),
                    )
                )

            pending_bill_ids = list(db.scalars(stmt).all())

        if first_load:
            clean_inserted = len(pending_bill_ids)
        else:
            clean_updated = len(pending_bill_ids)

        for bill_id in tqdm(pending_bill_ids, desc="Processing summaries"):
            try:
                result = summarize_bill_from_db(bill_id)

                with self.DBSession() as db:
                    db.execute(
                        update(Bill)
                        .where(Bill.id == bill_id)
                        .values(summary_oc=result["summary"])
                    )
                    db.commit()

                stats.processed += 1

            except KeyError as e:
                stats.errors += 1
                logger.error(
                    f"[bill_summary] Missing expected key {e} for bill_id={bill_id}"
                )
                continue

            except SQLAlchemyError as e:
                stats.errors += 1
                logger.exception(
                    f"[bill_summary] Database error while processing bill_id={bill_id}: {e}"
                )
                continue

        logger.info(
            "[bill_summary] "
            f"pending_summaries={len(pending_bill_ids)} "
            f"processed={stats.processed} "
            f"skipped={stats.skipped} "
            f"errors={stats.errors} "
            f"clean_inserted={clean_inserted} "
            f"clean_updated={clean_updated}"
        )

        return stats

    def _process_bill_text(self, *, limit: int | None) -> ProcessStats:
        stats = ProcessStats()

        with self.DBSession() as db:
            bill_pages = crud_bills.find_bills_with_pending_pages(db)

            for idx, ((bill_id, step_id, file_id), pending_pages) in enumerate(
                bill_pages.items()
            ):
                if limit is not None and idx >= limit:
                    break

                next_version = crud_bills.get_next_bill_text_version(db, bill_id)
                try:
                    text_schema = process_bill_text(pending_pages, next_version)
                except ValueError as exc:
                    stats.errors += 1
                    logger.error(
                        f"Error extracting Bill Text for bill_id {bill_id}, "
                        f"step_id: {step_id}, file_id: {file_id}: {exc}"
                    )
                    continue

                try:
                    crud_bills.upsert_bill_text(
                        db,
                        bill_id=text_schema.bill_id,
                        step_id=text_schema.step_id,
                        file_id=text_schema.file_id,
                        version_id=text_schema.version_id,
                        text=text_schema.text,
                    )

                    # RawBillDocument primary key is (bill_id, step_id, file_id)
                    # where step_id and file_id are strings in the raw schema.
                    raw_doc = db.get(
                        RawBillDocument, (bill_id, str(step_id), str(file_id))
                    )

                    if raw_doc is None:
                        stats.errors += 1
                        logger.error(
                            f"RawBillDocument not found for bill_id {bill_id}, "
                            f"step_id: {step_id}, file_id: {file_id}"
                        )
                        db.rollback()
                        continue

                    for page in pending_pages:
                        page.processed = True

                    raw_doc.processed = True

                    db.commit()

                    stats.processed += len(pending_pages)

                except SQLAlchemyError as exc:
                    stats.errors += 1
                    logger.error(
                        f"Error loading Bill Text for bill_id {bill_id}, "
                        f"step_id: {step_id}, file_id: {file_id}: {exc}"
                    )
                    db.rollback()
                    continue

        logger.info(
            f"[bill_text] n_bills={len(bill_pages)} processed={stats.processed} skipped={stats.skipped} errors={stats.errors}"
        )
        return stats

    def _process_bill_differences(self, *, limit: int | None) -> ProcessStats:
        """Recompute diffs for every bill that has at least one ``bill_texts`` row.

        Driven off ``bill_texts`` rather than ``RawBill`` so this stage is
        independent of raw bill processing — it can be re-run on its own
        after a ``PARSER_VERSION`` bump or a fix to the diff package without
        having to mark raw bills unprocessed.
        """
        stats = ProcessStats()
        with self.DBSession() as db:
            query = (
                select(db_models.BillText.bill_id)
                .distinct()
                .order_by(db_models.BillText.bill_id)
            )
            if limit is not None:
                query = query.limit(limit)
            bill_ids = db.execute(query).scalars().all()

            for bill_id in tqdm(bill_ids, desc="Bill differences"):
                # Commit per bill: each bill is its own atomic unit, so a
                # failure on one doesn't roll back diffs already written for
                # earlier bills in the same batch.
                try:
                    self._compute_bill_differences(db, bill_id)
                    db.commit()
                    stats.processed += 1
                except Exception as exc:
                    logger.exception(
                        f"Error computing bill differences for bill_id={bill_id}: {exc}"
                    )
                    db.rollback()
                    stats.errors += 1
        logger.info(
            f"[bill_differences] bills_total={len(bill_ids)} processed={stats.processed} errors={stats.errors}"
        )
        return stats

    def _compute_bill_differences(self, db, bill_id: str) -> None:
        """Compute and store text diffs for every step of a bill against the
        most recent text-bearing predecessor.

        Step ordering is ``(step_date ASC, step_id ASC)`` so same-date steps
        pair deterministically across pipeline runs. Steps with no BillText
        are skipped as predecessors — we walk back through text-less steps
        so the next text-bearing step doesn't get stored as ``first_version``
        when an earlier version exists.
        """
        steps = (
            db.execute(
                select(db_models.BillStep)
                .where(db_models.BillStep.bill_id == bill_id)
                .order_by(
                    db_models.BillStep.step_date.asc(),
                    db_models.BillStep.step_id.asc(),
                )
            )
            .scalars()
            .all()
        )

        billtexts: list[db_models.BillText | None] = [
            crud_bills.get_billtext_for_step(db, bill_id, s.step_id) for s in steps
        ]

        prev_step: db_models.BillStep | None = None
        prev_bt: db_models.BillText | None = None
        for step, new_bt in zip(steps, billtexts):
            result = compute_bill_difference(
                prev_bt.text if prev_bt else None,
                new_bt.text if new_bt else None,
            )

            crud_bills.upsert_bill_difference(
                db,
                bill_id=bill_id,
                step_id=step.step_id,
                prev_step_id=prev_step.step_id if prev_step else None,
                difference_type=result["type"],
                difference_content=json.dumps(result["content"])
                if result["content"]
                else None,
            )

            if new_bt is not None:
                prev_step = step
                prev_bt = new_bt

        crud_bills.refresh_bill_diff_flag(db, bill_id)

    @staticmethod
    def _process_motion_documents(
        db: Session, motion_id: str, stats: ProcessStats
    ) -> None:
        """Extract + upsert a motion's document text, if include_documents
        is set. The only piece of _process_motions with no _process_bills
        equivalent (bill text extraction is a fully separate stage, see
        _process_bill_text, run later in run_processing)."""
        for raw_doc in crud_motions.find_raw_motion_documents(db, motion_id):
            pages = crud_motions.find_raw_motion_pages(
                db, motion_id, raw_doc.step_id, raw_doc.file_id
            )
            if not pages:
                stats.skipped += 1
                continue
            try:
                text_schema = process_motion_text(pages)
            except ValueError:
                stats.skipped += 1
                continue
            crud_motions.upsert_motion_text(
                db,
                motion_id=text_schema.motion_id,
                step_id=text_schema.step_id,
                file_id=text_schema.file_id,
                version_id=text_schema.version_id,
                text=text_schema.text,
            )
            raw_doc.processed = True

    def _process_motions(
        self, *, include_documents: bool, limit: int | None
    ) -> ProcessStats:
        """Process unprocessed RawMotion rows into Motion + steps + org/cong relations + (optionally) text."""
        return self._process_legislative_documents(
            config=_MOTION_DOCUMENT_CONFIG,
            limit=limit,
            document_hook=self._process_motion_documents if include_documents else None,
        )

    def _process_leyes(self, *, limit: int | None) -> ProcessStats:
        """Process unprocessed RawLey rows into Ley records, skipping any whose referenced Bill is missing."""
        stats = ProcessStats()
        clean_inserted = 0
        clean_updated = 0
        with self.DBSession() as db:
            query = db.query(RawLey).filter(
                RawLey.last_update.is_(True), RawLey.processed.is_(False)
            )
            if limit is not None:
                query = query.limit(limit)
            rows = query.all()

            for raw_ley in tqdm(rows, desc="Process leyes"):
                try:
                    ley_schema = process_leyes(raw_ley)
                    if ley_schema is None:
                        logger.warning(
                            f"Skipping RawLey id={raw_ley.id}: unable to parse bill link"
                        )
                        raw_ley.processed = True
                        stats.skipped += 1
                        continue
                    if db.get(db_models.Bill, ley_schema.bill_id) is None:
                        logger.warning(
                            f"Skipping RawLey id={raw_ley.id}: referenced bill_id={ley_schema.bill_id} not found"
                        )
                        raw_ley.processed = False
                        stats.skipped += 1
                        continue
                    pre = db.get(db_models.Ley, ley_schema.id)
                    crud_core.upsert_ley(db, ley_schema)
                    if pre is None:
                        clean_inserted += 1
                    else:
                        clean_updated += 1

                    raw_ley.processed = True
                    stats.processed += 1
                except Exception as exc:
                    logger.exception(f"Error processing RawLey id={raw_ley.id}: {exc}")
                    db.rollback()
                    stats.errors += 1

            db.commit()
        logger.info(
            f"[leyes] raw_total={len(rows)} processed={stats.processed} skipped={stats.skipped} errors={stats.errors} clean_inserted={clean_inserted} clean_updated={clean_updated}"
        )
        return stats

    def _process_vote_extraction(
        self,
        *,
        kind: Literal["bill", "motion"],
        model: str,
        max_pages: int = 5,
        limit: int | None,
        max_cost_usd: float = 5.0,
    ) -> ProcessStats:
        """Run OpenAI structured extraction over pending vote-related documents."""
        with self.DBSession() as db:
            return votes_extract.run_sync_extraction(
                db,
                kind=kind,
                model=model,
                max_pages=max_pages,
                limit=limit,
                max_cost_usd=max_cost_usd,
            )

    def _process_vote_load(
        self, *, kind: Literal["bill", "motion"], model: str, limit: int | None
    ) -> ProcessStats:
        """Transform pending extraction results into Vote/Attendance/VoteEvent/VoteCounts."""
        with self.DBSession() as db:
            return votes_load.run_vote_load(db, kind=kind, model=model, limit=limit)

    def submit_vote_batches(
        self,
        *,
        kind: Literal["bill", "motion"],
        model: str = VOTES_DEFAULT_MODEL,
        max_pages: int = 5,
        limit: int | None = None,
        max_cost_usd: float = 5.0,
    ) -> dict:
        """
        Submit a Batch API job for historical vote-extraction backfills. Not
        part of run_processing -- a batch job can take hours, so this is a
        one-off operator action; pair with collect_vote_batches once the job
        finishes.
        """
        with self.DBSession() as db:
            return votes_extract.submit_batch_extraction(
                db,
                kind=kind,
                model=model,
                max_pages=max_pages,
                limit=limit,
                max_cost_usd=max_cost_usd,
            )

    def collect_vote_batches(
        self,
        *,
        batch_id: str,
        kind: Literal["bill", "motion"],
        model: str = VOTES_DEFAULT_MODEL,
    ) -> ProcessStats:
        """Poll+collect a previously submitted vote-extraction batch, then load it."""
        with self.DBSession() as db:
            extraction_stats = votes_extract.collect_batch_extraction(
                db, batch_id=batch_id, kind=kind, model=model
            )
        with self.DBSession() as db:
            votes_load.run_vote_load(db, kind=kind, model=model, limit=None)
        return extraction_stats
