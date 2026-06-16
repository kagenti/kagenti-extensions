# AIAC Agent — Visual Diagrams

## 1. Top-Level Architecture

```mermaid
flowchart TD
    TRIGGERS["HTTP Triggers\nPOST /apply/*"]
    CTRL["Controller\nroutes.py"]

    subgraph CO["Client Onboarding"]
        ORC1["Orchestrator"]
        SA1["Client Provision"]
        SA2["Client Policy"]
        ORC1 --> SA1
        ORC1 --> SA2
    end

    subgraph PU["Policy Update"]
        ORC2["Orchestrator"]
        SA3["Build"]
        SA4["Rebuild"]
        ORC2 --> SA3
        ORC2 --> SA4
    end

    subgraph URR["Users and Roles"]
        ORC3["Orchestrator"]
        SA5["Users"]
        SA6["Roles"]
        ORC3 --> SA5
        ORC3 --> SA6
    end

    TRIGGERS --> CTRL
    CTRL -->|"user/:id or role/:id"| ORC3
    CTRL -->|"build / rebuild"| ORC2
    CTRL -->|"client/:id"| ORC1
```

---

## 2. Client Onboarding Sub-agents

### 2a. Client Provision

```mermaid
flowchart TD
    START(("START")) --> CLASSIFY["classify_client\n\n1. Parse client_id format\n2. Lookup AgentRuntime CR K8s\n3. Populate ClientInfo"]

    CLASSIFY -->|"client_type = agent"| ANALYZE_AGENT["analyze_agent\nLLM -> ClientProvision"]
    CLASSIFY -->|"client_type = tool"| ANALYZE_TOOL["analyze_tool\nLLM -> ClientProvision"]

    ANALYZE_AGENT --> PROVISION["provision_client\n\ncreate_client_role\ncreate_client_scope\nper ClientProvision entry"]
    ANALYZE_TOOL --> PROVISION

    PROVISION --> FORMAT["format_response"]
    FORMAT --> END(("END"))

    %% State annotations
    style CLASSIFY fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style ANALYZE_AGENT fill:#713f12,color:#fef3c7,stroke:#d97706
    style ANALYZE_TOOL fill:#713f12,color:#fef3c7,stroke:#d97706
    style PROVISION fill:#14532d,color:#dcfce7,stroke:#4ade80
```

**State:** `OnboardingProvisionState` — adds `client_info: ClientInfo | None` and `client_provision: ClientProvision | None` to `BaseAgentState`.

### 2b. Client Policy

```mermaid
flowchart TD
    START(("START"))

    START --> FP["fetch_policy\nChromaDB: aiac-policies"]
    START --> FDK["fetch_domain_knowledge\nChromaDB: aiac-domain-knowledge"]
    START --> FKC["fetch_keycloak_state\nusers, roles,\nclient roles/scopes,\nuser role mappings"]

    FP & FDK & FKC --> PROPOSE["propose_mappings\nPlanner LLM -> ProposedDiff\nscoped to new client only"]

    PROPOSE --> VALIDATE["validate_mappings\n1. Existence check\n2. Safety guard rails <= MAX_CHANGES\n3. Auditor LLM re-confirmation\n4. Scope check new client only"]

    VALIDATE --> APPLY["apply_mappings\nassign_client_roles\nrevoke_client_roles"]
    APPLY --> FORMAT["format_response"]
    FORMAT --> END(("END"))

    style FP fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style FDK fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style FKC fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style PROPOSE fill:#713f12,color:#fef3c7,stroke:#d97706
    style VALIDATE fill:#713f12,color:#fef3c7,stroke:#d97706
    style APPLY fill:#14532d,color:#dcfce7,stroke:#4ade80
```

**State:** `BaseAgentState` (no extensions).

---

## 3. Policy Update Sub-agents

### 3a. Build

```mermaid
flowchart TD
    START(("START"))

    START --> FP["fetch_policy\nChromaDB"]
    START --> FDK["fetch_domain_knowledge\nChromaDB"]
    START --> FKC["fetch_keycloak_state\nall users, clients,\nroles, all\nrole mappings"]

    FP & FDK & FKC --> PROPOSE["propose_diff\nPlanner LLM -> ProposedDiff\nminimal delta vs live state"]

    PROPOSE --> VALIDATE["validate_diff\n1. Existence check\n2. Safety guard rails\n3. Auditor LLM\n4. Scope check"]

    VALIDATE --> APPLY["apply_diff\nassign_client_roles\nrevoke_client_roles"]
    APPLY --> FORMAT["format_response"]
    FORMAT --> END(("END"))

    style FP fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style FDK fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style FKC fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style PROPOSE fill:#713f12,color:#fef3c7,stroke:#d97706
    style VALIDATE fill:#713f12,color:#fef3c7,stroke:#d97706
    style APPLY fill:#14532d,color:#dcfce7,stroke:#4ade80
```

### 3b. Rebuild

```mermaid
flowchart TD
    START(("START")) --> CLEAR["clear_assignments\nrevoke_all_role_assignments\nrealm-wide wipe"]

    CLEAR --> FP["fetch_policy\nChromaDB"]
    CLEAR --> FDK["fetch_domain_knowledge\nChromaDB"]
    CLEAR --> FKC["fetch_keycloak_state\nempty snapshot\nafter wipe"]

    FP & FDK & FKC --> PROPOSE["propose_diff\nPlanner LLM -> ProposedDiff\nassign-only state is empty"]

    PROPOSE --> VALIDATE["validate_diff\n1. Existence check\n2. Safety guard rails\n3. Auditor LLM\n4. Scope check"]

    VALIDATE --> APPLY["apply_diff\nassign_client_roles only"]
    APPLY --> FORMAT["format_response"]
    FORMAT --> END(("END"))

    style CLEAR fill:#7f1d1d,color:#fee2e2,stroke:#f87171
    style FP fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style FDK fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style FKC fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style PROPOSE fill:#713f12,color:#fef3c7,stroke:#d97706
    style VALIDATE fill:#713f12,color:#fef3c7,stroke:#d97706
    style APPLY fill:#14532d,color:#dcfce7,stroke:#4ade80
```

**Rebuild differs from Build only by the `clear_assignments` node before the fan-out.**

---

## 4. Users & Roles Sub-agents

Both sub-agents share the same graph shape; only `fetch_keycloak_state` scope differs.

```mermaid
flowchart TD
    START(("START"))

    START --> FP["fetch_policy\nChromaDB"]
    START --> FDK["fetch_domain_knowledge\nChromaDB"]
    START --> FKC["fetch_keycloak_state\n\nUser: that users\ncurrent mappings\nand all clients/roles\n\nRole: all users\nand all roles"]

    FP & FDK & FKC --> PROPOSE["propose_mappings\nPlanner LLM -> ProposedDiff\nscoped to affected\nuser OR role"]

    PROPOSE --> VALIDATE["validate_mappings\n1. Existence check\n2. Safety guard rails\n3. Auditor LLM\n4. Scope check\n   affected entity only"]

    VALIDATE --> APPLY["apply_mappings\nassign_client_roles\nrevoke_client_roles"]
    APPLY --> FORMAT["format_response"]
    FORMAT --> END(("END"))

    style FP fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style FDK fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style FKC fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style PROPOSE fill:#713f12,color:#fef3c7,stroke:#d97706
    style VALIDATE fill:#713f12,color:#fef3c7,stroke:#d97706
    style APPLY fill:#14532d,color:#dcfce7,stroke:#4ade80
```

---

## 5. Shared Module

```mermaid
flowchart TD
    subgraph SHARED["shared - used by all policy-applying sub-agents"]
        FP["fetch_policy\nQuery: aiac-policies collection\nReturns: policy_chunks\nFails: 503 after UPSTREAM_MAX_RETRIES"]
        FDK["fetch_domain_knowledge\nQuery: aiac-domain-knowledge collection\nReturns: domain_knowledge_chunks\nFails: non-fatal"]
    end

    subgraph STATE["BaseAgentState"]
        S1["trigger: TriggerContext"]
        S2["realm: str"]
        S3["policy_chunks: list of str"]
        S4["domain_knowledge_chunks: list of str"]
        S5["keycloak_snapshot: KeycloakSnapshot"]
        S6["proposed_diff: ProposedDiff or None"]
        S7["validation_errors: list of str"]
        S8["applied / revoked: list of RoleAssignment"]
        S9["summary: str"]
    end

    subgraph QUERY_KEYS["ChromaDB query strings by trigger"]
        Q1["build / rebuild -> all access control rules"]
        Q2["user/:id -> user role assignment rules"]
        Q3["role/:id -> role assignment rules"]
        Q4["client/:id -> client access control rules"]
    end

    FP & FDK --> STATE
    QUERY_KEYS --> FP
    QUERY_KEYS --> FDK
```

---

## 6. Validate Node — Common Checks

All `validate_*` nodes execute the same four checks (binary abort on any failure):

```mermaid
flowchart TD
    IN["proposed_diff\n+ keycloak_snapshot"] --> C1

    C1{"1. Existence check\nEvery user_id, client_id,\nrole_id in diff exists\nin keycloak_snapshot"}
    C1 -->|"fail"| ABORT["ABORT\nvalidation_errors populated\napplied and revoked empty"]
    C1 -->|"pass"| C2

    C2{"2. Safety guard rails\ntotal changes\nassign + revoke\n<= MAX_CHANGES_PER_RUN"}
    C2 -->|"fail"| ABORT
    C2 -->|"pass"| C3

    C3{"3. LLM re-confirmation\nAuditor system prompt\n-> ValidationVerdict\napproved bool + reason str"}
    C3 -->|"approved=false"| ABORT
    C3 -->|"approved=true"| C4

    C4{"4. Scope check\nDiff bounded to entities\nreferenced by trigger\nno over-reach"}
    C4 -->|"fail"| ABORT
    C4 -->|"pass"| APPLY["proceed to apply_*"]

    style ABORT fill:#7f1d1d,color:#fee2e2,stroke:#f87171
    style APPLY fill:#14532d,color:#dcfce7,stroke:#4ade80
    style C3 fill:#713f12,color:#fef3c7,stroke:#d97706
```
