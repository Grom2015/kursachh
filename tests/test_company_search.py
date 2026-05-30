def test_search_lukoil_ru(client):
    response = client.get("/companies/search", params={"q": "лукойл"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["ticker"] == "LKOH"


def test_search_lukoil_ticker(client):
    response = client.get("/companies/search", params={"q": "LKOH"})
    assert response.status_code == 200
    assert response.json()["items"][0]["ticker"] == "LKOH"


def test_search_unknown_returns_empty(client):
    response = client.get("/companies/search", params={"q": "unknown-company"})
    assert response.status_code == 200
    assert response.json()["items"] == []

