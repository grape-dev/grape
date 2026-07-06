package gitlab

import (
	"crypto/hmac"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/devops-bot/devops-bot/internal/event"
	"github.com/devops-bot/devops-bot/internal/source"
)

type GitLabSource struct{}

func New() *GitLabSource {
	return &GitLabSource{}
}

func (g *GitLabSource) Name() event.SourceType {
	return event.SourceGitLab
}

func (g *GitLabSource) RegisterRoutes(mux *http.ServeMux, handler source.EventHandler) {
	mux.HandleFunc("/webhook/gitlab", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "read body failed", http.StatusBadRequest)
			return
		}
		eventType := g.ParseEventType(r, body)
		var raw json.RawMessage = body
		evt, err := g.Normalize(raw, eventType)
		if err != nil {
			http.Error(w, fmt.Sprintf("normalize failed: %v", err), http.StatusBadRequest)
			return
		}
		handler(evt)
		w.WriteHeader(http.StatusOK)
	})
}

func (g *GitLabSource) ParseEventType(r *http.Request, body []byte) string {
	return r.Header.Get("X-GitLab-Event")
}

func (g *GitLabSource) Normalize(raw json.RawMessage, eventType string) (*event.NormalizedEvent, error) {
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, fmt.Errorf("unmarshal gitlab payload: %w", err)
	}

	evt := &event.NormalizedEvent{
		ID:         fmt.Sprintf("gitlab-%d", time.Now().UnixNano()),
		Source:     event.SourceGitLab,
		Timestamp:  time.Now(),
		Payload:    payload,
		RawPayload: raw,
	}

	evt.Type = mapGitLabEvent(eventType)

	if project, ok := payload["project"].(map[string]any); ok {
		if path, ok := project["path_with_namespace"].(string); ok {
			evt.Repository = path
		}
	}

	if ref, ok := payload["ref"].(string); ok {
		evt.Branch = strings.TrimPrefix(ref, "refs/heads/")
	}

	if objectAttrs, ok := payload["object_attributes"].(map[string]any); ok {
		if sourceBranch, ok := objectAttrs["source_branch"].(string); ok {
			evt.Branch = sourceBranch
		}
		if lastCommit, ok := objectAttrs["last_commit"].(map[string]any); ok {
			if id, ok := lastCommit["id"].(string); ok {
				evt.Commit = id
			}
		}
	}

	if user, ok := payload["user"].(map[string]any); ok {
		if username, ok := user["username"].(string); ok {
			evt.Actor = username
		}
	}

	return evt, nil
}

func (g *GitLabSource) VerifySignature(r *http.Request, secret string) error {
	if secret == "" {
		return nil
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		return fmt.Errorf("read body: %w", err)
	}
	r.Body = io.NopCloser(strings.NewReader(string(body)))

	token := r.Header.Get("X-GitLab-Token")
	if token == "" {
		return fmt.Errorf("missing token header")
	}
	if !hmac.Equal([]byte(token), []byte(secret)) {
		return fmt.Errorf("token verification failed")
	}
	return nil
}

func mapGitLabEvent(glEvent string) event.EventType {
	mapping := map[string]event.EventType{
		"Push Hook":               event.EventPush,
		"Merge Request Hook":      event.EventPullRequestOpened,
		"Issue Hook":              event.EventIssueOpened,
		"Note Hook":               event.EventIssueComment,
		"Tag Push Hook":           event.EventTagCreated,
		"Release Hook":            event.EventReleaseCreated,
		"Pipeline Hook":           event.EventBuildStart,
		"Job Hook":                event.EventBuildStart,
	}
	if mapped, ok := mapping[glEvent]; ok {
		return mapped
	}
	return event.EventType(glEvent)
}
