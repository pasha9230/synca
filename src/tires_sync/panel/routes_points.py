"""«Подключения → Точки»: who works at a pickup point and how to reach them."""
from __future__ import annotations

from flask import abort, flash, redirect, render_template, request

from .. import connections, order_points as points, webmim_create, webmim_match, webmim_orders
from ..secretbox import SecretKeyError
from ..transport import urllib_transport
from .common import can_post, get_conn, login_required


POINT_FIELDS = (("Название точки", "name"), ("Точка с таким названием", "name"), ("Менеджер", "manager_name"),
                ("Телефон менеджера", "manager_phone"), ("Канал связи", "channel"), ("Адрес доставки WebMIM", "webmim_address_id"),
                ("Этот ВК", "vk_peer_id"), ("Время доставки", "default_time"), ("Адрес Авито", "avito_address_id"))      # message → field


def register(app) -> None:
    def webmim_addresses(conn) -> dict:
        """«Показать адреса WebMIM»: the delivery addresses of the chosen server, read when asked (never on every opening of the page)."""
        server = webmim_match.current_server(conn)
        out = {"server": webmim_orders.SERVER_NAMES[server], "rows": None, "problem": ""}
        box = app.config.get("SECRET_BOX")
        if not can_post("connections_points_save"):          # a read of WebMIM is a login there: those who edit the points only
            out["problem"] = "Адреса WebMIM читают те, кто правит точки."
            return out
        try:
            client = webmim_match.client_for(conn, box, app.config.get("WEBMIM_HTTP") or urllib_transport, server) if box is not None else None
        except SecretKeyError:
            client, box = None, None
        except ValueError:                                   # the settings, not the master key (their text is not shown: it may quote them)
            out["problem"] = "Настройки WebMIM не подходят: проверьте «Подключения → WebMIM → Заказы в WebMIM»."
            return out
        if client is None:
            out["problem"] = ("API-пользователь WebMIM не настроен (Подключения → WebMIM → Заказы в WebMIM)." if box is not None
                              else "Секреты не читаются: проверьте мастер-ключ.")
            return out
        try:
            out["rows"] = webmim_create.addresses(client)
        except (OSError, webmim_orders.OrdersError, ValueError) as err:
            out["problem"] = "Адреса WebMIM не прочитаны (%s)." % webmim_create._why(err)
        finally:
            client.close()
        return out

    @app.route("/connections/points")
    @login_required
    def connections_points_page():
        conn = get_conn()
        tabs = {"active": "points", "account_id": None, "supplier_id": None, "suppliers": [], "accounts": []}
        return render_template("conn_points.html", tabs=tabs, box_ok=True, rows=points.list_points(conn), channels=points.CHANNEL_NAMES,
                               accounts=connections.list_accounts(conn),
                               webmim=webmim_addresses(conn) if request.args.get("addresses") == "1" else None)

    @app.route("/connections/points/save", methods=["POST"])
    @login_required
    def connections_points_save():
        raw = request.form.get("id", "")
        point_id = int(raw) if raw.isascii() and raw.isdigit() else None
        try:
            points.save_point(get_conn(), request.form, point_id)
            flash("Точка сохранена.", "ok")
        except points.PointError as err:
            field = next((f for prefix, f in POINT_FIELDS if str(err).startswith(prefix)), "")
            flash(str(err), "field:%s@%s" % (field, point_id or "new") if field else "error")      # under the field of that point's form
        return redirect("/connections/points")

    @app.route("/connections/points/<int:point_id>/delete", methods=["POST"])
    @login_required
    def connections_points_delete(point_id):
        try:
            points.delete_point(get_conn(), point_id)
            flash("Точка удалена.")
        except points.PointError as err:
            flash(str(err))
        return redirect("/connections/points")

    @app.route("/connections/points/<int:point_id>/code", methods=["POST"])
    @login_required
    def connections_points_code(point_id):
        try:
            points.new_bind_code(get_conn(), point_id)
            flash("Код создан. Менеджер отправляет его сообщением сообществу в ВК: его ВК привяжется к точке, код сработает один раз.", "ok")
        except points.PointError:
            abort(404)
        return redirect("/connections/points")
