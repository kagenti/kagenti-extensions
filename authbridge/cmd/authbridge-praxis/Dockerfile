# AuthBridge praxis-sidecar combined image — the authbridge-praxis config
# generator plus a Praxis proxy binary, in a single container.
#
# How this differs from the other AuthBridge images: authbridge-praxis is not a
# proxy. It converts an AuthBridge config into a Praxis config (and, when the
# inbound pipeline declares jwt-validation, a Praxis policy document), and
# Praxis is the data plane. The entrypoint therefore runs the generator to
# completion first, then execs Praxis against the generated config. See
# entrypoint.sh for the ordering guarantees.
#
# The Praxis binary is built with `--features policy-engine`, which is OFF in
# Praxis's default feature set. That feature is what compiles in the `policy`
# filter; without it Praxis rejects the generated config outright with
# "unknown filter type: 'policy'" whenever a policy document was produced.
# Since the generator emits that filter for jwt-validation, the two must be
# built together — a default-feature Praxis in this image would fail to start
# on exactly the configs that need inbound auth.
#
# Build context: ./authbridge (needs access to authlib/, storage/, and
# cmd/authbridge-praxis/)
#
# Praxis source is not vendored in this repo, so the Rust stage fetches it from
# git. PRAXIS_REPO / PRAXIS_REF pin what gets built; PRAXIS_REF defaults to a
# commit rather than a branch so an image rebuild is reproducible. Point
# PRAXIS_REPO at a fork to build that instead.

# ── Stage 1: Build the Praxis proxy binary (with the policy engine) ─────────
#
# Mirrors the recipe in Praxis's own Containerfile: rust:1.96-alpine (matching
# its rust-toolchain.toml) plus the musl / OpenSSL / cmake toolchain its
# dependency tree needs. OPENSSL_STATIC links OpenSSL into the binary so the
# runtime stage needs no OpenSSL package.
FROM rust:1.96-alpine AS praxis-builder

ENV OPENSSL_STATIC=1

RUN apk add --no-cache musl-dev openssl-dev openssl-libs-static pkgconf cmake make g++ git

ARG PRAXIS_REPO=https://github.com/praxis-proxy/praxis.git
# A release tag, not a branch: pinning to `main` would make image rebuilds
# non-reproducible and could silently pick up a schema change the generator in
# stage 2 does not emit for. v0.5.3 carries the policy-engine feature and the
# `policy` filter fields this image's generated configs use.
ARG PRAXIS_REF=v0.5.3

WORKDIR /praxis

# Fetch the pinned ref without cloning full history. Three shapes of
# PRAXIS_REF are accepted, tried cheapest-first: a tag (the default), then a
# bare SHA or branch via a direct refspec fetch, then all branch tips as a last
# resort. The tag path is listed first because fetching an arbitrary commit by
# SHA requires the server to allow it, which GitHub does not always do — so a
# SHA-pinned build can fall through to the later forms.
RUN git init -q . \
    && git remote add origin "${PRAXIS_REPO}" \
    && ( git fetch -q --depth 1 origin "refs/tags/${PRAXIS_REF}:refs/tags/${PRAXIS_REF}" \
         || git fetch -q --depth 1 origin "${PRAXIS_REF}" \
         || git fetch -q --depth 1 origin '+refs/heads/*:refs/remotes/origin/*' ) \
    && ( git checkout -q "${PRAXIS_REF}" || git checkout -q FETCH_HEAD ) \
    && git --no-pager log --oneline -1

# `policy-engine` pulls in the praxis-policy crates (identity/jwt, Cedar PDP,
# OAuth delegator). Build only the server crate; the workspace's test and
# benchmark members are not needed for the binary.
RUN cargo build --release -p praxis-proxy --features policy-engine \
    && cp target/release/praxis /praxis-bin \
    && strip /praxis-bin

# Fail the build here rather than at container start if the feature flag did
# not take effect: a Praxis without the `policy` filter cannot run the configs
# this image generates. A missing config_path proves the filter is registered
# (an unregistered filter reports "unknown filter type" instead).
#
# The probe is EXPECTED to exit non-zero (its config_path is deliberately
# absent), so its output is redirected to a file and inspected rather than
# piped — a pipeline would either mask the status or, under pipefail, abort
# before the grep that actually decides the outcome.
RUN printf 'listeners:\n\
  - name: probe\n\
    address: "127.0.0.1:18099"\n\
    filter_chains: [c]\n\
filter_chains:\n\
  - name: c\n\
    filters:\n\
      - filter: policy\n\
        config_path: /nonexistent/policy.yaml\n\
      - filter: static_response\n\
        status: 200\n' > /probe.yaml \
    && { /praxis-bin --validate --config /probe.yaml > /probe.out 2>&1 || true; } \
    && cat /probe.out \
    && if grep -q "unknown filter type" /probe.out; then \
         echo "FATAL: praxis was built WITHOUT the policy-engine feature" >&2; exit 1; \
       fi \
    && rm -f /probe.yaml /probe.out

# ── Stage 2: Build the authbridge-praxis config generator ───────────────────
#
# `-ldflags="-s -w"` drops the symbol table and DWARF debug info to shave ~30%
# off the binary. Go's runtime keeps `pclntab` separately, so panic stack
# traces still show function names.
FROM golang:1.26-alpine@sha256:0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2 AS go-builder

RUN apk add --no-cache git

WORKDIR /app

COPY authlib/ authlib/
COPY storage/ storage/
COPY cmd/authbridge-praxis/ cmd/authbridge-praxis/

ARG GO_BUILD_TAGS=""
ENV GOWORK=off
RUN cd cmd/authbridge-praxis && CGO_ENABLED=0 GOOS=linux go build -tags "${GO_BUILD_TAGS}" -ldflags="-s -w" -o /authbridge-praxis .

# ── Stage 3: Runtime ───────────────────────────────────────────────────────
#
# alpine (has bash for the entrypoint, ~5 MB base). authbridge-praxis is
# CGO_ENABLED=0 static; the Praxis binary is musl-linked against the same libc
# family as this base, with OpenSSL statically linked in.
FROM alpine:3.20@sha256:beefdbd8a1da6d2915566fde36db9db0b524eb737fc57cd1367effd16dc0d06d

RUN apk add --no-cache bash ca-certificates

COPY --from=go-builder --chmod=755 /authbridge-praxis /usr/local/bin/authbridge-praxis
COPY --from=praxis-builder --chmod=755 /praxis-bin /usr/local/bin/praxis
COPY --chmod=755 cmd/authbridge-praxis/entrypoint.sh /usr/local/bin/entrypoint.sh

# The generator writes into /tmp and Praxis reads from there, so the runtime
# user needs a writable /tmp. Alpine ships it 1777, which covers an arbitrary
# UID; created explicitly so the contract is visible rather than inherited.
RUN mkdir -p /tmp && chmod 1777 /tmp

USER 1001

# 8080 reverse proxy / 8081 forward proxy — the listener addresses the
# generator derives from the AuthBridge config. 9091 is the Praxis admin
# endpoint (/healthy, /ready, /metrics); the generator binds it on
# AuthBridge's health port so an existing /readyz probe keeps working against
# Praxis's /ready.
EXPOSE 8080 8081 9091

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
