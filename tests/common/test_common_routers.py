PREFIX = "/payroll/api/v1"


class TestHealthCheck:
    def test_returns_200(self, client):
        response = client.get(f"{PREFIX}/healthcheck")
        assert response.status_code == 200

    def test_returns_healthy_status(self, client):
        data = client.get(f"{PREFIX}/healthcheck").json()
        assert data["status"] == "healthy"


class TestInfo:
    def test_returns_200(self, client):
        response = client.get(f"{PREFIX}/info")
        assert response.status_code == 200

    def test_returns_app_name(self, client):
        data = client.get(f"{PREFIX}/info").json()
        assert data["appName"] == "Payroll"

    def test_returns_version(self, client):
        data = client.get(f"{PREFIX}/info").json()
        assert "version" in data

    def test_returns_debug_mode(self, client):
        data = client.get(f"{PREFIX}/info").json()
        assert "debugMode" in data
