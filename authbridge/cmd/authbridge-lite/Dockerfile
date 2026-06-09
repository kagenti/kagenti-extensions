# AuthBridge proxy-sidecar lite combined image — authbridge-lite
# (jwt-validation + token-exchange only, no parsers) in a single
# container.
#
# Same listener layout as the full authbridge-proxy image, but with
# the a2a / mcp / inference parser plugins dropped to optimize binary
# size for deployments that don't need protocol-aware session events
# in abctl. Functionally identical for inbound JWT validation +
# outbound token exchange.
#
# SPIFFE credentials are obtained in-process via go-spiffe's
# WorkloadAPI client (no spiffe-helper sidecar binary).
#
# Build context: ./authbridge (needs access to authlib/ and cmd/authbridge-lite/)

# Stage 1: Build authbridge-lite binary.
# `-ldflags="-s -w"` drops the symbol table and DWARF debug info to
# shave ~30% off the binary. Go's runtime keeps `pclntab` separately,
# so panic stack traces still show function names.
FROM golang:1.25-alpine@sha256:a2ea69f8f07fe886989c61e9da297f932ddf50b42dad2f44d2664710c94dca37 AS builder

RUN apk add --no-cache git

WORKDIR /app

COPY authlib/ authlib/
COPY cmd/authbridge-lite/ cmd/authbridge-lite/

ENV GOWORK=off
RUN cd cmd/authbridge-lite && CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /authbridge-lite .

# Stage 2: Runtime — alpine (has bash, ~5 MB base)
FROM alpine:3.20@sha256:beefdbd8a1da6d2915566fde36db9db0b524eb737fc57cd1367effd16dc0d06d

RUN apk add --no-cache bash ca-certificates

COPY --from=builder --chmod=755 /authbridge-lite /usr/local/bin/authbridge-lite
COPY --chmod=755 cmd/authbridge-lite/entrypoint.sh /usr/local/bin/entrypoint.sh

USER 1001

EXPOSE 8080 8081 9091 9093 9094

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
