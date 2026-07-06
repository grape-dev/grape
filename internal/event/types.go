package event

import (
	"encoding/json"
	"time"
)

type SourceType string

const (
	SourceGitHub  SourceType = "github"
	SourceGitLab  SourceType = "gitlab"
	SourceGitCode SourceType = "gitcode"
	SourceJenkins SourceType = "jenkins"
	SourceSpanner SourceType = "spanner"
)

type EventType string

const (
	EventPush               EventType = "push"
	EventPullRequestOpened  EventType = "pull_request_opened"
	EventPullRequestUpdated EventType = "pull_request_updated"
	EventPullRequestClosed  EventType = "pull_request_closed"
	EventPullRequestMerged  EventType = "pull_request_merged"
	EventIssueOpened        EventType = "issue_opened"
	EventIssueComment       EventType = "issue_comment"
	EventBuildStart         EventType = "build_start"
	EventBuildSuccess       EventType = "build_success"
	EventBuildFailure       EventType = "build_failure"
	EventDeployStart        EventType = "deploy_start"
	EventDeploySuccess      EventType = "deploy_success"
	EventDeployFailure      EventType = "deploy_failure"
	EventReleaseCreated     EventType = "release_created"
	EventTagCreated         EventType = "tag_created"
)

type NormalizedEvent struct {
	ID         string                 `json:"id"`
	Source     SourceType             `json:"source"`
	Type       EventType              `json:"type"`
	Timestamp  time.Time              `json:"timestamp"`
	Repository string                 `json:"repository"`
	Branch     string                 `json:"branch"`
	Commit     string                 `json:"commit"`
	Actor      string                 `json:"actor"`
	Payload    map[string]any         `json:"payload"`
	RawPayload json.RawMessage        `json:"raw_payload"`
}
