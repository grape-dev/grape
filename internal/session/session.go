package session

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/devops-bot/devops-bot/internal/event"
)

type Status string

const (
	StatusIdle    Status = "idle"
	StatusActive  Status = "active"
	StatusFailed  Status = "failed"
)

type EventRecord struct {
	ID        string                  `json:"id"`
	Event     *event.NormalizedEvent  `json:"event"`
	Prompt    string                  `json:"prompt"`
	Stdout    string                  `json:"stdout"`
	Stderr    string                  `json:"stderr"`
	ExitCode  int                     `json:"exit_code"`
	Success   bool                    `json:"success"`
	Duration  time.Duration           `json:"duration"`
	ProcessedAt time.Time             `json:"processed_at"`
}

type Session struct {
	ID           string            `json:"id"`
	RepoKey      string            `json:"repo_key"`
	Source       event.SourceType  `json:"source"`
	Repository   string            `json:"repository"`
	AgentName    string            `json:"agent_name"`
	Status       Status            `json:"status"`
	Workspace    string            `json:"workspace"`
	EventHistory []EventRecord     `json:"event_history"`
	CurrentEvent *EventRecord      `json:"current_event,omitempty"`
	CreatedAt    time.Time         `json:"created_at"`
	LastActiveAt time.Time         `json:"last_active_at"`
	RetryCount   int               `json:"retry_count"`
	FailReason   string            `json:"fail_reason,omitempty"`
}

func RepoKey(source event.SourceType, repository string) string {
	return fmt.Sprintf("%s:%s", source, repository)
}

func (s *Session) ToJSON() ([]byte, error) {
	return json.Marshal(s)
}

func (s *Session) FromJSON(data []byte) error {
	return json.Unmarshal(data, s)
}

func (s *Session) ContextSummary() string {
	summary := fmt.Sprintf("Session %s (repo: %s, source: %s)\n", s.ID, s.Repository, s.Source)
	summary += fmt.Sprintf("Created: %s, Last active: %s\n", s.CreatedAt.Format("2006-01-02 15:04:05"), s.LastActiveAt.Format("2006-01-02 15:04:05"))
	summary += fmt.Sprintf("Events processed: %d\n", len(s.EventHistory))
	if len(s.EventHistory) > 0 {
		summary += "\n=== Recent Event History ===\n"
		start := 0
		if len(s.EventHistory) > 10 {
			start = len(s.EventHistory) - 10
		}
		for i := start; i < len(s.EventHistory); i++ {
			r := s.EventHistory[i]
			status := "success"
			if !r.Success {
				status = "failed"
			}
			summary += fmt.Sprintf("- [%s] %s/%s (%s) at %s\n", status, r.Event.Source, r.Event.Type, r.Event.Repository, r.ProcessedAt.Format("2006-01-02 15:04:05"))
		}
	}
	return summary
}

type Store interface {
	Create(ctx context.Context, s *Session) error
	Update(ctx context.Context, s *Session) error
	Get(ctx context.Context, id string) (*Session, error)
	GetByRepoKey(ctx context.Context, repoKey string) (*Session, error)
	ListByStatus(ctx context.Context, status Status) ([]*Session, error)
	ListRecent(ctx context.Context, limit int) ([]*Session, error)
	Close() error
}
