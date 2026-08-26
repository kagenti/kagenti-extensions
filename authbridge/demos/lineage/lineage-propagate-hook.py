"""Gated activation hook for the propagate-only OTel shim.

Dockerfile.otel-shim installs this file into the app environment's
site-packages as ``_lineage_propagate.py`` next to a one-line ``.pth`` file
(``import _lineage_propagate``), so Python's ``site`` machinery imports it at
interpreter startup — before any app code — for every process of that
environment. That makes the shim attach through the ENVIRONMENT, the same way
the Java agent (``JAVA_TOOL_OPTIONS``) and Node (``NODE_OPTIONS``) attach:
the container's own ENTRYPOINT/CMD is never rewritten, and no human ever
transcribes an app command.

The hook is inert unless ``LINEAGE_PROPAGATE=1`` is present in the
environment, so the baked ``-otel`` image behaves byte-for-byte like its base
image until a Deployment (attach-lineage.sh) or the image itself
(``SELF_ACTIVATE=1`` bake) switches it on.

When active it does exactly two things:

1. Pins the shim's propagate-only identity as environment DEFAULTS: all
   three signal exporters ``none`` (nothing is ever exported; only the W3C
   ``traceparent`` header flows through the app to the sidecar) and the
   ``tracecontext,baggage`` propagators. ``setdefault`` keeps a deliberate
   operator override possible while making the safe posture the built-in one.

2. Runs stock OpenTelemetry auto-instrumentation via the same
   ``initialize()`` the ``opentelemetry-instrument`` launcher and the OTel
   Kubernetes operator use. Which instrumentors activate is decided by what
   the app actually imports, exactly as before.

Failure policy: this hook must never take the app down. ``initialize()``
already swallows its own exceptions; the guard below covers everything else
(and a broken ``.pth`` line is itself non-fatal — ``site`` prints the error
and continues). A hook failure means propagation is OFF and the trace
fragments at this pod, visibly (``lineage.parent.source=wire``) — absent
lineage, never wrong lineage.
"""

import os

if os.environ.get("LINEAGE_PROPAGATE") == "1":
    os.environ.setdefault("OTEL_TRACES_EXPORTER", "none")
    os.environ.setdefault("OTEL_METRICS_EXPORTER", "none")
    os.environ.setdefault("OTEL_LOGS_EXPORTER", "none")
    os.environ.setdefault("OTEL_PROPAGATORS", "tracecontext,baggage")
    try:
        from opentelemetry.instrumentation.auto_instrumentation import initialize

        initialize()
    # Deliberately broad: never break the app this hook rides in.
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception(
            "lineage propagate hook failed to initialize; propagation is OFF for this process"
        )
