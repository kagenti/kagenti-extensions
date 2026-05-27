package jwtvalidation

import "github.com/kagenti/kagenti-extensions/authbridge/authlib/plugins/jwtvalidation/validation"

// claimsIdentity adapts a *validation.Claims to the pipeline.Identity
// interface that Context exposes. Kept in the plugin so the pipeline
// package doesn't import any validation-specific types.
//
// A nil *validation.Claims would cause NPEs on the accessor methods,
// so jwt-validation only wraps non-nil Claims.
type claimsIdentity struct {
	c *validation.Claims
}

func (i claimsIdentity) Subject() string {
	if i.c == nil {
		return ""
	}
	return i.c.Subject
}

func (i claimsIdentity) ClientID() string {
	if i.c == nil {
		return ""
	}
	return i.c.ClientID
}

func (i claimsIdentity) Scopes() []string {
	if i.c == nil {
		return nil
	}
	return i.c.Scopes
}

func (i claimsIdentity) Username() string {
	if i.c == nil {
		return ""
	}
	if v, ok := i.c.Extra["preferred_username"]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}
