package jenkins

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/devops-bot/devops-bot/internal/event"
	"github.com/devops-bot/devops-bot/internal/source"
)

type JenkinsSource struct{}

func New() *JenkinsSource {
	return &JenkinsSource{}
}

func (j *JenkinsSource) Name() event.SourceType {
	return event.SourceJenkins
}

func (j *JenkinsSource) RegisterRoutes(mux *http.ServeMux, handler source.EventHandler) {
	mux.HandleFunc("/webhook/jenkins", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "read body failed", http.StatusBadRequest)
			return
		}
		eventType := j.ParseEventType(r, body)
		var raw json.RawMessage = body
		evt, err := j.Normalize(raw, eventType)
		if err != nil {
			http.Error(w, fmt.Sprintf("normalize failed: %v", err), http.StatusBadRequest)
			return
		}
		handler(evt)
		w.WriteHeader(http.StatusOK)
	})
}

func (j *JenkinsSource) ParseEventType(r *http.Request, body []byte) string {
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		return "unknown"
	}
	if status, ok := payload["build"].(map[string]any); ok {
		if phase, ok := status["phase"].(string); ok {
			return phase
		}
	}
	return "notification"
}

func (j *JenkinsSource) Normalize(raw json.RawMessage, eventType string) (*event.NormalizedEvent, error) {
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, fmt.Errorf("unmarshal jenkins payload: %w", err)
	}

	evt := &event.NormalizedEvent{
		ID:         fmt.Sprintf("jenkins-%d", time.Now().UnixNano()),
		Source:     event.SourceJenkins,
		Timestamp:  time.Now(),
		Payload:    payload,
		RawPayload: raw,
	}

	evt.Type = mapJenkinsEvent(eventType)

	if name, ok := payload["name"].(string); ok {
		evt.Repository = name
	}

	if build, ok := payload["build"].(map[string]any); ok {
		if fullURL, ok := build["full_url"].(string); ok {
			evt.Commit = fullURL
		}
		if phase, ok := build["phase"].(string); ok {
			evt.Branch = phase
		}
		if scms, ok := build["scm"].(map[string]any); ok {
			if branch, ok := scms["branch"].(string); ok {
				evt.Branch = branch
			}
			if commit, ok := scms["commit"].(string); ok {
				evt.Commit = commit
			}
		}
	}

	return evt, nil
}

func (j *JenkinsSource) VerifySignature(r *http.Request, secret string) error {
	if secret == "" {
		return nil
	}
	token := r.URL.Query().Get("token")
	if token == "" {
		token = r.Header.Get("X-Jenkins-Token")
	}
	if token != secret {
		return fmt.Errorf("jenkins token verification failed")
	}
	return nil
}

func mapJenkinsEvent(phase string) event.EventType {
	switch phase {
	case "STARTED":
		return event.EventBuildStart
	case "COMPLETED":
		return event.EventBuildSuccess
	case "FINALIZED":
		return event.EventBuildSuccess
	case "FAILURE":
		return event.EventBuildFailure
	default:
		return event.EventType("jenkins_" + phase)
	}
}
