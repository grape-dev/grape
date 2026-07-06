package agent

import (
	"context"
	"time"
)

type Adapter interface {
	Name() string
	Execute(ctx context.Context, input *Input) (*Output, error)
	IsAvailable() bool
}

type Input struct {
	Prompt    string            `json:"prompt"`
	Workspace string            `json:"workspace"`
	SkillsDir string            `json:"skills_dir"`
	Env       map[string]string `json:"env"`
	Timeout   time.Duration     `json:"timeout"`
	SessionID string            `json:"session_id"`
}

type Output struct {
	ExitCode int           `json:"exit_code"`
	Stdout   string        `json:"stdout"`
	Stderr   string        `json:"stderr"`
	Duration time.Duration `json:"duration"`
	Success  bool          `json:"success"`
}
