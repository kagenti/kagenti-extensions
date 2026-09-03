// lite-tags emits the CSV of `exclude_plugin_*` build tags that define the
// authbridge-lite variant. Walks plugins_*.go under cmd/authbridge-proxy,
// finds every default-on plugin (`//go:build !exclude_plugin_<name>`), and
// prints an exclude tag for each one not in liteKeep.
//
// New default-on plugins are excluded from lite automatically. To keep one
// in lite, add its build-tag suffix to liteKeep.
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// liteKeep names plugins that stay in the lite build. Keys are the exact
// build-tag suffix, which may differ from the plugin's registered name
// (e.g. `litellm_budgettrack` vs `litellm-budget-track`).
var liteKeep = map[string]bool{
	"jwtvalidation":       true,
	"tokenexchange":       true,
	"litellm_budgettrack": true,
	"staticinject":        true,
}

// Path is relative to this script's module dir. Run via
// `go run ./authbridge/scripts/lite-tags` (authbridge/ with go.work) or
// `cd authbridge/scripts/lite-tags && go run .`.
const pluginsDir = "../../cmd/authbridge-proxy"

var buildTagPattern = regexp.MustCompile(`^//go:build !exclude_plugin_(\S+)$`)

func main() {
	tags, err := discover(pluginsDir)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Println(strings.Join(tags, ","))
}

func discover(dir string) ([]string, error) {
	matches, err := filepath.Glob(filepath.Join(dir, "plugins_*.go"))
	if err != nil {
		return nil, fmt.Errorf("glob %s: %w", dir, err)
	}
	if len(matches) == 0 {
		return nil, fmt.Errorf("no plugins_*.go files under %s", dir)
	}

	var tags []string
	for _, path := range matches {
		name, err := extractExcludeSuffix(path)
		if err != nil {
			return nil, err
		}
		if name == "" || liteKeep[name] {
			continue
		}
		tags = append(tags, "exclude_plugin_"+name)
	}
	sort.Strings(tags)
	return tags, nil
}

// extractExcludeSuffix returns the suffix of a `!exclude_plugin_*` directive,
// or "" if the file has none (e.g. opt-in `include_plugin_*` plugins).
func extractExcludeSuffix(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("read %s: %w", path, err)
	}
	for _, line := range strings.Split(string(data), "\n") {
		if m := buildTagPattern.FindStringSubmatch(strings.TrimSpace(line)); m != nil {
			return m[1], nil
		}
	}
	return "", nil
}
