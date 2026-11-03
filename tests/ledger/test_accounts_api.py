PREFIX = "/payroll/api/v1/ledger"


def _create(client, **kw):
    body = {"name": "Cash", "type": "asset", **kw}
    return client.post(f"{PREFIX}/accounts", json=body)


def test_owner_creates_updates_and_deactivates(owner_client):
    response = _create(owner_client, number="1000", detailType="Bank")
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Cash"
    assert data["fullName"] == "Cash"
    assert data["normalBalance"] == "debit"
    assert data["isActive"] is True
    assert data["balance"] == "0.00"
    pk = data["id"]

    patched = owner_client.patch(f"{PREFIX}/accounts/{pk}", json={"name": "Checking"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "Checking"

    off = owner_client.post(f"{PREFIX}/accounts/{pk}/deactivate")
    assert off.status_code == 200
    assert off.json()["isActive"] is False
    on = owner_client.post(f"{PREFIX}/accounts/{pk}/activate")
    assert on.json()["isActive"] is True


def test_patch_can_clear_parent(owner_client):
    parent = _create(owner_client, name="P", type="expense").json()
    child = _create(owner_client, name="C", type="expense", parentId=parent["id"]).json()
    assert child["fullName"] == "P:C"
    cleared = owner_client.patch(f"{PREFIX}/accounts/{child['id']}", json={"parentId": None})
    assert cleared.json()["parentId"] is None
    # an omitted parentId leaves it alone
    owner_client.patch(f"{PREFIX}/accounts/{child['id']}", json={"parentId": parent["id"]})
    kept = owner_client.patch(f"{PREFIX}/accounts/{child['id']}", json={"name": "C2"})
    assert kept.json()["parentId"] == parent["id"]


def test_viewer_reads_but_cannot_mutate(owner_client, viewer_client):
    pk = _create(owner_client).json()["id"]
    assert viewer_client.get(f"{PREFIX}/accounts").status_code == 200
    assert viewer_client.get(f"{PREFIX}/accounts/tree").status_code == 200
    assert viewer_client.get(f"{PREFIX}/accounts/{pk}").status_code == 200
    assert _create(viewer_client, name="X").status_code == 403
    assert viewer_client.patch(f"{PREFIX}/accounts/{pk}", json={"name": "Z"}).status_code == 403
    assert viewer_client.post(f"{PREFIX}/accounts/{pk}/deactivate").status_code == 403
    assert viewer_client.post(f"{PREFIX}/accounts/{pk}/activate").status_code == 403
    response = viewer_client.put(
        f"{PREFIX}/system-roles/accounts_receivable", json={"accountId": pk}
    )
    assert response.status_code == 403


def test_list_and_tree(owner_client):
    parent = _create(owner_client, name="P", type="expense").json()
    _create(owner_client, name="C", type="expense", parentId=parent["id"])
    rows = owner_client.get(f"{PREFIX}/accounts").json()
    assert {r["fullName"] for r in rows} == {"P", "P:C"}
    tree = owner_client.get(f"{PREFIX}/accounts/tree").json()
    assert [(n["fullName"], n["depth"]) for n in tree] == [("P", 0), ("P:C", 1)]
    owner_client.post(f"{PREFIX}/accounts/{parent['id']}/deactivate")
    assert len(owner_client.get(f"{PREFIX}/accounts").json()) == 1
    assert len(owner_client.get(f"{PREFIX}/accounts?includeInactive=true").json()) == 2


def test_system_role_bind_and_unbind(owner_client):
    pk = _create(owner_client).json()["id"]
    url = f"{PREFIX}/system-roles/accounts_receivable"
    bound = owner_client.put(url, json={"accountId": pk})
    assert bound.status_code == 200
    roles = owner_client.get(f"{PREFIX}/system-roles").json()
    assert roles["accounts_receivable"]["id"] == pk
    assert roles["due_to_owner"] is None
    assert owner_client.get(f"{PREFIX}/accounts/{pk}").json()["systemRole"] == "accounts_receivable"
    assert owner_client.put(url, json={"accountId": None}).status_code == 200
    assert owner_client.get(f"{PREFIX}/system-roles").json()["accounts_receivable"] is None
    assert (
        owner_client.put(f"{PREFIX}/system-roles/bogus", json={"accountId": pk}).status_code == 422
    )


def test_errors_use_structured_body(owner_client):
    missing = owner_client.get(f"{PREFIX}/accounts/99999")
    assert missing.status_code == 404
    assert missing.json()["errors"][0]["type"] == "AccountNotFoundError"
    assert owner_client.patch(f"{PREFIX}/accounts/99999", json={"name": "x"}).status_code == 404
    assert owner_client.post(f"{PREFIX}/accounts/99999/deactivate").status_code == 404
    _create(owner_client)
    dup = _create(owner_client)
    assert dup.status_code == 409
    bad = _create(owner_client, name="Child", parentId=99999)
    assert bad.status_code == 404


def test_unauthenticated_is_401(anon_client):
    assert anon_client.get(f"{PREFIX}/accounts").status_code == 401
    assert anon_client.get(f"{PREFIX}/accounts/tree").status_code == 401


def test_racing_create_is_409_not_500(owner_client, monkeypatch):
    from payroll.ledger.accounts import AccountService

    assert _create(owner_client, number="1000").status_code == 201
    monkeypatch.setattr(AccountService, "_check_unique", lambda *a, **k: None)
    response = _create(owner_client, name="Other", number="1000")
    assert response.status_code == 409
    assert response.json()["errors"][0]["type"] == "AccountConflictError"
