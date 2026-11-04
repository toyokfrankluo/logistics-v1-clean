"""Trusted test setup: disposable database and network guard required."""
import requests
from public_tracking import PublicState
from isolated_runtime import shadow_runtime_valid


def configure_public(app):
    assert shadow_runtime_valid(app)
    app.extensions['public_tracking'] = PublicState()
    app.config.update(PUBLIC_SIMULATION=True, PUBLIC_SUPPLIER_HOURLY=1000,
                      PUBLIC_RATE_MINUTE=1000, PUBLIC_RATE_HOUR=10000,
                      PUBLIC_CACHE_TTL=300, PUBLIC_CACHE_CAPACITY=256, PUBLIC_TRUSTED_PROXIES=())
    def fake_response(provider, number, shipment):
        # requests.post is mocked by each test; global socket guard is the backstop.
        response = requests.post('https://fictional.invalid/test-only', json={'number': number}, timeout=1)
        return response.status_code, response.json()
    app.extensions['public_simulated_response'] = fake_response
