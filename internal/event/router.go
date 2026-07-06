package event

import (
	"fmt"

	"github.com/devops-bot/devops-bot/internal/config"
)

type Router struct {
	rules []RouteRule
}

type RouteRule struct {
	Name           string
	MatchSource    []string
	MatchEvent     []string
	Agent          string
	PromptTemplate string
	Workspace      config.WorkspaceRule
}

type RouteResult struct {
	Rule    *RouteRule
	Matched bool
}

func NewRouter(rulesCfg *config.RulesConfig) *Router {
	var rules []RouteRule
	for _, r := range rulesCfg.Rules {
		rules = append(rules, RouteRule{
			Name:           r.Name,
			MatchSource:    r.Match.Source,
			MatchEvent:     r.Match.Event,
			Agent:          r.Agent,
			PromptTemplate: r.PromptTemplate,
			Workspace:      r.Workspace,
		})
	}
	return &Router{rules: rules}
}

func (r *Router) Match(evt *NormalizedEvent) *RouteResult {
	for i := range r.rules {
		rule := &r.rules[i]
		if !matchSource(rule, string(evt.Source)) {
			continue
		}
		if !matchEvent(rule, string(evt.Type)) {
			continue
		}
		return &RouteResult{
			Rule:    rule,
			Matched: true,
		}
	}
	return &RouteResult{Matched: false}
}

func matchSource(rule *RouteRule, source string) bool {
	if len(rule.MatchSource) == 0 {
		return true
	}
	for _, s := range rule.MatchSource {
		if s == source {
			return true
		}
	}
	return false
}

func matchEvent(rule *RouteRule, eventType string) bool {
	if len(rule.MatchEvent) == 0 {
		return true
	}
	for _, e := range rule.MatchEvent {
		if e == eventType {
			return true
		}
	}
	return false
}

func (r *Router) Reload(rulesCfg *config.RulesConfig) {
	var rules []RouteRule
	for _, rule := range rulesCfg.Rules {
		rules = append(rules, RouteRule{
			Name:           rule.Name,
			MatchSource:    rule.Match.Source,
			MatchEvent:     rule.Match.Event,
			Agent:          rule.Agent,
			PromptTemplate: rule.PromptTemplate,
			Workspace:      rule.Workspace,
		})
	}
	r.rules = rules
}

func (r *Router) RuleCount() int {
	return len(r.rules)
}

func (r *Router) Summary() string {
	return fmt.Sprintf("router: %d rules loaded", len(r.rules))
}
