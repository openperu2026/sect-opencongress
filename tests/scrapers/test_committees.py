from datetime import datetime

from lxml.html import fromstring
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from backend.database.raw_models import Base, RawCommittee
from backend.scrapers.committees import (
    BASE_URL,
    COMMITTEE_SELECT,
    LINKS_SELECTOR,
    ROWS_SELECTOR,
    TABLE_SELECTOR,
    RawCommitteeScraper,
)


# ---------- helpers ----------


def make_scraper():
    """
    Avoid calling RawCommitteeScraper.__init__ because it creates
    a real engine from settings.DB_URL.
    """
    scraper = RawCommitteeScraper.__new__(RawCommitteeScraper)
    scraper.url = BASE_URL
    scraper.session = None
    scraper._tracking_updates = []
    return scraper


def setup_inmemory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    return engine, SessionLocal


class FakeLocator:
    def __init__(self, count_value=1, html="<tbody>before</tbody>"):
        self.count_value = count_value
        self.html = html

    def count(self):
        return self.count_value

    def inner_html(self):
        return self.html


class FakePage:
    def __init__(
        self,
        rows_count=1,
        links_count=1,
        content="<html>OK</html>",
        timeout_on_selector=False,
        timeout_on_change=False,
    ):
        self.rows_count = rows_count
        self.links_count = links_count
        self._content = content
        self.timeout_on_selector = timeout_on_selector
        self.timeout_on_change = timeout_on_change
        self.goto_calls = []

    def wait_for_selector(self, selector, state=None):
        if self.timeout_on_selector:
            raise PlaywrightTimeoutError("selector timed out")
        return True

    def wait_for_function(self, script, arg=None, timeout=None):
        if self.timeout_on_change and timeout == 5000:
            raise PlaywrightTimeoutError("table did not change")
        return True

    def locator(self, selector):
        if selector == TABLE_SELECTOR:
            return FakeLocator(html="<tbody>before</tbody>")
        if selector == ROWS_SELECTOR:
            return FakeLocator(count_value=self.rows_count)
        if selector == LINKS_SELECTOR:
            return FakeLocator(count_value=self.links_count)
        return FakeLocator()

    def evaluate(self, script, arg):
        return True

    def eval_on_selector(self, selector, script):
        assert selector == COMMITTEE_SELECT
        return {"Ordinaria": "1", "Especial": "2"}

    def content(self):
        return self._content

    def goto(self, url, wait_until=None):
        self.goto_calls.append((url, wait_until))


# ---------- get_options ----------


def test_get_options_parses_select(monkeypatch):
    scraper = make_scraper()

    def fake_parse_url(url):
        assert url == BASE_URL
        html = """
        <html><body>
          <select name="idRegistroPadre">
            <option value="2021">2021</option>
            <option value="2022">2022</option>
            <option>--Seleccione--</option>
          </select>
        </body></html>
        """
        return fromstring(html)

    monkeypatch.setattr("backend.scrapers.committees.parse_url", fake_parse_url)

    options = scraper.get_options(url=BASE_URL, select_name="idRegistroPadre")

    assert options == {
        "2021": "2021",
        "2022": "2022",
    }


def test_get_options_returns_empty_when_parse_fails(monkeypatch):
    scraper = make_scraper()

    monkeypatch.setattr("backend.scrapers.committees.parse_url", lambda url: None)

    assert scraper.get_options(BASE_URL) == {}


# ---------- _get_committee_options_current_page ----------


def test_get_committee_options_current_page():
    page = FakePage()

    options = RawCommitteeScraper._get_committee_options_current_page(page)

    assert options == {
        "Ordinaria": "1",
        "Especial": "2",
    }


# ---------- get_html_with_selections ----------


def test_get_html_with_selections_success(monkeypatch):
    scraper = make_scraper()
    page = FakePage(
        rows_count=2,
        links_count=2,
        content="<html>committee data</html>",
    )

    monkeypatch.setattr(scraper, "_select_year", lambda page, year_value: None)
    monkeypatch.setattr(
        RawCommitteeScraper,
        "_set_select",
        staticmethod(lambda page, selector, value: None),
    )

    html = scraper.get_html_with_selections(
        page=page,
        year_value="2025",
        committee_value="1",
    )

    assert html == "<html>committee data</html>"


def test_get_html_with_selections_returns_none_when_table_empty(monkeypatch):
    scraper = make_scraper()
    page = FakePage(rows_count=0, links_count=0)

    warnings = []

    monkeypatch.setattr(scraper, "_select_year", lambda page, year_value: None)
    monkeypatch.setattr(
        RawCommitteeScraper,
        "_set_select",
        staticmethod(lambda page, selector, value: None),
    )
    monkeypatch.setattr(
        "backend.scrapers.committees.logger.warning",
        lambda message: warnings.append(message),
    )

    html = scraper.get_html_with_selections(
        page=page,
        year_value="2025",
        committee_value="1",
    )

    assert html is None
    assert any(
        "No committees found for type=1 and year=2025" in msg for msg in warnings
    )


def test_get_html_with_selections_continues_when_table_does_not_change(monkeypatch):
    scraper = make_scraper()
    page = FakePage(
        rows_count=1,
        links_count=1,
        timeout_on_change=True,
        content="<html>still valid</html>",
    )

    warnings = []

    monkeypatch.setattr(scraper, "_select_year", lambda page, year_value: None)
    monkeypatch.setattr(
        RawCommitteeScraper,
        "_set_select",
        staticmethod(lambda page, selector, value: None),
    )
    monkeypatch.setattr(
        "backend.scrapers.committees.logger.warning",
        lambda message: warnings.append(message),
    )

    html = scraper.get_html_with_selections(
        page=page,
        year_value="2025",
        committee_value="1",
    )

    assert html == "<html>still valid</html>"
    assert any("Table content did not visibly change" in msg for msg in warnings)


def test_get_html_with_selections_returns_none_on_playwright_timeout(monkeypatch):
    scraper = make_scraper()
    page = FakePage(timeout_on_selector=True)

    monkeypatch.setattr(scraper, "_select_year", lambda page, year_value: None)

    html = scraper.get_html_with_selections(
        page=page,
        year_value="2025",
        committee_value="1",
    )

    assert html is None


# ---------- get_raw_committees ----------


def test_get_raw_committees_builds_committee_list(monkeypatch):
    scraper = make_scraper()

    monkeypatch.setattr(
        scraper,
        "get_options",
        lambda url, select_name="idRegistroPadre": {
            "2024-2025": "2025",
            "2023-2024": "2024",
        },
    )
    monkeypatch.setattr(scraper, "_select_year", lambda page, year_value: None)
    monkeypatch.setattr(
        scraper,
        "_get_committee_options_current_page",
        lambda page: {
            "Ordinaria": "1",
            "Especial": "2",
        },
    )

    def fake_get_html_with_selections(page, year_value, committee_value):
        if year_value == "2025" and committee_value == "2":
            return None
        return f"<html>year={year_value}, committee={committee_value}</html>"

    monkeypatch.setattr(
        scraper,
        "get_html_with_selections",
        fake_get_html_with_selections,
    )
    monkeypatch.setattr(scraper, "update_tracking", lambda committee: [committee])

    class FakeBrowser:
        def __init__(self):
            self.closed = False
            self.page = FakePage()

        def new_page(self):
            return self.page

        def close(self):
            self.closed = True

    class FakeChromium:
        def __init__(self):
            self.browser = FakeBrowser()

        def launch(self, headless=True):
            assert headless is True
            return self.browser

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "backend.scrapers.committees.sync_playwright",
        lambda: FakePlaywright(),
    )

    scraper.get_raw_committees(only_current=True)

    assert hasattr(scraper, "committee_list")
    assert len(scraper.committee_list) == 1

    committee = scraper.committee_list[0]
    assert committee.legislative_year == "2024-2025"
    assert committee.committee_type == "Ordinaria"
    assert committee.raw_html == "<html>year=2025, committee=1</html>"


def test_get_raw_committees_all_years(monkeypatch):
    scraper = make_scraper()

    monkeypatch.setattr(
        scraper,
        "get_options",
        lambda url, select_name="idRegistroPadre": {
            "2024-2025": "2025",
            "2023-2024": "2024",
        },
    )
    monkeypatch.setattr(scraper, "_select_year", lambda page, year_value: None)
    monkeypatch.setattr(
        scraper,
        "_get_committee_options_current_page",
        lambda page: {"Ordinaria": "1"},
    )
    monkeypatch.setattr(
        scraper,
        "get_html_with_selections",
        lambda page, year_value, committee_value: (
            f"<html>year={year_value}, committee={committee_value}</html>"
        ),
    )
    monkeypatch.setattr(scraper, "update_tracking", lambda committee: [committee])

    class FakeBrowser:
        def new_page(self):
            return FakePage()

        def close(self):
            pass

    class FakePlaywright:
        def __enter__(self):
            self.chromium = self
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def launch(self, headless=True):
            return FakeBrowser()

    monkeypatch.setattr(
        "backend.scrapers.committees.sync_playwright",
        lambda: FakePlaywright(),
    )

    scraper.get_raw_committees(only_current=False)

    assert len(scraper.committee_list) == 2
    assert {c.legislative_year for c in scraper.committee_list} == {
        "2024-2025",
        "2023-2024",
    }


def test_get_raw_committees_aborts_when_no_years(monkeypatch):
    scraper = make_scraper()

    monkeypatch.setattr(scraper, "get_options", lambda url, select_name: {})

    scraper.get_raw_committees()

    assert scraper.committee_list == []


# ---------- update_tracking ----------


def test_update_tracking_first_version_marks_changed():
    engine, SessionLocal = setup_inmemory_db()
    scraper = make_scraper()
    scraper.Session = SessionLocal

    committee = RawCommittee(
        timestamp=datetime(2026, 1, 1),
        legislative_year="2025-2026",
        committee_type="Ordinaria",
        raw_html="<html>new</html>",
        changed=False,
        processed=True,
        last_update=False,
    )

    result = scraper.update_tracking(committee)

    assert result == [committee]
    assert committee.changed is True
    assert committee.processed is False
    assert committee.last_update is True


def test_update_tracking_existing_same_version_returns_empty_list():
    """Regression test for the store-only-if-changed fix: an unchanged
    snapshot must return [] and must NOT flip the existing row's
    last_update -- the original bug flipped it unconditionally in the
    "else" branch regardless of whether anything actually changed."""
    engine, SessionLocal = setup_inmemory_db()
    scraper = make_scraper()
    scraper.Session = SessionLocal

    old = RawCommittee(
        timestamp=datetime(2026, 1, 1),
        legislative_year="2025-2026",
        committee_type="Ordinaria",
        raw_html="<html>same</html>",
        changed=True,
        processed=False,
        last_update=True,
    )

    with SessionLocal() as session:
        session.add(old)
        session.commit()

    new = RawCommittee(
        timestamp=datetime(2026, 1, 2),
        legislative_year="2025-2026",
        committee_type="Ordinaria",
        raw_html="<html>same</html>",
        changed=False,
        processed=False,
        last_update=True,
    )

    result = scraper.update_tracking(new)

    assert result == []

    with SessionLocal() as session:
        old_from_db = session.query(RawCommittee).first()
        assert old_from_db.last_update is True


def test_update_tracking_existing_different_version_marks_changed():
    engine, SessionLocal = setup_inmemory_db()
    scraper = make_scraper()
    scraper.Session = SessionLocal

    old = RawCommittee(
        timestamp=datetime(2026, 1, 1),
        legislative_year="2025-2026",
        committee_type="Ordinaria",
        raw_html="<html>old</html>",
        changed=True,
        processed=False,
        last_update=True,
    )

    with SessionLocal() as session:
        session.add(old)
        session.commit()

    new = RawCommittee(
        timestamp=datetime(2026, 1, 2),
        legislative_year="2025-2026",
        committee_type="Ordinaria",
        raw_html="<html>new</html>",
        changed=False,
        processed=False,
        last_update=True,
    )

    result = scraper.update_tracking(new)

    assert len(result) == 2
    assert result[0] is new
    assert new.changed is True
    assert new.processed is False
    assert new.last_update is True


# ---------- add_committees_to_db ----------


def test_add_committees_to_db_persists():
    engine, SessionLocal = setup_inmemory_db()

    scraper = make_scraper()
    scraper.Session = SessionLocal

    scraper.committee_list = [
        RawCommittee(
            timestamp=datetime(2026, 1, 1),
            legislative_year="2025-2026",
            committee_type="Ordinaria",
            raw_html="<html>data</html>",
            changed=True,
            processed=False,
            last_update=True,
        )
    ]

    assert scraper.add_committees_to_db() is True

    with SessionLocal() as session:
        rows = session.query(RawCommittee).all()

    assert len(rows) == 1
    assert rows[0].legislative_year == "2025-2026"
    assert rows[0].committee_type == "Ordinaria"
    assert rows[0].raw_html == "<html>data</html>"


def test_add_committees_to_db_returns_false_when_empty():
    """An empty buffer is now the ROUTINE outcome of an all-unchanged
    scrape run -- must return False gracefully, not raise/assert."""
    scraper = make_scraper()
    scraper.committee_list = []

    assert scraper.add_committees_to_db() is False


def test_add_committees_to_db_handles_sqlalchemy_error():
    scraper = make_scraper()

    scraper.committee_list = [
        RawCommittee(
            timestamp=datetime.now(),
            legislative_year="2025-2026",
            committee_type="Ordinaria",
            raw_html="<html></html>",
        )
    ]

    class DummySession:
        def __init__(self):
            self.rolled_back = False
            self.closed = False

        def bulk_save_objects(self, objs):
            raise SQLAlchemyError("boom")

        def commit(self):
            pass

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    dummy_session = DummySession()
    scraper.Session = lambda: dummy_session

    assert scraper.add_committees_to_db() is False
    assert dummy_session.rolled_back is True
    assert dummy_session.closed is True


# ---------- 2026-2031 chamber committees index ----------


def test_get_chamber_committees_synthesizes_index_and_roundtrips_process_committee(
    monkeypatch,
):
    """CRITICAL: proves the scraper's synthetic index HTML is
    byte-compatible with process_committee()'s existing xpath contract."""
    from backend.process.organizations import process_committee

    scraper = make_scraper()
    scraper.update_tracking = lambda c: [c]

    index_html = """
    <html><body>
      <a href="https://senado.congreso.gob.pe/comision-de-justicia-y-derechos-humanos/">
        Comisión de Justicia y Derechos Humanos
      </a>
      <a href="https://senado.congreso.gob.pe/comision-de-defensa-nacional-y-orden-interno/">
        Comisión de Defensa Nacional y Orden Interno
      </a>
      <a href="https://senado.congreso.gob.pe/otra-pagina/">Not a committee</a>
    </body></html>
    """

    def fake_parse_url(url, *args, **kwargs):
        assert url == "https://senado.congreso.gob.pe/comisiones/"
        return fromstring(index_html)

    monkeypatch.setattr("backend.scrapers.committees.parse_url", fake_parse_url)

    result = scraper.get_chamber_committees("Senadores")

    assert len(result) == 1
    raw_comm = result[0]
    assert raw_comm.chamber == "Senadores"
    assert raw_comm.legislative_year == "2026"

    orgs = process_committee(raw_comm)
    assert {o.org_name for o in orgs} == {
        "Comisión de Justicia y Derechos Humanos",
        "Comisión de Defensa Nacional y Orden Interno",
    }
    assert all(o.parent_org_name == "Senado de la República" for o in orgs)
    assert all(o.org_subtype == "Comisión Ordinaria" for o in orgs)


_WP_COMISIONES_HTML = """
<html><body>
<div class="wp-comisiones">
<div class="titulo-seccion">Comisiones Ordinarias Legislativas</div>
<div class="nombre-comision"><a href="https://diputados.congreso.gob.pe/constitucion-reglamento-y-relaciones-exteriores/">Comisión de Constitución, Reglamento y Relaciones Exteriores</a></div>
<div class="nombre-comision"><a href="https://diputados.congreso.gob.pe/justicia-y-derechos-humanos/">Comisión de Justicia y Derechos Humanos.</a></div>
<div class="titulo-seccion">Comisiones Ordinarias No Legislativas (art.45)</div>
<div class="nombre-comision"><a href="https://diputados.congreso.gob.pe/etica/">Comisión de Ética Parlamentaria.</a></div>
<div class="titulo-seccion">Comisiones Extraordinarias</div>
<div class="nombre-comision"><a href="https://diputados.congreso.gob.pe/futura-comision/">Comisión Futura</a></div>
</div>
</body></html>
"""


def test_get_chamber_committees_tags_rows_by_section_title(monkeypatch):
    """CRITICAL: confirms the real wp-comisiones/titulo-seccion structure
    (confirmed live 2026-09-02) tags each committee with its own section's
    title, not a single hardcoded "Comisión Ordinaria" for every row --
    and that an unrecognized future section title ("Comisiones
    Extraordinarias") is written through verbatim by the scraper, then
    logged and skipped (not raised) by process_committee(), so it doesn't
    block the OTHER committees sharing the same chamber's RawCommittee
    row/raw_html blob."""
    from backend.process.organizations import process_committee

    scraper = make_scraper()
    scraper.update_tracking = lambda c: [c]

    monkeypatch.setattr(
        "backend.scrapers.committees.parse_url",
        lambda url, *a, **k: fromstring(_WP_COMISIONES_HTML),
    )

    result = scraper.get_chamber_committees("Diputados")
    assert len(result) == 1
    raw_comm = result[0]

    # The unrecognized section's committee is NOT silently dropped by the
    # scraper -- its raw section title is still present verbatim in
    # raw_html, confirming the scraper layer doesn't do any filtering.
    assert "Comisión Futura" in raw_comm.raw_html
    assert "Comisiones Extraordinarias" in raw_comm.raw_html

    orgs = process_committee(raw_comm)
    by_name = {o.org_name: o for o in orgs}
    assert (
        by_name[
            "Comisión de Constitución, Reglamento y Relaciones Exteriores"
        ].org_subtype
        == "Comisión Ordinaria Legislativa"
    )
    assert (
        by_name["Comisión de Justicia y Derechos Humanos."].org_subtype
        == "Comisión Ordinaria Legislativa"
    )
    # Classified by SECTION membership (it's under "No Legislativas" on
    # the index page), not by parse_comm_type's name-based rule for this
    # committee -- that rule matches when the type text is literally
    # "Comisión de Ética Parlamentaria" (e.g. a legacy dropdown value),
    # which isn't the case here.
    assert (
        by_name["Comisión de Ética Parlamentaria."].org_subtype
        == "Comisión Ordinaria No Legislativa"
    )
    # Unrecognized type: logged and skipped, not raised -- the other 3
    # committees in the same batch still process correctly.
    assert "Comisión Futura" not in by_name
    assert len(orgs) == 3


_JOINT_COMMITTEE_PAGE_HTML = """
<html><body>
  <h1>Comisión Bicameral de Presupuesto y Cuenta General de la República</h1>
  <table>
    <thead><tr>
      <th>Foto</th><th>Apellidos y Nombres</th>
      <th>Grupo Parlamentario</th><th>Cargo</th><th>e-Mail</th>
    </tr></thead>
    <tbody>
      <tr>
        <td><img/></td>
        <td><a href="https://senado.congreso.gob.pe/senador/arista-arbildo-jose-berley">
          Arista Arbildo, Jose Berley</a></td>
        <td>FUERZA POPULAR</td>
        <td>Presidente</td>
        <td>jarista@congreso.gob.pe</td>
      </tr>
    </tbody>
  </table>
</body></html>
"""


def test_get_joint_committees_builds_congreso_tagged_committee(monkeypatch):
    """CRITICAL: proves the joint committee scrape resolves through
    process_committee() as a child of "Congreso de la República" (the
    whole-Congress body, chamber="Congreso") -- the member table itself is
    irrelevant (existence-only scrape, see get_joint_committees'
    docstring), only the <h1> name is used."""
    from backend.process.organizations import process_committee

    scraper = make_scraper()
    scraper.update_tracking = lambda c: [c]

    monkeypatch.setattr(
        "backend.scrapers.committees.parse_url",
        lambda url, *a, **k: fromstring(_JOINT_COMMITTEE_PAGE_HTML),
    )

    result = scraper.get_joint_committees()
    assert len(result) == 1
    raw_comm = result[0]
    assert raw_comm.chamber == "Congreso"
    assert raw_comm.committee_type == "Comisión Bicameral"

    orgs = process_committee(raw_comm)
    assert len(orgs) == 1
    org = orgs[0]
    assert (
        org.org_name
        == "Comisión Bicameral de Presupuesto y Cuenta General de la República"
    )
    assert org.org_subtype == "Comisión Bicameral"
    assert org.parent_org_name == "Congreso de la República"
    assert org.parent_org_type == "Cámara"


def test_get_joint_committees_skips_page_missing_h1(monkeypatch):
    scraper = make_scraper()
    scraper.update_tracking = lambda c: [c]

    monkeypatch.setattr(
        "backend.scrapers.committees.parse_url",
        lambda url, *a, **k: fromstring("<html><body>no h1 here</body></html>"),
    )

    result = scraper.get_joint_committees()
    assert result == []


def test_get_joint_committees_skips_failed_fetch(monkeypatch):
    scraper = make_scraper()
    scraper.update_tracking = lambda c: [c]

    monkeypatch.setattr(
        "backend.scrapers.committees.parse_url", lambda url, *a, **k: None
    )

    result = scraper.get_joint_committees()
    assert result == []


def test_get_chamber_committees_excludes_index_self_link(monkeypatch):
    """Regression: the page's own nav link back to /comisiones/ (plural)
    also starts with "{base_url}/comision" -- must not be scraped as a
    fake committee named "Comisiones"."""
    scraper = make_scraper()
    scraper.update_tracking = lambda c: [c]

    index_html = """
    <html><body>
      <a href="https://senado.congreso.gob.pe/comisiones/">Comisiones</a>
      <a href="https://senado.congreso.gob.pe/comision-de-justicia-y-derechos-humanos/">
        Comisión de Justicia y Derechos Humanos
      </a>
    </body></html>
    """
    monkeypatch.setattr(
        "backend.scrapers.committees.parse_url",
        lambda url, *a, **k: fromstring(index_html),
    )

    result = scraper.get_chamber_committees("Senadores")

    assert len(result) == 1
    parsed = fromstring(result[0].raw_html)
    names = [
        content.text_content().strip()
        for _, content in (row.getchildren() for row in parsed.xpath("//tr"))
    ]
    assert names == ["Comisión de Justicia y Derechos Humanos"]


def test_get_chamber_committees_no_links_found_returns_empty(monkeypatch):
    scraper = make_scraper()
    monkeypatch.setattr(
        "backend.scrapers.committees.parse_url",
        lambda *a, **k: fromstring("<html><body>nothing here</body></html>"),
    )
    assert scraper.get_chamber_committees("Diputados") == []


def test_get_chamber_committees_fetch_failure_returns_empty(monkeypatch):
    scraper = make_scraper()
    monkeypatch.setattr("backend.scrapers.committees.parse_url", lambda *a, **k: None)
    assert scraper.get_chamber_committees("Senadores") == []
