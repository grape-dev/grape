package config

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

type RulesConfig struct {
	Rules []Rule `yaml:"rules"`
}

type Rule struct {
	Name           string        `yaml:"name"`
	Match          MatchConfig   `yaml:"match"`
	Agent          string        `yaml:"agent"`
	PromptTemplate string        `yaml:"prompt_template"`
	Workspace      WorkspaceRule `yaml:"workspace"`
}

type MatchConfig struct {
	Source []string `yaml:"source"`
	Event  []string `yaml:"event"`
}

type WorkspaceRule struct {
	Clone  bool   `yaml:"clone"`
	Branch string `yaml:"branch"`
}

func LoadRules(path string) (*RulesConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read rules file: %w", err)
	}
	var rules RulesConfig
	if err := yaml.Unmarshal(data, &rules); err != nil {
		return nil, fmt.Errorf("parse rules file: %w", err)
	}
	return &rules, nil
}
