# uc1-onboarding demo — AIAC discovers an agent and a tool, reads a two-line plain-English
# policy, and generates enforceable least-privilege authorization. Strictly live: every target
# talks to a real cluster/Keycloak/LLM, no offline replay and no fixtures.
#
#   make demo   # prereqs -> clear -> setup -> onboard-agent -> show -> onboard-tool -> show
#   make dev    # drive dev-user's intents through the after-tool snapshot
#
# Prereqs: a rossoctl/kind cluster with SPIRE + Keycloak + the rossoctl operator (see
# ../../assets/INSTALL.md and ../../../k8s/aiac-deployment-guide.md), and KEYCLOAK_URL /
# KEYCLOAK_ADMIN_USERNAME / KEYCLOAK_ADMIN_PASSWORD in the environment.

.PHONY: help prereqs clear setup onboard-agent onboard-tool show diff dev test devops demo

.DEFAULT_GOAL := help

# AIAC_ROOT is fixed relative to this Makefile's location (demo/use-cases/uc1-onboarding/), not
# an override knob — the scripts resolve their own paths the same way.
AIAC_ROOT := ../../..
VENV      := $(AIAC_ROOT)/.venv
PYTHON    ?= $(VENV)/bin/python

help:  ## Show this menu
	@printf "\nuc1-onboarding demo — live AIAC onboarding of a real agent + tool.\n\n"
	@printf "Run:\n  \033[1mmake demo\033[0m   (prereqs -> clear -> setup -> onboard-agent -> show -> onboard-tool -> show)\n"
	@printf "  \033[1mmake dev / make test / make devops\033[0m   (drive one user's intents)\n\n"
	@printf "Targets:\n"
	@awk 'BEGIN{FS=":.*## "} /^[a-zA-Z_-]+:.*## /{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)
	@printf "\nVars: PYTHON(=%s)\n\n" "$(PYTHON)"

prereqs:  ## Verify (and install if absent) everything the demo needs before touching Keycloak
	$(PYTHON) init/00-prereqs.py

clear:  ## Reset to a clean slate: Keycloak roles/scopes, Policy Store, writer's /rego, generated/
	$(PYTHON) init/01-clear.py

setup:  ## Provision users/roles, mount policy.md, configure token exchange; prints both UUIDs
	$(PYTHON) init/02-setup.py

onboard-agent:  ## POST /apply/service on github-agent; captures generated/01-after-agent/
	$(PYTHON) onboard/03-onboard-agent.py

onboard-tool:  ## POST /apply/service on github-tool; captures generated/02-after-tool/
	$(PYTHON) onboard/04-onboard-tool.py

show:  ## Show live Keycloak roles/scopes + the latest generated .rego (or "no policy yet")
	$(PYTHON) show-state.py

diff:  ## Show the latest snapshot, diffed against generated/<PRIOR> (e.g. make diff PRIOR=01-after-agent)
	$(PYTHON) show-state.py --diff $(PRIOR)

dev:  ## Drive dev-user's intents against generated/02-after-tool/
	$(PYTHON) run/run-developer.py

test:  ## Drive test-user's intents against generated/02-after-tool/
	$(PYTHON) run/run-tester.py

devops:  ## Drive devops-user — denied at the inbound gate (the intended story)
	$(PYTHON) run/run-devops.py

demo: prereqs clear setup onboard-agent show onboard-tool show  ## Full end-to-end: provision through both onboardings
	@echo
	@echo "Onboarding complete. Now: make dev / make test / make devops"
