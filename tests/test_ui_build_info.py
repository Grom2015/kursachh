def test_app_html_injects_backend_build_metadata(client):
    response = client.get("/app")
    assert response.status_code == 200
    html = response.text
    assert '__APP_VERSION__' not in html
    assert '__BUILD_ID__' not in html
    assert 'meta name="app-version"' in html
    assert 'meta name="app-build-id"' in html
    assert "/app/assets/control-center.js?v=" in html
    assert "/app/assets/control-center.css?v=" in html
    assert "Cache-Control" in response.headers
    assert "no-store" in response.headers["Cache-Control"]


def test_health_exposes_runtime_build_details(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["app_version"]
    assert payload["build_id"]
    assert payload["started_at"]
    assert isinstance(payload["process_id"], int)
