package gitcode

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

type GitCodeSource struct{}

func New() *GitCodeSource {
	return &GitCodeSource{}
}

func (g *GitCodeSource) Name() event.SourceType {
	return event.SourceGitCode
}

func (g *GitCodeSource) RegisterRoutes(mux *http.ServeMux, handler source.EventHandler) {
	mux.HandleFunc("/webhook/gitcode", func(w http.ResponseWriter, r *http.Request) {
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

func (g *GitCodeSource) ParseEventType(r *http.Request, body []byte) string {
	return r.Header.Get("X-GitCode-Event")
}

func (g *GitCodeSource) Normalize(raw json.RawMessage, eventType string) (*event.NormalizedEvent, error) {
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, fmt.Errorf("unmarshal gitcode payload: %w", err)
	}

	evt := &event.NormalizedEvent{
		ID:         fmt.Sprintf("gitcode-%d", time.Now().UnixNano()),
		Source:     event.SourceGitCode,
		Timestamp:  time.Now(),
		Payload:    payload,
		RawPayload: raw,
	}

	evt.Type = mapGitCodeEvent(eventType)

	if repo, ok := payload["repository"].(map[string]any); ok {
		if fullName, ok := repo["full_name"].(string); ok {
			evt.Repository = fullName
		} else if path, ok := repo["path_with_namespace"].(string); ok {
			evt.Repository = path
		}
	}

	if project, ok := payload["project"].(map[string]any); ok {
		if path, ok := project["path_with_namespace"].(string); ok {
			evt.Repository = path
		}
	}

	if ref, ok := payload["ref"].(string); ok {
		evt.Branch = strings.TrimPrefix(ref, "refs/heads/")
	}

	if user, ok := payload["user_name"].(string); ok {
		evt.Actor = user
	}
	if user, ok := payload["user"].(map[string]any); ok {
		if name, ok := user["username"].(string); ok {
			evt.Actor = name
		}
	}
	if sender, ok := payload["sender"].(map[string]any); ok {
		if login, ok := sender["username"].(string); ok {
			evt.Actor = login
		}
	}
	if issue, ok := payload["issue"].(map[string]any); ok {
		if author, ok := issue["author"].(map[string]any); ok {
			if login, ok := author["username"].(string); ok {
				if evt.Actor == "" {
					evt.Actor = login
				}
			}
		}
	}
	if pr, ok := payload["pull_request"].(map[string]any); ok {
		if author, ok := pr["author"].(map[string]any); ok {
			if login, ok := author["username"].(string); ok {
				if evt.Actor == "" {
					evt.Actor = login
				}
			}
		}
		if src, ok := pr["source_branch"].(string); ok {
			evt.Branch = src
		}
		if target, ok := pr["target_branch"].(string); ok {
			if evt.Branch == "" {
				evt.Branch = target
			}
		}
	}

	return evt, nil
}

func (g *GitCodeSource) VerifySignature(r *http.Request, secret string) error {
	if secret == "" {
		return nil
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		return fmt.Errorf("read body: %w", err)
	}
	r.Body = io.NopCloser(strings.NewReader(string(body)))

	sig := r.Header.Get("X-GitCode-Signature")
	if sig == "" {
		token := r.Header.Get("X-GitCode-Token")
		if token != "" {
			if hmac.Equal([]byte(token), []byte(secret)) {
				return nil
			}
			return fmt.Errorf("token verification failed")
		}
		return fmt.Errorf("missing signature/token header")
	}

	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	expected := "sha256=" + hex.EncodeToString(mac.Sum(nil))
	if !hmac.Equal([]byte(sig), []byte(expected)) {
		return fmt.Errorf("signature verification failed")
	}
	return nil
}

func mapGitCodeEvent(gcEvent string) event.EventType {
	mapping := map[string]event.EventType{
		"push":                 event.EventPush,
		"merge_request":        event.EventPullRequestOpened,
		"issues":               event.EventIssueOpened,
		"issue":                event.EventIssueOpened,
		"issue_comment":        event.EventIssueComment,
		"note":                 event.EventIssueComment,
		"pull_request":         event.EventPullRequestOpened,
		"tag_push":             event.EventTagCreated,
		"release":              event.EventReleaseCreated,
		"pipeline":             event.EventBuildStart,
	}
	if mapped, ok := mapping[gcEvent]; ok {
		return mapped
	}
	return event.EventType(gcEvent)
}
