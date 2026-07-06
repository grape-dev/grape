package github

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/devops-bot/devops-bot/internal/event"
	"github.com/devops-bot/devops-bot/internal/source"
)

type GitHubSource struct{}

func New() *GitHubSource {
	return &GitHubSource{}
}

func (g *GitHubSource) Name() event.SourceType {
	return event.SourceGitHub
}

func (g *GitHubSource) RegisterRoutes(mux *http.ServeMux, handler source.EventHandler) {
	mux.HandleFunc("/webhook/github", func(w http.ResponseWriter, r *http.Request) {
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

func (g *GitHubSource) ParseEventType(r *http.Request, body []byte) string {
	return r.Header.Get("X-GitHub-Event")
}

func (g *GitHubSource) Normalize(raw json.RawMessage, eventType string) (*event.NormalizedEvent, error) {
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, fmt.Errorf("unmarshal github payload: %w", err)
	}

	evt := &event.NormalizedEvent{
		ID:         fmt.Sprintf("github-%d", time.Now().UnixNano()),
		Source:     event.SourceGitHub,
		Timestamp:  time.Now(),
		Payload:    payload,
		RawPayload: raw,
	}

	evt.Type = mapGitHubEvent(eventType)

	if repo, ok := payload["repository"].(map[string]any); ok {
		if fullName, ok := repo["full_name"].(string); ok {
			evt.Repository = fullName
		}
	}

	if ref, ok := payload["ref"].(string); ok {
		evt.Branch = strings.TrimPrefix(ref, "refs/heads/")
	}

	if headCommit, ok := payload["head_commit"].(map[string]any); ok {
		if id, ok := headCommit["id"].(string); ok {
			evt.Commit = id
		}
		if author, ok := headCommit["author"].(map[string]any); ok {
			if name, ok := author["username"].(string); ok {
				evt.Actor = name
			}
		}
	}

	if sender, ok := payload["sender"].(map[string]any); ok {
		if login, ok := sender["login"].(string); ok {
			evt.Actor = login
		}
	}

	if pr, ok := payload["pull_request"].(map[string]any); ok {
		if base, ok := pr["base"].(map[string]any); ok {
			if ref, ok := base["ref"].(string); ok {
				evt.Branch = ref
			}
			if repo, ok := base["repo"].(map[string]any); ok {
				if fullName, ok := repo["full_name"].(string); ok {
					evt.Repository = fullName
				}
			}
		}
	}

	return evt, nil
}

func (g *GitHubSource) VerifySignature(r *http.Request, secret string) error {
	if secret == "" {
		return nil
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		return fmt.Errorf("read body: %w", err)
	}
	r.Body = io.NopCloser(strings.NewReader(string(body)))

	sig := r.Header.Get("X-Hub-Signature-256")
	if sig == "" {
		return fmt.Errorf("missing signature header")
	}

	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	expected := "sha256=" + hex.EncodeToString(mac.Sum(nil))

	if !hmac.Equal([]byte(sig), []byte(expected)) {
		return fmt.Errorf("signature verification failed")
	}
	return nil
}

func mapGitHubEvent(ghEvent string) event.EventType {
	mapping := map[string]event.EventType{
		"push":                      event.EventPush,
		"pull_request":              event.EventPullRequestOpened,
		"issues":                    event.EventIssueOpened,
		"issue_comment":             event.EventIssueComment,
		"release":                   event.EventReleaseCreated,
		"create":                    event.EventTagCreated,
	}
	if mapped, ok := mapping[ghEvent]; ok {
		return mapped
	}
	return event.EventType(ghEvent)
}
