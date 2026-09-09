from types import SimpleNamespace

import pytest
import backend.process.organizations as mod
from backend import RoleOrganization, TypeAdmin, TypeCommittee, TypeOrganization


def _raw_committee(
    *, raw_html: str, legislative_year: str = "2025", chamber: str | None = None
):
    return SimpleNamespace(
        raw_html=raw_html, legislative_year=legislative_year, chamber=chamber
    )


def _raw_org(
    *,
    raw_html: str,
    legislative_year: str = "2025",
    type_org: str = "Mesa Directiva",
    org_link: str = "/org/mesa",
    web_page: str = "www.org.gob.pe/org/mesa",
    chamber: str | None = None,
):
    return SimpleNamespace(
        raw_html=raw_html,
        legislative_year=legislative_year,
        type_org=type_org,
        org_link=org_link,
        web_page=web_page,
        timestamp=f"{legislative_year}-08-01T00:00:00",
        chamber=chamber,
    )


@pytest.fixture
def committee_html_two_rows():
    return """
    <table class="congresistas">
      <tbody>
        <tr>
          <td>Comisión Ordinaria</td>
          <td><a href="/comisiones/economia">Comisión de Economía</a></td>
        </tr>
        <tr>
          <td>Comisiones Especiales</td>
          <td><a href="/comisiones/salud">Comisión Especial de Salud</a></td>
        </tr>
      </tbody>
    </table>
    """


@pytest.fixture
def org_membership_html():
    return """
    <table class="congresistas">
      <tbody>
        <tr>
          <th>#</th><th>Nombre</th><th>Web</th><th>Dato</th><th>Cargo</th>
        </tr>
        <tr>
          <td>1</td>
          <td>Juan Pérez</td>
          <td><a href="https://example.com/juan">Perfil</a></td>
          <td>-</td>
          <td>presidente</td>
        </tr>
        <tr>
          <td>2</td>
          <td>Maria Lopez</td>
          <td><a href="https://example.com/maria">Perfil</a></td>
          <td>-</td>
          <td>miembro</td>
        </tr>
      </tbody>
    </table>
    """


def test_process_committee_builds_organizations(monkeypatch, committee_html_two_rows):
    raw = _raw_committee(raw_html=committee_html_two_rows, legislative_year="2025")

    out = mod.process_committee(raw)

    assert len(out) == 2

    assert out[0].org_type == TypeOrganization.COMMITTEE
    assert out[0].org_subtype == TypeCommittee.COM_ORD
    assert out[0].org_name == "Comisión de Economía"
    assert out[0].org_link == "/comisiones/economia"

    assert out[1].org_subtype == TypeCommittee.COM_ESP
    assert out[1].org_name == "Comisión Especial de Salud"
    assert out[1].org_link == "/comisiones/salud"


def test_process_committee_classifies_legislativa_and_no_legislativa_subtypes():
    """2026-2031 term: committees now get tagged with their committees-
    index SECTION title (see committees.py::get_chamber_committees), not
    a single generic "Comisión Ordinaria" -- confirms both new subtypes
    classify correctly and an unrecognized future section type is logged
    and skipped rather than raising for the whole batch."""
    html = """
    <table class="congresistas">
      <tbody>
        <tr>
          <td>Comisiones Ordinarias Legislativas</td>
          <td><a href="/comision-justicia/">Comisión de Justicia y Derechos Humanos</a></td>
        </tr>
        <tr>
          <td>Comisiones Ordinarias No Legislativas (art.45)</td>
          <td><a href="/comision-etica/">Comisión de Ética Parlamentaria.</a></td>
        </tr>
        <tr>
          <td>Comisiones Extraordinarias</td>
          <td><a href="/comision-futura/">Comisión Futura</a></td>
        </tr>
      </tbody>
    </table>
    """
    raw = _raw_committee(raw_html=html, legislative_year="2026", chamber="Diputados")

    out = mod.process_committee(raw)

    assert len(out) == 2
    by_name = {o.org_name: o for o in out}
    assert by_name["Comisión de Justicia y Derechos Humanos"].org_subtype == (
        TypeCommittee.COM_ORD_LEG
    )
    assert by_name["Comisión de Ética Parlamentaria."].org_subtype == (
        TypeCommittee.COM_ORD_NO_LEG
    )
    assert "Comisión Futura" not in by_name


def test_process_committee_senadores_chamber_sets_senado_parent(
    committee_html_two_rows,
):
    raw = _raw_committee(
        raw_html=committee_html_two_rows,
        legislative_year="2026",
        chamber="Senadores",
    )

    out = mod.process_committee(raw)

    assert out[0].parent_org_name == "Senado de la República"
    assert out[0].parent_org_type == "Cámara"


def test_process_committee_congreso_chamber_is_whole_congress_joint_entity(
    committee_html_two_rows,
):
    """Confirmed real 2026-09-08: joint/bicameral committees like "Comisión
    Bicameral de Presupuesto..." are children of "Congreso de la República"
    (the whole-Congress body, shared with legacy 2021-2026 data) -- must NOT
    default to either bicameral chamber, and (since the unification) must
    NOT be parentless either."""
    raw = _raw_committee(
        raw_html=committee_html_two_rows,
        legislative_year="2026",
        chamber="Congreso",
    )

    out = mod.process_committee(raw)

    assert out[0].parent_org_name == "Congreso de la República"
    assert out[0].parent_org_type == "Cámara"


def test_process_committee_unrecognized_chamber_raises():
    """Strict lookup by design (Issue 2): an unrecognized chamber label must
    raise, not silently misattribute to the wrong chamber."""
    raw = _raw_committee(raw_html="<table/>", chamber="Bogus")

    with pytest.raises(KeyError):
        mod.process_committee(raw)


def test_process_org_maps_fields(monkeypatch):
    raw = _raw_org(
        raw_html="<table/>",
        legislative_year="2024",
        type_org="Mesa Directiva",
        org_link="/org/mesa",
        web_page="www.org.gob.pe/org/mesa",
    )

    org = mod.process_org(raw)

    assert org.org_name == "Mesa Directiva"
    assert org.org_type == TypeOrganization.ADMINISTRATIVE
    assert org.org_subtype == TypeAdmin.MESA_DIRECTIVA
    assert org.org_link == "/org/mesa"


def test_process_org_membership_creates_memberships_with_year_window(
    monkeypatch, org_membership_html
):
    raw_org = _raw_org(
        raw_html=org_membership_html,
        legislative_year="2025",
        type_org="Mesa Directiva",
        org_link="/org/mesa",
        web_page="www.org.gob.pe/org/mesa",
    )

    org = mod.process_org(raw_org)
    out = mod.process_org_membership(raw_org, org)

    assert len(out) == 2

    assert out[0].cong_name == "Juan Pérez"
    assert out[0].role == RoleOrganization.PRESIDENTE
    assert out[0].start_date is None
    assert out[0].end_date is None

    assert out[1].cong_name == "Maria Lopez"
    assert out[1].role == RoleOrganization.MIEMBRO
    assert out[1].start_date is None
    assert out[1].end_date is None


def test_process_admin_org_senadores_chamber_sets_senado_parent():
    raw = _raw_org(raw_html="<table/>", legislative_year="2026", chamber="Senadores")

    org, _ = mod.process_admin_org(raw)

    assert org.parent_org_name == "Senado de la República"
    assert org.parent_org_type == "Cámara"


def test_process_admin_org_congreso_chamber_is_whole_congress_joint_entity():
    """Confirmed real 2026-09-08: "Comisión Permanente" and the bicameral
    budget committee are children of "Congreso de la República" (the
    whole-Congress body), not parentless."""
    raw = _raw_org(raw_html="<table/>", legislative_year="2026", chamber="Congreso")

    org, _ = mod.process_admin_org(raw)

    assert org.parent_org_name == "Congreso de la República"
    assert org.parent_org_type == "Cámara"


def test_process_admin_org_chamber_none_defaults_to_congreso_de_la_republica():
    raw = _raw_org(raw_html="<table/>", legislative_year="2024", chamber=None)

    org, _ = mod.process_admin_org(raw)

    assert org.parent_org_name == "Congreso de la República"


def test_process_chambers_returns_correct_names():
    """Regression + correction: Senado's official name is "Senado de la
    República" (confirmed by the user directly, Step 0 item 9) -- the
    original code hardcoded the wrong "Cámara de Senadores". Also seeds
    "Congreso de la República" (the pre-2026 unicameral body, kept distinct
    so legacy data never gets conflated with the bicameral chambers --
    confirmed 2026-09-08)."""
    chambers = mod.process_chambers()

    names = {c.org_name for c in chambers}
    assert names == {
        "Cámara de Diputados",
        "Senado de la República",
        "Congreso de la República",
    }
    assert "Cámara de Senadores" not in names
