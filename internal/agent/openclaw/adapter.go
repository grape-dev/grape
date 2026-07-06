package openclaw

import (
	"context"
	"fmt"
	"time"

	"github.com/devops-bot/devops-bot/internal/agent"
	"github.com/devops-bot/devops-bot/internal/config"
	"github.com/devops-bot/devops-bot/pkg/cli"
)

type Adapter struct {
	cfg     config.AgentConfig
	executor *cli.Executor
}

func NewAdapter(cfg config.AgentConfig) *Adapter {
	return &Adapter{
		cfg:     cfg,
		executor: cli.NewExecutor(),
	}
}

func (a *Adapter) Name() string {
	return "openclaw"
}

func (a *Adapter) Execute(ctx context.Context, input *agent.Input) (*agent.Output, error) {
	timeout := a.cfg.Timeout
	if input.Timeout > 0 {
		timeout = input.Timeout
	}
	if timeout == 0 {
		timeout = 5 * time.Minute
	}

	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	vars := map[string]string{
		"Prompt":    input.Prompt,
		"Workspace": input.Workspace,
		"SessionID": input.SessionID,
		"SkillsDir": input.SkillsDir,
	}
	args := cli.BuildArgs(a.cfg.Args, vars)

	env := make(map[string]string)
	for k, v := range input.Env {
		env[k] = v
	}
	env["DEVOPS_SESSION_ID"] = input.SessionID
	if input.SkillsDir != "" {
		env["DEVOPS_SKILLS_DIR"] = input.SkillsDir
	}

	result, err := a.executor.Run(ctx, a.cfg.Cli, args, env, input.Workspace)
	if err != nil {
		return nil, fmt.Errorf("execute openclaw: %w", err)
	}

	return &agent.Output{
		ExitCode: result.ExitCode,
		Stdout:   result.Stdout,
		Stderr:   result.Stderr,
		Duration: result.Duration,
		Success:  result.ExitCode == 0,
	}, nil
}

func (a *Adapter) IsAvailable() bool {
	return a.cfg.Available && a.executor.IsAvailable(a.cfg.Cli)
}
