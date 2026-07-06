package failure

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/devops-bot/devops-bot/internal/agent"
	"github.com/devops-bot/devops-bot/internal/prompt"
	"github.com/devops-bot/devops-bot/internal/session"
)

type Handler struct {
	agentRegistry *agent.Registry
	promptBuilder *prompt.Builder
	sessionMgr   *session.Manager
	maxRetries   int
}

func NewHandler(
	agentRegistry *agent.Registry,
	promptBuilder *prompt.Builder,
	sessionMgr *session.Manager,
	maxRetries int,
) *Handler {
	if maxRetries == 0 {
		maxRetries = 1
	}
	return &Handler{
		agentRegistry: agentRegistry,
		promptBuilder: promptBuilder,
		sessionMgr:   sessionMgr,
		maxRetries:   maxRetries,
	}
}

func (h *Handler) Handle(ctx context.Context, sess *session.Session, record *session.EventRecord) error {
	if sess.RetryCount >= h.maxRetries {
		slog.Error("session exceeded max retries, giving up",
			"session_id", sess.ID,
			"repo_key", sess.RepoKey,
			"retry_count", sess.RetryCount,
			"max_retries", h.maxRetries,
		)
		return fmt.Errorf("exceeded max retries (%d) for session %s", h.maxRetries, sess.ID)
	}

	agentAdapter, err := h.agentRegistry.Get(sess.AgentName)
	if err != nil {
		return fmt.Errorf("get agent %s: %w", sess.AgentName, err)
	}

	var evt *session.EventRecord
	if record != nil {
		evt = record
	} else if sess.CurrentEvent != nil {
		evt = sess.CurrentEvent
	} else if len(sess.EventHistory) > 0 {
		evt = &sess.EventHistory[len(sess.EventHistory)-1]
	} else {
		return fmt.Errorf("no event to retry in session %s", sess.ID)
	}

	failurePrompt := h.promptBuilder.BuildFailurePrompt(
		evt.Event,
		sess.FailReason,
		evt.Stderr,
	)

	slog.Info("delegating failure analysis to agent",
		"session_id", sess.ID,
		"repo_key", sess.RepoKey,
		"agent", sess.AgentName,
		"retry", sess.RetryCount+1,
	)

	input := &agent.Input{
		Prompt:    sess.ContextSummary() + "\n\n=== Failure Analysis Request ===\n" + failurePrompt,
		Workspace: sess.Workspace,
		Timeout:   5 * time.Minute,
		SessionID: sess.ID,
	}

	output, err := agentAdapter.Execute(ctx, input)
	if err != nil {
		slog.Error("agent failed to analyze failure",
			"session_id", sess.ID,
			"error", err,
		)
		return fmt.Errorf("agent failure analysis failed: %w", err)
	}

	if output.Success {
		slog.Info("agent successfully analyzed and resolved failure",
			"session_id", sess.ID,
			"agent", sess.AgentName,
		)
		return h.sessionMgr.RecordSuccess(ctx, sess.ID, &session.EventRecord{
			ID:          fmt.Sprintf("retry-%d", time.Now().UnixNano()),
			Event:       evt.Event,
			Prompt:      failurePrompt,
			Stdout:      output.Stdout,
			Stderr:      output.Stderr,
			ExitCode:    output.ExitCode,
			Success:     true,
			Duration:    output.Duration,
			ProcessedAt: time.Now(),
		})
	}

	slog.Warn("agent could not resolve failure",
		"session_id", sess.ID,
		"agent", sess.AgentName,
		"exit_code", output.ExitCode,
	)
	return h.sessionMgr.RecordFailure(ctx, sess.ID, &session.EventRecord{
		ID:          fmt.Sprintf("retry-%d", time.Now().UnixNano()),
		Event:       evt.Event,
		Prompt:      failurePrompt,
		Stdout:      output.Stdout,
		Stderr:      output.Stderr,
		ExitCode:    output.ExitCode,
		Success:     false,
		Duration:    output.Duration,
		ProcessedAt: time.Now(),
	}, "agent self-analysis failed")
}
