from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import Service


class ServiceMaps:
    """Maps scope names and role names to the services that contain them."""

    def __init__(self, realm: str) -> None:
        config = Configuration.for_realm(realm)
        services = config.get_services()

        self.scope_to_service: dict[str, list[Service]] = {}
        self.role_to_service: dict[str, list[Service]] = {}

        for service in services:
            for scope in service.scopes:
                self.scope_to_service.setdefault(scope.name, []).append(service)
            for role in service.roles:
                self.role_to_service.setdefault(role.name, []).append(service)
