"""CRM, the clients: the list with a search and a tag filter, the card (phones, the car, the tags, the inquiries, the purchases, the chats,
the calls, a comment), the proposals to join two clients, joining, undoing a join and splitting an order or a chat off into a client of its
own. Every change is in the journal."""
from __future__ import annotations

from flask import abort, flash, redirect, render_template, request

from .. import clients, connections, order_manager
from .common import author, can, get_conn, has_right, now_iso

DUPLICATES_SHOWN = 300                  # the page of the duplicates shows this many pairs at most (the rest after these are joined or waved off)


def register(app) -> None:
    @app.route("/clients")
    def clients_page():
        conn = get_conn()
        query, tag = request.args.get("q", "").strip()[:80], request.args.get("tag", "").strip()[:clients.TAG_MAX]
        return render_template("clients.html", rows=clients.list_clients(conn, query, fold=True, tag=tag), query=query, tag=tag,
                               tags=clients.all_tags(conn), PRAGMATIC=clients.PRAGMATIC, HANDED=clients.HANDED)

    @app.route("/clients/duplicates")
    def clients_duplicates():
        conn = get_conn()
        pairs = clients.duplicates(conn)
        more, pairs = max(0, len(pairs) - DUPLICATES_SHOWN), pairs[:DUPLICATES_SHOWN]
        ids = sorted({p["a"] for p in pairs} | {p["b"] for p in pairs})
        names = dict(conn.execute("SELECT id, name FROM client WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)) if ids else {}
        return render_template("clients_duplicates.html", pairs=pairs, names=names, more=more)

    @app.route("/clients/<int:client_id>")
    def client_page(client_id):
        conn = get_conn()
        try:
            client = clients.get_client(conn, client_id)
        except clients.ClientError:
            abort(404)
        if client["merged_into"]:
            return redirect("/clients/%d" % client["merged_into"])
        accounts = {a["id"]: a["name"] for a in connections.list_accounts(conn)}
        bought = clients.purchases(conn, client_id) if has_right("money") else None
        dups = clients.duplicates(conn, client_id)
        names = dict(conn.execute("SELECT id, name FROM client WHERE id IN (%s)" % ",".join("?" * len(dups)), [d["other"] for d in dups])) if dups else {}
        return render_template("client.html", c=client, entity=clients.card_view(conn, client, accounts, bought, can("chats")),
                               handed_calls=order_manager.handed_calls(conn, [k["id"] for k in client.get("calls") or []]),
                               dups=dups, dup_names=names, joined=clients.merged_from(conn, client_id), PRAGMATIC=clients.PRAGMATIC,
                               HANDED=clients.HANDED, GAP=clients.NEW_INQUIRY_GAP_HOURS)

    def _back(client_id):
        if request.form.get("back") == "duplicates":
            return redirect("/clients/duplicates")
        return redirect("/clients/%d" % client_id)

    @app.route("/clients/<int:client_id>/save", methods=["POST"])
    def client_save(client_id):
        form = request.form
        car = {"brand": form.get("car_brand", ""), "model": form.get("car_model", ""), "year": form.get("car_year", "")} if "car_year" in form else None
        try:
            clients.save(get_conn(), client_id, form.get("name", ""), form.get("note", ""), now_iso(), author(), car=car)
            flash("Сохранено.", "ok")
        except clients.ClientError as err:
            get_conn().rollback()
            flash(str(err))
        return _back(client_id)

    @app.route("/clients/<int:client_id>/tag", methods=["POST"])
    def client_tag(client_id):
        try:
            clients.set_tag(get_conn(), client_id, request.form.get("tag", ""), request.form.get("on") == "1", now_iso(), author())
            get_conn().commit()
        except clients.ClientError as err:
            flash(str(err))
        return _back(client_id)

    @app.route("/clients/<int:client_id>/merge", methods=["POST"])
    def client_merge(client_id):
        other = request.form.get("other", "").strip().lstrip("№")
        try:
            if not other.isdigit():
                raise clients.ClientError("Номер клиента: только цифры.")
            clients.merge(get_conn(), client_id, int(other), now_iso(), author())
            flash("Клиент №%s склеен с №%d. Разъединить можно в карточке №%d." % (other, client_id, client_id), "ok")
        except clients.ClientError as err:
            flash(str(err))
        return _back(client_id)

    @app.route("/clients/<int:client_id>/unmerge/<int:other>", methods=["POST"])
    def client_unmerge(client_id, other):
        try:
            clients.unmerge(get_conn(), client_id, other, now_iso(), author())
            flash("Клиент №%d снова отдельный." % other, "ok")
        except clients.ClientError as err:
            get_conn().rollback()
            flash(str(err))
        return _back(client_id)

    @app.route("/clients/<int:client_id>/skip/<int:other>", methods=["POST"])
    def client_skip(client_id, other):
        try:
            clients.skip_pair(get_conn(), client_id, other, now_iso(), author())
        except clients.ClientError as err:
            flash(str(err))
        return _back(client_id)

    @app.route("/clients/<int:client_id>/split-order/<int:order_id>", methods=["POST"])
    def client_split_order(client_id, order_id):
        try:
            return _back(clients.split_order(get_conn(), order_id, now_iso(), author()))
        except clients.ClientError as err:
            flash(str(err))
            return _back(client_id)

    @app.route("/clients/<int:client_id>/split-chat/<int:chat_row>", methods=["POST"])
    def client_split_chat(client_id, chat_row):
        try:
            return _back(clients.split_chat(get_conn(), chat_row, now_iso(), author()))
        except clients.ClientError as err:
            flash(str(err))
            return _back(client_id)
