package cli

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"runtime"
	"strings"
	"time"
)

type Executor struct{}

func NewExecutor() *Executor {
	return &Executor{}
}

type Result struct {
	ExitCode int
	Stdout   string
	Stderr   string
	Duration time.Duration
}

func (e *Executor) Run(ctx context.Context, command string, args []string, env map[string]string, dir string) (*Result, error) {
	var cmd *exec.Cmd
	if runtime.GOOS == "windows" && (strings.HasSuffix(strings.ToLower(command), ".cmd") || strings.HasSuffix(strings.ToLower(command), ".bat")) {
		allArgs := append([]string{"/c", command}, args...)
		cmd = exec.CommandContext(ctx, "cmd", allArgs...)
	} else {
		cmd = exec.CommandContext(ctx, command, args...)
	}
	if dir != "" {
		cmd.Dir = dir
	}
	if len(env) > 0 {
		cmd.Env = append(cmd.Environ(), fmt.Sprintf("%s=%s", "DEVOPS_BOT", "1"))
		for k, v := range env {
			cmd.Env = append(cmd.Env, fmt.Sprintf("%s=%s", k, v))
		}
	} else {
		cmd.Env = append(cmd.Environ(), fmt.Sprintf("%s=%s", "DEVOPS_BOT", "1"))
	}

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	start := time.Now()
	err := cmd.Run()
	duration := time.Since(start)

	result := &Result{
		ExitCode: 0,
		Stdout:   stdout.String(),
		Stderr:   stderr.String(),
		Duration: duration,
	}

	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			result.ExitCode = exitErr.ExitCode()
		} else {
			return nil, fmt.Errorf("execute command: %w", err)
		}
	}

	return result, nil
}

func (e *Executor) IsAvailable(command string) bool {
	if runtime.GOOS == "windows" && (strings.HasSuffix(strings.ToLower(command), ".cmd") || strings.HasSuffix(strings.ToLower(command), ".bat")) {
		return true
	}
	path, err := exec.LookPath(command)
	return err == nil && path != ""
}

func BuildArgs(template []string, vars map[string]string) []string {
	result := make([]string, len(template))
	for i, t := range template {
		for k, v := range vars {
			placeholder := "{{." + k + "}}"
			t = strings.ReplaceAll(t, placeholder, v)
		}
		result[i] = t
	}
	return result
}
