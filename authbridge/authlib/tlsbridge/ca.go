package tlsbridge

import (
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"time"
)

// CASource supplies the signing CA used to mint per-origin leaves.
type CASource interface {
	// Issuer returns the CA certificate and its private key for signing leaves.
	Issuer() (cert *x509.Certificate, key crypto.Signer)
	// CACertPEM returns the CA certificate in PEM form (for the agent's trust store).
	CACertPEM() []byte
}

type staticSource struct {
	cert    *x509.Certificate
	key     crypto.Signer
	certPEM []byte
}

func (s *staticSource) Issuer() (*x509.Certificate, crypto.Signer) { return s.cert, s.key }
func (s *staticSource) CACertPEM() []byte                          { return s.certPEM }

// genSelfSignedCA mints a fresh in-memory self-signed ECDSA-P256 CA and returns
// the parsed cert, its signer, and both PEM encodings (cert + PKCS#8 key).
// Shared by NewEphemeralSource (in-memory only) and NewGeneratedFileSource
// (which persists the PEM to disk).
func genSelfSignedCA() (cert *x509.Certificate, key crypto.Signer, certPEM, keyPEM []byte, err error) {
	ecKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("tlsbridge: generate CA key: %w", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "authbridge-tls-bridge-ca"},
		NotBefore:             time.Now().Add(-time.Minute),
		NotAfter:              time.Now().Add(365 * 24 * time.Hour),
		IsCA:                  true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, ecKey.Public(), ecKey)
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("tlsbridge: self-sign CA: %w", err)
	}
	cert, err = x509.ParseCertificate(der)
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("tlsbridge: parse self-signed CA: %w", err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(ecKey)
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("tlsbridge: marshal CA key: %w", err)
	}
	certPEM = pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM = pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})
	return cert, ecKey, certPEM, keyPEM, nil
}

// NewEphemeralSource generates an in-memory self-signed CA. Used as the
// standalone / no-cert-manager fallback and in tests. NewGeneratedFileSource is
// the persisted variant used by the demo/standalone binary path (generate_ca).
func NewEphemeralSource() (CASource, error) {
	cert, key, certPEM, _, err := genSelfSignedCA()
	if err != nil {
		return nil, err
	}
	return &staticSource{cert: cert, key: key, certPEM: certPEM}, nil
}

// NewFileSource loads a CA (tls.crt/tls.key) from disk — the cert-manager /
// operator-coordinated path (Phase 2). Keys may be PKCS#8, PKCS#1 (RSA) or
// SEC1 (EC); cert-manager's DEFAULT encoding is PKCS#1, so all three are tried.
func NewFileSource(certPath, keyPath string) (CASource, error) {
	certPEM, err := os.ReadFile(certPath)
	if err != nil {
		return nil, fmt.Errorf("tlsbridge: read CA cert %s: %w", certPath, err)
	}
	keyPEM, err := os.ReadFile(keyPath)
	if err != nil {
		return nil, fmt.Errorf("tlsbridge: read CA key %s: %w", keyPath, err)
	}
	cb, _ := pem.Decode(certPEM)
	if cb == nil {
		return nil, fmt.Errorf("tlsbridge: CA cert %s is not PEM", certPath)
	}
	cert, err := x509.ParseCertificate(cb.Bytes)
	if err != nil {
		return nil, fmt.Errorf("tlsbridge: parse CA cert: %w", err)
	}
	kb, _ := pem.Decode(keyPEM)
	if kb == nil {
		return nil, fmt.Errorf("tlsbridge: CA key %s is not PEM", keyPath)
	}
	key, err := parsePrivateKey(kb.Bytes)
	if err != nil {
		return nil, err
	}
	// Fail loud at load on a misissued Secret. Without these checks a non-CA or
	// mismatched cert/key loads "fine" and then silently fails to sign minted
	// leaves at request time → the agent rejects the chain → every call falls
	// open to tunnel, with no error. A cert-manager Secret can be misconfigured
	// (wrong issuerRef, leaf instead of CA, mid-rotation key mismatch), so verify.
	if !cert.IsCA {
		return nil, fmt.Errorf("tlsbridge: CA cert %s is not a CA (IsCA=false)", certPath)
	}
	if cert.KeyUsage != 0 && cert.KeyUsage&x509.KeyUsageCertSign == 0 {
		return nil, fmt.Errorf("tlsbridge: CA cert %s lacks KeyUsageCertSign", certPath)
	}
	pub, ok := cert.PublicKey.(interface{ Equal(x crypto.PublicKey) bool })
	if !ok || !pub.Equal(key.Public()) {
		return nil, fmt.Errorf("tlsbridge: CA cert %s and key %s do not match", certPath, keyPath)
	}
	return &staticSource{cert: cert, key: key, certPEM: certPEM}, nil
}

// NewGeneratedFileSource mints a fresh self-signed CA and persists it into the
// directory of certPath: the signing key at keyPath (0600), the CA cert at
// certPath (0644), and — when trustPath is non-empty — a copy of the CA cert at
// trustPath (0644, the "ca.crt" clients load into their trust store, e.g. via
// NODE_EXTRA_CA_CERTS). Returns a CASource backed by the freshly minted
// material. Used only on the generate_ca standalone/demo path.
func NewGeneratedFileSource(certPath, keyPath, trustPath string) (CASource, error) {
	cert, key, certPEM, keyPEM, err := genSelfSignedCA()
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(certPath), 0o755); err != nil {
		return nil, fmt.Errorf("tlsbridge: create ca_dir: %w", err)
	}
	// Each file is written atomically (temp + rename) so a reader or a
	// subsequent boot never observes a half-written cert or key. The three
	// writes are still sequential, so a crash or error between them leaves an
	// INCOMPLETE set — EnsureFileSource treats that as "regenerate" rather than
	// letting an orphaned tls.key wedge the next boot. Key first at 0600 (a
	// signing key must never be world-readable); cert and ca.crt at 0644.
	if err := atomicWriteFile(keyPath, keyPEM, 0o600); err != nil {
		return nil, fmt.Errorf("tlsbridge: write CA key %s: %w", keyPath, err)
	}
	if err := atomicWriteFile(certPath, certPEM, 0o644); err != nil {
		return nil, fmt.Errorf("tlsbridge: write CA cert %s: %w", certPath, err)
	}
	if trustPath != "" {
		if err := atomicWriteFile(trustPath, certPEM, 0o644); err != nil {
			return nil, fmt.Errorf("tlsbridge: write trust cert %s: %w", trustPath, err)
		}
	}
	return &staticSource{cert: cert, key: key, certPEM: certPEM}, nil
}

// EnsureFileSource loads the signing CA (tls.crt/tls.key) from caDir. With
// generate=false it is exactly NewFileSource — a missing or invalid CA fails
// loud, so an operator-mounted cert-manager Secret is never silently replaced.
//
// With generate=true it self-heals an INCOMPLETE on-disk set: when any of
// tls.crt, tls.key, or ca.crt is missing it mints a fresh CA and persists all
// three (generated=true). This closes two failure modes of a partial write (a
// run killed or erroring between the three writes): an orphaned tls.key that
// would otherwise wedge every subsequent boot (NewFileSource fails on the
// missing cert → fatal), and a missing ca.crt trust anchor that loads fine yet
// leaves clients unable to verify the forged leaves. A COMPLETE set is never
// regenerated — it is loaded, and a complete-but-invalid cert/key still fails
// loud via NewFileSource so a real Secret is not overwritten.
func EnsureFileSource(caDir string, generate bool) (src CASource, generated bool, err error) {
	certPath := filepath.Join(caDir, "tls.crt")
	keyPath := filepath.Join(caDir, "tls.key")
	trustPath := filepath.Join(caDir, "ca.crt")
	complete := fileExists(certPath) && fileExists(keyPath) && fileExists(trustPath)
	if generate && !complete {
		src, err = NewGeneratedFileSource(certPath, keyPath, trustPath)
		return src, err == nil, err
	}
	src, err = NewFileSource(certPath, keyPath)
	return src, false, err
}

// fileExists reports whether path exists and is statable.
func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// atomicWriteFile writes data to a temp file in the same directory and renames
// it into place — atomic on POSIX, so a reader or the next boot never sees a
// half-written cert or key. Mirrors authlib/spiffe.atomicWrite (unexported
// there; kept local to avoid coupling the bridge to the spiffe package).
func atomicWriteFile(path string, data []byte, mode os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".tmp-"+filepath.Base(path)+"-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }() // best-effort; a no-op after a successful rename
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Chmod(mode); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}

// parsePrivateKey accepts PKCS#8, PKCS#1 (RSA) and SEC1 (EC) DER.
func parsePrivateKey(der []byte) (crypto.Signer, error) {
	if k, err := x509.ParsePKCS8PrivateKey(der); err == nil {
		if s, ok := k.(crypto.Signer); ok {
			return s, nil
		}
		// A successful PKCS#8 parse means the bytes ARE PKCS#8, so falling
		// through to PKCS#1/SEC1 would be pointless — a non-Signer PKCS#8
		// key is a hard error, not a format we should keep guessing at.
		return nil, fmt.Errorf("tlsbridge: PKCS#8 key is not a crypto.Signer")
	}
	if k, err := x509.ParsePKCS1PrivateKey(der); err == nil {
		return k, nil
	}
	if k, err := x509.ParseECPrivateKey(der); err == nil {
		return k, nil
	}
	return nil, fmt.Errorf("tlsbridge: unsupported CA key format (tried PKCS#8, PKCS#1, SEC1)")
}
