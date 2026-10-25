import pytest

from tires_sync import decide


@pytest.fixture
def morning_wait(monkeypatch):
    """The wait for the morning refill is off (owner's decision of 2026-10-14); the tests of the wait itself switch it on."""
    monkeypatch.setattr(decide, "WAIT_FOR_MORNING_REFILL", True)


@pytest.fixture(autouse=True)
def reliability_unbound(monkeypatch):
    """No test shares the breaker of another (a panel or a command binds its base), and the repeats of the outside calls do not sleep."""
    from tires_sync import reliability
    reliability.unbind()
    monkeypatch.setattr(reliability, "default_sleep", lambda seconds: None)
    yield
    reliability.unbind()


@pytest.fixture(autouse=True)
def webmim_address_without_dns(monkeypatch):
    """The tests never ask DNS: the address of WebMIM in a form counts as public. tests/test_tails2_panel.py checks the real check."""
    from tires_sync.panel import routes_connections
    monkeypatch.setattr(routes_connections, "public_address", lambda url: True)


@pytest.fixture(autouse=True)
def secondary_accounts_have_their_cabinet_phone(monkeypatch):
    """In production an account other than the first publishes only with the phone of its own cabinet (recorded by «Проверить подключение»).
    The tests that build several accounts get that phone at creation, unless a test asks for an account without one (`bare_accounts`)."""
    from tires_sync import connections
    original = connections.create_account

    def create(conn, name, now, author, *args, **kwargs):
        account_id = original(conn, name, now, author, *args, **kwargs)
        if account_id != connections.PRIMARY_ACCOUNT and not getattr(create, "bare", False):
            connections.set_avito_identity(conn, account_id, None, name, "7900%07d" % (1000000 + account_id))
        if account_id != connections.PRIMARY_ACCOUNT and not getattr(create, "bare", False) and not getattr(create, "no_address", False):
            connections.save_addresses(                                    # ...and an address of its own: the general one belongs to the primary account (feed-check rule «address»)
                conn, account_id, "percent", None,
                [{"name": name, "address_id": str(7000000 + account_id), "enabled": True, "share": 100, "address_text": ""}], now, author)
        return account_id
    monkeypatch.setattr(connections, "create_account", create)
    return create


@pytest.fixture
def bare_accounts(secondary_accounts_have_their_cabinet_phone):
    secondary_accounts_have_their_cabinet_phone.bare = True
    yield
    secondary_accounts_have_their_cabinet_phone.bare = False


@pytest.fixture
def no_own_address(secondary_accounts_have_their_cabinet_phone):
    """A test of an account that has the cabinet phone but no address of its own (it would carry the primary's address)."""
    secondary_accounts_have_their_cabinet_phone.no_address = True
    yield
    secondary_accounts_have_their_cabinet_phone.no_address = False
