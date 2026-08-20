# uc1-onboarding demo — AIAC discovers an agent and a tool, reads a two-line plain-English
# policy, and generates enforceable least-privilege authorization. Strictly live: every target
# talks to a real cluster/Keycloak/LLM, no offline replay and no fixtures.
#
#   make demo   # init (00-03) -> onboard (04-05) -> run (dev/test/devops)
#   make dev    # drive dev-user's intents through the after-tool snapshot
#
# How the demo sources generated Rego (see demo.md "How the demo sources the generated Rego"):
# the reworked PDP Policy Writer is CR-backed — production writes AuthorizationPolicy CRs only, no
# /rego file dump. So `make agent`/`tool` read the agent's Rego straight from its
# `authorizationpolicies.agent.rossoctl.dev/github-agent` CR (spec.policies[].content) into
# generated/<snapshot>/team1/github-agent/{inbound,outbound}/request.rego, and `make clear` deletes
# that CR. No demo-only deployment overlay is needed (we do NOT enable POLICY_WRITER_DUMP_REGO).
#
# Prereqs: a rossoctl/kind cluster with SPIRE + Keycloak + the rossoctl operator (see
# ../../assets/INSTALL.md and ../../../k8s/aiac-deployment-guide.md). KEYCLOAK_URL /
# KEYCLOAK_ADMIN_USERNAME / KEYCLOAK_ADMIN_PASSWORD are discovered automatically by the
# `keycloak` target (init/00-discover-keycloak.sh); no need to export them by hand. To target a
# Keycloak this can't reach that way, export the three vars yourself and they win.

# The discover script is bash (arrays, [[, BASH_SOURCE) — make's default /bin/sh can't source it.
SHELL := /bin/bash

.PHONY: help keycloak prereqs clear setup agent tool show diff dev test devops init onboard run demo

.DEFAULT_GOAL := help

# AIAC_ROOT is fixed relative to this Makefile's location (demo/use-cases/uc1-onboarding/), not
# an override knob — the scripts resolve their own paths the same way.
AIAC_ROOT := ../../..
VENV      := $(AIAC_ROOT)/.venv
PYTHON    ?= $(VENV)/bin/python

# Every Keycloak-touching recipe sources the discovery script first. A make recipe can't export
# env into the caller's shell (nor into the next recipe), so each recipe that needs the Keycloak
# vars self-sources. The script is idempotent — it reuses an existing port-forward — so repeated
# sourcing across a `make demo` chain sets the forward up once and reuses it thereafter.
KC_ENV := . init/00-discover-keycloak.sh &&

help:  ## Show this menu
	@printf "\nuc1-onboarding demo — live AIAC onboarding of a real agent + tool.\n\n"
	@printf "Run:\n  \033[1mmake demo\033[0m   (init 00-03 -> onboard 04-05 -> run)\n"
	@printf "  \033[1mmake init / make onboard / make run\033[0m   (one phase at a time)\n"
	@printf "  \033[1mmake dev / make test / make devops\033[0m   (drive one user's intents)\n\n"
	@printf "Targets:\n"
	@awk 'BEGIN{FS=":.*## "} /^[a-zA-Z_-]+:.*## /{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)
	@printf "\nVars: PYTHON(=%s)\n\n" "$(PYTHON)"

keycloak:  ## Port-forward the in-cluster Keycloak and discover admin creds (runs before prereqs)
	$(KC_ENV) true

prereqs: keycloak  ## Verify (and install if absent) everything the demo needs before touching Keycloak
	$(KC_ENV) $(PYTHON) init/01-prereqs.py

clear:  ## Reset to a clean slate: Keycloak roles/scopes, Policy Store, the agent's AuthorizationPolicy CR, generated/
	$(KC_ENV) $(PYTHON) init/02-clear.py

setup:  ## Provision users/roles, mount policy.md, configure token exchange; prints both UUIDs
	$(KC_ENV) $(PYTHON) init/03-setup.py

agent:  ## POST /apply/service on github-agent; captures generated/01-after-agent/
	$(KC_ENV) $(PYTHON) onboard/04-onboard-agent.py

tool:  ## POST /apply/service on github-tool; captures generated/02-after-tool/
	$(KC_ENV) $(PYTHON) onboard/05-onboard-tool.py

show:  ## Show live Keycloak roles/scopes + the latest generated .rego (or "no policy yet")
	$(KC_ENV) $(PYTHON) show-state.py

diff:  ## Show the latest snapshot, diffed against generated/<PRIOR> (e.g. make diff PRIOR=01-after-agent)
	$(KC_ENV) $(PYTHON) show-state.py --diff $(PRIOR)

dev:  ## Drive dev-user's intents against generated/02-after-tool/
	$(KC_ENV) $(PYTHON) run/run-developer.py

test:  ## Drive test-user's intents against generated/02-after-tool/
	$(KC_ENV) $(PYTHON) run/run-tester.py

devops:  ## Drive devops-user — denied at the inbound gate (the intended story)
	$(KC_ENV) $(PYTHON) run/run-devops.py

# --- grouped phases (each aggregates the numbered steps above) --------------------------------

init: keycloak prereqs clear setup  ## Phase 1 (steps 00-03): discover Keycloak, verify prereqs, reset, provision

onboard: agent tool  ## Phase 2 (steps 04-05): onboard the agent, then the tool

run: dev test devops  ## Phase 3: drive all three users (developer, tester, devops)

demo: init onboard run  ## Full end-to-end: init -> onboard -> run
	@echo
	@echo "Demo complete. Re-drive a single user with: make dev / make test / make devops"
