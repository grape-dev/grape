package spanner

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/devops-bot/devops-bot/internal/event"
	"github.com/devops-bot/devops-bot/internal/source"
)

type SpannerSource struct{}

func New() *SpannerSource {
	return &SpannerSource{}
}

func (s *SpannerSource) Name() event.SourceType {
	return event.SourceSpanner
}

func (s *SpannerSource) RegisterRoutes(mux *http.ServeMux, handler source.EventHandler) {
	mux.HandleFunc("/webhook/spanner", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "read body failed", http.StatusBadRequest)
			return
		}
		eventType := s.ParseEventType(r, body)
		var raw json.RawMessage = body
		evt, err := s.Normalize(raw, eventType)
		if err != nil {
			http.Error(w, fmt.Sprintf("normalize failed: %v", err), http.StatusBadRequest)
			return
		}
		handler(evt)
		w.WriteHeader(http.StatusOK)
	})
}

func (s *SpannerSource) ParseEventType(r *http.Request, body []byte) string {
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		return "unknown"
	}
	if action, ok := payload["action"].(string); ok {
		return action
	}
	if eventType, ok := payload["event_type"].(string); ok {
		return eventType
	}
	return "notification"
}

func (s *SpannerSource) Normalize(raw json.RawMessage, eventType string) (*event.NormalizedEvent, error) {
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, fmt.Errorf("unmarshal spanner payload: %w", err)
	}

	evt := &event.NormalizedEvent{
		ID:         fmt.Sprintf("spanner-%d", time.Now().UnixNano()),
		Source:     event.SourceSpanner,
		Timestamp:  time.Now(),
		Payload:    payload,
		RawPayload: raw,
	}

	evt.Type = mapSpannerEvent(eventType)

	if repo, ok := payload["repository"].(string); ok {
		evt.Repository = repo
	}
	if project, ok := payload["project"].(string); ok {
		if evt.Repository == "" {
			evt.Repository = project
		}
	}
	if branch, ok := payload["branch"].(string); ok {
		evt.Branch = branch
	}
	if commit, ok := payload["commit"].(string); ok {
		evt.Commit = commit
	}
	if actor, ok := payload["actor"].(string); ok {
		evt.Actor = actor
	}
	if build, ok := payload["build"].(map[string]any); ok {
		if status, ok := build["status"].(string); ok {
			evt.Branch = status
		}
		if url, ok := build["url"].(string); ok {
			evt.Commit = url
		}
	}

	return evt, nil
}

func (s *SpannerSource) VerifySignature(r *http.Request, secret string) error {
	if secret == "" {
		return nil
	}
	token := r.Header.Get("X-Spanner-Token")
	if token == "" {
		token = r.URL.Query().Get("token")
	}
	if token != secret {
		return fmt.Errorf("spanner token verification failed")
	}
	return nil
}

func mapSpannerEvent(action string) event.EventType {
	switch action {
	case "build_start":
		return event.EventBuildStart
	case "build_success":
		return event.EventBuildSuccess
	case "build_failure":
		return event.EventBuildFailure
	case "deploy_start":
		return event.EventDeployStart
	case "deploy_success":
		return event.EventDeploySuccess
	case "deploy_failure":
		return event.EventDeployFailure
	default:
		return event.EventType("spanner_" + action)
	}
}
