from flask import Blueprint, flash, redirect, render_template, request, url_for

from changedetectionio.auth_decorator import login_optionally_required
from changedetectionio.notification_groups import (
    INHERITANCE_TEXT,
    NOTIFICATION_EVENTS,
    form_submitted_routes,
    public_group,
    routes_from_form,
)
from changedetectionio.store import ChangeDetectionStore


def construct_blueprint(datastore: ChangeDetectionStore):
    blueprint = Blueprint(
        "notification_groups",
        __name__,
        template_folder="templates",
    )

    def _groups():
        return datastore.list_notification_groups()

    @blueprint.route("", methods=["GET"])
    @login_optionally_required
    def index():
        groups = _groups()
        cards = []
        for group_uuid, group in sorted(groups.items(), key=lambda item: str(item[1].get("title") or "").lower()):
            references = datastore.notification_group_references(group_uuid)
            cards.append({
                "group": group,
                "public": public_group(group),
                "references": references,
            })
        return render_template(
            "notification-groups.html",
            cards=cards,
            inheritance=INHERITANCE_TEXT,
            notification_events=NOTIFICATION_EVENTS,
            notification_groups=groups,
            route_values=datastore.data["settings"]["application"].get("notification_group_routes") or {},
        )

    @blueprint.route("", methods=["POST"])
    @login_optionally_required
    def create():
        urls = _lines(request.form.get("notification_urls", ""))
        try:
            group = datastore.add_notification_group(request.form.get("title", ""), urls)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("notification_groups.index"))
        flash(f"Notification group “{group['title']}” saved.")
        return redirect(url_for("notification_groups.index"))

    @blueprint.route("/<string:uuid>", methods=["POST"])
    @login_optionally_required
    def update(uuid):
        urls = _lines(request.form.get("notification_urls", ""))
        if request.form.get("confirm") != "yes":
            flash("Tick the confirmation box to replace this group's destinations.", "error")
            return redirect(url_for("notification_groups.index"))
        try:
            group = datastore.update_notification_group(uuid, title=request.form.get("title", ""), notification_urls=urls)
        except KeyError:
            flash("That notification group no longer exists.", "error")
            return redirect(url_for("notification_groups.index"))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("notification_groups.index"))
        flash(f"Notification group “{group['title']}” updated.")
        return redirect(url_for("notification_groups.index"))

    @blueprint.route("/<string:uuid>/delete", methods=["POST"])
    @login_optionally_required
    def delete(uuid):
        if request.form.get("confirm") != "yes":
            flash("Tick the confirmation box to delete this notification group.", "error")
            return redirect(url_for("notification_groups.index"))
        try:
            references = datastore.delete_notification_group(uuid)
        except KeyError:
            flash("That notification group no longer exists.", "error")
            return redirect(url_for("notification_groups.index"))
        flash(
            "Notification group deleted. "
            f"Cleared it from {len(references['watches'])} watches, "
            f"{len(references['tags'])} tags, and "
            f"{len(references['global_events'])} system alerts. "
            "Those alerts now use the next inheritance level. Existing notification URL lists were kept."
        )
        return redirect(url_for("notification_groups.index"))

    @blueprint.route("/routes", methods=["POST"])
    @login_optionally_required
    def routes():
        if not form_submitted_routes(request.form):
            flash("Choose a group for at least one alert, or Inherit to clear it.", "error")
            return redirect(url_for("notification_groups.index"))
        try:
            saved = routes_from_form(request.form, datastore.data["settings"]["application"])
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("notification_groups.index"))
        datastore.data["settings"]["application"]["notification_group_routes"] = saved or {}
        datastore.needs_write_urgent = True
        flash("System notification groups updated. Watches and tags that set their own group still win.")
        return redirect(url_for("notification_groups.index"))

    return blueprint


def _lines(value: str) -> list[str]:
    return [line.strip() for line in (value or "").splitlines() if line.strip()]
