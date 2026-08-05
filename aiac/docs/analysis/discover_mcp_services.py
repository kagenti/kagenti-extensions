#!/usr/bin/env python3
# Copyright 2025 IBM Corp.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Discover all Kubernetes services explicitly exposed as MCP servers."""

from kubernetes import client, config

# Load config: in-cluster if available, else fall back to kubeconfig
try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

v1 = client.CoreV1Api()

# Find all Kubernetes services explicitly exposed as MCP servers.
# The label value is "" (empty string) — the selector matches on key presence.
mcp_services = v1.list_namespaced_service(
    namespace="team1",
    label_selector="protocol.rossoctl.io/mcp",
)

for svc in mcp_services.items:
    # Build the internal ClusterIP endpoint
    cluster_url = (
        f"http://{svc.metadata.name}.{svc.metadata.namespace}"
        f".svc.cluster.local:{svc.spec.ports[0].port}/mcp"
    )
    print(f"Discovered available tool endpoint: {cluster_url}")
