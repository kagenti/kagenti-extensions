"""Service Offboarding sub-agent (UC4) — stub.

Counterpart to Service Onboarding. Where onboarding *adds* a service's policy
footprint, offboarding *removes* it: the Controller's ``/apply/offboard`` route
resolves the service key here and then calls the PCE's ``decommission`` directly
(no ``compute_and_apply`` / ``(rules, override)`` tuple — decommission is a
whole-service teardown, not a rule fold).

Identity asymmetry with onboard. Onboarding is keyed by the Keycloak **internal
UUID** (``Service.id``) and resolves it to the clientId via ``get_services()``.
Offboarding cannot: an offboarded client is gone from ``get_services()``, so
UUID→clientId resolution is impossible. The offboard contract therefore carries
the **clientId (the SPM key)** directly, and this stub returns it unchanged.
Full validation/resolution lands with the UC4 implementation (issue 3.21).
"""


def offboard_service(service_id: str) -> str:
    return service_id
