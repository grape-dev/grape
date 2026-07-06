package server

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/devops-bot/devops-bot/internal/agent"
	"github.com/devops-bot/devops-bot/internal/config"
	"github.com/devops-bot/devops-bot/internal/event"
	"github.com/devops-bot/devops-bot/internal/failure"
	"github.com/devops-bot/devops-bot/internal/prompt"
	"github.com/devops-bot/devops-bot/internal/session"
	"github.com/devops-bot/devops-bot/internal/source"
	githubsrc "github.com/devops-bot/devops-bot/internal/source/github"
	gitlabsrc "github.com/devops-bot/devops-bot/internal/source/gitlab"
	gitcodesrc "github.com/devops-bot/devops-bot/internal/source/gitcode"
	jenkinssrc "github.com/devops-bot/devops-bot/internal/source/jenkins"
	spannersrc "github.com/devops-bot/devops-bot/internal/source/spanner"
)

type Server struct {
	cfg            *config.Config
	router         *event.Router
	sessionMgr     *session.Manager
	agentRegistry  *agent.Registry
	promptBuilder  *prompt.Builder
	failureHandler *failure.Handler
	skillsDir      string
	sources        map[event.SourceType]source.Source
	httpServer     *http.Server
}

func New(
	cfg *config.Config,
	router *event.Router,
	sessionMgr *session.Manager,
	agentRegistry *agent.Registry,
	promptBuilder *prompt.Builder,
	failureHandler *failure.Handler,
	skillsDir string,
) *Server {
	s := &Server{
		cfg:            cfg,
		router:         router,
		sessionMgr:     sessionMgr,
		agentRegistry:  agentRegistry,
		promptBuilder:  promptBuilder,
		failureHandler: failureHandler,
		skillsDir:      skillsDir,
		sources:        make(map[event.SourceType]source.Source),
	}

	s.registerSources()
	return s
}

func (s *Server) registerSources() {
	s.sources[event.SourceGitHub] = githubsrc.New()
	s.sources[event.SourceGitLab] = gitlabsrc.New()
	s.sources[event.SourceGitCode] = gitcodesrc.New()
	s.sources[event.SourceJenkins] = jenkinssrc.New()
	s.sources[event.SourceSpanner] = spannersrc.New()
}

func (s *Server) Start(ctx context.Context) error {
	mux := http.NewServeMux()

	eventHandler := func(evt *event.NormalizedEvent) {
		go s.processEvent(ctx, evt)
	}

	for _, src := range s.sources {
		src.RegisterRoutes(mux, eventHandler)
		slog.Info("registered source", "source", src.Name())
	}

	mux.HandleFunc("/health", s.healthHandler)
	mux.HandleFunc("/api/sessions", s.sessionsHandler)

	s.httpServer = &http.Server{
		Addr:         fmt.Sprintf(":%d", s.cfg.Server.Port),
		Handler:      loggingMiddleware(mux),
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 30 * time.Second,
	}

	slog.Info("starting server", "port", s.cfg.Server.Port)
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		s.httpServer.Shutdown(shutdownCtx)
	}()

	return s.httpServer.ListenAndServe()
}

func (s *Server) processEvent(ctx context.Context, evt *event.NormalizedEvent) {
	slog.Info("processing event",
		"source", evt.Source,
		"type", evt.Type,
		"repo", evt.Repository,
		"branch", evt.Branch,
	)

	result := s.router.Match(evt)
	if !result.Matched {
		slog.Info("no matching rule for event", "source", evt.Source, "type", evt.Type)
		return
	}

	agentName := result.Rule.Agent

	workspace := s.cfg.Workspace.BaseDir
	if result.Rule.Workspace.Clone && evt.Repository != "" {
		workspace = workspace + "/" + evt.Repository
	}

	if err := os.MkdirAll(workspace, 0755); err != nil {
		slog.Error("create workspace dir failed", "error", err, "workspace", workspace)
	}

	sess, isNew, err := s.sessionMgr.GetOrCreate(ctx, evt, agentName, workspace)
	if err != nil {
		slog.Error("get or create session failed", "error", err)
		return
	}

	if isNew {
		slog.Info("new session created for repo",
			"session_id", sess.ID,
			"repo_key", sess.RepoKey,
			"agent", agentName,
		)
	} else {
		slog.Info("reusing existing session for repo",
			"session_id", sess.ID,
			"repo_key", sess.RepoKey,
			"events_history", len(sess.EventHistory),
		)
	}

	promptText, err := s.promptBuilder.Build(evt, result.Rule.PromptTemplate)
	if err != nil {
		slog.Error("build prompt failed", "error", err)
		return
	}

	contextPrompt := s.buildAgentPrompt(sess, evt, result.Rule, promptText)

	if err := s.sessionMgr.Acquire(ctx); err != nil {
		slog.Error("acquire concurrency slot failed", "error", err)
		return
	}
	defer s.sessionMgr.Release()

	currentRecord := &session.EventRecord{
		ID:      fmt.Sprintf("evt-%d", time.Now().UnixNano()),
		Event:   evt,
		Prompt:  promptText,
	}

	if err := s.sessionMgr.SetActive(ctx, sess.ID, currentRecord); err != nil {
		slog.Error("set session active failed", "error", err)
		return
	}

	agentAdapter, err := s.agentRegistry.Get(agentName)
	if err != nil {
		slog.Error("get agent failed", "agent", agentName, "error", err)
		return
	}

	input := &agent.Input{
		Prompt:    contextPrompt,
		Workspace: workspace,
		SkillsDir: s.skillsDir,
		Timeout:   s.cfg.Session.DefaultTimeout,
		SessionID: sess.ID,
		Env:       s.buildAgentEnv(evt),
	}

	slog.Info("agent input",
		"session_id", sess.ID,
		"agent", agentName,
		"prompt_len", len(contextPrompt),
		"workspace", workspace,
		"skills_dir", s.skillsDir,
	)
	s.logAgentInteraction(sess.ID, "prompt", contextPrompt)

	output, err := agentAdapter.Execute(ctx, input)
	if err != nil {
		slog.Error("agent execution failed", "session_id", sess.ID, "error", err)
		s.logAgentInteraction(sess.ID, "error", err.Error())
		currentRecord.Success = false
		currentRecord.Stderr = err.Error()
		_ = s.sessionMgr.RecordFailure(ctx, sess.ID, currentRecord, err.Error())
		s.handleFailure(ctx, sess, currentRecord)
		return
	}

	currentRecord.Stdout = output.Stdout
	currentRecord.Stderr = output.Stderr
	currentRecord.ExitCode = output.ExitCode
	currentRecord.Duration = output.Duration
	currentRecord.Success = output.Success
	currentRecord.ProcessedAt = time.Now()

	s.logAgentInteraction(sess.ID, "stdout", output.Stdout)
	slog.Info("agent output",
		"session_id", sess.ID,
		"exit_code", output.ExitCode,
		"duration", output.Duration,
		"stdout_len", len(output.Stdout),
	)

	comment := extractComment(output.Stdout)
	if comment != "" {
		postErr := s.postComment(ctx, evt, comment)
		if postErr != nil {
			slog.Warn("failed to post comment", "session_id", sess.ID, "error", postErr)
		} else {
			slog.Info("comment posted successfully", "session_id", sess.ID, "repo", evt.Repository)
		}
	}

	if output.Success {
		slog.Info("event processed successfully",
			"session_id", sess.ID,
			"repo_key", sess.RepoKey,
			"duration", output.Duration,
		)
		_ = s.sessionMgr.RecordSuccess(ctx, sess.ID, currentRecord)
	} else {
		slog.Warn("event processed with failure",
			"session_id", sess.ID,
			"repo_key", sess.RepoKey,
			"exit_code", output.ExitCode,
		)
		_ = s.sessionMgr.RecordFailure(ctx, sess.ID, currentRecord, "agent returned non-zero exit code")
		s.handleFailure(ctx, sess, currentRecord)
	}
}

func (s *Server) handleFailure(ctx context.Context, sess *session.Session, record *session.EventRecord) {
	updatedSess, err := s.sessionMgr.Get(ctx, sess.ID)
	if err != nil {
		slog.Error("get session for failure handling failed", "error", err)
		return
	}
	if err := s.failureHandler.Handle(ctx, updatedSess, record); err != nil {
		slog.Error("failure handler failed", "session_id", sess.ID, "error", err)
	}
}

func (s *Server) healthHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"status":  "ok",
		"agents":  s.agentRegistry.Available(),
		"rules":   s.router.RuleCount(),
	})
}

func (s *Server) sessionsHandler(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	sessions, err := s.sessionMgr.ListRecent(ctx, 50)
	if err != nil {
		http.Error(w, "failed to list sessions", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(sessions)
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		slog.Info("http request",
			"method", r.Method,
			"path", r.URL.Path,
			"duration", time.Since(start),
		)
	})
}

func (s *Server) buildAgentPrompt(sess *session.Session, evt *event.NormalizedEvent, rule *event.RouteRule, templatePrompt string) string {
	var sb strings.Builder

	sb.WriteString("You are a DevOps event processing agent. Analyze the following event and provide your response.\n\n")
	sb.WriteString(fmt.Sprintf("Event: %s / %s on %s by %s\n", evt.Source, evt.Type, evt.Repository, evt.Actor))
	sb.WriteString(fmt.Sprintf("Raw event data:\n%s\n\n", string(evt.RawPayload)))
	sb.WriteString(templatePrompt)
	sb.WriteString("\n\nProvide your analysis and the exact comment text that should be posted to this issue/PR. Start your response with: ANALYSIS: and then COMMENT: with the text to be posted.\n")

	return sb.String()
}

func (s *Server) buildAgentEnv(evt *event.NormalizedEvent) map[string]string {
	env := make(map[string]string)

	for name, srcCfg := range s.cfg.Sources {
		secret := srcCfg.ResolvedSecret()
		if secret != "" {
			key := fmt.Sprintf("%s_TOKEN", strings.ToUpper(name))
			env[key] = secret
		}
	}

	return env
}

func extractComment(agentOutput string) string {
	idx := strings.Index(agentOutput, "COMMENT:")
	if idx >= 0 {
		comment := agentOutput[idx+8:]
		comment = strings.TrimSpace(comment)
		lines := strings.SplitN(comment, "\n", 2)
		if len(lines) > 1 {
			body := strings.TrimSpace(lines[1])
			if body != "" {
				return body
			}
		}
		return comment
	}

	idx = strings.Index(agentOutput, "ANALYSIS:")
	if idx >= 0 {
		return strings.TrimSpace(agentOutput)
	}

	if len(agentOutput) > 50 {
		return strings.TrimSpace(agentOutput)
	}
	return ""
}

func (s *Server) postComment(ctx context.Context, evt *event.NormalizedEvent, body string) error {
	var number string
	if issue, ok := evt.Payload["issue"].(map[string]any); ok {
		if n, ok := issue["number"]; ok {
			number = fmt.Sprintf("%v", n)
		}
	}
	if pr, ok := evt.Payload["pull_request"].(map[string]any); ok {
		if n, ok := pr["number"]; ok {
			number = fmt.Sprintf("%v", n)
		}
	}
	if number == "" {
		if n, ok := evt.Payload["number"]; ok {
			number = fmt.Sprintf("%v", n)
		}
	}
	if number == "" {
		return fmt.Errorf("no issue/PR number in event payload")
	}

	token := ""
	if srcCfg, ok := s.cfg.Sources[string(evt.Source)]; ok {
		token = srcCfg.ResolvedSecret()
	}
	if token == "" {
		return fmt.Errorf("no token for source %s", evt.Source)
	}

	escapedBody := strings.ReplaceAll(body, "\"", "\\\"")
	escapedBody = strings.ReplaceAll(escapedBody, "\n", "\\n")
	jsonBody := fmt.Sprintf(`{"body":"[DevOps Bot] %s"}`, escapedBody)

	var endpoint string
	switch evt.Type {
	case event.EventIssueOpened:
		endpoint = fmt.Sprintf("https://api.gitcode.com/api/v5/repos/%s/issues/%s/comments", evt.Repository, number)
	case event.EventPullRequestOpened:
		endpoint = fmt.Sprintf("https://api.gitcode.com/api/v5/repos/%s/pulls/%s/comments", evt.Repository, number)
	default:
		return fmt.Errorf("unsupported event type for commenting: %s", evt.Type)
	}

	req, err := http.NewRequestWithContext(ctx, "POST", endpoint, strings.NewReader(jsonBody))
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("do request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		respBody, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("API returned %d: %s", resp.StatusCode, string(respBody))
	}

	return nil
}

func (s *Server) logAgentInteraction(sessionID string, label string, content string) {
	if content == "" {
		return
	}
	logDir := s.cfg.Workspace.BaseDir + "/../logs"
	os.MkdirAll(logDir, 0755)
	logFile := fmt.Sprintf("%s/%s-%s.log", logDir, sessionID, label)
	f, err := os.OpenFile(logFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()
	f.WriteString(fmt.Sprintf("\n\n=== %s %s ===\n%s\n", time.Now().Format("2006-01-02 15:04:05"), label, content))
}
