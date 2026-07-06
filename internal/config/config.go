package config

import (
	"fmt"
	"os"
	"time"

	"gopkg.in/yaml.v3"
)

type Config struct {
	Server   ServerConfig            `yaml:"server"`
	Agents   map[string]AgentConfig  `yaml:"agents"`
	Session  SessionConfig           `yaml:"session"`
	Workspace WorkspaceConfig        `yaml:"workspace"`
	Sources  map[string]SourceConfig `yaml:"sources"`
}

type ServerConfig struct {
	Port    int    `yaml:"port"`
	BaseURL string `yaml:"base_url"`
}

type AgentConfig struct {
	Cli       string        `yaml:"cli"`
	Args      []string      `yaml:"args"`
	Timeout   time.Duration `yaml:"timeout"`
	Available bool          `yaml:"available"`
}

type SessionConfig struct {
	Store         string        `yaml:"store"`
	DBPath        string        `yaml:"db_path"`
	MaxConcurrent int           `yaml:"max_concurrent"`
	DefaultTimeout time.Duration `yaml:"default_timeout"`
}

type WorkspaceConfig struct {
	BaseDir   string `yaml:"base_dir"`
	AutoClone bool   `yaml:"auto_clone"`
}

type SourceConfig struct {
	Secret     string `yaml:"secret"`
	SecretEnv  string `yaml:"secret_env"`
}

func (s *SourceConfig) ResolvedSecret() string {
	if s.SecretEnv != "" {
		if val := os.Getenv(s.SecretEnv); val != "" {
			return val
		}
	}
	return s.Secret
}

func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config file: %w", err)
	}
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse config file: %w", err)
	}
	if cfg.Server.Port == 0 {
		cfg.Server.Port = 8080
	}
	if cfg.Session.MaxConcurrent == 0 {
		cfg.Session.MaxConcurrent = 10
	}
	if cfg.Session.DefaultTimeout == 0 {
		cfg.Session.DefaultTimeout = 10 * time.Minute
	}
	if cfg.Session.Store == "" {
		cfg.Session.Store = "sqlite"
	}
	if cfg.Session.DBPath == "" {
		cfg.Session.DBPath = "./data/sessions.db"
	}
	if cfg.Workspace.BaseDir == "" {
		cfg.Workspace.BaseDir = "./data/workspaces"
	}
	return &cfg, nil
}
