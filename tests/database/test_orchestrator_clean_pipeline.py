import json
from datetime import date, datetime
import pytest

from backend.core.enums import Proponents, TypeOrganization
from backend.database import models as db_models
from backend.database.crud import pipeline_bills as crud_bills
from backend.database.crud import pipeline_motions as crud_motions
from backend.database.crud import pipeline_core as crud_core
from backend.database.orchestrator import OpenPeruOrchestrator
from backend.database.raw_models import (
    RawBancada,
    RawBill,
    RawBillDocument,
    RawBillPage,
    RawCommittee,
    RawCongresista,
    RawLey,
    RawMotion,
    RawOrganization,
)
from backend import OcrModel
from backend.database.crud.pipeline_core import ProcessStats
from backend.process import schema
from backend import TypeBillStep, TypeMotionStep


@pytest.fixture
def orchestrator(engine):
    return OpenPeruOrchestrator(engine=engine)


def test_run_processing_loads_reference_definitions_before_memberships(monkeypatch):
    calls = []
    orch = OpenPeruOrchestrator.__new__(OpenPeruOrchestrator)

    def record(name):
        def _inner(*args, **kwargs):
            calls.append(name)
            return ProcessStats()

        return _inner

    monkeypatch.setattr(orch, "_process_organization_definitions", record("orgs"))
    monkeypatch.setattr(orch, "_process_bancada_definitions", record("bancadas"))
    monkeypatch.setattr(orch, "_process_congresistas", record("congresistas"))
    monkeypatch.setattr(orch, "_process_admin_memberships", record("admin_ms"))
    monkeypatch.setattr(orch, "_process_bancada_memberships", record("bancada_ms"))
    monkeypatch.setattr(orch, "_semantic_table", record("semantic"))

    orch.run_processing(
        process_bills=False,
        process_motions=False,
        process_leyes=False,
        process_others=True,
        process_documents=False,
    )

    assert calls == [
        "orgs",
        "bancadas",
        "congresistas",
        "admin_ms",
        "bancada_ms",
        "semantic",
    ]


_PROFILE_HTML = """
<html>
  <div class="nombres"><span>Label</span><span>Juan Alberto Perez Quispe</span></div>
  <div class="grupo"><span>Label</span><span>Accion Popular</span></div>
  <div class="votacion"><span>Label</span><span>12,345</span></div>
  <div class="representa"><span>Label</span><span>Lima</span></div>
  <div class="condicion"><span>Label</span><span>Titular</span></div>
  <div class="foto"><img src="/FotosCongresista/juan.jpg"/></div>
</html>
"""


def test_process_congresistas_creates_party_and_chamber_memberships(
    orchestrator, monkeypatch
):
    """CRITICAL regression (found in the bicameral migration's 3rd review round):
    Step 4b's parent_org_id fix, if applied unconditionally to every entry in
    this method's shared membership loop, would silently stop creating
    party_mem/chamber_mem for every congresista -- since both are top-level
    (parent_org_id=NULL) and would be searched for under a non-existent
    "parent of themselves". Confirms the org_type-conditional branch fix
    (orchestrator.py:884) actually works end-to-end."""
    monkeypatch.setattr(
        "backend.database.orchestrator.get_cong_data", lambda path, **kwargs: {}
    )

    with orchestrator.DBSession() as db:
        db.add(
            RawCongresista(
                id=1,
                leg_period="Parlamentario 2021 - 2026",
                chamber=None,
                website="https://www.congreso.gob.pe/congresista/juan",
                profile_content=_PROFILE_HTML,
                memberships_content=None,
                timestamp=datetime(2025, 8, 1),
                last_update=True,
                processed=False,
                changed=True,
            )
        )
        db.commit()

    stats = orchestrator._process_congresistas()

    assert stats.errors == 0
    assert stats.skipped == 0
    assert stats.processed == 1

    with orchestrator.DBSession() as db:
        cong = db.query(db_models.Congresista).one()
        memberships = (
            db.query(db_models.Membership)
            .filter(db_models.Membership.person_id == cong.id)
            .all()
        )
        org_types = sorted(m.org_type for m in memberships)
        assert org_types == sorted(["Partido", "Cámara"])

        chamber_org = (
            db.query(db_models.Organization)
            .filter(db_models.Organization.org_type == "Cámara")
            .one()
        )
        # chamber=None is legacy (pre-2026) data -- resolves to the old
        # unicameral "Congreso de la República" body, not either bicameral
        # chamber (see CHAMBER_LABEL_TO_ORG_NAME[None]).
        assert chamber_org.org_name == "Congreso de la República"
        assert chamber_org.parent_org_id is None


def test_process_congresistas_finds_joint_administrative_membership_across_chambers(
    orchestrator, monkeypatch
):
    """Regression for the _CHAMBER_UNSCOPED_ORG_TYPES false-negative
    (2026-09-08 production incident): ADMINISTRATIVE/COMMITTEE memberships
    were unconditionally scoped to the congresista's own chamber, but joint/
    bicameral bodies like Comisión Permanente are genuinely top-level
    (parent_org_id=NULL) and can never match a chamber-scoped lookup.
    Confirms the NULL-parent fallback resolves it instead of skipping."""
    monkeypatch.setattr(
        "backend.database.orchestrator.get_cong_data", lambda path, **kwargs: {}
    )

    memberships_content = json.dumps(
        {
            "data": [
                {
                    "desOrgano": "Comisión Permanente",
                    "desOrganoCongresista": "COMISIÓN PERMANENTE",
                    "desCargo": "Titular",
                    "fechaInicio": "2026-08-01T00:00:00",
                    "fechaFin": None,
                }
            ]
        }
    )

    with orchestrator.DBSession() as db:
        # Seeded exactly as process_admin_org creates it for chamber="Congreso"
        # (CHAMBER_LABEL_TO_ORG_NAME["Congreso"] is None): a genuine top-level
        # joint entity, not scoped to either chamber.
        crud_core.upsert_organization(
            db,
            schema.Organization(
                org_name="Comisión Permanente", org_type="Administrativo"
            ),
        )
        db.add(
            RawCongresista(
                id=1,
                leg_period="Parlamentario 2021 - 2026",
                chamber=None,
                website="https://www.congreso.gob.pe/congresista/juan",
                profile_content=_PROFILE_HTML,
                memberships_content=memberships_content,
                timestamp=datetime(2025, 8, 1),
                last_update=True,
                processed=False,
                changed=True,
            )
        )
        db.commit()

    stats = orchestrator._process_congresistas()

    assert stats.errors == 0
    assert stats.skipped == 0

    with orchestrator.DBSession() as db:
        cong = db.query(db_models.Congresista).one()
        admin_membership = (
            db.query(db_models.Membership)
            .filter(db_models.Membership.org_type == "Administrativo")
            .one()
        )
        assert admin_membership.person_id == cong.id


def test_process_congresistas_persists_earlier_rows_when_a_later_row_fails(
    orchestrator, monkeypatch
):
    """Regression test for the transaction-boundary fix: before the fix, a
    single db.commit() after the whole loop meant one row's db.rollback()
    discarded every earlier row's uncommitted work from the same run, even
    though stats.processed already counted those earlier rows as successes.
    Confirms each row now commits independently."""
    monkeypatch.setattr(
        "backend.database.orchestrator.get_cong_data", lambda path, **kwargs: {}
    )

    import backend.database.orchestrator as orch_module

    real_process_profile_content = orch_module.process_profile_content

    def flaky_process_profile_content(raw_cong, *args, **kwargs):
        if raw_cong.id == 2:
            raise RuntimeError("simulated failure for row 2")
        return real_process_profile_content(raw_cong, *args, **kwargs)

    monkeypatch.setattr(
        orch_module, "process_profile_content", flaky_process_profile_content
    )

    # Distinct profile HTML per row (different names) -- reusing identical
    # name HTML across rows would make find_congresista's fuzzy-match tier
    # treat them as the SAME person (matching by name when no website
    # matches), silently upserting row 3 into row 1's just-created record
    # instead of creating a second one.
    profiles = [
        (1, "https://www.congreso.gob.pe/congresista/uno", _PROFILE_HTML),
        (2, "https://www.congreso.gob.pe/congresista/dos", _PROFILE_HTML),
        (3, "https://www.congreso.gob.pe/congresista/tres", _SENADOR_PROFILE_HTML),
    ]

    with orchestrator.DBSession() as db:
        for cid, website, profile_html in profiles:
            db.add(
                RawCongresista(
                    id=cid,
                    leg_period="Parlamentario 2021 - 2026",
                    chamber=None,
                    website=website,
                    profile_content=profile_html,
                    memberships_content=None,
                    timestamp=datetime(2025, 8, 1),
                    last_update=True,
                    processed=False,
                    changed=True,
                )
            )
        db.commit()

    stats = orchestrator._process_congresistas()

    assert stats.errors == 1
    assert stats.processed == 2

    with orchestrator.DBSession() as db:
        websites = {c.website for c in db.query(db_models.Congresista).all()}
        assert websites == {
            "https://www.congreso.gob.pe/congresista/uno",
            "https://www.congreso.gob.pe/congresista/tres",
        }
        raw_1 = db.get(RawCongresista, 1)
        raw_2 = db.get(RawCongresista, 2)
        raw_3 = db.get(RawCongresista, 3)
        assert raw_1.processed is True
        assert raw_2.processed is False
        assert raw_3.processed is True


_SENADOR_PROFILE_HTML = """
<html>
  <div class="nombres"><span>Label</span><span>Ana Maria Torres Vega</span></div>
  <div class="grupo"><span>Label</span><span>Fuerza Popular</span></div>
  <div class="votacion"><span>Label</span><span>25,642</span></div>
  <div class="representa"><span>Label</span><span>Lambayeque</span></div>
  <div class="condicion"><span>Label</span><span>En Ejercicio</span></div>
  <div class="foto"><img src="/FotosCongresista/ana.jpg"/></div>
</html>
"""


def test_first_load_seeds_2026_2031_first_membership_with_term_start_date(
    orchestrator, monkeypatch
):
    """New: on first_load=True, the FIRST-ever chamber_mem/party_mem
    recorded for a 2026-2031 congresista gets start_date=2026-07-28
    (confirmed real term-start date), not whatever date we happened to
    scrape on.

    Mutation-verified: the scrape timestamp deliberately falls in the
    term's SECOND legislative year (Oct 2027, after the Jul 27 boundary),
    where the existing timestamp-derived fallback (get_current_leg_year)
    would incorrectly derive 2027-07-28 instead of the true term start
    2026-07-28 -- a timestamp still within the first legislative year
    (e.g. Oct 2026) would accidentally derive the same date either way and
    not actually prove this fix does anything."""
    monkeypatch.setattr(
        "backend.database.orchestrator.get_cong_data", lambda path, **kwargs: {}
    )

    with orchestrator.DBSession() as db:
        db.add(
            RawCongresista(
                id=1,
                leg_period="Parlamentario 2026 - 2031",
                chamber="Senadores",
                website="https://senado.congreso.gob.pe/senador/ana-torres/",
                profile_content=_SENADOR_PROFILE_HTML,
                memberships_content=None,
                # Scraped well into the term's second legislative year --
                # see the mutation-verification note above.
                timestamp=datetime(2027, 10, 15),
                last_update=True,
                processed=False,
                changed=True,
            )
        )
        db.commit()

    stats = orchestrator._process_congresistas(first_load=True)
    assert stats.errors == 0

    with orchestrator.DBSession() as db:
        cong = db.query(db_models.Congresista).one()
        memberships = (
            db.query(db_models.Membership)
            .filter(db_models.Membership.person_id == cong.id)
            .all()
        )
        assert len(memberships) == 2
        for ms in memberships:
            assert ms.start_date == date(2026, 7, 28)


def test_first_load_does_not_affect_legacy_2021_2026_memberships(
    orchestrator, monkeypatch
):
    """Regression: first_load=True must never force-reset legacy (pre-2026)
    memberships' start_date to 2026-07-28."""
    monkeypatch.setattr(
        "backend.database.orchestrator.get_cong_data", lambda path, **kwargs: {}
    )

    with orchestrator.DBSession() as db:
        db.add(
            RawCongresista(
                id=1,
                leg_period="Parlamentario 2021 - 2026",
                chamber=None,
                website="https://www.congreso.gob.pe/congresista/juan",
                profile_content=_PROFILE_HTML,
                memberships_content=None,
                timestamp=datetime(2025, 8, 1),
                last_update=True,
                processed=False,
                changed=True,
            )
        )
        db.commit()

    stats = orchestrator._process_congresistas(first_load=True)
    assert stats.errors == 0

    with orchestrator.DBSession() as db:
        cong = db.query(db_models.Congresista).one()
        memberships = (
            db.query(db_models.Membership)
            .filter(db_models.Membership.person_id == cong.id)
            .all()
        )
        assert len(memberships) == 2
        for ms in memberships:
            # Falls through to the existing timestamp-derived fallback
            # (legislative year containing 2025-08-01 -> starts 2025-07-28),
            # unaffected by first_load.
            assert ms.start_date == date(2025, 7, 28)


def test_process_congresistas_resyncs_photo_when_photo_url_changed(
    orchestrator, monkeypatch
):
    """Found 2026-09: a matched (reelected) congresista never got their
    photo refreshed at all -- sync was only ever called for brand-new
    rows. Confirms a matched congresista whose photo_url actually changed
    (e.g. legacy profile -> new 2026-2031 chamber profile) now triggers a
    forced re-sync."""
    monkeypatch.setattr(
        "backend.database.orchestrator.get_cong_data", lambda path, **kwargs: {}
    )
    calls = []
    monkeypatch.setattr(
        "backend.database.orchestrator.sync_congresista_photo",
        lambda db, cong, **kwargs: calls.append((cong.id, kwargs.get("force", False))),
    )

    with orchestrator.DBSession() as db:
        existing = db_models.Congresista(
            full_name="Ana Maria Torres Vega",
            website="https://www.congreso.gob.pe/congresistas2021/AnaTorres/",
            photo_url="https://www.congreso.gob.pe/old-photo.jpg",
        )
        db.add(existing)
        db.commit()

        db.add(
            RawCongresista(
                id=1,
                leg_period="Parlamentario 2026 - 2031",
                chamber="Senadores",
                website="https://senado.congreso.gob.pe/senador/ana-torres/",
                profile_content=_SENADOR_PROFILE_HTML,
                memberships_content=None,
                timestamp=datetime(2027, 10, 15),
                last_update=True,
                processed=False,
                changed=True,
            )
        )
        db.commit()

    stats = orchestrator._process_congresistas()
    assert stats.errors == 0

    with orchestrator.DBSession() as db:
        cong = db.query(db_models.Congresista).one()
        assert calls == [(cong.id, True)]


def test_process_congresistas_skips_photo_resync_when_photo_url_unchanged(
    orchestrator, monkeypatch
):
    """Regression: a matched congresista whose photo_url hasn't actually
    changed must NOT trigger a re-download on every reprocess."""
    monkeypatch.setattr(
        "backend.database.orchestrator.get_cong_data", lambda path, **kwargs: {}
    )
    calls = []
    monkeypatch.setattr(
        "backend.database.orchestrator.sync_congresista_photo",
        lambda db, cong, **kwargs: calls.append((cong.id, kwargs.get("force", False))),
    )

    with orchestrator.DBSession() as db:
        # xpath2('//*[@class="foto"]/img/@src') resolves relative to
        # website via urljoin -- matches _SENADOR_PROFILE_HTML's
        # "/FotosCongresista/ana.jpg" against this website exactly.
        existing = db_models.Congresista(
            full_name="Ana Maria Torres Vega",
            website="https://senado.congreso.gob.pe/senador/ana-torres/",
            photo_url="https://senado.congreso.gob.pe/FotosCongresista/ana.jpg",
        )
        db.add(existing)
        db.commit()

        db.add(
            RawCongresista(
                id=1,
                leg_period="Parlamentario 2026 - 2031",
                chamber="Senadores",
                website="https://senado.congreso.gob.pe/senador/ana-torres/",
                profile_content=_SENADOR_PROFILE_HTML,
                memberships_content=None,
                timestamp=datetime(2027, 10, 15),
                last_update=True,
                processed=False,
                changed=True,
            )
        )
        db.commit()

    stats = orchestrator._process_congresistas()
    assert stats.errors == 0
    assert calls == []


def test_first_load_does_not_override_start_date_when_membership_already_exists(
    orchestrator,
):
    """Regression: first_load=True must NOT force-apply the 2026-07-28
    term-start date when a Membership already exists for this exact
    (person, org, leg_period, org_type) -- e.g. a bancada switch, where a
    NEW membership genuinely starts on the date it was detected, not on
    day one of the term. Exercises _upsert_membership_schema directly
    (unit-level) rather than the full congresistas pipeline, since
    reproducing an org-switch through the HTML-fixture-driven integration
    path would obscure exactly which mechanism is under test.

    Mutation-verified: the new membership's timestamp deliberately falls
    in the term's SECOND legislative year (Sep 2027), so if the
    already-exists guard were missing, this call would incorrectly
    override start_date to 2026-07-28 instead of deriving 2027-07-28 from
    the timestamp -- a timestamp still within the first legislative year
    would accidentally produce the same result either way and not prove
    the guard does anything.
    """
    with orchestrator.DBSession() as db:
        cong = db_models.Congresista(full_name="Ana Torres", website="w", photo_url="p")
        org_a = db_models.Organization(org_name="Bancada A", org_type="Bancada")
        org_b = db_models.Organization(org_name="Bancada B", org_type="Bancada")
        db.add_all([cong, org_a, org_b])
        db.flush()

        # An existing membership already recorded for org_a.
        db.add(
            db_models.BancadaMembership(
                person_id=cong.id,
                org_id=org_a.org_id,
                leg_period="2026-2031",
                org_type="Bancada",
                role="Miembro",
                start_date=date(2026, 7, 28),
                end_date=date(2027, 7, 28),
            )
        )
        db.commit()

        # New membership for a DIFFERENT org (org_b) -- e.g. a bancada
        # switch detected on a later scrape. No prior membership exists
        # for (cong, org_b, ...), so this SHOULD get the term-start
        # override under first_load=True per the "first-ever record" rule.
        new_membership = schema.Membership(
            cong_name="Ana Torres",
            org_name="Bancada B",
            org_type="Bancada",
            leg_period="2026-2031",
            role="Miembro",
            time_stamp=datetime(2027, 9, 1),
        )
        result = orchestrator._upsert_membership_schema(
            db,
            cong=cong,
            org=org_b,
            membership=new_membership,
            first_load=True,
        )
        assert result.start_date == date(2026, 7, 28)

        # Re-upserting the SAME (cong, org_b) membership again (simulating
        # a later re-process finding it already exists) must NOT re-derive
        # 2026-07-28 from the override -- it must fall through to the
        # timestamp-derived fallback, which for Sep 2027 yields 2027-07-28.
        again = schema.Membership(
            cong_name="Ana Torres",
            org_name="Bancada B",
            org_type="Bancada",
            leg_period="2026-2031",
            role="Presidente",  # role change -> upsert_membership treats as a new row
            time_stamp=datetime(2027, 9, 1),
        )
        result2 = orchestrator._upsert_membership_schema(
            db,
            cong=cong,
            org=org_b,
            membership=again,
            first_load=True,
        )
        assert result2.start_date == date(2027, 7, 28)


def test_process_organization_definitions_persists_earlier_committees_when_a_later_row_fails(
    orchestrator, monkeypatch
):
    """Regression test for the transaction-boundary fix applied to
    _process_organization_definitions's committees sub-loop."""
    import backend.database.orchestrator as orch_module

    def flaky_process_committee(raw_comm):
        if raw_comm.id == 2:
            raise RuntimeError("simulated failure for row 2")
        return [
            schema.Organization(
                org_name=f"Comisión de Prueba {raw_comm.id}",
                org_type=TypeOrganization.COMMITTEE,
                org_subtype="Comisión Ordinaria",
                parent_org_name="Cámara de Diputados",
                parent_org_type=TypeOrganization.CHAMBER,
            )
        ]

    monkeypatch.setattr(orch_module, "process_committee", flaky_process_committee)

    with orchestrator.DBSession() as db:
        db.add(
            db_models.Organization(
                org_name="Cámara de Diputados",
                org_type="Cámara",
            )
        )
        for cid in (1, 2, 3):
            db.add(
                RawCommittee(
                    id=cid,
                    legislative_year="2026",
                    chamber=None,
                    committee_type="Ordinaria",
                    raw_html="<html></html>",
                    timestamp=datetime(2026, 1, 1),
                    last_update=True,
                    processed=False,
                    changed=True,
                )
            )
        db.commit()

    stats = orchestrator._process_organization_definitions()

    assert stats.errors == 1
    assert stats.processed == 2

    with orchestrator.DBSession() as db:
        org_names = {
            o.org_name
            for o in db.query(db_models.Organization)
            .filter(db_models.Organization.org_type == "Comisión")
            .all()
        }
        assert org_names == {"Comisión de Prueba 1", "Comisión de Prueba 3"}
        assert db.get(RawCommittee, 1).processed is True
        assert db.get(RawCommittee, 2).processed is False
        assert db.get(RawCommittee, 3).processed is True


def test_process_admin_memberships_persists_earlier_rows_when_a_later_row_fails(
    orchestrator, monkeypatch
):
    """Regression test for the transaction-boundary fix applied to
    _process_admin_memberships."""
    import backend.database.orchestrator as orch_module

    def flaky_process_admin_org(raw_org):
        if raw_org.id == 2:
            raise RuntimeError("simulated failure for row 2")
        org_schema = schema.Organization(
            org_name=f"Mesa Directiva {raw_org.id}",
            org_type=TypeOrganization.ADMINISTRATIVE,
            org_subtype="Mesa Directiva",
            org_link="",
            parent_org_name="Cámara de Diputados",
            parent_org_type=TypeOrganization.CHAMBER,
        )
        return org_schema, []

    monkeypatch.setattr(orch_module, "process_admin_org", flaky_process_admin_org)

    with orchestrator.DBSession() as db:
        db.add(
            db_models.Organization(
                org_name="Cámara de Diputados",
                org_type="Cámara",
            )
        )
        for cid in (1, 2, 3):
            db.add(
                RawOrganization(
                    id=cid,
                    legislative_year="2026",
                    chamber=None,
                    type_org="Mesa Directiva",
                    org_link="",
                    raw_html="<html></html>",
                    timestamp=datetime(2026, 1, 1),
                    last_update=True,
                    processed=False,
                    changed=True,
                )
            )
        db.commit()

    stats = orchestrator._process_admin_memberships()

    assert stats.errors == 1
    assert stats.processed == 2

    with orchestrator.DBSession() as db:
        org_names = {
            o.org_name
            for o in db.query(db_models.Organization)
            .filter(db_models.Organization.org_type == "Administrativo")
            .all()
        }
        assert org_names == {"Mesa Directiva 1", "Mesa Directiva 3"}
        assert db.get(RawOrganization, 1).processed is True
        assert db.get(RawOrganization, 2).processed is False
        assert db.get(RawOrganization, 3).processed is True


def test_process_bancada_definitions_persists_earlier_rows_when_a_later_row_fails(
    orchestrator, monkeypatch
):
    """Regression test for the transaction-boundary fix applied to
    _process_bancada_definitions."""
    import backend.database.orchestrator as orch_module

    def flaky_process_bancada(raw_bancada):
        if raw_bancada.id == 2:
            raise RuntimeError("simulated failure for row 2")
        org_schema = schema.Organization(
            org_name=f"Bancada Prueba {raw_bancada.id}",
            org_type=TypeOrganization.BANCADA,
        )
        return [org_schema], []

    monkeypatch.setattr(orch_module, "process_bancada", flaky_process_bancada)

    with orchestrator.DBSession() as db:
        for cid in (1, 2, 3):
            db.add(
                RawBancada(
                    id=cid,
                    legislative_period="Parlamentario 2021 - 2026",
                    chamber=None,
                    raw_html="<html></html>",
                    timestamp=datetime(2026, 1, 1),
                    last_update=True,
                    processed=False,
                    changed=True,
                )
            )
        db.commit()

    stats = orchestrator._process_bancada_definitions()

    assert stats.errors == 1
    assert stats.processed == 2

    with orchestrator.DBSession() as db:
        org_names = {
            o.org_name
            for o in db.query(db_models.Organization)
            .filter(db_models.Organization.org_type == "Bancada")
            .all()
        }
        assert org_names == {"Bancada Prueba 1", "Bancada Prueba 3"}
        assert db.get(RawBancada, 1).processed is True
        assert db.get(RawBancada, 2).processed is False
        assert db.get(RawBancada, 3).processed is True


def test_process_bancada_memberships_persists_earlier_rows_when_a_later_row_fails(
    orchestrator, monkeypatch
):
    """Regression test for the transaction-boundary fix applied to
    _process_bancada_memberships. Feeds raw rows directly (bypassing
    _process_bancada_definitions) since this method has its own independent
    query on RawBancada.processed."""
    import backend.database.orchestrator as orch_module

    def flaky_process_bancada(raw_bancada):
        if raw_bancada.id == 2:
            raise RuntimeError("simulated failure for row 2")
        return [], []

    monkeypatch.setattr(orch_module, "process_bancada", flaky_process_bancada)

    with orchestrator.DBSession() as db:
        for cid in (1, 2, 3):
            db.add(
                RawBancada(
                    id=cid,
                    legislative_period="Parlamentario 2021 - 2026",
                    chamber=None,
                    raw_html="<html></html>",
                    timestamp=datetime(2026, 1, 1),
                    last_update=True,
                    processed=False,
                    changed=True,
                )
            )
        db.commit()

    stats = orchestrator._process_bancada_memberships()

    assert stats.errors == 1
    assert stats.processed == 2

    with orchestrator.DBSession() as db:
        assert db.get(RawBancada, 1).processed is True
        assert db.get(RawBancada, 2).processed is False
        assert db.get(RawBancada, 3).processed is True


def test_process_bills_senado_bill_links_committee_and_chamber(orchestrator):
    """CRITICAL regression (found in 3rd review round): the org_type-conditional
    fix at orchestrator.py's bill_orgs loop must not scope the chamber's own
    entry by its own org_id as parent, while still correctly scoping the
    committee entry by the bill's actual chamber (Senado, not Diputados)."""
    with orchestrator.DBSession() as db:
        senado = db_models.Organization(
            org_name="Senado de la República", org_type="Cámara"
        )
        db.add(senado)
        db.flush()
        db.add(
            db_models.Organization(
                org_name="Comisión de Economía",
                org_type="Comisión",
                parent_org_id=senado.org_id,
            )
        )
        db.add(
            RawBill(
                id="00006-2026-2031-S",
                timestamp=datetime(2026, 1, 10),
                general=json.dumps(
                    {
                        "fecPresentacion": "2026-01-10",
                        "titulo": "Proyecto de Ley Senado",
                        "sumilla": "Resumen",
                        "observaciones": "",
                        "desEstado": "Presentado",
                        "desProponente": "Ministerio Público",
                        "desGpar": "Bancada Ausente",
                        "proyectoLey": "00006-2026-2031-S",
                    }
                ),
                congresistas=json.dumps([]),
                steps=json.dumps(
                    [
                        {
                            "seguimientoPleyId": 1,
                            "fecha": "2026-01-01",
                            "desEstado": "Presentado",
                            "detalle": "Presentado",
                        },
                        {
                            "seguimientoPleyId": 2,
                            "fecha": "2026-01-02",
                            "desEstado": "En Comisión",
                            "detalle": "Pasa a comisión",
                            "desComisiones": json.dumps(["Comisión de Economía"]),
                        },
                    ]
                ),
                committees=json.dumps([]),
                last_update=True,
                processed=False,
                changed=True,
            )
        )
        db.commit()

    stats = orchestrator._process_bills(limit=None)

    assert stats.errors == 0
    assert stats.skipped == 0
    assert stats.processed == 1

    with orchestrator.DBSession() as db:
        bill = db.get(db_models.Bill, "00006-2026-2031-S")
        assert bill is not None

        bill_orgs = (
            db.query(db_models.BillOrganization)
            .filter(db_models.BillOrganization.bill_id == "00006-2026-2031-S")
            .all()
        )
        orgs_by_org_id = {
            org.org_id: org for org in db.query(db_models.Organization).all()
        }
        orgs_by_name = {
            orgs_by_org_id[bo.org_id].org_name: orgs_by_org_id[bo.org_id]
            for bo in bill_orgs
        }
        assert set(orgs_by_name) == {"Senado de la República", "Comisión de Economía"}
        assert orgs_by_name["Senado de la República"].parent_org_id is None
        assert (
            orgs_by_name["Comisión de Economía"].parent_org_id
            == orgs_by_name["Senado de la República"].org_id
        )


def test_process_bills_loads_bill_when_author_and_bancada_are_missing(orchestrator):
    with orchestrator.DBSession() as db:
        db.add(
            db_models.Organization(
                org_name="Congreso de la República",
                org_type="Cámara",
            )
        )
        db.add(
            RawBill(
                id="2026_1",
                timestamp=datetime(2026, 1, 10),
                general=json.dumps(
                    {
                        "fecPresentacion": "2026-01-10",
                        "titulo": "Proyecto de Ley X",
                        "sumilla": "Resumen",
                        "observaciones": "",
                        "desEstado": "Presentado",
                        "desProponente": "Ministerio Público",
                        "desGpar": "Bancada Ausente",
                        "proyectoLey": "2026_1",
                    }
                ),
                congresistas=json.dumps(
                    [
                        {
                            "nombre": "Autora Ausente",
                            "pagWeb": "https://example.com/autora",
                            "tipoFirmanteId": 1,
                        }
                    ]
                ),
                steps=json.dumps([]),
                committees=json.dumps([]),
                last_update=True,
                processed=False,
                changed=True,
            )
        )
        db.commit()

    stats = orchestrator._process_bills(limit=None)

    with orchestrator.DBSession() as db:
        bill = db.get(db_models.Bill, "2026_1")
        raw = db.get(RawBill, ("2026_1", datetime(2026, 1, 10)))

        assert stats.processed == 1
        assert stats.errors == 0
        assert bill is not None
        assert bill.author_id is None
        assert raw.processed is True
        assert db.query(db_models.BillCongresistas).count() == 0


def test_process_bills_persists_earlier_rows_when_a_later_row_fails(
    orchestrator, monkeypatch
):
    """Regression test for the transaction-boundary fix applied to
    _process_bills."""
    real_upsert_bill = crud_bills.upsert_bill

    def flaky_upsert_bill(db, bill_schema):
        if bill_schema.id == "2026_2":
            raise RuntimeError("simulated failure for bill 2026_2")
        return real_upsert_bill(db, bill_schema)

    monkeypatch.setattr(crud_bills, "upsert_bill", flaky_upsert_bill)

    with orchestrator.DBSession() as db:
        db.add(
            db_models.Organization(
                org_name="Congreso de la República",
                org_type="Cámara",
            )
        )
        for bill_id in ("2026_1", "2026_2", "2026_3"):
            db.add(
                RawBill(
                    id=bill_id,
                    timestamp=datetime(2026, 1, 10),
                    general=json.dumps(
                        {
                            "fecPresentacion": "2026-01-10",
                            "titulo": "Proyecto de Ley X",
                            "sumilla": "Resumen",
                            "observaciones": "",
                            "desEstado": "Presentado",
                            "desProponente": "Ministerio Público",
                            "desGpar": "Bancada Ausente",
                            "proyectoLey": bill_id,
                        }
                    ),
                    congresistas=json.dumps(
                        [
                            {
                                "nombre": "Autora Ausente",
                                "pagWeb": "https://example.com/autora",
                                "tipoFirmanteId": 1,
                            }
                        ]
                    ),
                    steps=json.dumps([]),
                    committees=json.dumps([]),
                    last_update=True,
                    processed=False,
                    changed=True,
                )
            )
        db.commit()

    stats = orchestrator._process_bills(limit=None)

    assert stats.errors == 1
    assert stats.processed == 2

    with orchestrator.DBSession() as db:
        bill_ids = {b.id for b in db.query(db_models.Bill).all()}
        assert bill_ids == {"2026_1", "2026_3"}
        assert db.get(RawBill, ("2026_1", datetime(2026, 1, 10))).processed is True
        assert db.get(RawBill, ("2026_2", datetime(2026, 1, 10))).processed is False
        assert db.get(RawBill, ("2026_3", datetime(2026, 1, 10))).processed is True


def test_process_bills_marks_raw_pages_processed_when_bill_text_extracted(
    orchestrator,
):
    """When include_documents=True and bill text is extracted, the raw pages
    that fed the extraction must be flipped to processed=True alongside the
    raw document."""
    bill_id = "2026_2"
    step_id = "5"
    file_id = "50"
    presentation_date = datetime(2026, 1, 10)

    with orchestrator.DBSession() as db:
        db.add(
            db_models.Organization(
                org_name="Congreso de la República",
                org_type="Cámara",
            )
        )
        db.add(
            RawBill(
                id=bill_id,
                timestamp=presentation_date,
                general=json.dumps(
                    {
                        "fecPresentacion": "2026-01-10",
                        "titulo": "Proyecto con texto",
                        "sumilla": "Resumen",
                        "observaciones": "",
                        "desEstado": "Presentado",
                        "desProponente": "Ministerio Público",
                        "desGpar": "Bancada Ausente",
                        "proyectoLey": bill_id,
                    }
                ),
                congresistas=json.dumps([]),
                steps=json.dumps([]),
                committees=json.dumps([]),
                last_update=True,
                processed=False,
                changed=True,
            )
        )
        db.add(
            RawBillDocument(
                timestamp=presentation_date,
                bill_id=bill_id,
                step_id=step_id,
                file_id=file_id,
                step_date=presentation_date,
                url="https://example.com/doc.pdf",
                last_update=True,
                processed=False,
            )
        )
        db.add_all(
            [
                RawBillPage(
                    timestamp=presentation_date,
                    bill_id=bill_id,
                    step_id=step_id,
                    file_id=file_id,
                    page_num=1,
                    text="FÓRMULA LEGAL\nArticulo 1. Inicio.",
                    ocr_model=OcrModel.CHANDRA.value,
                    last_update=True,
                    processed=False,
                ),
                RawBillPage(
                    timestamp=presentation_date,
                    bill_id=bill_id,
                    step_id=step_id,
                    file_id=file_id,
                    page_num=2,
                    text="Articulo 2. Final.",
                    ocr_model=OcrModel.CHANDRA.value,
                    last_update=True,
                    processed=False,
                ),
            ]
        )
        db.commit()

    stats = orchestrator._process_bills(limit=None)
    stats_bill_text = orchestrator._process_bill_text(limit=None)

    with orchestrator.DBSession() as db:
        raw_doc = db.get(RawBillDocument, (bill_id, step_id, file_id))
        pages = (
            db.query(RawBillPage)
            .filter(
                RawBillPage.bill_id == bill_id,
                RawBillPage.step_id == step_id,
                RawBillPage.file_id == file_id,
            )
            .order_by(RawBillPage.page_num)
            .all()
        )
        bill_text = db.get(db_models.BillText, (bill_id, int(step_id), int(file_id), 1))

        assert stats.processed == 1
        assert stats.errors == 0
        assert stats_bill_text.processed == 2
        assert stats_bill_text.errors == 0
        assert bill_text is not None
        assert raw_doc.processed is True
        assert len(pages) == 2
        assert all(page.processed is True for page in pages)


def test_process_motions_loads_motion_when_author_is_missing(orchestrator):
    with orchestrator.DBSession() as db:
        db.add(
            db_models.Organization(
                org_name="Congreso de la República",
                org_type="Cámara",
            )
        )
        db.add(
            RawMotion(
                id="2026_2",
                timestamp=datetime(2026, 1, 10),
                general=json.dumps(
                    {
                        "fecPresentacion": "2026-01-10",
                        "desTipoMocion": "Otras",
                        "sumilla": "Resumen",
                        "observacion": None,
                        "desEstadoMocion": "Presentado",
                    }
                ),
                congresistas=json.dumps(
                    [
                        {
                            "nombre": "Autor Ausente",
                            "pagWeb": "https://example.com/autor",
                            "tipoFirmanteId": 1,
                        }
                    ]
                ),
                steps=json.dumps([]),
                last_update=True,
                processed=False,
                changed=True,
            )
        )
        db.commit()

    stats = orchestrator._process_motions(include_documents=False, limit=None)

    with orchestrator.DBSession() as db:
        motion = db.get(db_models.Motion, "2026_2")
        raw = db.get(RawMotion, ("2026_2", datetime(2026, 1, 10)))

        assert stats.processed == 1
        assert stats.errors == 0
        assert motion is not None
        assert motion.author_id is None
        assert raw.processed is True
        assert db.query(db_models.MotionCongresistas).count() == 0


def test_process_motions_persists_earlier_rows_when_a_later_row_fails(
    orchestrator, monkeypatch
):
    """Regression test for the transaction-boundary fix applied to
    _process_motions."""
    real_upsert_motion = crud_motions.upsert_motion

    def flaky_upsert_motion(db, motion_schema):
        if motion_schema.id == "2026_11":
            raise RuntimeError("simulated failure for motion 2026_11")
        return real_upsert_motion(db, motion_schema)

    monkeypatch.setattr(crud_motions, "upsert_motion", flaky_upsert_motion)

    with orchestrator.DBSession() as db:
        db.add(
            db_models.Organization(
                org_name="Congreso de la República",
                org_type="Cámara",
            )
        )
        for motion_id in ("2026_10", "2026_11", "2026_12"):
            db.add(
                RawMotion(
                    id=motion_id,
                    timestamp=datetime(2026, 1, 10),
                    general=json.dumps(
                        {
                            "fecPresentacion": "2026-01-10",
                            "desTipoMocion": "Otras",
                            "sumilla": "Resumen",
                            "observacion": None,
                            "desEstadoMocion": "Presentado",
                        }
                    ),
                    congresistas=json.dumps(
                        [
                            {
                                "nombre": "Autor Ausente",
                                "pagWeb": "https://example.com/autor",
                                "tipoFirmanteId": 1,
                            }
                        ]
                    ),
                    steps=json.dumps([]),
                    last_update=True,
                    processed=False,
                    changed=True,
                )
            )
        db.commit()

    stats = orchestrator._process_motions(include_documents=False, limit=None)

    assert stats.errors == 1
    assert stats.processed == 2

    with orchestrator.DBSession() as db:
        motion_ids = {m.id for m in db.query(db_models.Motion).all()}
        assert motion_ids == {"2026_10", "2026_12"}
        assert db.get(RawMotion, ("2026_10", datetime(2026, 1, 10))).processed is True
        assert db.get(RawMotion, ("2026_11", datetime(2026, 1, 10))).processed is False
        assert db.get(RawMotion, ("2026_12", datetime(2026, 1, 10))).processed is True


def test_process_bills_sets_bancada_from_membership_as_of_presentation_date(
    orchestrator,
):
    with orchestrator.DBSession() as db:
        db.add(
            db_models.Organization(
                org_name="Congreso de la República",
                org_type="Cámara",
            )
        )
        bancada = db_models.Organization(
            org_name="Bancada Test",
            org_type="Bancada",
        )
        db.add(bancada)
        db.flush()

        cong = db_models.Congresista(
            full_name="Ana Torres",
            photo_url="https://x/a.jpg",
            website="https://x/ana-torres",
        )
        db.add(cong)
        db.flush()

        cong_id = cong.id
        bancada_org_id = bancada.org_id

        db.add(
            db_models.BancadaMembership(
                person_id=cong_id,
                org_id=bancada_org_id,
                leg_period="2021-2026",
                org_type="Bancada",
                role="Miembro",
                start_date=date(2025, 1, 1),
                end_date=date(2026, 12, 31),
            )
        )

        db.add(
            RawBill(
                id="2026_1",
                timestamp=datetime(2026, 1, 10),
                general=json.dumps(
                    {
                        "fecPresentacion": "2026-01-10",
                        "titulo": "Proyecto de Ley X",
                        "sumilla": "Resumen",
                        "observaciones": "",
                        "desEstado": "Presentado",
                        "desProponente": "Ministerio Público",
                        "desGpar": "Bancada Test",
                        "proyectoLey": "2026_1",
                    }
                ),
                congresistas=json.dumps(
                    [
                        {
                            "nombre": "TORRES, Ana",
                            "pagWeb": None,
                            "tipoFirmanteId": 1,
                        }
                    ]
                ),
                steps=json.dumps([]),
                committees=json.dumps([]),
                last_update=True,
                processed=False,
                changed=True,
            )
        )
        db.commit()

    stats = orchestrator._process_bills(limit=None)

    with orchestrator.DBSession() as db:
        assert stats.processed == 1
        assert stats.errors == 0

        rel = db.get(db_models.BillCongresistas, ("2026_1", cong_id))
        assert rel is not None
        assert rel.bancada_id == bancada_org_id


def test_process_motions_sets_bancada_from_membership_as_of_presentation_date(
    orchestrator,
):
    with orchestrator.DBSession() as db:
        db.add(
            db_models.Organization(
                org_name="Congreso de la República",
                org_type="Cámara",
            )
        )
        bancada = db_models.Organization(
            org_name="Bancada Test",
            org_type="Bancada",
        )
        db.add(bancada)
        db.flush()

        cong = db_models.Congresista(
            full_name="Ana Torres",
            photo_url="https://x/a.jpg",
            website="https://x/ana-torres",
        )
        db.add(cong)
        db.flush()

        cong_id = cong.id
        bancada_org_id = bancada.org_id

        db.add(
            db_models.BancadaMembership(
                person_id=cong_id,
                org_id=bancada_org_id,
                leg_period="2021-2026",
                org_type="Bancada",
                role="Miembro",
                start_date=date(2025, 1, 1),
                end_date=date(2026, 12, 31),
            )
        )

        db.add(
            RawMotion(
                id="2026_2",
                timestamp=datetime(2026, 1, 10),
                general=json.dumps(
                    {
                        "fecPresentacion": "2026-01-10",
                        "desTipoMocion": "Otras",
                        "sumilla": "Resumen",
                        "observacion": None,
                        "desEstadoMocion": "Presentado",
                    }
                ),
                congresistas=json.dumps(
                    [
                        {
                            "nombre": "TORRES, Ana",
                            "pagWeb": None,
                            "tipoFirmanteId": 1,
                        }
                    ]
                ),
                steps=json.dumps([]),
                last_update=True,
                processed=False,
                changed=True,
            )
        )
        db.commit()

    stats = orchestrator._process_motions(include_documents=False, limit=None)

    with orchestrator.DBSession() as db:
        assert stats.processed == 1
        assert stats.errors == 0

        rel = db.get(db_models.MotionCongresistas, ("2026_2", cong_id))
        assert rel is not None
        assert rel.bancada_id == bancada_org_id


def test_process_leyes_leaves_parsed_missing_bill_pending(orchestrator):
    xml = """
    <root>
      <data>
        <ley>
          <numley>32558</numley>
          <tituloley>LEY DE PRUEBA</tituloley>
        </ley>
        <ignored></ignored>
        <recursos>
          <recursos>
            <tiporecursoleyitemmenu>6</tiporecursoleyitemmenu>
            <enlace>https://wb2server.congreso.gob.pe/spley-portal/#/expediente/2021/3623</enlace>
          </recursos>
        </recursos>
      </data>
    </root>
    """.strip()

    with orchestrator.DBSession() as db:
        raw = RawLey(
            timestamp=datetime(2026, 1, 10),
            data=xml,
            last_update=True,
            processed=False,
            changed=True,
        )
        db.add(raw)
        db.commit()
        raw_id = raw.id

    stats = orchestrator._process_leyes(limit=None)

    with orchestrator.DBSession() as db:
        raw = db.get(RawLey, raw_id)

        assert stats.processed == 0
        assert stats.skipped == 1
        assert stats.errors == 0
        assert raw.processed is False
        assert db.query(db_models.Ley).count() == 0


def test_process_leyes_marks_unparseable_rows_skipped(orchestrator):
    with orchestrator.DBSession() as db:
        raw = RawLey(
            timestamp=datetime(2026, 1, 10),
            data="<root><data></data></root>",
            last_update=True,
            processed=False,
            changed=True,
        )
        db.add(raw)
        db.commit()
        raw_id = raw.id

    stats = orchestrator._process_leyes(limit=None)

    with orchestrator.DBSession() as db:
        raw = db.get(RawLey, raw_id)

        assert stats.processed == 0
        assert stats.skipped == 1
        assert stats.errors == 0
        assert raw.processed is True


def test_bill_step_upsert_retains_planned_vote_event_reference(orchestrator):
    with orchestrator.DBSession() as db:
        db.add(
            db_models.Bill(
                id="2026_10",
                title="PL",
                summary_congreso="Resumen",
                observations="",
                status="Presentado",
                proponent="Ministerio Público",
                author_id=None,
                bill_approved=False,
                summary_oc="Resumen OC",
                pley_id="2026_10",
            )
        )
        db.flush()

        step = crud_bills.upsert_bill_step(
            db,
            schema.BillStep(
                bill_id="2026_10",
                step_id=10,
                step_type=TypeBillStep.VOTACION,
                vote_step=True,
                vote_event_id="B_2026_10_1",
                step_date=datetime(2026, 1, 10),
                step_detail="Votación",
                step_committees=[],
            ),
        )

        assert step.vote_event_id == "B_2026_10_1"


def test_motion_step_upsert_retains_planned_vote_event_reference(orchestrator):
    with orchestrator.DBSession() as db:
        db.add(
            db_models.Motion(
                id="2026_20",
                motion_type="Otras",
                summary_congreso="Resumen",
                observations="",
                status="Presentado",
                author_id=None,
                motion_approved=False,
                summary_oc="Resumen OC",
            )
        )
        db.flush()

        step = crud_motions.upsert_motion_step(
            db,
            schema.MotionStep(
                motion_id="2026_20",
                step_id=20,
                step_type=TypeMotionStep.VOTACION_O_DECISION,
                vote_step=True,
                vote_event_id="M_2026_20_1",
                step_date=datetime(2026, 1, 10),
                step_detail="Votación",
            ),
        )

        assert step.vote_event_id == "M_2026_20_1"


def _seed_bill_with_two_text_steps(db, bill_id="2026_30"):
    db.add(
        db_models.Bill(
            id=bill_id,
            title="PL",
            summary_congreso="Resumen",
            observations="",
            status="Presentado",
            proponent=Proponents.CONGRESO,
            bill_approved=False,
            summary_oc="Resumen OC",
            pley_id=bill_id,
        )
    )
    db.add(
        db_models.BillStep(
            bill_id=bill_id,
            step_id=1,
            vote_step=False,
            step_type=TypeBillStep.VOTACION,
            step_date=date(2026, 1, 10),
            step_detail="presented",
        )
    )
    db.add(
        db_models.BillStep(
            bill_id=bill_id,
            step_id=2,
            vote_step=False,
            step_type=TypeBillStep.VOTACION,
            step_date=date(2026, 1, 20),
            step_detail="amended",
        )
    )
    db.add(
        db_models.BillText(
            bill_id=bill_id,
            step_id=1,
            file_id=1,
            version_id=1,
            text="Artículo 1.- Texto original.\n",
        )
    )
    db.add(
        db_models.BillText(
            bill_id=bill_id,
            step_id=2,
            file_id=1,
            version_id=1,
            text="Artículo 1.- Texto modificado.\n",
        )
    )
    db.commit()


def test_process_bill_differences_runs_over_bill_texts(orchestrator):
    with orchestrator.DBSession() as db:
        _seed_bill_with_two_text_steps(db, bill_id="2026_30")

    stats = orchestrator._process_bill_differences(limit=None)

    assert stats.processed == 1
    assert stats.errors == 0

    with orchestrator.DBSession() as db:
        rows = (
            db.query(db_models.BillDifference)
            .filter_by(bill_id="2026_30")
            .order_by(db_models.BillDifference.step_id)
            .all()
        )
        assert [r.step_id for r in rows] == [1, 2]
        assert rows[0].difference_type == "first_version"
        assert rows[0].prev_step_id is None
        assert rows[1].difference_type == "modified"
        assert rows[1].prev_step_id == 1
        assert rows[1].difference_content is not None

        assert db.get(db_models.Bill, "2026_30").bill_diff is True


def test_process_bill_differences_isolates_failures_per_bill(orchestrator, monkeypatch):
    # Regression: a failure on one bill must not roll back diffs already
    # written for earlier bills in the same batch. We seed two bills, then
    # force ``_compute_bill_differences`` to raise on the second one; the
    # first bill's BillDifference rows must still be persisted.
    with orchestrator.DBSession() as db:
        _seed_bill_with_two_text_steps(db, bill_id="2026_40")
        _seed_bill_with_two_text_steps(db, bill_id="2026_41")

    real = orchestrator._compute_bill_differences

    def raise_on_41(db, bill_id):
        if bill_id == "2026_41":
            raise RuntimeError("boom")
        return real(db, bill_id)

    monkeypatch.setattr(orchestrator, "_compute_bill_differences", raise_on_41)

    stats = orchestrator._process_bill_differences(limit=None)

    assert stats.processed == 1
    assert stats.errors == 1

    with orchestrator.DBSession() as db:
        persisted = {row.bill_id for row in db.query(db_models.BillDifference).all()}
        assert persisted == {"2026_40"}


def test_process_bill_differences_skips_bills_without_text(orchestrator):
    # A bill with no bill_texts row should not appear in the driver query at
    # all — _process_bill_differences is driven off bill_texts.
    with orchestrator.DBSession() as db:
        db.add(
            db_models.Bill(
                id="2026_31",
                title="PL",
                summary_congreso="Resumen",
                observations="",
                status="Presentado",
                proponent=Proponents.CONGRESO,
                bill_approved=False,
                summary_oc="Resumen OC",
                pley_id="2026_31",
            )
        )
        db.commit()

    stats = orchestrator._process_bill_differences(limit=None)

    assert stats.processed == 0
    with orchestrator.DBSession() as db:
        assert db.query(db_models.BillDifference).count() == 0
        # Never touched by the diff stage, so it stays at its column default.
        assert db.get(db_models.Bill, "2026_31").bill_diff is False
