from types import SimpleNamespace
from datetime import date
from flask import Blueprint, Response, abort, redirect, render_template, request
from flask_babel import gettext as _
from sqlalchemy import case, func, or_, select
from backend.core.enums import TypeCommittee, TypeOrganization
from backend.database.models import (
    Bill,
    ChamberMembership,
    BillOrganization,
    Congresista,
    Membership,
    Organization,
)


from .utils import (
    create_committee_option,
    create_party_option,
    create_region_option,
    create_special_committee_option,
    latest_org_name,
)
from .processed_session import SessionProcessed

congress_bp = Blueprint("congress", __name__, template_folder="../templates")


# Get the main information of the Congressmember
def _congresista_view(db, congresista: Congresista) -> SimpleNamespace:
    party_name = latest_org_name(db, congresista.id, TypeOrganization.PARTY)
    chamber_membership = db.execute(
        select(ChamberMembership)
        .where(
            ChamberMembership.person_id == congresista.id,
            ChamberMembership.org_id == 1,
        )
        .order_by(ChamberMembership.end_date.desc())
        .limit(1)
    ).scalar_one_or_none()

    return SimpleNamespace(
        id=congresista.id,
        full_name=congresista.full_name,
        first_name=congresista.first_name,
        last_name=congresista.last_name,
        photo_url=congresista.photo_url,
        website=congresista.website,
        party_name=party_name,
        dist_electoral=(
            chamber_membership.dist_electoral if chamber_membership else None
        ),
        condicion=(
            chamber_membership.condicion if chamber_membership else _("No disponible")
        ),
        votes_in_election=(
            chamber_membership.votes_in_election if chamber_membership else 0
        ),
    )


def _photo_mimetype(photo_bytes: bytes) -> str:
    if photo_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if photo_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if photo_bytes.startswith(b"RIFF") and photo_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


@congress_bp.route("/congress")
def index():
    name_q = request.args.get("name_q", "").strip()
    party_q = request.args.get("party_q", "").strip()
    region_q = request.args.get("region_q", "").strip()
    commission_q = request.args.get("commission_q", "").strip()
    special_committee_q = request.args.get("special_committee_q", "").strip()

    congresistas = []
    filters = []
    party_options = []
    region_options = []
    committee_options = []
    special_committee_options = []

    if name_q:
        filters.append(
            func.unaccent(func.lower(Congresista.full_name)).like(
                func.unaccent(func.lower(f"%{name_q}%"))
            )
        )

    if party_q:
        filters.append(
            Congresista.id.in_(
                select(Membership.person_id)
                .join(Organization, Organization.org_id == Membership.org_id)
                .where(
                    Membership.org_type == TypeOrganization.PARTY,
                    Organization.org_name == party_q,
                )
            )
        )

    if region_q:
        filters.append(
            Congresista.id.in_(
                select(ChamberMembership.person_id)
                .where(ChamberMembership.dist_electoral == region_q)
                .distinct()
            )
        )

    if commission_q:
        filters.append(
            Congresista.id.in_(
                select(Membership.person_id)
                .join(Organization, Organization.org_id == Membership.org_id)
                .where(
                    Membership.org_type == TypeOrganization.COMMITTEE,
                    Organization.org_subtype == TypeCommittee.COM_ORD,
                    Organization.org_name == commission_q,
                )
            )
        )

    if special_committee_q:
        filters.append(
            Congresista.id.in_(
                select(Membership.person_id)
                .join(Organization, Organization.org_id == Membership.org_id)
                .where(
                    Membership.org_type == TypeOrganization.COMMITTEE,
                    Organization.org_subtype == TypeCommittee.COM_ESP,
                    Organization.org_short_name == special_committee_q,
                )
            )
        )

    with SessionProcessed() as db:
        party_options = create_party_option(db)
        region_options = create_region_option(db)
        committee_options = create_committee_option(db)
        special_committee_options = create_special_committee_option(db)

        query = select(Congresista).order_by(Congresista.full_name.asc())
        if filters:
            query = query.where(*filters).limit(50)

        rows = db.execute(query).scalars()
        congresistas = [_congresista_view(db, row) for row in rows]

    return render_template(
        "congress/search.html",
        name_q=name_q,
        party_q=party_q,
        region_q=region_q,
        commission_q=commission_q,
        special_committee_q=special_committee_q,
        congresistas=congresistas,
        party_options=party_options,
        region_options=region_options,
        committee_options=committee_options,
        special_committee_options=special_committee_options,
    )


@congress_bp.route("/congress/<int:congresista_id>/photo")
def congress_photo(congresista_id):
    with SessionProcessed() as db:
        congresista = db.get(Congresista, congresista_id)

        if not congresista:
            abort(404)

        if congresista.photo_bytes:
            return Response(
                congresista.photo_bytes,
                mimetype=_photo_mimetype(congresista.photo_bytes),
            )

        if congresista.photo_url:
            return redirect(
                congresista.photo_url.replace(
                    "https://www.congreso.gob.pe",
                    "https://www3.congreso.gob.pe",
                )
            )

    abort(404)


@congress_bp.route("/congress/<congresista_id>")
def congress_detail(congresista_id):
    with SessionProcessed() as db:
        congresista_row = db.get(Congresista, int(congresista_id))

        if not congresista_row:
            abort(404)

        congresista = _congresista_view(db, congresista_row)

        # To avoid duplicated bills
        latest_bill_dates = (
            select(
                BillOrganization.bill_id,
                func.max(BillOrganization.presentation_date).label(
                    "latest_presentation_date"
                ),
            )
            .group_by(BillOrganization.bill_id)
            .subquery()
        )

        bills_authored = [
            SimpleNamespace(
                id=bill.id,
                pley_id=bill.pley_id,
                title=bill.title,
                presentation_date=presentation_date,
            )
            for bill, presentation_date in db.execute(
                select(Bill, latest_bill_dates.c.latest_presentation_date)
                .join(latest_bill_dates, latest_bill_dates.c.bill_id == Bill.id)
                .where(Bill.author_id == congresista.id)
                .order_by(latest_bill_dates.c.latest_presentation_date.desc())
                .limit(5)
            ).all()
        ]

        bills_authored_count = db.execute(
            select(func.count())
            .select_from(Bill)
            .where(Bill.author_id == congresista.id)
        ).scalar_one()

        successful_bills_count = db.execute(
            select(func.count())
            .select_from(Bill)
            .where(
                Bill.author_id == congresista.id,
                Bill.bill_approved.is_(True),
            )
        ).scalar_one()

        approval_rate_rows = db.execute(
            select(
                Bill.author_id,
                func.count(Bill.id).label("total_bills"),
                func.sum(case((Bill.bill_approved.is_(True), 1), else_=0)).label(
                    "approved_bills"
                ),
            )
            .where(Bill.author_id.is_not(None))
            .group_by(Bill.author_id)
        ).all()
        average_success_rate = (
            round(
                sum(
                    100 * (approved_bills / total_bills)
                    for _, total_bills, approved_bills in approval_rate_rows
                    if total_bills
                )
                / len(approval_rate_rows),
                1,
            )
            if approval_rate_rows
            else 0
        )

        memberships = (
            db.execute(
                select(
                    Membership.role,
                    Membership.start_date,
                    Membership.end_date,
                    Organization.org_name,
                    Organization.org_type,
                    Organization.org_subtype,
                    Organization.org_short_name,
                )
                .join(Organization, Organization.org_id == Membership.org_id)
                .where(
                    Membership.person_id == congresista.id,
                    Membership.end_date >= date(2026, 7, 26),
                    func.lower(Membership.role) != "accesitario",
                    or_(
                        Membership.org_type == TypeOrganization.COMMITTEE,
                        Organization.org_type == TypeOrganization.COMMITTEE,
                    ),
                )
                .order_by(Membership.end_date.desc(), Membership.start_date.desc())
            )
            .mappings()
            .all()
        )

        profile_stats = {
            "assistance_rate": "45%",
            "bills_authored": bills_authored_count,
            "success_rate": f"{
                (
                    round(100 * (successful_bills_count / bills_authored_count), 1)
                    if bills_authored_count
                    else 0
                )
            } %",
            "average_success_rate": f"{average_success_rate} %",
            "successful_bills": successful_bills_count,
        }

        recent_votes = [
            {
                "position": _("A favor"),
                "bill": "Proyecto de ley N 32014",
                "description": _(
                    "Los datos de votación aún no están disponibles. Este es contenido temporal para la vista de detalle del congresista."
                ),
            },
            {
                "position": _("En contra"),
                "bill": "Proyecto de ley N 32074",
                "description": _(
                    "Los datos de votación aún no están disponibles. Este es contenido temporal para la vista de detalle del congresista."
                ),
            },
        ]

        return render_template(
            "congress/congress_detail.html",
            congresista=congresista,
            memberships=memberships,
            bills_authored=bills_authored,
            profile_stats=profile_stats,
            recent_votes=recent_votes,
        )
