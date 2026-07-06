package session

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/devops-bot/devops-bot/internal/event"
)

type Manager struct {
	store         Store
	mu            sync.Mutex
	semaphore     chan struct{}
	maxConcurrent int
}

func NewManager(store Store, maxConcurrent int) *Manager {
	return &Manager{
		store:         store,
		semaphore:     make(chan struct{}, maxConcurrent),
		maxConcurrent: maxConcurrent,
	}
}

func (m *Manager) GetOrCreate(ctx context.Context, evt *event.NormalizedEvent, agentName string, workspace string) (*Session, bool, error) {
	repoKey := RepoKey(evt.Source, evt.Repository)

	existing, err := m.store.GetByRepoKey(ctx, repoKey)
	if err == nil && existing != nil {
		return existing, false, nil
	}

	sess := &Session{
		ID:           generateID(repoKey),
		RepoKey:      repoKey,
		Source:       evt.Source,
		Repository:   evt.Repository,
		AgentName:    agentName,
		Status:       StatusIdle,
		Workspace:    workspace,
		EventHistory: []EventRecord{},
		CreatedAt:    time.Now(),
		LastActiveAt: time.Now(),
	}
	if err := m.store.Create(ctx, sess); err != nil {
		return nil, false, fmt.Errorf("store session: %w", err)
	}
	return sess, true, nil
}

func (m *Manager) Acquire(ctx context.Context) error {
	select {
	case m.semaphore <- struct{}{}:
		return nil
	case <-ctx.Done():
		return fmt.Errorf("context cancelled while waiting for slot: %w", ctx.Err())
	}
}

func (m *Manager) Release() {
	<-m.semaphore
}

func (m *Manager) SetActive(ctx context.Context, id string, currentEvent *EventRecord) error {
	s, err := m.store.Get(ctx, id)
	if err != nil {
		return err
	}
	s.Status = StatusActive
	s.CurrentEvent = currentEvent
	s.LastActiveAt = time.Now()
	return m.store.Update(ctx, s)
}

func (m *Manager) RecordSuccess(ctx context.Context, id string, record *EventRecord) error {
	s, err := m.store.Get(ctx, id)
	if err != nil {
		return err
	}
	s.EventHistory = append(s.EventHistory, *record)
	s.CurrentEvent = nil
	s.Status = StatusIdle
	s.LastActiveAt = time.Now()
	s.FailReason = ""
	return m.store.Update(ctx, s)
}

func (m *Manager) RecordFailure(ctx context.Context, id string, record *EventRecord, failReason string) error {
	s, err := m.store.Get(ctx, id)
	if err != nil {
		return err
	}
	s.EventHistory = append(s.EventHistory, *record)
	s.CurrentEvent = nil
	s.Status = StatusFailed
	s.LastActiveAt = time.Now()
	s.FailReason = failReason
	s.RetryCount++
	return m.store.Update(ctx, s)
}

func (m *Manager) Get(ctx context.Context, id string) (*Session, error) {
	return m.store.Get(ctx, id)
}

func (m *Manager) GetByRepoKey(ctx context.Context, repoKey string) (*Session, error) {
	return m.store.GetByRepoKey(ctx, repoKey)
}

func (m *Manager) ListByStatus(ctx context.Context, status Status) ([]*Session, error) {
	return m.store.ListByStatus(ctx, status)
}

func (m *Manager) ListRecent(ctx context.Context, limit int) ([]*Session, error) {
	return m.store.ListRecent(ctx, limit)
}

func generateID(repoKey string) string {
	return fmt.Sprintf("sess-%s-%d", repoKey, time.Now().UnixNano())
}
