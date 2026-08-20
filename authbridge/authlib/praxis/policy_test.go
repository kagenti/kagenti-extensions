package praxis

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/rossoctl/cortex/authbridge/authlib/config"
	"gopkg.in/yaml.v3"
)

// jwtPlugin builds a jwt-validation plugin entry with the given config.
func jwtPlugin(t *testing.T, cfg map[string]any) config.PluginEntry {
	t.Helper()
	raw, err := json.Marshal(cfg)
	if err != nil {
		t.Fatalf("marshal plugin config: %v", err)
	}
	return config.PluginEntry{Name: "jwt-validation", Config: raw}
}

// keycloakJWT is the shape the weather-service example uses: public issuer,
// internal Keycloak URL + realm for JWKS derivation, explicit audience.
func keycloakJWT(t *testing.T) config.PluginEntry {
	t.Helper()
	return jwtPlugin(t, map[string]any{
		"issuer":         "http://keycloak.localtest.me:8080/realms/rossoctl",
		"keycloak_url":   "http://keycloak.localtest.me:8080/",
		"keycloak_realm": "rossoctl",
		"audience":       "spiffe://localtest.me/ns/team1/sa/weather-service",
	})
}

func TestBuildPolicy_NilConfig(t *testing.T) {
	if _, err := BuildPolicy(nil); err == nil {
		t.Fatal("expected an error for a nil config")
	}
}

// No jwt-validation in the pipeline means nothing for the policy engine to
// enforce, so no document should be produced — writing one, and pointing a
// policy filter at it, would add a filter that enforces nothing.
func TestBuildPolicy_NoJWTPlugin_NoDocument(t *testing.T) {
	res, err := BuildPolicy(proxySidecar(t, nil))
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	if res.Document != nil {
		t.Errorf("expected no policy document, got %+v", res.Document)
	}
	if len(res.Enforced) != 0 {
		t.Errorf("expected nothing enforced, got %v", res.Enforced)
	}
}

func TestBuildPolicy_JWTValidation(t *testing.T) {
	res, err := BuildPolicy(proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{keycloakJWT(t)}
	}))
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	if res.Document == nil {
		t.Fatal("expected a policy document")
	}
	if len(res.Document.Plugins) != 1 {
		t.Fatalf("plugins = %d, want 1", len(res.Document.Plugins))
	}
	p := res.Document.Plugins[0]
	if p.Kind != policyPluginKindJWT {
		t.Errorf("kind = %q, want %q", p.Kind, policyPluginKindJWT)
	}
	if len(p.Hooks) != 1 || p.Hooks[0] != policyHookIdentity {
		t.Errorf("hooks = %v, want [%s]", p.Hooks, policyHookIdentity)
	}
	// Fail-closed is the whole point: a plugin that ignores its own errors
	// would let unvalidated requests through.
	if p.OnError != policyOnErrorFail {
		t.Errorf("on_error = %q, want %q", p.OnError, policyOnErrorFail)
	}
	// sequential is the only band that can both block and modify.
	if p.Mode != policyModeSequential {
		t.Errorf("mode = %q, want %q", p.Mode, policyModeSequential)
	}

	jc, ok := p.Config.(JWTIdentityConfig)
	if !ok {
		t.Fatalf("config has type %T", p.Config)
	}
	ti := jc.TrustedIssuers[0]
	if ti.Issuer != "http://keycloak.localtest.me:8080/realms/rossoctl" {
		t.Errorf("issuer = %q", ti.Issuer)
	}
	if len(ti.Audiences) != 1 || ti.Audiences[0] != "spiffe://localtest.me/ns/team1/sa/weather-service" {
		t.Errorf("audiences = %v", ti.Audiences)
	}
	// JWKS must be derived from keycloak_url + keycloak_realm, matching
	// jwt-validation's own derivation.
	want := "http://keycloak.localtest.me:8080/realms/rossoctl/protocol/openid-connect/certs"
	if ti.DecodingKey.URL != want {
		t.Errorf("jwks url = %q, want %q", ti.DecodingKey.URL, want)
	}
	if ti.DecodingKey.Kind != "jwks_url" {
		t.Errorf("decoding_key kind = %q, want jwks_url", ti.DecodingKey.Kind)
	}
	if !containsSubstring(res.Enforced, "jwt-validation") {
		t.Errorf("Enforced = %v, want jwt-validation", res.Enforced)
	}
}

// The policy engine rejects a plaintext http:// JWKS URL unless insecure_http
// is set, so a local config that works under AuthBridge would otherwise fail
// Praxis startup. The flag must be set AND the weakening reported.
func TestBuildPolicy_PlaintextJWKS_SetsInsecureAndWarns(t *testing.T) {
	res, err := BuildPolicy(proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{keycloakJWT(t)}
	}))
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	jc := res.Document.Plugins[0].Config.(JWTIdentityConfig)
	if !jc.TrustedIssuers[0].DecodingKey.InsecureHTTP {
		t.Error("expected insecure_http for a plaintext http:// JWKS URL")
	}
	if !containsSubstring(res.Warnings, "insecure_http") {
		t.Errorf("expected a warning about plaintext JWKS, got %v", res.Warnings)
	}
}

func TestBuildPolicy_HTTPSJWKS_NoInsecureFlag(t *testing.T) {
	res, err := BuildPolicy(proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{jwtPlugin(t, map[string]any{
			"issuer":   "https://idp.example.com/realms/r",
			"jwks_url": "https://idp.example.com/realms/r/protocol/openid-connect/certs",
			"audience": "svc",
		})}
	}))
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	jc := res.Document.Plugins[0].Config.(JWTIdentityConfig)
	if jc.TrustedIssuers[0].DecodingKey.InsecureHTTP {
		t.Error("https JWKS must not set insecure_http")
	}
	if containsSubstring(res.Warnings, "insecure_http") {
		t.Errorf("https JWKS should not warn about plaintext: %v", res.Warnings)
	}
}

// An explicit jwks_url wins over keycloak_url derivation, matching
// jwt-validation's priority order.
func TestBuildPolicy_ExplicitJWKSWins(t *testing.T) {
	res, err := BuildPolicy(proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{jwtPlugin(t, map[string]any{
			"issuer":         "https://public.example.com/realms/r",
			"jwks_url":       "https://internal.svc/jwks",
			"keycloak_url":   "https://other.example.com",
			"keycloak_realm": "r",
			"audience":       "svc",
		})}
	}))
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	jc := res.Document.Plugins[0].Config.(JWTIdentityConfig)
	if got := jc.TrustedIssuers[0].DecodingKey.URL; got != "https://internal.svc/jwks" {
		t.Errorf("jwks url = %q, want the explicit one", got)
	}
}

// Falling back to the issuer is jwt-validation's third derivation step.
func TestBuildPolicy_JWKSFromIssuer(t *testing.T) {
	res, err := BuildPolicy(proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{jwtPlugin(t, map[string]any{
			"issuer":   "https://idp.example.com/realms/r",
			"audience": "svc",
		})}
	}))
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	jc := res.Document.Plugins[0].Config.(JWTIdentityConfig)
	want := "https://idp.example.com/realms/r/protocol/openid-connect/certs"
	if got := jc.TrustedIssuers[0].DecodingKey.URL; got != want {
		t.Errorf("jwks url = %q, want %q", got, want)
	}
}

// allowed_audiences and audience are unioned with OR semantics, deduplicated.
func TestBuildPolicy_AudienceUnion(t *testing.T) {
	res, err := BuildPolicy(proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{jwtPlugin(t, map[string]any{
			"issuer":            "https://idp.example.com/realms/r",
			"audience":          "primary",
			"allowed_audiences": []string{"extra-1", "extra-2", "primary"},
		})}
	}))
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	jc := res.Document.Plugins[0].Config.(JWTIdentityConfig)
	got := jc.TrustedIssuers[0].Audiences
	want := []string{"extra-1", "extra-2", "primary"}
	if len(got) != len(want) {
		t.Fatalf("audiences = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("audiences = %v, want %v", got, want)
			break
		}
	}
}

// An audience_file is read at runtime by AuthBridge and cannot be resolved
// here. The resulting policy does NOT validate aud, which is a real weakening
// and must be reported rather than passing silently.
func TestBuildPolicy_AudienceFile_WarnsAboutNoAudValidation(t *testing.T) {
	res, err := BuildPolicy(proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{jwtPlugin(t, map[string]any{
			"issuer":        "https://idp.example.com/realms/r",
			"audience_file": "/shared/client-id.txt",
		})}
	}))
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	jc := res.Document.Plugins[0].Config.(JWTIdentityConfig)
	if len(jc.TrustedIssuers[0].Audiences) != 0 {
		t.Errorf("expected no audiences, got %v", jc.TrustedIssuers[0].Audiences)
	}
	if !containsSubstring(res.Warnings, "does NOT validate") {
		t.Errorf("expected a warning that aud is unvalidated, got %v", res.Warnings)
	}
	if !containsSubstring(res.Warnings, "/shared/client-id.txt") {
		t.Errorf("expected the warning to name the file, got %v", res.Warnings)
	}
}

func TestBuildPolicy_PerHostAudience_Warns(t *testing.T) {
	res, err := BuildPolicy(proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{jwtPlugin(t, map[string]any{
			"issuer":        "https://idp.example.com/realms/r",
			"audience_mode": "per-host",
		})}
	}))
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	if !containsSubstring(res.Warnings, "per-host") {
		t.Errorf("expected a per-host warning, got %v", res.Warnings)
	}
}

// bypass_paths has no identity-plugin equivalent, so those paths now require a
// token. Health probes flipping from open to 401 must be surfaced.
func TestBuildPolicy_BypassPaths_Warns(t *testing.T) {
	res, err := BuildPolicy(proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{jwtPlugin(t, map[string]any{
			"issuer":       "https://idp.example.com/realms/r",
			"audience":     "svc",
			"bypass_paths": []string{"/healthz", "/metrics"},
		})}
	}))
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	if !containsSubstring(res.Warnings, "/healthz") {
		t.Errorf("expected a bypass_paths warning naming the paths, got %v", res.Warnings)
	}
}

// Generating a policy that cannot verify anything would be worse than failing:
// it would look like enforcement while accepting every token.
func TestBuildPolicy_MissingIssuer_IsError(t *testing.T) {
	_, err := BuildPolicy(proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{jwtPlugin(t, map[string]any{
			"audience": "svc",
		})}
	}))
	if err == nil {
		t.Fatal("expected an error when jwt-validation has no issuer")
	}
	if !strings.Contains(err.Error(), "issuer") {
		t.Errorf("error should mention issuer: %v", err)
	}
}

// on_error: off means AuthBridge drops the plugin, so no policy is generated
// for it — otherwise the Praxis proxy would enforce what AuthBridge did not.
func TestBuildPolicy_DisabledPlugin_NoDocument(t *testing.T) {
	res, err := BuildPolicy(proxySidecar(t, func(c *config.Config) {
		e := keycloakJWT(t)
		e.OnError = "off"
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{e}
	}))
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	if res.Document != nil {
		t.Error("a plugin disabled with on_error: off must not produce a policy")
	}
}

// With a policy document, the inbound chain must carry the `policy` filter
// instead of the inert UNMAPPED marker, and jwt-validation must no longer be
// reported unmapped.
func TestConvertWithPolicy_EmitsPolicyFilter(t *testing.T) {
	cfg := proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{keycloakJWT(t)}
	})
	res, pol, err := ConvertWithPolicy(cfg, "/tmp/praxis-policy.yaml")
	if err != nil {
		t.Fatalf("ConvertWithPolicy: %v", err)
	}
	if pol.Document == nil {
		t.Fatal("expected a policy document")
	}
	if containsSubstring(res.Unmapped, "jwt-validation") {
		t.Errorf("jwt-validation is enforced by the policy and must not be unmapped: %v", res.Unmapped)
	}

	var found *Filter
	for i, f := range res.Config.FilterChains[0].Filters {
		if f.Type == "policy" {
			found = &res.Config.FilterChains[0].Filters[i]
		}
	}
	if found == nil {
		t.Fatal("expected a policy filter in the inbound chain")
	}
	var gotPath any
	var gotMeta any
	for _, fl := range found.Fields {
		switch fl.Key {
		case "config_path":
			gotPath = fl.Value
		case "require_protocol_metadata":
			gotMeta = fl.Value
		}
	}
	if gotPath != "/tmp/praxis-policy.yaml" {
		t.Errorf("config_path = %v, want the policy path", gotPath)
	}
	// True (the upstream default) would reject every request for missing
	// classifier metadata rather than judging it on its token.
	if gotMeta != false {
		t.Errorf("require_protocol_metadata = %v, want false", gotMeta)
	}
}

// Without a policy path, the auth plugin stays unmapped and no policy filter is
// emitted — a default-feature Praxis build must still get a loadable config.
func TestConvertWithoutPolicy_NoPolicyFilter(t *testing.T) {
	res, err := Convert(proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{keycloakJWT(t)}
	}), nil)
	if err != nil {
		t.Fatalf("Convert: %v", err)
	}
	for _, f := range res.Config.FilterChains[0].Filters {
		if f.Type == "policy" {
			t.Error("no policy filter should be emitted without a policy path")
		}
	}
	if !containsSubstring(res.Unmapped, "jwt-validation") {
		t.Errorf("expected jwt-validation unmapped, got %v", res.Unmapped)
	}
}

// A pipeline with no enforceable plugin must not get a policy filter, since it
// would point at a file the caller never writes and Praxis would fail to start.
func TestConvertWithPolicy_NoEnforceable_NoFilter(t *testing.T) {
	res, pol, err := ConvertWithPolicy(proxySidecar(t, nil), "/tmp/praxis-policy.yaml")
	if err != nil {
		t.Fatalf("ConvertWithPolicy: %v", err)
	}
	if pol.Document != nil {
		t.Error("expected no policy document")
	}
	for _, ch := range res.Config.FilterChains {
		for _, f := range ch.Filters {
			if f.Type == "policy" {
				t.Error("no policy filter should be emitted when nothing is enforceable")
			}
		}
	}
}

// Two jwt-validation entries in one stage still yield a single policy filter:
// one document carries both, so a second entry would re-run the same engine.
func TestConvertWithPolicy_SinglePolicyFilterPerChain(t *testing.T) {
	res, _, err := ConvertWithPolicy(proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{keycloakJWT(t), keycloakJWT(t)}
	}), "/tmp/praxis-policy.yaml")
	if err != nil {
		t.Fatalf("ConvertWithPolicy: %v", err)
	}
	n := 0
	for _, f := range res.Config.FilterChains[0].Filters {
		if f.Type == "policy" {
			n++
		}
	}
	if n != 1 {
		t.Errorf("policy filters = %d, want 1", n)
	}
}

func TestRenderPolicyResult_ParsesAsYAML(t *testing.T) {
	pol, err := BuildPolicy(proxySidecar(t, func(c *config.Config) {
		c.Pipeline.Inbound.Plugins = []config.PluginEntry{keycloakJWT(t)}
	}))
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	out, err := RenderPolicyResult(pol, "/tmp/praxis-config.yaml")
	if err != nil {
		t.Fatalf("RenderPolicyResult: %v", err)
	}
	var probe struct {
		Plugins []struct {
			Name   string   `yaml:"name"`
			Kind   string   `yaml:"kind"`
			Hooks  []string `yaml:"hooks"`
			Config struct {
				TrustedIssuers []struct {
					Issuer      string   `yaml:"issuer"`
					Audiences   []string `yaml:"audiences"`
					Algorithms  []string `yaml:"algorithms"`
					DecodingKey struct {
						Kind string `yaml:"kind"`
						URL  string `yaml:"url"`
					} `yaml:"decoding_key"`
				} `yaml:"trusted_issuers"`
			} `yaml:"config"`
		} `yaml:"plugins"`
	}
	if err := yaml.Unmarshal(out, &probe); err != nil {
		t.Fatalf("rendered policy does not parse: %v\n%s", err, out)
	}
	if len(probe.Plugins) != 1 {
		t.Fatalf("plugins = %d, want 1", len(probe.Plugins))
	}
	if probe.Plugins[0].Kind != policyPluginKindJWT {
		t.Errorf("kind = %q", probe.Plugins[0].Kind)
	}
	if len(probe.Plugins[0].Config.TrustedIssuers) != 1 {
		t.Fatal("expected one trusted issuer to survive the round trip")
	}
	if probe.Plugins[0].Config.TrustedIssuers[0].DecodingKey.Kind != "jwks_url" {
		t.Error("expected the jwks_url decoding key to survive the round trip")
	}
}

func TestRenderPolicyResult_NilDocument_IsError(t *testing.T) {
	if _, err := RenderPolicyResult(&PolicyResult{}, "x.yaml"); err == nil {
		t.Fatal("expected an error rendering a nil document")
	}
}

// TestGeneratedPolicy_ValidatesWithPraxis runs the real policy-engine Praxis
// binary against the generated pair. This is what actually pins the goal: the
// policy engine parses the document at filter-construction time and fails the
// server start on a malformed policy, which no Go-side assertion can stand in
// for.
//
// Requires a Praxis binary built with --features policy-engine. Skipped
// otherwise, and skipped when the available binary is a default build (detected
// by it rejecting the policy filter as unknown).
func TestGeneratedPolicy_ValidatesWithPraxis(t *testing.T) {
	bin := findPraxisBinary(t)
	if bin == "" {
		t.Skip("no praxis binary found; set PRAXIS_BIN to enable this test")
	}
	if !praxisHasPolicyEngine(t, bin) {
		t.Skip("praxis binary lacks the policy-engine feature; rebuild with --features policy-engine")
	}

	cases := []struct {
		name    string
		plugins []config.PluginEntry
	}{
		{
			name:    "keycloak issuer with explicit audience",
			plugins: []config.PluginEntry{keycloakJWT(t)},
		},
		{
			name: "https jwks, multiple audiences",
			plugins: []config.PluginEntry{jwtPlugin(t, map[string]any{
				"issuer":            "https://idp.example.com/realms/r",
				"jwks_url":          "https://idp.example.com/realms/r/protocol/openid-connect/certs",
				"audience":          "primary",
				"allowed_audiences": []string{"extra"},
			})},
		},
		{
			name: "no audience (aud validation disabled)",
			plugins: []config.PluginEntry{jwtPlugin(t, map[string]any{
				"issuer":        "https://idp.example.com/realms/r",
				"audience_file": "/shared/client-id.txt",
			})},
		},
		{
			name: "bypass paths declared",
			plugins: []config.PluginEntry{jwtPlugin(t, map[string]any{
				"issuer":       "https://idp.example.com/realms/r",
				"audience":     "svc",
				"bypass_paths": []string{"/healthz", "/.well-known/*"},
			})},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := &config.Config{
				Mode: config.ModeProxySidecar,
				Listener: config.ListenerConfig{
					Roles:               []string{config.RoleReverse},
					ReverseProxyBackend: "http://127.0.0.1:8001",
				},
				// Loopback so the admin bind needs no insecure override.
				Stats: config.StatsConfig{StatsAddress: "127.0.0.1:19093"},
			}
			cfg.Pipeline.Inbound.Plugins = tc.plugins
			config.ApplyPreset(cfg)

			dir := t.TempDir()
			policyPath := filepath.Join(dir, "praxis-policy.yaml")
			cfgPath := filepath.Join(dir, "praxis-config.yaml")

			res, pol, err := ConvertWithPolicy(cfg, policyPath)
			if err != nil {
				t.Fatalf("ConvertWithPolicy: %v", err)
			}
			if pol.Document == nil {
				t.Fatal("expected a policy document")
			}
			policyData, err := RenderPolicyResult(pol, cfgPath)
			if err != nil {
				t.Fatalf("RenderPolicyResult: %v", err)
			}
			if err := os.WriteFile(policyPath, policyData, 0o600); err != nil {
				t.Fatalf("write policy: %v", err)
			}
			cfgData, err := RenderResult(res, cfgPath)
			if err != nil {
				t.Fatalf("RenderResult: %v", err)
			}
			if err := os.WriteFile(cfgPath, cfgData, 0o600); err != nil {
				t.Fatalf("write config: %v", err)
			}

			out, err := exec.Command(bin, "-t", "-c", cfgPath).CombinedOutput()
			if err != nil {
				t.Errorf("praxis rejected the generated pair: %v\n--- praxis ---\n%s\n--- config ---\n%s\n--- policy ---\n%s",
					err, out, cfgData, policyData)
			}
		})
	}
}

// praxisHasPolicyEngine reports whether the binary was built with the
// policy-engine feature, by checking whether it recognizes the filter name.
func praxisHasPolicyEngine(t *testing.T, bin string) bool {
	t.Helper()
	dir := t.TempDir()
	probe := filepath.Join(dir, "probe.yaml")
	// A policy filter pointing at a missing file: a default build reports
	// "unknown filter type", a policy-engine build gets far enough to complain
	// about the unreadable config_path.
	const yamlBody = `listeners:
  - name: l
    address: "127.0.0.1:18099"
    filter_chains: [c]
filter_chains:
  - name: c
    filters:
      - filter: policy
        config_path: /nonexistent/policy.yaml
      - filter: static_response
        status: 200
`
	if err := os.WriteFile(probe, []byte(yamlBody), 0o600); err != nil {
		t.Fatalf("write probe: %v", err)
	}
	out, _ := exec.Command(bin, "-t", "-c", probe).CombinedOutput()
	return !strings.Contains(string(out), "unknown filter type")
}
