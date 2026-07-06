package agent

import (
	"fmt"
	"sync"
)

type Registry struct {
	mu      sync.RWMutex
	agents  map[string]Adapter
}

func NewRegistry() *Registry {
	return &Registry{
		agents: make(map[string]Adapter),
	}
}

func (r *Registry) Register(a Adapter) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.agents[a.Name()] = a
}

func (r *Registry) Get(name string) (Adapter, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	a, ok := r.agents[name]
	if !ok {
		return nil, fmt.Errorf("agent not found: %s", name)
	}
	return a, nil
}

func (r *Registry) List() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	names := make([]string, 0, len(r.agents))
	for name := range r.agents {
		names = append(names, name)
	}
	return names
}

func (r *Registry) Available() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	var names []string
	for name, a := range r.agents {
		if a.IsAvailable() {
			names = append(names, name)
		}
	}
	return names
}
