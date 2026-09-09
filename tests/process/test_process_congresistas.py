import json
from types import SimpleNamespace
import pytest
import backend.process.congresistas as mod
from backend import RoleOrganization
from datetime import datetime


@pytest.fixture()
def dict_data_cong():
    website = "https://www.congreso.gob.pe/congresista/juan"
    return {
        website: {
            "first_name": "Juan Alberto",
            "last_name": "Perez Quispe",
            "full_name": "Juan Alberto Perez Quispe",
            "dni": "12345678",
            "gender": "Masculino",
            "website": website,
        }
    }


@pytest.fixture
def profile_html():
    # Must match the xpaths used in process_profile_content
    return """
    <html>
      <div class="nombres"><span>Label</span><span>Juan Alberto Perez Quispe</span></div>
      <div class="grupo"><span>Label</span><span>Accion Popular</span></div>
      <div class="bancada"><span>Label</span><span>Accion Popular</span></div>
      <div class="votacion"><span>Label</span><span>12,345</span></div>
      <div class="representa"><span>Label</span><span>Lima</span></div>
      <div class="condicion"><span>Label</span><span>Titular</span></div>
      <div class="foto"><img src="/FotosCongresista/juan.jpg"/></div>
    </html>
    """


def _raw_cong(
    *,
    profile_content="",
    memberships_content=None,
    leg_period="2021-2026",
    website="https://www.congreso.gob.pe/congresista/juan",
    chamber=None,
):
    if memberships_content is None:
        memberships_content = {"data": []}
    return SimpleNamespace(
        profile_content=profile_content,
        memberships_content=json.dumps(memberships_content),
        leg_period=leg_period,
        website=website,
        timestamp=datetime(2025, 8, 1),
        chamber=chamber,
    )


def test_xpath2_returns_text_when_found(profile_html):
    from lxml.html import fromstring

    html = fromstring(profile_html)
    assert (
        mod.xpath2('//*[@class="nombres"]/span[2]', html) == "Juan Alberto Perez Quispe"
    )


def test_xpath2_returns_none_when_missing(profile_html):
    from lxml.html import fromstring

    html = fromstring(profile_html)
    assert mod.xpath2('//*[@class="does-not-exist"]/span[2]', html) is None


def test_process_profile_content_parses_fields_and_votes_int(
    profile_html, dict_data_cong
):
    raw = _raw_cong(profile_content=profile_html, leg_period="2021-2026")

    cong, orgs, memberships = mod.process_profile_content(raw, dict_data_cong)

    assert cong.full_name == "Juan Alberto Perez Quispe"
    assert cong.website == "https://www.congreso.gob.pe/congresista/juan"
    assert cong.photo_url == "https://www.congreso.gob.pe/FotosCongresista/juan.jpg"
    assert [org.org_name for org in orgs] == [
        "Accion Popular",
        "Congreso de la República",
    ]
    assert memberships[0].org_name == "Accion Popular"
    assert memberships[1].votes_in_election == 12345
    assert memberships[1].org_name == "Congreso de la República"


def test_process_profile_content_chamber_none_resolves_to_congreso_de_la_republica(
    profile_html, dict_data_cong
):
    """chamber=None is what every historical 2021-2026 row has -- confirmed
    2026-09-08 against production data that this must resolve to the old
    unicameral "Congreso de la República" body, distinct from an explicit
    chamber="Diputados" row (2026-2031 term). Previously these were required
    to be byte-identical (both defaulting to "Cámara de Diputados"), which is
    exactly what caused every legacy committee/organization lookup to
    silently miss in production."""
    raw_none = _raw_cong(
        profile_content=profile_html, leg_period="2021-2026", chamber=None
    )
    raw_diputados = _raw_cong(
        profile_content=profile_html, leg_period="2021-2026", chamber="Diputados"
    )

    cong_none, orgs_none, memberships_none = mod.process_profile_content(
        raw_none, dict_data_cong
    )
    cong_diputados, orgs_diputados, memberships_diputados = mod.process_profile_content(
        raw_diputados, dict_data_cong
    )

    assert memberships_none[1].org_name == "Congreso de la República"
    assert memberships_diputados[1].org_name == "Cámara de Diputados"
    assert memberships_none[1].role == memberships_diputados[1].role


def test_process_profile_content_reorders_comma_formatted_chamber_name():
    """Found 2026-09: the 2026-2031 chamber roster's raw name is
    "Apellidos, Nombres" (comma), synthesized verbatim into the same
    .nombres div the legacy scraper's already-correctly-ordered
    "Nombres Apellidos" text fills -- process_profile_content's else
    branch must reorder it via split_and_sort_name, not pass it through,
    and should also populate first_name/last_name from it."""
    html = """
    <html>
      <div class="nombres"><span>Label</span><span>Aguinaga Recuenco, Alejandro Aurelio</span></div>
      <div class="grupo"><span>Label</span><span>Fuerza Popular</span></div>
      <div class="votacion"><span>Label</span><span>0</span></div>
      <div class="representa"><span>Label</span><span>Lima</span></div>
      <div class="condicion"><span>Label</span><span>Titular</span></div>
      <div class="foto"><img src="https://example.org/photo.png"/></div>
    </html>
    """
    raw = _raw_cong(profile_content=html, leg_period="2026-2031", chamber="Senadores")

    cong, orgs, memberships = mod.process_profile_content(raw, {})

    assert cong.full_name == "Alejandro Aurelio Aguinaga Recuenco"
    assert cong.first_name == "Alejandro Aurelio"
    assert cong.last_name == "Aguinaga Recuenco"


def test_process_profile_content_enriches_from_2026_2031_dict_by_normalized_name():
    """The 2026-2031 mined dict is keyed by normalized name (no website
    available for this term, confirmed live 2026-09-03) -- dni/gender/
    congresista_id must be pulled in via that lookup, not left None just
    because they're genuinely absent from the profile HTML itself."""
    html = """
    <html>
      <div class="nombres"><span>Label</span><span>Aguinaga Recuenco, Alejandro Aurelio</span></div>
      <div class="grupo"><span>Label</span><span>Fuerza Popular</span></div>
      <div class="votacion"><span>Label</span><span>0</span></div>
      <div class="representa"><span>Label</span><span>Lima</span></div>
      <div class="condicion"><span>Label</span><span>Titular</span></div>
      <div class="foto"><img src="https://example.org/photo.png"/></div>
    </html>
    """
    raw = _raw_cong(profile_content=html, leg_period="2026-2031", chamber="Senadores")
    dict_cong_data_current = {
        mod.normalize_name("Alejandro Aurelio Aguinaga Recuenco", sort_tokens=True): {
            "dni": "08236035",
            "gender": "Masculino",
            "congresista_id": 4,
        }
    }

    cong, orgs, memberships = mod.process_profile_content(
        raw, {}, dict_cong_data_current=dict_cong_data_current
    )

    assert cong.dni == "08236035"
    assert cong.gender == "Masculino"
    assert cong.congresista_id == 4


def test_process_profile_content_no_2026_2031_match_stays_none():
    """Coverage grows over time as more 2026-2031 bills/motions get
    scraped -- must degrade gracefully (no error) when nothing matches
    yet, same as when dict_cong_data_current is omitted entirely."""
    html = """
    <html>
      <div class="nombres"><span>Label</span><span>Someone New, Not Yet Mined</span></div>
      <div class="grupo"><span>Label</span><span>Fuerza Popular</span></div>
      <div class="votacion"><span>Label</span><span>0</span></div>
      <div class="representa"><span>Label</span><span>Lima</span></div>
      <div class="condicion"><span>Label</span><span>Titular</span></div>
      <div class="foto"><img src="https://example.org/photo.png"/></div>
    </html>
    """
    raw = _raw_cong(profile_content=html, leg_period="2026-2031", chamber="Senadores")

    cong, orgs, memberships = mod.process_profile_content(
        raw, {}, dict_cong_data_current={"someone else": {"dni": "1"}}
    )

    assert cong.dni is None
    assert cong.gender is None
    assert cong.congresista_id is None


def test_process_profile_content_legacy_no_comma_name_unchanged(profile_html):
    """Regression: the legacy scraper's .nombres text has no comma and is
    already "Nombres Apellidos" -- split_and_sort_name must pass it
    through unchanged (its documented no-comma fallback), not corrupt it."""
    raw = _raw_cong(profile_content=profile_html, leg_period="2021-2026", chamber=None)

    cong, orgs, memberships = mod.process_profile_content(raw, {})

    assert cong.full_name == "Juan Alberto Perez Quispe"
    # split_and_sort_name's no-comma fallback returns (name, None, None) --
    # first_name/last_name stay unavailable, same as before this fix, since
    # there's no reliable way to split a no-comma string into parts.
    assert cong.first_name is None
    assert cong.last_name is None


def test_process_profile_content_senadores_chamber_maps_to_senado(
    profile_html, dict_data_cong
):
    raw = _raw_cong(
        profile_content=profile_html, leg_period="2026-2031", chamber="Senadores"
    )

    cong, orgs, memberships = mod.process_profile_content(raw, dict_data_cong)

    assert [org.org_name for org in orgs] == [
        "Accion Popular",
        "Senado de la República",
    ]
    assert memberships[1].org_name == "Senado de la República"
    from backend import RoleOrganization

    assert memberships[1].role == RoleOrganization.SENADOR


def test_process_memberships_all_branches(monkeypatch):
    # Normalize role is external logic: mock it for deterministic tests
    monkeypatch.setattr(
        mod,
        "normalize_membership_role",
        lambda s: RoleOrganization((s or "").strip().title()),
    )

    memberships_payload = {
        "data": [
            # Special case: Subcomisión de Acusaciones Constitucionales
            {
                "period": "2021-2026",
                "anio": "2025",
                "desOrgano": "X",
                "desOrganoCongresista": "Subcomisión de Acusaciones Constitucionales",
                "desCargo": "Presidente",
                "fechaInicio": "2025-08-01",
                "fechaFin": None,
            },
            # type_org != '' => Comisión, comm_type = type_org
            {
                "period": "2021-2026",
                "anio": "2025",
                "desOrgano": "Comisión Ordinaria",
                "desOrganoCongresista": "Comisión de Economía",
                "desCargo": "Miembro",
                "fechaInicio": "2025-08-02",
                "fechaFin": "2025-12-31",
            },
            # type_org == '' => org_type = org_name, comm_type = None
            {
                "period": "2021-2026",
                "anio": "2025",
                "desOrgano": "",
                "desOrganoCongresista": "Mesa Directiva",
                "desCargo": "Secretario",
                "fechaInicio": "2025-09-01",
                "fechaFin": "2026-07-27",
            },
        ]
    }

    raw = _raw_cong(memberships_content=memberships_payload, leg_period="2021-2026")
    cong = SimpleNamespace(
        full_name="Juan Pérez",
        leg_period="2021-2026",
        website="www.congreso.gob.pe/juan",
    )

    out = mod.process_memberships(raw, cong)

    assert len(out) == 3

    # 1) Special case
    m0 = out[0]
    assert m0.cong_name == "Juan Pérez"
    assert m0.leg_period == "2021-2026"
    assert m0.role == RoleOrganization.PRESIDENTE
    assert m0.org_name == "Subcomisión de Acusaciones Constitucionales"
    assert m0.org_type == "Comisión"
    assert m0.start_date == datetime.fromisoformat("2025-08-01").date()
    assert m0.end_date is None

    # 2) type_org != ''
    m1 = out[1]
    assert m1.role == RoleOrganization.MIEMBRO
    assert m1.org_name == "Comisión de Economía"
    assert m1.org_type == "Comisión"
    assert m1.start_date == datetime.fromisoformat("2025-08-02").date()
    assert m1.end_date == datetime.fromisoformat("2025-12-31").date()

    # 3) type_org == ''
    m2 = out[2]
    assert m2.role == RoleOrganization.SECRETARIO
    assert m2.org_name == "Mesa Directiva"
    assert m2.org_type == "Administrativo"
    assert m2.start_date == datetime.fromisoformat("2025-09-01").date()
    assert m2.end_date == datetime.fromisoformat("2026-07-27").date()


def test_process_memberships_raises_if_data_is_none(monkeypatch):
    """
    Your code does: json.loads(...).get('data', None) then iterates it.
    If data is None, it will raise TypeError. This test documents current behavior.
    """
    monkeypatch.setattr(mod, "normalize_membership_role", lambda s: s)

    raw = _raw_cong(memberships_content={"data": None})
    cong = SimpleNamespace(full_name="X", leg_period="2021-2026")

    with pytest.raises(TypeError):
        mod.process_memberships(raw, cong)
