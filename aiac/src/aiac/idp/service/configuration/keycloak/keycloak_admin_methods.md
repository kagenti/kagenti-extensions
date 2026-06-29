# KeycloakAdmin — Available Methods

Source: `python-keycloak` library (`keycloak/keycloak_admin.py`)

## Realm
- `get_realms`, `get_realm`, `create_realm`, `update_realm`, `delete_realm`
- `import_realm`, `partial_import_realm`, `export_realm`
- `get_current_realm`, `change_current_realm`
- `get_realm_users_profile`, `update_realm_users_profile`
- `get_server_info`

## Users
- `get_users`, `create_user`, `get_user`, `get_user_id`, `update_user`, `delete_user`
- `users_count`
- `disable_user`, `enable_user`, `disable_all_users`, `enable_all_users`
- `set_user_password`
- `get_credentials`, `delete_credential`
- `user_logout`, `user_consents`, `revoke_consent`
- `get_user_groups`
- `get_user_social_logins`, `add_user_social_login`, `delete_user_social_login`
- `send_update_account`, `send_verify_email`
- `get_sessions`
- `sync_users`
- `get_bruteforce_detection_status`, `clear_bruteforce_attempts_for_user`, `clear_all_bruteforce_attempts`

## Realm Roles
- `get_realm_roles`, `get_realm_role`, `get_realm_role_by_id`, `get_realm_role_groups`, `get_realm_role_members`
- `create_realm_role`, `update_realm_role`, `delete_realm_role`
- `get_default_realm_role_id`, `get_realm_default_roles`, `add_realm_default_roles`, `remove_realm_default_roles`
- `add_composite_realm_roles_to_role`, `remove_composite_realm_roles_to_role`, `get_composite_realm_roles_of_role`
- `get_role_by_id`, `update_role_by_id`, `delete_role_by_id`, `get_role_composites_by_id`
- `assign_realm_roles`, `delete_realm_roles_of_user`, `get_realm_roles_of_user`
- `get_available_realm_roles_of_user`, `get_composite_realm_roles_of_user`
- `assign_group_realm_roles`, `delete_group_realm_roles`, `get_group_realm_roles`

## Clients
- `get_clients`, `get_client`, `get_client_id`, `create_client`, `update_client`, `delete_client`
- `get_client_installation_provider`
- `create_initial_access_token`
- `generate_client_secrets`, `get_client_secrets`
- `get_client_service_account_user`
- `get_client_all_sessions`, `get_client_sessions_stats`
- `get_client_management_permissions`, `update_client_management_permissions`
- `get_mappers_from_client`, `add_mapper_to_client`, `update_client_mapper`, `remove_client_mapper`

## Client Roles
- `get_client_roles`, `get_client_role`, `get_client_role_id`
- `create_client_role`, `update_client_role`, `delete_client_role`
- `add_composite_client_roles_to_role`, `remove_composite_client_roles_from_role`
- `get_composite_client_roles_of_role`, `get_composite_client_roles_of_group`
- `assign_client_role`, `get_client_role_members`, `get_client_role_groups`
- `get_client_roles_of_user`, `get_available_client_roles_of_user`, `get_composite_client_roles_of_user`, `delete_client_roles_of_user`
- `assign_group_client_roles`, `get_group_client_roles`, `delete_group_client_roles`
- `get_all_roles_of_user`
- `get_role_client_level_children`

## Client Scopes
- `get_client_scopes`, `get_client_scope`, `get_client_scope_by_name`
- `create_client_scope`, `update_client_scope`, `delete_client_scope`
- `get_mappers_from_client_scope`, `add_mapper_to_client_scope`, `delete_mapper_from_client_scope`, `update_mapper_in_client_scope`
- `get_all_roles_of_client_scope`
- `get_client_default_client_scopes`, `add_client_default_client_scope`, `delete_client_default_client_scope`
- `get_client_optional_client_scopes`, `add_client_optional_client_scope`, `delete_client_optional_client_scope`
- `get_default_default_client_scopes`, `add_default_default_client_scope`, `delete_default_default_client_scope`
- `get_default_optional_client_scopes`, `add_default_optional_client_scope`, `delete_default_optional_client_scope`
- `assign_realm_roles_to_client_scope`, `delete_realm_roles_of_client_scope`, `get_realm_roles_of_client_scope`
- `assign_client_roles_to_client_scope`, `delete_client_roles_of_client_scope`, `get_client_roles_of_client_scope`
- `add_client_specific_roles_to_client_scope`, `remove_client_specific_roles_of_client_scope`, `get_client_specific_roles_of_client_scope`

## Client Authorization
- `get_client_authz_settings`, `import_client_authz_config`
- `get_client_authz_resources`, `get_client_authz_resource`, `create_client_authz_resource`, `update_client_authz_resource`, `delete_client_authz_resource`
- `get_client_authz_scopes`, `create_client_authz_scopes`
- `get_client_authz_permissions`, `get_client_authz_policy_resources`, `get_client_authz_policy_scopes`
- `get_client_authz_policies`, `get_client_authz_policy`, `delete_client_authz_policy`, `get_client_authz_permission_associated_policies`
- `create_client_authz_role_based_policy`, `create_client_authz_policy`, `create_client_authz_resource_based_permission`
- `get_client_authz_scope_permission`, `create_client_authz_scope_permission`, `update_client_authz_scope_permission`, `update_client_authz_resource_permission`
- `get_client_authz_client_policies`, `create_client_authz_client_policy`
- `get_client_certificate_key_info`, `upload_certificate`

## Groups
- `get_groups`, `get_group`, `get_subgroups`, `get_group_children`, `get_group_by_path`
- `create_group`, `update_group`, `delete_group`
- `groups_count`
- `get_group_members`
- `group_set_permissions`, `group_user_add`, `group_user_remove`
- `get_composite_client_roles_of_group`

## Identity Providers
- `get_idps`, `get_idp`, `create_idp`, `update_idp`, `delete_idp`
- `add_mapper_to_idp`, `update_mapper_in_idp`, `get_idp_mappers`

## Authentication Flows
- `get_authentication_flows`, `get_authentication_flow_for_id`, `create_authentication_flow`, `update_authentication_flow`, `copy_authentication_flow`, `delete_authentication_flow`
- `get_authentication_flow_executions`, `update_authentication_flow_executions`, `get_authentication_flow_execution`, `create_authentication_flow_execution`, `delete_authentication_flow_execution`
- `change_execution_priority`
- `create_authentication_flow_subflow`
- `get_authenticator_providers`, `get_authenticator_provider_config_description`, `get_authenticator_config`, `create_execution_config`, `update_authenticator_config`, `delete_authenticator_config`
- `get_required_actions`, `get_required_action_by_alias`, `update_required_action`

## Organizations
- `get_organizations`, `get_organization`, `create_organization`, `update_organization`, `delete_organization`
- `get_organization_idps`, `organization_idp_add`, `organization_idp_remove`
- `get_organization_members`, `get_organization_members_count`, `organization_user_add`, `organization_user_remove`
- `get_user_organizations`

## Components & Events
- `get_components`, `create_component`, `get_component`, `update_component`, `delete_component`
- `get_keys`
- `get_admin_events`, `get_events`, `set_events`

## Cache
- `clear_keys_cache`, `clear_realm_cache`, `clear_user_cache`
