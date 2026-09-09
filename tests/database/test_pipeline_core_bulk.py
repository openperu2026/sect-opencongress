from datetime import date

import pytest

from backend import LegPeriod, RoleOrganization, TypeOrganization
from backend.database import models as db_models
from backend.database.crud import pipeline_core as crud_core
from backend.process import schema


@pytest.fixture()
def create_congresista(session):
    def _create_congresista(
        full_name: str = "María Grimaneza Acuña Peralta",
        first_name: str = "María Grimaneza",
        last_name: str = "Acuña Peralta",
        dni: str = "12345678",
        gender: str = "F",
        photo_url: str = "www.congreso.gob.pe/photo1",
        website: str = "https://www.congreso.gob.pe/congresistas2021/GrimanezaAcuna/",
        congresista_id: int | None = None,
    ) -> db_models.Congresista:
        cong = db_models.Congresista(
            full_name=full_name,
            first_name=first_name,
            last_name=last_name,
            dni=dni,
            gender=gender,
            photo_url=photo_url,
            website=website,
            congresista_id=congresista_id,
        )
        session.add(cong)
        session.flush()
        return cong

    return _create_congresista


def test_upsert_congresista_does_not_wipe_fields_missing_from_new_payload(
    session, create_congresista
):
    """Found 2026-09: a matched update whose schema has None for
    dni/gender/first_name/last_name (e.g. the 2026-2031 chamber scrape,
    which structurally never carries those fields) must NOT clobber
    already-known values from a prior scrape (e.g. legacy) -- this is
    exactly what wiped reelected congresistas' data."""
    existing = create_congresista(
        full_name="Alejandro Aurelio Aguinaga Recuenco",
        first_name="Alejandro Aurelio",
        last_name="Aguinaga Recuenco",
        dni="12345678",
        gender="M",
        website="https://www.congreso.gob.pe/congresistas2021/Aguinaga/",
    )

    updated = crud_core.upsert_congresista(
        session,
        schema.Congresista(
            full_name="Alejandro Aurelio Aguinaga Recuenco",
            first_name=None,
            last_name=None,
            dni=None,
            gender=None,
            photo_url="https://senado.congreso.gob.pe/photo2.png",
            website="https://senado.congreso.gob.pe/senador/aguinaga-recuenco/",
        ),
    )

    assert updated.id == existing.id
    assert updated.first_name == "Alejandro Aurelio"
    assert updated.last_name == "Aguinaga Recuenco"
    assert updated.dni == "12345678"
    assert updated.gender == "M"
    # Fields the new payload DOES carry a real value for still update.
    assert updated.photo_url == "https://senado.congreso.gob.pe/photo2.png"
    assert (
        updated.website == "https://senado.congreso.gob.pe/senador/aguinaga-recuenco/"
    )


def test_upsert_congresista_matches_by_congresista_id_despite_name_mismatch(
    session, create_congresista
):
    """congresista_id is a confirmed-stable cross-term identifier (found
    2026-09-03) -- upsert_congresista must use it to find the existing row
    even when full_name/website differ (e.g. a name-order glitch or a
    completely new term profile), rather than risking a fuzzy-match miss
    that would create a duplicate person."""
    existing = create_congresista(
        full_name="Fernando Miguel Rospigliosi Capurro",
        website="https://www.congreso.gob.pe/congresistas2021/Rospigliosi/",
        congresista_id=135,
    )

    updated = crud_core.upsert_congresista(
        session,
        schema.Congresista(
            full_name="Rospigliosi Capurro, Fernando Miguel",  # unconverted, on purpose
            photo_url="https://senado.congreso.gob.pe/photo2.png",
            website="https://senado.congreso.gob.pe/senador/fernando-rospigliosi/",
            congresista_id=135,
        ),
    )

    assert updated.id == existing.id
    assert updated.congresista_id == 135


def test_upsert_congresista_real_value_overwrites_existing(session, create_congresista):
    """Regression: the coalesce fix must not turn upsert into a no-op --
    a genuinely new, non-None value still overwrites the old one."""
    existing = create_congresista(dni="12345678")

    updated = crud_core.upsert_congresista(
        session,
        schema.Congresista(
            full_name=existing.full_name,
            dni="87654321",
            photo_url=existing.photo_url,
            website=existing.website,
        ),
    )

    assert updated.id == existing.id
    assert updated.dni == "87654321"


def test_upsert_bancada_uses_organization_rows(session):
    existing = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Accion Popular",
            org_type=TypeOrganization.BANCADA,
        ),
    )

    same = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Accion Popular",
            org_type="Bancada",
        ),
    )
    inserted = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Fuerza Popular",
            org_type=TypeOrganization.BANCADA,
        ),
    )

    assert same.org_id == existing.org_id
    assert inserted.org_id != existing.org_id
    assert session.query(db_models.Organization).count() == 2


def test_upsert_bancada_membership_is_idempotent(session, create_congresista):
    congresista = create_congresista()
    bancada = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Accion Popular",
            org_type=TypeOrganization.BANCADA,
        ),
    )

    first = crud_core.upsert_membership(
        session,
        person_id=congresista.id,
        org_id=bancada.org_id,
        leg_period=LegPeriod.PERIODO_2021_2026,
        org_type=TypeOrganization.BANCADA,
        role=RoleOrganization.MIEMBRO,
        start_date=date(2025, 7, 28),
        end_date=date(2026, 7, 28),
    )
    second = crud_core.upsert_membership(
        session,
        person_id=congresista.id,
        org_id=bancada.org_id,
        leg_period="2021-2026",
        org_type="Bancada",
        role="Miembro",
        start_date=date(2025, 7, 28),
        end_date=date(2026, 7, 28),
    )

    assert second.id == first.id
    assert session.query(db_models.BancadaMembership).count() == 1
    assert session.query(db_models.Membership).count() == 1


def test_upsert_organization_same_name_type_different_parent_creates_two_rows(
    session,
):
    """CRITICAL regression: this is the exact bug the bicameral migration's
    Step 4b fix exists for. Before the fix, upserting the second chamber's
    same-named committee would match the first chamber's row via
    find_organization (name+type only) and silently overwrite its
    parent_org_id, corrupting every membership that pointed at it."""
    diputados = crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Cámara de Diputados", org_type="Cámara"),
    )
    senado = crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Senado de la República", org_type="Cámara"),
    )

    committee_diputados = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comisión de Justicia",
            org_type="Comisión",
            parent_org_name="Cámara de Diputados",
            parent_org_type="Cámara",
        ),
    )
    committee_senado = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comisión de Justicia",
            org_type="Comisión",
            parent_org_name="Senado de la República",
            parent_org_type="Cámara",
        ),
    )

    assert committee_diputados.org_id != committee_senado.org_id
    assert committee_diputados.parent_org_id == diputados.org_id
    assert committee_senado.parent_org_id == senado.org_id
    assert (
        session.query(db_models.Organization)
        .filter(db_models.Organization.org_name == "Comisión de Justicia")
        .count()
        == 2
    )


def test_upsert_organization_same_name_type_same_parent_updates_in_place(session):
    crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Cámara de Diputados", org_type="Cámara"),
    )

    first = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comisión de Economía",
            org_type="Comisión",
            parent_org_name="Cámara de Diputados",
            parent_org_type="Cámara",
        ),
    )
    second = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comisión de Economía",
            org_type="Comisión",
            org_link="updated-link",
            parent_org_name="Cámara de Diputados",
            parent_org_type="Cámara",
        ),
    )

    assert second.org_id == first.org_id
    assert second.org_link == "updated-link"
    assert (
        session.query(db_models.Organization)
        .filter(db_models.Organization.org_name == "Comisión de Economía")
        .count()
        == 1
    )


def test_upsert_organization_with_no_parent_unaffected_by_parent_scoping(session):
    """Chambers and parties are top-level (parent_org_id=NULL) -- confirms the
    parent_org_id fix doesn't regress orgs that never had a parent to begin
    with."""
    first = crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Partido Morado", org_type="Partido"),
    )
    second = crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Partido Morado", org_type="Partido"),
    )

    assert second.org_id == first.org_id
    assert first.parent_org_id is None
    assert (
        session.query(db_models.Organization)
        .filter(db_models.Organization.org_name == "Partido Morado")
        .count()
        == 1
    )


def test_find_organization_parent_org_id_scoping(session):
    diputados = crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Cámara de Diputados", org_type="Cámara"),
    )
    senado = crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Senado de la República", org_type="Cámara"),
    )
    committee_diputados = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comisión de Salud",
            org_type="Comisión",
            parent_org_name="Cámara de Diputados",
            parent_org_type="Cámara",
        ),
    )
    crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comisión de Salud",
            org_type="Comisión",
            parent_org_name="Senado de la República",
            parent_org_type="Cámara",
        ),
    )

    # Scoped by the Diputados parent finds only the Diputados committee.
    found = crud_core.find_organization(
        session,
        org_name="Comisión de Salud",
        org_type="Comisión",
        parent_org_id=diputados.org_id,
    )
    assert found.org_id == committee_diputados.org_id

    # Unscoped (parent_org_id omitted) preserves prior behavior: picks by
    # fuzzy score then lowest org_id -- still returns *a* match, not an error.
    unscoped = crud_core.find_organization(
        session, org_name="Comisión de Salud", org_type="Comisión"
    )
    assert unscoped is not None

    # A parent_org_id that doesn't match either committee finds nothing.
    none_found = crud_core.find_organization(
        session,
        org_name="Comisión de Salud",
        org_type="Comisión",
        parent_org_id=senado.org_id + 999,
    )
    assert none_found is None


def test_find_organization_explicit_none_parent_requires_null_parent(session):
    """parent_org_id=None must mean "require a NULL parent" (a genuinely
    top-level org, e.g. a joint/bicameral entity like Comisión Permanente),
    never "unscoped" -- that overload previously let a top-level lookup
    cross-match a per-chamber org sharing the same name+type under a real
    parent (2026-09-08 regression: this is what made joint-entity membership
    lookups impossible to express safely without risking a cross-chamber
    false match)."""
    crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Cámara de Diputados", org_type="Cámara"),
    )
    scoped_committee = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comisión Permanente",
            org_type="Comisión",
            parent_org_name="Cámara de Diputados",
            parent_org_type="Cámara",
        ),
    )
    joint_committee = crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Comisión Permanente", org_type="Comisión"),
    )

    assert scoped_committee.org_id != joint_committee.org_id
    assert joint_committee.parent_org_id is None

    found = crud_core.find_organization(
        session,
        org_name="Comisión Permanente",
        org_type="Comisión",
        parent_org_id=None,
    )
    assert found.org_id == joint_committee.org_id


def test_find_organization_with_congreso_fallback_own_scope_hit(session):
    """Tier 1: the own-chamber-scoped lookup succeeds directly, no fallback
    needed."""
    diputados = crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Cámara de Diputados", org_type="Cámara"),
    )
    committee = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comisión de Salud",
            org_type="Comisión",
            parent_org_name="Cámara de Diputados",
            parent_org_type="Cámara",
        ),
    )

    found = crud_core.find_organization_with_congreso_fallback(
        session,
        org_name="Comisión de Salud",
        org_type="Comisión",
        own_parent_org_id=diputados.org_id,
    )
    assert found.org_id == committee.org_id


def test_find_organization_with_congreso_fallback_congreso_de_la_republica_hit(
    session,
):
    """Tier 2: the own-chamber-scoped lookup misses, but the org is a child
    of WHOLE_CONGRESS_ORG_NAME ("Congreso de la República") -- the current
    convention for joint/bicameral bodies (see CHAMBER_LABEL_TO_ORG_NAME)."""
    diputados = crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Cámara de Diputados", org_type="Cámara"),
    )
    crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Congreso de la República", org_type="Cámara"),
    )
    joint_committee = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comisión Bicameral de Presupuesto y Cuenta General de la República",
            org_type="Comisión",
            parent_org_name="Congreso de la República",
            parent_org_type="Cámara",
        ),
    )

    found = crud_core.find_organization_with_congreso_fallback(
        session,
        org_name="Comisión Bicameral de Presupuesto y Cuenta General de la República",
        org_type="Comisión",
        own_parent_org_id=diputados.org_id,
    )
    assert found.org_id == joint_committee.org_id


def test_find_organization_with_congreso_fallback_null_parent_hit(session):
    """Tier 3 (transitional): both the own-chamber scope and the
    "Congreso de la República" scope miss, but a NULL-parent row exists --
    some joint-entity rows created before the WHOLE_CONGRESS_ORG_NAME
    convention was unified may still be NULL-parent."""
    diputados = crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Cámara de Diputados", org_type="Cámara"),
    )
    orphan_joint_committee = crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Comisión Permanente", org_type="Administrativo"),
    )

    found = crud_core.find_organization_with_congreso_fallback(
        session,
        org_name="Comisión Permanente",
        org_type="Administrativo",
        own_parent_org_id=diputados.org_id,
    )
    assert found.org_id == orphan_joint_committee.org_id


def test_resolve_organization_match_tiers(session):
    """All four MatchTier outcomes, directly against
    resolve_organization_match. Accent differences are used to force a
    fuzzy (not exact) match deterministically -- Jaro-Winkler + unaccent
    scores these ~1.0, but the tier function's own exact check is a plain
    strip().lower() (no unaccent), so it correctly falls through to the
    fuzzy tiers."""
    diputados = crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Cámara de Diputados", org_type="Cámara"),
    )
    existing = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comisión de Salud",
            org_type="Comisión",
            org_link="https://congreso.gob.pe/comision-salud",
            parent_org_name="Cámara de Diputados",
            parent_org_type="Cámara",
        ),
    )

    exact_tier, exact_match = crud_core.resolve_organization_match(
        session,
        schema.Organization(org_name="Comisión de Salud", org_type="Comisión"),
        diputados.org_id,
    )
    assert exact_tier == crud_core.MatchTier.EXACT
    assert exact_match.org_id == existing.org_id

    corroborated_tier, corroborated_match = crud_core.resolve_organization_match(
        session,
        schema.Organization(
            org_name="Comision de Salud",  # missing accent -- fuzzy, not exact
            org_type="Comisión",
            org_link="https://congreso.gob.pe/comision-salud",  # matches existing
        ),
        diputados.org_id,
    )
    assert corroborated_tier == crud_core.MatchTier.FUZZY_CORROBORATED
    assert corroborated_match.org_id == existing.org_id

    uncorroborated_tier, uncorroborated_match = crud_core.resolve_organization_match(
        session,
        schema.Organization(org_name="Comision de Salud", org_type="Comisión"),
        diputados.org_id,
    )
    assert uncorroborated_tier == crud_core.MatchTier.FUZZY_UNCORROBORATED
    assert uncorroborated_match.org_id == existing.org_id

    none_tier, none_match = crud_core.resolve_organization_match(
        session,
        schema.Organization(
            org_name="Comisión Totalmente Distinta", org_type="Comisión"
        ),
        diputados.org_id,
    )
    assert none_tier == crud_core.MatchTier.NO_MATCH
    assert none_match is None


def test_upsert_organization_fuzzy_uncorroborated_still_renames_in_shadow_mode(
    session,
):
    """Default (ORG_UPSERT_STRICT_IDENTITY=False): a fuzzy match with no
    corroborating signal still gets the old unconditional-overwrite
    behavior -- shadow mode only logs what the stricter tier would decide,
    it doesn't change behavior yet."""
    assert crud_core.settings.ORG_UPSERT_STRICT_IDENTITY is False

    crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Cámara de Diputados", org_type="Cámara"),
    )
    first = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comision de Educacion",
            org_type="Comisión",
            parent_org_name="Cámara de Diputados",
            parent_org_type="Cámara",
        ),
    )
    second = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comisión de Educación",
            org_type="Comisión",
            parent_org_name="Cámara de Diputados",
            parent_org_type="Cámara",
        ),
    )

    assert second.org_id == first.org_id
    assert second.org_name == "Comisión de Educación"


def test_upsert_organization_fuzzy_uncorroborated_protects_identity_when_strict(
    session, monkeypatch
):
    """ORG_UPSERT_STRICT_IDENTITY=True: a fuzzy match with no corroborating
    signal keeps its existing identity fields (org_name, parent_org_id)
    untouched, but non-identity fields (org_link) still update -- and no
    duplicate row gets created."""
    monkeypatch.setattr(crud_core.settings, "ORG_UPSERT_STRICT_IDENTITY", True)

    crud_core.upsert_organization(
        session,
        schema.Organization(org_name="Cámara de Diputados", org_type="Cámara"),
    )
    first = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comision de Educacion",
            org_type="Comisión",
            parent_org_name="Cámara de Diputados",
            parent_org_type="Cámara",
        ),
    )
    second = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Comisión de Educación",
            org_type="Comisión",
            org_link="https://new-link.example",
            parent_org_name="Cámara de Diputados",
            parent_org_type="Cámara",
        ),
    )

    assert second.org_id == first.org_id
    assert second.org_name == "Comision de Educacion"
    assert second.org_link == "https://new-link.example"
    assert (
        session.query(db_models.Organization)
        .filter(db_models.Organization.org_type == "Comisión")
        .count()
        == 1
    )


def test_upsert_model_writes_explicit_none_when_field_in_fields_set(session):
    """The coalesce-skips-None rule in _upsert_model (see its own docstring
    comment) has one correct override: fields_set lets a caller assert a
    field's new value is genuinely None, not merely absent from this
    source -- without it, a legitimate correction to None could never be
    written once a row already has a non-null value."""
    org = db_models.Organization(
        org_name="Comisión de Prueba",
        org_type=TypeOrganization.COMMITTEE.value,
        org_link="https://old-link.example",
    )
    session.add(org)
    session.flush()

    # Without fields_set: a None in the payload is coalesced away (unchanged).
    crud_core._upsert_model(
        session,
        existing=org,
        model=db_models.Organization,
        payload={"org_link": None},
    )
    assert org.org_link == "https://old-link.example"

    # With fields_set: an explicit None is honored and written.
    crud_core._upsert_model(
        session,
        existing=org,
        model=db_models.Organization,
        payload={"org_link": None},
        fields_set={"org_link"},
    )
    assert org.org_link is None


def test_membership_exists(session, create_congresista):
    cong = create_congresista()
    org = crud_core.upsert_organization(
        session,
        schema.Organization(
            org_name="Fuerza Popular", org_type=TypeOrganization.BANCADA
        ),
    )

    # Nothing recorded yet.
    assert (
        crud_core.membership_exists(
            session,
            person_id=cong.id,
            org_id=org.org_id,
            leg_period="2026-2031",
            org_type=TypeOrganization.BANCADA,
        )
        is False
    )

    crud_core.upsert_membership(
        session,
        person_id=cong.id,
        org_id=org.org_id,
        leg_period="2026-2031",
        org_type=TypeOrganization.BANCADA,
        role=RoleOrganization.MIEMBRO,
        start_date=date(2026, 7, 28),
        end_date=date(2027, 7, 28),
    )

    # Now exists -- and the check is independent of role/dates (unlike
    # upsert_membership's own exact-match existing-row lookup).
    assert (
        crud_core.membership_exists(
            session,
            person_id=cong.id,
            org_id=org.org_id,
            leg_period="2026-2031",
            org_type=TypeOrganization.BANCADA,
        )
        is True
    )

    # A different leg_period/org_type for the same person+org is unaffected.
    assert (
        crud_core.membership_exists(
            session,
            person_id=cong.id,
            org_id=org.org_id,
            leg_period="2021-2026",
            org_type=TypeOrganization.BANCADA,
        )
        is False
    )
