package source

import (
	"encoding/json"
	"net/http"

	"github.com/devops-bot/devops-bot/internal/event"
)

type Source interface {
	Name() event.SourceType
	RegisterRoutes(mux *http.ServeMux, handler EventHandler)
	Normalize(raw json.RawMessage, eventType string) (*event.NormalizedEvent, error)
	VerifySignature(r *http.Request, secret string) error
	ParseEventType(r *http.Request, body []byte) string
}

type EventHandler func(evt *event.NormalizedEvent)
