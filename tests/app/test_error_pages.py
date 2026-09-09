"""Tests for the app-wide styled 404 page.

Invalid bill/congressmember IDs used to return a bare, unstyled "Not Found"
string with no site header/nav. They should now render the shared
``errors/404.html`` template (which extends ``base.html``) via a registered
``@app.errorhandler(404)``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.models import Base


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
    import app.routes.congress as congress_module
    from app.app import create_app

    monkeypatch.setattr(bills_module, "SessionProcessed", session_factory)
    monkeypatch.setattr(congress_module, "SessionProcessed", session_factory)
    flask_app = create_app()
    flask_app.testing = True
    return flask_app.test_client()


def test_invalid_bill_id_shows_styled_404_page(client):
    response = client.get("/bills/9999_99999")
    body = response.get_data(as_text=True)

    assert response.status_code == 404
    assert 'class="site-navbar"' in body
    assert 'class="footer"' in body
    assert "Página no encontrada" in body


def test_invalid_congressmember_id_shows_styled_404_page(client):
    response = client.get("/congress/99999")
    body = response.get_data(as_text=True)

    assert response.status_code == 404
    assert 'class="site-navbar"' in body
    assert 'class="footer"' in body
    assert "Página no encontrada" in body
