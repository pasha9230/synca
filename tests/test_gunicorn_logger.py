from tires_sync.panel.gunicorn_logger import mask


def test_search_query_is_masked_wherever_it_stands():
    assert mask("GET /search?q=%2B7900 HTTP/1.1") == "GET /search?q=*** HTTP/1.1"
    assert mask("GET /orders?q=Иван&tag=vip HTTP/1.1") == "GET /orders?q=***&tag=vip HTTP/1.1"
    assert mask("GET /orders?tag=vip&q=9000000011 HTTP/1.1") == "GET /orders?tag=vip&q=*** HTTP/1.1"
    assert mask("/chats?q=%D0%98%D0%B2%D0%B0%D0%BD&page=2") == "/chats?q=***&page=2"
    assert mask("/catalog?q=") == "/catalog?q=***"


def test_other_parameters_and_tokens_stay_as_they_are():
    assert mask("GET /orders?seq=5&tag=a HTTP/1.1") == "GET /orders?seq=5&tag=a HTTP/1.1"
    assert mask("GET /s/abcDEF123/photo?q=1 HTTP/1.1") == "GET /s/***/photo?q=*** HTTP/1.1"
    assert mask("POST /hook/vk/secret-path HTTP/1.1") == "POST /hook/vk/*** HTTP/1.1"


def test_query_is_masked_in_the_referer_the_bare_query_atom_and_percent_written_name():
    assert mask("https://panel.example/orders?q=%2B79000000011") == "https://panel.example/orders?q=***"
    assert mask("q=%D0%98%D0%B2%D0%B0%D0%BD&tag=vip") == "q=***&tag=vip"
    assert mask("GET /orders?%71=Иван&tag=a HTTP/1.1") == "GET /orders?%71=***&tag=a HTTP/1.1"
    assert mask("GET /orders?tag=a&%71=9000000011 HTTP/1.1") == "GET /orders?tag=a&%71=*** HTTP/1.1"


def test_the_feed_token_of_an_account_is_masked_in_its_photo_and_feed_addresses():
    token = "0123456789abcdef0123456789abcdef"
    assert mask("GET /img/%s/100.jpg HTTP/1.1" % token) == "GET /img/***/100.jpg HTTP/1.1"
    assert mask("GET /feed/%s/avito_feed.xml HTTP/1.1" % token) == "GET /feed/***/avito_feed.xml HTTP/1.1"
    assert mask("https://panel.example/img/%s/1.jpg" % token) == "https://panel.example/img/***/1.jpg"
    assert mask("GET /feed/update HTTP/1.1") == "GET /feed/update HTTP/1.1"                 # the panel's own page stays readable
