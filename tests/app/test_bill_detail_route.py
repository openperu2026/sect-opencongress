"""Tests for the bill detail page's per-step ``View changes`` link.

The link should only appear when the bill-level ``bill_diff`` flag is true,
the step is ``Revisión o cambio de texto``, and the ``BillDifference`` row
carries content the user can actually see (``modified`` or ``incomparable``).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.enums import Proponents, TypeBillStep, TypeOrganization
from backend.database.models import (
    Base,
    Bill,
    BillDifference,
    BillStep,
    CommitteeMembership,
    Congresista,
    Organization,
    PartyMembership,
)


@pytest.fixture()
def session_factory(tmp_path):
    db_path = tmp_path / "processed_test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture()
def client(monkeypatch, session_factory):
    import app.routes.bills as bills_module
    from app.app import create_app

    monkeypatch.setattr(bills_module, "SessionProcessed", session_factory)
    flask_app = create_app()
    flask_app.testing = True
    return flask_app.test_client()


def _seed(session_factory, *, steps_with_diff_types, bill_diff=True):
    """Seed a bill with one step per entry; each entry is (step_id, diff_type | None)."""
    bill_id = "2021_1234"
    with session_factory() as db:
        db.add(
            Bill(
                id=bill_id,
                title="Test bill",
                summary_congreso="",
                observations="",
                status="presentado",
                proponent=Proponents.CONGRESO,
                bill_approved=False,
                summary_oc="",
                pley_id=bill_id,
                bill_diff=bill_diff,
            )
        )
        for step_id, step_type, diff_type in steps_with_diff_types:
            db.add(
                BillStep(
                    bill_id=bill_id,
                    step_id=step_id,
                    vote_step=False,
                    step_date=date(2022, 2, step_id),
                    step_type=step_type,
                    step_detail="",
                )
            )
            if diff_type is not None:
                db.add(
                    BillDifference(
                        bill_id=bill_id,
                        step_id=step_id,
                        prev_step_id=None,
                        difference_type=diff_type,
                        difference_content=None,
                    )
                )
        db.commit()


def _seed_author_affiliations(session_factory):
    bill_id = "2021_1234"
    with session_factory() as db:
        db.add_all(
            [
                Bill(
                    id=bill_id,
                    title="Test bill",
                    summary_congreso="",
                    observations="",
                    status="presentado",
                    proponent=Proponents.CONGRESO,
                    author_id=1,
                    bill_approved=False,
                    summary_oc="",
                    pley_id=bill_id,
                ),
                Congresista(
                    id=1,
                    full_name="Ana Perez",
                    first_name="Ana",
                    last_name="Perez",
                    dni="00000001",
                    gender="F",
                    photo_url="",
                    website="",
                ),
                Organization(
                    org_id=10,
                    org_name="Partido Verde",
                    org_type=TypeOrganization.PARTY,
                    org_subtype=None,
                    org_link=None,
                    parent_org_id=None,
                    date_founding=None,
                    date_dissolution=None,
                ),
                Organization(
                    org_id=11,
                    org_name="Comisión de Economía",
                    org_type=TypeOrganization.COMMITTEE,
                    org_subtype=None,
                    org_link=None,
                    parent_org_id=None,
                    date_founding=None,
                    date_dissolution=None,
                ),
                PartyMembership(
                    person_id=1,
                    org_id=10,
                    leg_period="2021-2026",
                    role="member",
                    start_date=date(2021, 1, 1),
                    end_date=date(2026, 12, 31),
                ),
                CommitteeMembership(
                    person_id=1,
                    org_id=11,
                    leg_period="2021-2026",
                    role="member",
                    start_date=date(2021, 1, 1),
                    end_date=date(2026, 12, 31),
                ),
                BillStep(
                    bill_id=bill_id,
                    step_id=1,
                    vote_step=False,
                    step_date=date(2022, 2, 1),
                    step_type="Presentado",
                    step_detail="",
                ),
            ]
        )
        db.commit()


def test_view_changes_link_only_for_revision_steps_with_bill_diff(
    client, session_factory
):
    _seed(
        session_factory,
        steps_with_diff_types=[
            (1, TypeBillStep.TEXTO_SUSTITUTORIO_O_REVISION, "modified"),
            (2, "Votación", "no_change"),
            (3, "En Comisión", "unavailable"),
            (4, "Presentado", "first_version"),
            (5, "En Agenda del Pleno", "incomparable"),
            (6, TypeBillStep.TEXTO_SUSTITUTORIO_O_REVISION, None),
        ],
    )

    body = client.get("/bills/2021_1234").get_data(as_text=True)

    assert "/bills/2021_1234/difference/1" in body
    assert "/bills/2021_1234/difference/5" not in body
    assert "/bills/2021_1234/difference/2" not in body
    assert "/bills/2021_1234/difference/3" not in body
    assert "/bills/2021_1234/difference/4" not in body
    assert "/bills/2021_1234/difference/6" not in body


def test_view_changes_link_hidden_when_bill_diff_is_false(client, session_factory):
    _seed(
        session_factory,
        bill_diff=False,
        steps_with_diff_types=[
            (1, TypeBillStep.TEXTO_SUSTITUTORIO_O_REVISION, "modified"),
            (2, TypeBillStep.PRESENTADO, None),
        ],
    )

    body = client.get("/bills/2021_1234").get_data(as_text=True)

    assert "/bills/2021_1234/difference/1" not in body


def test_detail_page_shows_author_party_and_committee(client, session_factory):
    _seed_author_affiliations(session_factory)

    body = client.get("/bills/2021_1234").get_data(as_text=True)

    assert "Autor:" in body
    assert "Ana Perez" in body
    assert "Partido:" in body
    assert "Partido Verde" in body


def test_summary_is_labeled_untranslated_in_english(client, session_factory):
    _seed_author_affiliations(session_factory)

    body = client.get("/bills/2021_1234?lang=en").get_data(as_text=True)

    assert "(Original text in Spanish)" in body


def test_summary_is_not_labeled_untranslated_in_spanish(client, session_factory):
    _seed_author_affiliations(session_factory)

    body = client.get("/bills/2021_1234").get_data(as_text=True)

    assert "(Original text in Spanish)" not in body
    assert "(Texto original en español)" not in body
