package praxis

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/url"
	"strings"

	"github.com/rossoctl/cortex/authbridge/authlib/config"
)

// This file generates the Praxis *policy document* — the second file the
// `policy` filter needs, referenced by its `config_path`. It is a separate
// document from the proxy config: the proxy config declares the filter chain,
// and the policy document declares the identity plugins and routes the policy
// engine enforces.
//
// The `policy` filter requires Praxis to be built with the `policy-engine`
// cargo feature:
//
//	cargo run --features policy-engine -p praxis-proxy -- -c /tmp/praxis-config.yaml
//
// A default-feature build rejects the filter with "unknown filter type:
// 'policy'", so [Convert] only emits it when a policy document is actually
// being generated alongside — see [Options.PolicyPath].

// Policy plugin wiring constants. These strings are part of the policy
// engine's public API: `identity/jwt` selects the JWT identity resolver, and
// `identity.resolve` is the hook it runs on.
const (
	policyPluginKindJWT    = "identity/jwt"
	policyHookIdentity     = "identity.resolve"
	policyModeSequential   = "sequential"
	policyOnErrorFail      = "fail"
	policyClaimMapperStd   = "standard"
	policyDefaultLeewaySec = 60

	// policyJWTPluginPriority orders the identity plugin within its mode
	// band; lower runs first. Identity resolution must precede anything that
	// reads the resolved identity, so it takes a low number.
	policyJWTPluginPriority = 10

	// policyJWKSRefreshSecs matches the policy engine's own default (10
	// minutes): high enough not to hammer the IdP, low enough that a routine
	// key rotation propagates within a change window. Emitted explicitly so
	// the generated document states its refresh cadence rather than relying
	// on an upstream default that could change.
	policyJWKSRefreshSecs = 600
)

// PolicyDocument is the top-level Praxis policy document (the policy engine's
// "unified config"). Only the fields this converter populates are modeled.
//
// Routing is deliberately left off: with `plugin_settings.routing_enabled`
// absent (false), the engine resolves identity on the request phase and denies
// on a missing or invalid JWT, which is exactly AuthBridge's jwt-validation
// semantics. Turning routing on would shift enforcement to per-entity routes
// and require protocol-classifier metadata, changing behavior rather than
// translating it.
type PolicyDocument struct {
	Plugins []PolicyPlugin `yaml:"plugins"`
}

// PolicyPlugin is one plugin declaration in the policy document.
type PolicyPlugin struct {
	Name        string `yaml:"name"`
	Kind        string `yaml:"kind"`
	Description string `yaml:"description,omitempty"`
	// Hooks are the hook names this plugin handles, e.g. identity.resolve.
	Hooks []string `yaml:"hooks"`
	// Mode is the execution band: sequential can both block and modify,
	// which is what a deny-capable identity gate needs.
	Mode string `yaml:"mode,omitempty"`
	// Priority orders plugins within a mode band; lower runs first.
	Priority int `yaml:"priority,omitempty"`
	// OnError is the failure posture. "fail" halts the pipeline and
	// propagates the error — fail-closed, matching AuthBridge's rejection of
	// requests it cannot validate.
	OnError string `yaml:"on_error,omitempty"`
	// Config is the plugin's own typed configuration.
	Config any `yaml:"config,omitempty"`
}

// JWTIdentityConfig is the `identity/jwt` plugin's configuration.
type JWTIdentityConfig struct {
	// Header is the request header the token is read from; the "Bearer "
	// prefix is stripped if present.
	Header string `yaml:"header"`
	// ClaimMapper selects how claims map onto the identity. "standard" is
	// the OIDC default.
	ClaimMapper string `yaml:"claim_mapper,omitempty"`
	// TrustedIssuers is the set of accepted issuers. At least one required.
	TrustedIssuers []TrustedIssuer `yaml:"trusted_issuers"`
}

// TrustedIssuer is one accepted issuer: which `iss` to expect, which `aud`
// values to accept, which algorithms, and where the verification key comes
// from.
type TrustedIssuer struct {
	Issuer string `yaml:"issuer"`
	// Audiences are the accepted `aud` values (OR semantics). An empty list
	// disables audience validation upstream, so this converter never emits
	// an empty list without saying so — see [BuildPolicy].
	Audiences   []string    `yaml:"audiences,omitempty"`
	Algorithms  []string    `yaml:"algorithms"`
	DecodingKey DecodingKey `yaml:"decoding_key"`
	// LeewaySeconds is the clock-skew tolerance for exp/nbf validation.
	LeewaySeconds int `yaml:"leeway_seconds,omitempty"`
}

// DecodingKey is where JWT signing key material comes from. This converter
// emits the `jwks_url` form, since AuthBridge's jwt-validation verifies
// against a JWKS endpoint.
type DecodingKey struct {
	Kind string `yaml:"kind"`
	URL  string `yaml:"url,omitempty"`
	// InsecureHTTP permits a plaintext http:// JWKS URL. The policy engine
	// rejects http:// JWKS endpoints unless this is set, because anyone on
	// the network path could swap the key material and forge accepted JWTs.
	InsecureHTTP bool `yaml:"insecure_http,omitempty"`
	// RefreshSecs is how often the background task refetches the key set.
	RefreshSecs int `yaml:"refresh_secs,omitempty"`
}

// jwtValidationConfig mirrors the subset of AuthBridge's jwt-validation plugin
// config that the policy translation consumes. Decoded from the plugin entry's
// raw config subtree; unknown fields are ignored here because the plugin's own
// typed decode is the authority on validity.
type jwtValidationConfig struct {
	Issuer           string   `json:"issuer"`
	JWKSURL          string   `json:"jwks_url"`
	KeycloakURL      string   `json:"keycloak_url"`
	KeycloakRealm    string   `json:"keycloak_realm"`
	Audience         string   `json:"audience"`
	AudienceFile     string   `json:"audience_file"`
	AudienceMode     string   `json:"audience_mode"`
	AllowedAudiences []string `json:"allowed_audiences"`
	BypassPaths      []string `json:"bypass_paths"`
}

// resolveJWKSURL reproduces jwt-validation's derivation priority so the
// generated policy fetches keys from the same endpoint AuthBridge would:
//
//  1. explicit jwks_url
//  2. keycloak_url + keycloak_realm (the internal URL, for split-horizon)
//  3. issuer (single-horizon fallback)
//
// Keeping this in step with the plugin matters: pointing the policy at a
// different JWKS endpoint than AuthBridge used would either fail to verify
// tokens that AuthBridge accepted, or verify against the wrong key material.
func (c jwtValidationConfig) resolveJWKSURL() string {
	if c.JWKSURL != "" {
		return c.JWKSURL
	}
	if c.KeycloakURL != "" && c.KeycloakRealm != "" {
		return strings.TrimRight(c.KeycloakURL, "/") + "/realms/" + c.KeycloakRealm +
			"/protocol/openid-connect/certs"
	}
	if c.Issuer != "" {
		return strings.TrimRight(c.Issuer, "/") + "/protocol/openid-connect/certs"
	}
	return ""
}

// audiences returns the accepted audience values, mirroring jwt-validation's
// OR semantics: allowed_audiences entries in config order, then the literal
// audience. Deduplicated, first occurrence winning.
//
// audience_file is NOT resolved here: it names a file read at runtime (the
// Rossoctl client-id convention), and this converter does not read the
// filesystem — a generated policy that silently baked in whatever happened to
// be on the generating machine's disk would be worse than one that reports the
// gap. Callers see it via [PolicyResult.Warnings].
func (c jwtValidationConfig) audiences() []string {
	var (
		out  []string
		seen = map[string]bool{}
	)
	add := func(s string) {
		if s == "" || seen[s] {
			return
		}
		seen[s] = true
		out = append(out, s)
	}
	for _, a := range c.AllowedAudiences {
		add(a)
	}
	add(c.Audience)
	return out
}

// PolicyResult is the outcome of building a policy document.
type PolicyResult struct {
	// Document is the generated policy document. Nil when no AuthBridge
	// plugin in the pipeline maps onto a policy plugin, in which case no
	// policy file should be written and no `policy` filter emitted.
	Document *PolicyDocument
	// Enforced lists the AuthBridge plugins whose enforcement the policy
	// document carries, e.g. "jwt-validation".
	Enforced []string
	// Warnings records fidelity gaps in the generated policy — settings that
	// could not be translated exactly and that change what the proxy accepts.
	Warnings []string
}

// BuildPolicy generates a Praxis policy document enforcing JWT validation from
// an AuthBridge config's inbound pipeline.
//
// It reads the `jwt-validation` plugin's config (issuer, JWKS URL derivation,
// audiences) and emits an `identity/jwt` policy plugin that verifies the same
// tokens: same issuer, same audiences, same JWKS endpoint, fail-closed on a
// missing or invalid token.
//
// Returns a Document of nil when the inbound pipeline declares no plugin that
// maps onto a policy plugin — there is nothing to enforce, so no policy file
// should be written. A jwt-validation entry disabled with `on_error: off` is
// skipped, matching AuthBridge dropping it from the pipeline.
//
// An error is returned only when a jwt-validation plugin is present but cannot
// be translated into something that would actually verify tokens (no issuer,
// or no derivable JWKS URL) — emitting a policy that accepts everything, or
// one the engine rejects at startup, would both be worse than failing here.
func BuildPolicy(cfg *config.Config) (*PolicyResult, error) {
	if cfg == nil {
		return nil, fmt.Errorf("praxis: nil AuthBridge config")
	}
	res := &PolicyResult{}

	for _, p := range cfg.Pipeline.Inbound.Plugins {
		if p.Name != "jwt-validation" || p.OnError == "off" {
			continue
		}
		plugin, warnings, err := jwtPolicyPlugin(p)
		if err != nil {
			return nil, err
		}
		if res.Document == nil {
			res.Document = &PolicyDocument{}
		}
		res.Document.Plugins = append(res.Document.Plugins, plugin)
		res.Enforced = append(res.Enforced, p.Name)
		res.Warnings = append(res.Warnings, warnings...)
	}

	return res, nil
}

// jwtPolicyPlugin translates one jwt-validation entry into an identity/jwt
// policy plugin.
func jwtPolicyPlugin(entry config.PluginEntry) (PolicyPlugin, []string, error) {
	var jc jwtValidationConfig
	if len(entry.Config) > 0 {
		if err := json.Unmarshal(entry.Config, &jc); err != nil {
			return PolicyPlugin{}, nil, fmt.Errorf(
				"praxis: decoding jwt-validation config: %w", err)
		}
	}

	if jc.Issuer == "" {
		return PolicyPlugin{}, nil, fmt.Errorf(
			"praxis: jwt-validation has no issuer, so no policy could be generated that " +
				"verifies tokens; set issuer in the plugin config")
	}
	jwks := jc.resolveJWKSURL()
	if jwks == "" {
		return PolicyPlugin{}, nil, fmt.Errorf(
			"praxis: jwt-validation jwks_url could not be derived for issuer %q; set "+
				"jwks_url, or keycloak_url + keycloak_realm", jc.Issuer)
	}

	var warnings []string

	// The policy engine rejects a plaintext http:// JWKS URL unless
	// insecure_http is set. AuthBridge does not have this guard, so a local /
	// demo config that works under AuthBridge would fail Praxis startup
	// outright. Set the flag to preserve behavior, and say so — over plaintext
	// anyone on the path can swap the key material and forge accepted JWTs.
	insecureHTTP := false
	if u, err := url.Parse(jwks); err == nil && u.Scheme == "http" {
		insecureHTTP = true
		warnings = append(warnings, fmt.Sprintf(
			"jwt-validation JWKS endpoint %q is plaintext http://, so the generated policy sets "+
				"decoding_key.insecure_http: true (the policy engine rejects http:// JWKS URLs "+
				"otherwise). Anyone on the network path to that endpoint can substitute key "+
				"material and forge tokens this proxy will accept. Acceptable for local "+
				"development; use https for anything else.", jwks))
	}

	auds := jc.audiences()
	if len(auds) == 0 {
		// Upstream treats an empty audiences list as "disable aud validation".
		// AuthBridge always validates audience, so an empty list here is a
		// real weakening and must be reported rather than quietly emitted.
		switch {
		case jc.AudienceMode == "per-host":
			warnings = append(warnings,
				"jwt-validation uses audience_mode: per-host, which derives the expected "+
					"audience from each request's Host. The policy engine's trusted_issuers take "+
					"a static audience list, so the generated policy does NOT validate the aud "+
					"claim. Add the concrete audiences to trusted_issuers[0].audiences.")
		case jc.AudienceFile != "":
			warnings = append(warnings, fmt.Sprintf(
				"jwt-validation reads its expected audience from the file %q at runtime, which "+
					"this converter does not read. The generated policy does NOT validate the "+
					"aud claim — any token from the trusted issuer is accepted regardless of "+
					"audience. Set `audience` explicitly in the plugin config, or add the value "+
					"to trusted_issuers[0].audiences in the generated policy.", jc.AudienceFile))
		default:
			warnings = append(warnings,
				"jwt-validation declares no audience, so the generated policy does NOT validate "+
					"the aud claim: any token from the trusted issuer is accepted.")
		}
	}

	if len(jc.BypassPaths) > 0 {
		// jwt-validation skips validation on these path globs. The identity
		// plugin has no per-path exemption, so those paths would now require a
		// token. That flips health probes and .well-known endpoints from open
		// to 401 — worth stating explicitly.
		warnings = append(warnings, fmt.Sprintf(
			"jwt-validation exempts the paths %v from validation, which the identity plugin "+
				"has no equivalent for: in the generated policy those paths require a valid "+
				"token too. Health and readiness probes to them will get 401. Praxis serves "+
				"its own liveness/readiness on the admin endpoint, which does not run this "+
				"chain.", jc.BypassPaths))
	}

	return PolicyPlugin{
		Name:        "jwt-validation",
		Kind:        policyPluginKindJWT,
		Description: "Inbound JWT validation, translated from AuthBridge's jwt-validation plugin.",
		Hooks:       []string{policyHookIdentity},
		Mode:        policyModeSequential,
		Priority:    policyJWTPluginPriority,
		OnError:     policyOnErrorFail,
		Config: JWTIdentityConfig{
			Header:      "Authorization",
			ClaimMapper: policyClaimMapperStd,
			TrustedIssuers: []TrustedIssuer{{
				Issuer:     jc.Issuer,
				Audiences:  auds,
				Algorithms: []string{"RS256"},
				DecodingKey: DecodingKey{
					Kind:         "jwks_url",
					URL:          jwks,
					InsecureHTTP: insecureHTTP,
					RefreshSecs:  policyJWKSRefreshSecs,
				},
				LeewaySeconds: policyDefaultLeewaySec,
			}},
		},
	}, warnings, nil
}

// policyHeader is the comment block prepended to a generated policy document.
const policyHeader = `# Praxis POLICY DOCUMENT — GENERATED from an AuthBridge config.
#
# Generated by authbridge/authlib/praxis (BuildPolicy). Edits are overwritten
# on the next run; change the AuthBridge config instead.
#
# This is the document the proxy config's ` + "`policy`" + ` filter loads via its
# ` + "`config_path`" + `. It declares the identity plugins the Praxis Policy Engine
# enforces — here, inbound JWT validation translated from AuthBridge's
# jwt-validation plugin.
#
# Requires Praxis built with the policy-engine cargo feature:
#   cargo run --features policy-engine -p praxis-proxy -- -c %s
#
# Enforcement: the engine resolves identity on the request phase and denies a
# missing or invalid token with 401 (WWW-Authenticate: Bearer, plus an
# X-Policy-Violation header naming the failure). ` + "`on_error: fail`" + ` makes the
# plugin fail closed.
#
# Routing is deliberately not enabled. With plugin_settings.routing_enabled
# absent, identity resolution alone gates every request — which is what
# AuthBridge's jwt-validation does. Enabling routes would move enforcement to
# per-entity rules and require protocol-classifier metadata in the chain.
`

// RenderPolicy marshals a policy document to YAML with an explanatory header.
//
// proxyConfigPath is used only to make the header's example command
// copy-pasteable.
func RenderPolicy(doc *PolicyDocument, proxyConfigPath string) ([]byte, error) {
	if doc == nil {
		return nil, fmt.Errorf("praxis: nil policy document")
	}
	return renderYAML(fmt.Sprintf(policyHeader, proxyConfigPath), doc)
}

// RenderPolicyResult marshals a policy document, appending the fidelity
// warnings as a trailing comment block so the generated file carries its own
// caveats — the places where it accepts more than AuthBridge would.
func RenderPolicyResult(res *PolicyResult, proxyConfigPath string) ([]byte, error) {
	if res == nil || res.Document == nil {
		return nil, fmt.Errorf("praxis: no policy document to render")
	}
	out, err := RenderPolicy(res.Document, proxyConfigPath)
	if err != nil {
		return nil, err
	}
	if len(res.Warnings) == 0 {
		return out, nil
	}
	var buf bytes.Buffer
	buf.Write(out)
	buf.WriteString("\n# ── POLICY FIDELITY NOTES ")
	buf.WriteString(strings.Repeat("─", 45))
	buf.WriteString("\n#\n")
	buf.WriteString("# Where this policy differs from the AuthBridge config it came from:\n#\n")
	for _, w := range res.Warnings {
		writeCommentBlock(&buf, w)
	}
	return buf.Bytes(), nil
}
