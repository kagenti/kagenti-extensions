// Package reverseproxy implements an HTTP reverse proxy listener.
// Inbound requests are validated via the inbound pipeline before being
// forwarded to a fixed backend.
package reverseproxy

import (
	"bytes"
	"context"
	cryptotls "crypto/tls"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"

	"github.com/kagenti/kagenti-extensions/authbridge/authlib/listener/httpx"
	"github.com/kagenti/kagenti-extensions/authbridge/authlib/listener/internal/tlssniff"
	"github.com/kagenti/kagenti-extensions/authbridge/authlib/pipeline"
	"github.com/kagenti/kagenti-extensions/authbridge/authlib/session"
	"github.com/kagenti/kagenti-extensions/authbridge/authlib/spiffe"
	authtls "github.com/kagenti/kagenti-extensions/authbridge/authlib/tls"
)

const maxBodySize = 1 << 20 // 1MB — matches Envoy's default per_stream_buffer_limit_bytes

type pctxKey struct{}

// cancelImmuneTransport wraps an http.RoundTripper and replaces each request's
// context with context.Background() before forwarding. This prevents client
// disconnections from cancelling the outgoing backend connection — critical for
// SSE streaming where the backend keeps sending events after the client drops.
type cancelImmuneTransport struct {
	wrapped http.RoundTripper
}

func (t *cancelImmuneTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	// Detach from the inbound request context so client disconnections don't
	// cancel the backend connection. Preserve pctxKey so modifyResponse can
	// still access the pipeline context for SSE capture and response plugins.
	bgCtx := context.WithValue(context.Background(), pctxKey{}, req.Context().Value(pctxKey{}))
	return t.wrapped.RoundTrip(req.WithContext(bgCtx))
}

// responseRejectedError carries a pipeline Reject from the roundTripper
// back to the error handler, where it's rendered into the
// http.ResponseWriter. The embedded action keeps Violation.Render() and
// helper constructors available at the render site.
type responseRejectedError struct {
	action pipeline.Action
}

func (e *responseRejectedError) Error() string {
	if e.action.Violation != nil {
		return e.action.Violation.Reason
	}
	return "response rejected"
}

// Server is an HTTP reverse proxy with inbound JWT validation.
//
// InboundPipeline is a holder so the bound pipeline can be hot-swapped
// under the running listener; each handleRequest Loads through it so
// in-flight requests finish on the pipeline they started with.
type Server struct {
	InboundPipeline *pipeline.Holder
	Sessions        *session.Store // nil when session tracking is disabled
	proxy           *httputil.ReverseProxy
	backend         string

	// mtlsCfg is the *tls.Config wrapping the local SVID for inbound
	// mTLS, or nil when mTLS is disabled. mtlsMode is consulted by
	// the byte-peek listener (Listen) to decide whether non-TLS
	// connections are passed through (permissive) or closed (strict).
	mtlsCfg     *cryptotls.Config
	mtlsMode    tlssniff.Mode
	mtlsMetrics *authtls.Metrics
}

// MTLSOptions configures inbound mTLS. Pass nil (or a zero-value
// MTLSOptions with Source nil) to construct a server with TLS off.
type MTLSOptions struct {
	// Source supplies the local SVID + trust bundle. Required when
	// MTLSOptions is non-nil; the constructor errors otherwise.
	Source spiffe.X509Source

	// Strict: when true, the listener rejects non-TLS callers. When
	// false (default), it accepts both TLS and plaintext on the same
	// port via byte-peek detection.
	Strict bool

	// Metrics, when non-nil, receives counter increments on TLS
	// accept / plaintext-accept / plaintext-reject paths. The caller
	// owns the *Metrics and exposes its Snapshot via /stats.
	Metrics *authtls.Metrics
}

// NewServer creates a reverse proxy that forwards to the given backend URL.
// When mtls is non-nil, the listener returned by Listen wraps the inbound
// connection in TLS sniffing using the provided X.509 source.
func NewServer(inbound *pipeline.Holder, sessions *session.Store, backendURL string, mtls *MTLSOptions) (*Server, error) {
	target, err := url.Parse(backendURL)
	if err != nil {
		return nil, err
	}
	proxy := httputil.NewSingleHostReverseProxy(target)
	// Use a transport that strips the inbound request context before forwarding.
	// Without this, when the client disconnects (e.g. ztunnel 30s lifetime cut),
	// Go's HTTP transport cancels the outgoing backend connection, making the
	// backend response body unreadable in drainAsync. With context.Background(),
	// the backend connection stays alive and drainAsync can capture the final
	// SSE event even after the client has gone.
	proxy.Transport = &cancelImmuneTransport{wrapped: http.DefaultTransport}
	s := &Server{
		InboundPipeline: inbound,
		Sessions:        sessions,
		proxy:           proxy,
		backend:         backendURL,
	}
	if mtls != nil {
		if mtls.Source == nil {
			return nil, fmt.Errorf("reverseproxy: MTLSOptions.Source is required when mtls is non-nil")
		}
		tlsCfg, err := authtls.ServerConfig(mtls.Source)
		if err != nil {
			return nil, fmt.Errorf("reverseproxy: build server tls config: %w", err)
		}
		s.mtlsCfg = tlsCfg
		s.mtlsMode = tlssniff.ModePermissive
		if mtls.Strict {
			s.mtlsMode = tlssniff.ModeStrict
		}
		s.mtlsMetrics = mtls.Metrics
	}
	proxy.ModifyResponse = s.modifyResponse
	proxy.ErrorHandler = s.errorHandler
	return s, nil
}

// Listen returns a net.Listener bound to addr. When mTLS is configured
// the listener is a tlssniff.Listener that dispatches TLS handshakes
// through the local SVID and pass-throughs plain HTTP per the
// configured mode (permissive / strict). When mTLS is disabled the
// returned listener is a plain net.Listen("tcp", addr).
//
// Callers pass the result to http.Server.Serve.
func (s *Server) Listen(addr string) (net.Listener, error) {
	inner, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, err
	}
	if s.mtlsCfg == nil {
		return inner, nil
	}
	sniff := tlssniff.New(inner, s.mtlsCfg, s.mtlsMode)
	if s.mtlsMetrics != nil {
		sniff.SetOnPlainRejected(func(_ net.Conn) {
			s.mtlsMetrics.InboundPlainRejected.Add(1)
		})
	}
	return sniff, nil
}

// MTLSEnabled reports whether the listener is wrapping connections
// in TLS-sniffing. Used by the bin's startup-log path to surface a
// clear message about the listener mode.
func (s *Server) MTLSEnabled() bool { return s.mtlsCfg != nil }

// eventTLS builds a *pipeline.EventTLS from the pctx's connection
// state, extracting the peer SPIFFE ID via authlib/tls. Returns nil
// for plaintext or absent TLS state — sites that pass the result
// through to a SessionEvent get the right thing for any caller.
func eventTLS(pctx *pipeline.Context) *pipeline.EventTLS {
	if pctx == nil || pctx.TLS == nil {
		return nil
	}
	return pipeline.NewEventTLS(pctx.TLS, authtls.PeerSPIFFEID(pctx.PeerCertificate()))
}

// Handler returns the HTTP handler for the reverse proxy.
func (s *Server) Handler() http.Handler {
	return http.HandlerFunc(s.handleRequest)
}

func (s *Server) handleRequest(w http.ResponseWriter, r *http.Request) {
	pctx := &pipeline.Context{
		Direction:  pipeline.Inbound,
		Scheme:     requestScheme(r),
		Host:       r.Host,
		Path:       r.URL.Path,
		RemoteAddr: r.RemoteAddr,
		Headers:    r.Header.Clone(),
		StartedAt:  time.Now(),
	}

	// Surface connection-level identity to plugins that opt in. r.TLS is
	// non-nil only when the connection went through TLS — for plain HTTP
	// callers (UI, healthchecks), pctx.TLS stays nil and any plugin
	// reading it sees the absence cleanly.
	if r.TLS != nil {
		pctx.TLS = r.TLS
		if s.mtlsMetrics != nil && len(r.TLS.PeerCertificates) > 0 {
			s.mtlsMetrics.InboundTLSAccepted.Add(1)
		}
	} else if s.mtlsMetrics != nil {
		s.mtlsMetrics.InboundPlainAccepted.Add(1)
	}

	// Finisher dispatch runs after every exit path from this handler —
	// allowed requests, plugin denials, upstream errors, and even panics
	// (e.g. http.ErrAbortHandler when the client disconnects mid-stream).
	// SSE capture is applied here so it runs even when proxy.ServeHTTP
	// panics (broken pipe) and the code after ServeHTTP is never reached.
	defer func() {
		if pctx != nil && pctx.Extensions.Custom != nil {
			if cap, ok := pctx.Extensions.Custom["sse.capture"].(*sseFinalCapture); ok && cap != nil {
				// If the final event wasn't captured during normal proxy flow
				// (e.g. ztunnel cut the client connection before it arrived),
				// drain the still-open backend body to find it.
				if !cap.done {
					drainDone := cap.drainAsync()
					select {
					case <-drainDone:
					case <-time.After(15 * time.Second):
						// KNOWN ISSUE (flagged in the pre-push audit, 2026-08-02):
						// the drain goroutine is still running here — RealClose
						// below closes the reader under it and applyToContext
						// reads shared capture state without synchronization.
						// Logged loudly so the condition is at least visible;
						// the redesign is deferred for review with the original
						// author (see the lineage PR's known-issues section).
						slog.Warn("reverse-proxy: SSE drain timed out after 15s; final event may be lost or torn",
							"host", r.Host, "path", r.URL.Path)
					}
				}
				// Close the intercepted backend body now that we're done.
				cap.src.RealClose() //nolint:errcheck
				cap.applyToContext()
				if len(pctx.ResponseBody) > 0 {
					s.InboundPipeline.RunResponse(context.Background(), pctx) //nolint:errcheck
				}
			}
		}
		s.InboundPipeline.RunFinish(r.Context(), pctx, pipeline.OutcomeFromContext(pctx))
	}()

	if s.InboundPipeline.NeedsBody() && r.Body != nil {
		r.Body = http.MaxBytesReader(w, r.Body, maxBodySize)
		body, err := io.ReadAll(r.Body)
		if err != nil {
			slog.Warn("reverse-proxy: request body too large or unreadable", "host", r.Host, "error", err)
			http.Error(w, `{"error":"request body too large"}`, http.StatusRequestEntityTooLarge)
			return
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
		pctx.Body = body
		slog.Debug("reverse-proxy: buffered request body", "host", r.Host, "bodyLen", len(body))
	}

	action := s.InboundPipeline.Run(r.Context(), pctx)
	if action.Type == pipeline.Reject {
		s.recordInboundReject(pctx, action)
		httpx.WriteRejection(w, action)
		return
	}

	// If a WritesBody plugin rewrote pctx.Body, send the new bytes to
	// the backend and clear Content-Encoding (same rationale as the
	// response path — plugin may have decompressed).
	if pctx.BodyMutated() {
		r.Body = io.NopCloser(bytes.NewReader(pctx.Body))
		r.ContentLength = int64(len(pctx.Body))
		r.Header.Set("Content-Length", fmt.Sprintf("%d", len(pctx.Body)))
		r.Header.Del("Content-Encoding")
	}

	// Propagate W3C trace context injected by the lineage plugin into the
	// forwarded request. Plugins write to pctx.Headers (a clone of r.Header);
	// r.Header is what the reverse proxy actually forwards, so we sync the
	// two trace headers back here.
	for _, hdr := range []string{"traceparent", "tracestate"} {
		if v := pctx.Headers.Get(hdr); v != "" {
			r.Header.Set(hdr, v)
		}
	}

	// Inbound recording is gated on A2A by design: reverseproxy is the
	// A2A-only listener (its session keying and rekey logic are A2A-specific
	// — see modifyResponse). Forwardproxy widens the analogous gate to
	// cover MCP/Inference/Invocations/plugins because outbound traffic is
	// not A2A-only. A non-A2A inbound, or an A2A request that fails to
	// parse, is intentionally not recorded here.
	if s.Sessions != nil && pctx.Extensions.A2A != nil {
		sid := pctx.Extensions.A2A.SessionID
		if sid == "" {
			sid = s.Sessions.ActiveSession()
		}
		if sid == "" {
			sid = session.DefaultSessionID
		}
		// Snapshot-copy the protocol extension and use the shared helpers
		// for plugin invocations / observability / identity. Mirrors what
		// extproc does so request events don't pick up response-phase
		// mutations on the same pctx.Extensions.A2A struct.
		s.Sessions.Append(sid, pipeline.SessionEvent{
			At:          time.Now(),
			Direction:   pipeline.Inbound,
			Phase:       pipeline.SessionRequest,
			A2A:         pipeline.SnapshotA2A(pctx.Extensions.A2A),
			Invocations: pipeline.SnapshotInvocations(pctx.Extensions.Invocations, pipeline.InvocationPhaseRequest),
			Plugins:     pipeline.SnapshotPlugins(pctx.Extensions.Custom),
			Identity:    pipeline.SnapshotIdentity(pctx),
			Host:        pctx.Host,
			TLS:         eventTLS(pctx),
		})
	}

	r = r.WithContext(context.WithValue(r.Context(), pctxKey{}, pctx))
	s.proxy.ServeHTTP(w, r)
}

// closeInterceptor wraps the backend response body so that proxy's
// resp.Body.Close() call becomes a no-op. The actual close is deferred
// until after drainAsync finishes reading the remaining backend data.
type closeInterceptor struct {
	rc        io.ReadCloser
	proxDone  bool // set to true when the proxy calls Close()
}

func (ci *closeInterceptor) Read(p []byte) (int, error) { return ci.rc.Read(p) }
func (ci *closeInterceptor) Close() error               { ci.proxDone = true; return nil }

// RealClose closes the underlying reader.
func (ci *closeInterceptor) RealClose() error { return ci.rc.Close() }

// sseFinalCapture wraps the backend response body (agent → proxy).
// As data flows from the agent, it scans for "data: " lines and captures
// the last one. When it sees an event with "final":true or "input-required"
// it stores the event and returns io.EOF on the next Read so that io.Copy
// exits promptly — preventing the proxy from blocking forever on an open
// SSE stream.
type sseFinalCapture struct {
	src       *closeInterceptor
	pctx      *pipeline.Context
	pending   []byte
	lastEvent []byte
	done      bool
}

func (c *sseFinalCapture) Read(p []byte) (int, error) {
	if c.done {
		return 0, io.EOF
	}
	n, err := c.src.Read(p)
	if n > 0 {
		c.pending = append(c.pending, p[:n]...)
		for {
			idx := bytes.IndexByte(c.pending, '\n')
			if idx < 0 {
				break
			}
			line := bytes.TrimSpace(c.pending[:idx])
			c.pending = c.pending[idx+1:]
			if bytes.HasPrefix(line, []byte("data: ")) {
				data := bytes.TrimPrefix(line, []byte("data: "))
				if len(data) > 0 {
					c.lastEvent = data
					// Signal EOF when we see a terminal event so io.Copy
					// exits and proxy.ServeHTTP returns promptly.
					if bytes.Contains(data, []byte(`"final":true`)) ||
						bytes.Contains(data, []byte(`"input-required"`)) {
						c.done = true
					}
				}
			}
		}
	}
	if c.done && err == nil {
		// Deliver the data we just read, then return EOF on next call.
		return n, nil
	}
	return n, err
}

func (c *sseFinalCapture) Close() error { return c.src.Close() }

// drainAsync starts a goroutine that reads from the intercepted backend body
// (which was not closed when the proxy exited) until it finds a terminal
// "data:" line ("final":true or "input-required") or the backend closes.
// Returns a channel that is closed when the goroutine exits.
func (c *sseFinalCapture) drainAsync() <-chan struct{} {
	done := make(chan struct{})
	go func() {
		defer close(done)
		buf := make([]byte, 4096)
		for {
			n, err := c.src.rc.Read(buf)
			if n > 0 {
				c.pending = append(c.pending, buf[:n]...)
				for {
					idx := bytes.IndexByte(c.pending, '\n')
					if idx < 0 {
						break
					}
					line := bytes.TrimSpace(c.pending[:idx])
					c.pending = c.pending[idx+1:]
					if bytes.HasPrefix(line, []byte("data: ")) {
						data := bytes.TrimPrefix(line, []byte("data: "))
						if len(data) > 0 {
							c.lastEvent = data
							if bytes.Contains(data, []byte(`"final":true`)) ||
								bytes.Contains(data, []byte(`"input-required"`)) {
								c.done = true
								return
							}
						}
					}
				}
			}
			if err != nil {
				return
			}
		}
	}()
	return done
}

func (c *sseFinalCapture) applyToContext() {
	if c.pctx == nil || len(c.pctx.ResponseBody) > 0 {
		return
	}
	if len(c.lastEvent) > 0 {
		c.pctx.ResponseBody = c.lastEvent
	}
}

func (s *Server) modifyResponse(resp *http.Response) error {
	pctx, _ := resp.Request.Context().Value(pctxKey{}).(*pipeline.Context)
	if pctx == nil {
		return nil
	}

	pctx.StatusCode = resp.StatusCode
	pctx.ResponseHeaders = resp.Header.Clone()

	// SSE (text/event-stream) responses must not be buffered: reading the
	// full body would block until the stream closes, preventing the client
	// from receiving any events. Instead, wrap the backend body with
	// sseFinalCapture which scans events as they flow, captures the last
	// one, and signals EOF when it sees "final":true so proxy.ServeHTTP
	// returns promptly and RunFinish can emit output.value.
	isSSE := strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream")
	if isSSE && resp.Body != nil {
		cap := &sseFinalCapture{src: &closeInterceptor{rc: resp.Body}, pctx: pctx}
		resp.Body = cap
		// Store the capture in pctx.Extensions.Custom so handleRequest
		// can retrieve it after proxy.ServeHTTP returns.
		if pctx.Extensions.Custom == nil {
			pctx.Extensions.Custom = map[string]any{}
		}
		pctx.Extensions.Custom["sse.capture"] = cap
	}
	if s.InboundPipeline.NeedsBody() && resp.Body != nil && !isSSE {
		body, err := io.ReadAll(io.LimitReader(resp.Body, maxBodySize+1))
		if err != nil {
			return err
		}
		resp.Body.Close()
		if len(body) > maxBodySize {
			return fmt.Errorf("response body too large (%d bytes)", len(body))
		}
		pctx.ResponseBody = body
		resp.Body = io.NopCloser(bytes.NewReader(body))
	}

	action := s.InboundPipeline.RunResponse(resp.Request.Context(), pctx)
	if action.Type == pipeline.Reject {
		return &responseRejectedError{action: action}
	}

	// A plugin that called pctx.SetResponseBody flipped the mutation flag.
	// Use the replaced bytes and rewrite Content-Length so the downstream
	// client gets a consistent response. Content-Encoding is cleared —
	// see the same comment in forwardproxy for the rationale.
	if pctx.ResponseBodyMutated() {
		resp.Body = io.NopCloser(bytes.NewReader(pctx.ResponseBody))
		resp.ContentLength = int64(len(pctx.ResponseBody))
		resp.Header.Set("Content-Length", fmt.Sprintf("%d", len(pctx.ResponseBody)))
		resp.Header.Del("Content-Encoding")
	}

	// Rekey the default bucket → A2A contextId when the response
	// reveals one. The first turn of an A2A conversation arrives
	// without a contextId (the agent assigns it on response), so the
	// inbound request + any outbound MCP/inference calls during
	// processing land in `default`. Without rekey those events stay
	// orphaned while only the response goes to the contextId bucket.
	// Mirrors extproc.rekeyInboundSession.
	//
	// Skip when SessionID is empty (auth-only or non-A2A response —
	// no contextId to merge against) or already "default" (a no-op
	// that would also collide with the source bucket name).
	if s.Sessions != nil && pctx.Extensions.A2A != nil &&
		pctx.Extensions.A2A.SessionID != "" &&
		pctx.Extensions.A2A.SessionID != session.DefaultSessionID {
		s.Sessions.Rekey(session.DefaultSessionID, pctx.Extensions.A2A.SessionID)
	}

	// Mirror forwardproxy's response-phase event so abctl pairs every
	// inbound request with a response row. Without this, A2A
	// `message/stream` requests show up as orphan request events.
	// SSE responses still get recorded — the body is whatever the
	// pipeline saw at this point (may be empty for streamed bodies),
	// but the status code and plugin invocations are always meaningful.
	if s.Sessions != nil && pctx.Extensions.A2A != nil {
		sid := pctx.Extensions.A2A.SessionID
		if sid == "" {
			sid = s.Sessions.ActiveSession()
		}
		if sid == "" {
			sid = session.DefaultSessionID
		}
		s.Sessions.Append(sid, pipeline.SessionEvent{
			At:          time.Now(),
			Direction:   pipeline.Inbound,
			Phase:       pipeline.SessionResponse,
			A2A:         pipeline.SnapshotA2A(pctx.Extensions.A2A),
			Invocations: pipeline.SnapshotInvocations(pctx.Extensions.Invocations, pipeline.InvocationPhaseResponse),
			Plugins:     pipeline.SnapshotPlugins(pctx.Extensions.Custom),
			Identity:    pipeline.SnapshotIdentity(pctx),
			Host:        pctx.Host,
			StatusCode:  resp.StatusCode,
			Error:       pipeline.DeriveError(pctx),
			Duration:    pipeline.DurationSince(pctx.StartedAt),
			TLS:         eventTLS(pctx),
		})
	}
	return nil
}

func (s *Server) errorHandler(w http.ResponseWriter, _ *http.Request, err error) {
	if rErr, ok := err.(*responseRejectedError); ok {
		httpx.WriteRejection(w, rErr.action)
		return
	}
	http.Error(w, `{"error":"bad gateway"}`, http.StatusBadGateway)
}

// recordInboundReject emits a SessionDenied event for inbound requests
// a pipeline plugin rejected. Lets gate plugins (jwt-validation and
// future inbound guardrails) show operators what was blocked and why
// via /v1/sessions and abctl, instead of the block appearing only as
// a 401/403 on the caller side.
//
// Skips when no Invocations were appended — the deny came from a
// plugin that didn't contribute diagnostic context, and a content-free
// SessionDenied event would be noise without attribution.
func (s *Server) recordInboundReject(pctx *pipeline.Context, action pipeline.Action) {
	if s.Sessions == nil || pctx.Extensions.Invocations == nil {
		return
	}
	// Inbound uses the A2A-stated contextId when available; otherwise
	// falls through to the default bucket. Matches the accept path's
	// bucketing rule (A2A request event at line 112-125).
	sid := ""
	if pctx.Extensions.A2A != nil {
		sid = pctx.Extensions.A2A.SessionID
	}
	if sid == "" {
		sid = s.Sessions.ActiveSession()
	}
	if sid == "" {
		sid = session.DefaultSessionID
	}
	var status int
	var code, message string
	if action.Violation != nil {
		status = action.Violation.Status
		if status == 0 {
			status = pipeline.StatusFromCode(action.Violation.Code)
		}
		code = action.Violation.Code
		message = action.Violation.Reason
	}
	ev := pipeline.SessionEvent{
		At:          time.Now(),
		Direction:   pipeline.Inbound,
		Phase:       pipeline.SessionDenied,
		Invocations: pipeline.SnapshotInvocations(pctx.Extensions.Invocations, pipeline.InvocationPhaseRequest),
		Host:        pctx.Host,
		StatusCode:  status,
		Error: &pipeline.EventError{
			Kind:    "policy",
			Code:    code,
			Message: message,
		},
		TLS: eventTLS(pctx),
	}
	s.Sessions.Append(sid, ev)
}

// requestScheme derives the URL scheme for an incoming server-side
// request. On server requests Go does not populate r.URL.Scheme (it's
// only set for client-side / proxy requests where the full URL is on
// the request line), so we read it from r.TLS instead: TLS present =>
// https, absent => http.
//
// Contract note: this listener intentionally diverges from the
// Context.Scheme godoc's "empty when undetermined" convention — it
// always returns "http" or "https" based on r.TLS. The fallback is
// confidently wrong when reverseproxy sits behind a TLS-terminating
// upstream (LB, ingress): r.TLS is nil on the inner hop even though
// the caller's actual scheme was https. Consumers that need the
// caller's scheme in that topology should plumb X-Forwarded-Proto
// once a trusted-upstream policy exists (not in this PR).
//
// Does not consult X-Forwarded-Proto. Honoring that header is only
// safe when the upstream proxy is trusted; wiring a trust policy is
// deferred until we have a concrete multi-hop deployment story.
func requestScheme(r *http.Request) string {
	if r.TLS != nil {
		return "https"
	}
	return "http"
}
