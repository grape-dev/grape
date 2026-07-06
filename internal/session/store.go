package session

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	_ "github.com/glebarez/go-sqlite"
)

type SQLiteStore struct {
	db *sql.DB
}

func NewSQLiteStore(dbPath string) (*SQLiteStore, error) {
	db, err := sql.Open("sqlite", dbPath+"?_journal_mode=WAL&_busy_timeout=5000")
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	if err := db.Ping(); err != nil {
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}
	store := &SQLiteStore{db: db}
	if err := store.migrate(); err != nil {
		return nil, fmt.Errorf("migrate: %w", err)
	}
	return store, nil
}

func (s *SQLiteStore) migrate() error {
	_, err := s.db.Exec(`
		CREATE TABLE IF NOT EXISTS sessions (
			id TEXT PRIMARY KEY,
			repo_key TEXT NOT NULL,
			source TEXT NOT NULL,
			repository TEXT NOT NULL,
			agent_name TEXT NOT NULL,
			status TEXT NOT NULL,
			workspace TEXT NOT NULL DEFAULT '',
			event_history JSON NOT NULL DEFAULT '[]',
			current_event JSON,
			created_at DATETIME NOT NULL,
			last_active_at DATETIME NOT NULL,
			retry_count INTEGER NOT NULL DEFAULT 0,
			fail_reason TEXT NOT NULL DEFAULT ''
		);
		CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_repo_key ON sessions(repo_key);
		CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
		CREATE INDEX IF NOT EXISTS idx_sessions_last_active ON sessions(last_active_at);
	`)
	return err
}

func (s *SQLiteStore) Create(ctx context.Context, sess *Session) error {
	historyJSON, err := json.Marshal(sess.EventHistory)
	if err != nil {
		return fmt.Errorf("marshal event_history: %w", err)
	}
	var currentEventJSON any
	if sess.CurrentEvent != nil {
		b, err := json.Marshal(sess.CurrentEvent)
		if err != nil {
			return fmt.Errorf("marshal current_event: %w", err)
		}
		currentEventJSON = string(b)
	}
	_, err = s.db.ExecContext(ctx,
		`INSERT INTO sessions (id, repo_key, source, repository, agent_name, status, workspace,
		 event_history, current_event, created_at, last_active_at, retry_count, fail_reason)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		sess.ID, sess.RepoKey, sess.Source, sess.Repository, sess.AgentName, sess.Status, sess.Workspace,
		string(historyJSON), currentEventJSON, sess.CreatedAt, sess.LastActiveAt, sess.RetryCount, sess.FailReason,
	)
	return err
}

func (s *SQLiteStore) Update(ctx context.Context, sess *Session) error {
	historyJSON, err := json.Marshal(sess.EventHistory)
	if err != nil {
		return fmt.Errorf("marshal event_history: %w", err)
	}
	var currentEventJSON any
	if sess.CurrentEvent != nil {
		b, err := json.Marshal(sess.CurrentEvent)
		if err != nil {
			return fmt.Errorf("marshal current_event: %w", err)
		}
		currentEventJSON = string(b)
	}
	_, err = s.db.ExecContext(ctx,
		`UPDATE sessions SET repo_key=?, source=?, repository=?, agent_name=?, status=?, workspace=?,
		 event_history=?, current_event=?, last_active_at=?, retry_count=?, fail_reason=?
		 WHERE id=?`,
		sess.RepoKey, sess.Source, sess.Repository, sess.AgentName, sess.Status, sess.Workspace,
		string(historyJSON), currentEventJSON, sess.LastActiveAt, sess.RetryCount, sess.FailReason,
		sess.ID,
	)
	return err
}

func (s *SQLiteStore) Get(ctx context.Context, id string) (*Session, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT id, repo_key, source, repository, agent_name, status, workspace,
		        event_history, current_event, created_at, last_active_at, retry_count, fail_reason
		 FROM sessions WHERE id=?`, id,
	)
	return s.scanRow(row)
}

func (s *SQLiteStore) GetByRepoKey(ctx context.Context, repoKey string) (*Session, error) {
	row := s.db.QueryRowContext(ctx,
		`SELECT id, repo_key, source, repository, agent_name, status, workspace,
		        event_history, current_event, created_at, last_active_at, retry_count, fail_reason
		 FROM sessions WHERE repo_key=?`, repoKey,
	)
	sess, err := s.scanRow(row)
	if err != nil {
		return nil, err
	}
	return sess, nil
}

func (s *SQLiteStore) ListByStatus(ctx context.Context, status Status) ([]*Session, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id, repo_key, source, repository, agent_name, status, workspace,
		        event_history, current_event, created_at, last_active_at, retry_count, fail_reason
		 FROM sessions WHERE status=? ORDER BY last_active_at DESC`, status,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return s.scanRows(rows)
}

func (s *SQLiteStore) ListRecent(ctx context.Context, limit int) ([]*Session, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id, repo_key, source, repository, agent_name, status, workspace,
		        event_history, current_event, created_at, last_active_at, retry_count, fail_reason
		 FROM sessions ORDER BY last_active_at DESC LIMIT ?`, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return s.scanRows(rows)
}

func (s *SQLiteStore) Close() error {
	return s.db.Close()
}

func (s *SQLiteStore) scanRow(row *sql.Row) (*Session, error) {
	var sess Session
	var historyJSON string
	var currentEventJSON sql.NullString
	var createdAtStr string
	var lastActiveAtStr string

	err := row.Scan(
		&sess.ID, &sess.RepoKey, &sess.Source, &sess.Repository, &sess.AgentName, &sess.Status, &sess.Workspace,
		&historyJSON, &currentEventJSON, &createdAtStr, &lastActiveAtStr, &sess.RetryCount, &sess.FailReason,
	)
	if err != nil {
		return nil, err
	}

	if err := json.Unmarshal([]byte(historyJSON), &sess.EventHistory); err != nil {
		sess.EventHistory = []EventRecord{}
	}

	if currentEventJSON.Valid && currentEventJSON.String != "" && currentEventJSON.String != "null" {
		var er EventRecord
		if err := json.Unmarshal([]byte(currentEventJSON.String), &er); err == nil {
			sess.CurrentEvent = &er
		}
	}

	sess.CreatedAt = parseTime(createdAtStr)
	sess.LastActiveAt = parseTime(lastActiveAtStr)

	return &sess, nil
}

func (s *SQLiteStore) scanRows(rows *sql.Rows) ([]*Session, error) {
	var sessions []*Session
	for rows.Next() {
		var sess Session
		var historyJSON string
		var currentEventJSON sql.NullString
		var createdAtStr string
		var lastActiveAtStr string

		err := rows.Scan(
			&sess.ID, &sess.RepoKey, &sess.Source, &sess.Repository, &sess.AgentName, &sess.Status, &sess.Workspace,
			&historyJSON, &currentEventJSON, &createdAtStr, &lastActiveAtStr, &sess.RetryCount, &sess.FailReason,
		)
		if err != nil {
			return nil, err
		}

		if err := json.Unmarshal([]byte(historyJSON), &sess.EventHistory); err != nil {
			sess.EventHistory = []EventRecord{}
		}

		if currentEventJSON.Valid && currentEventJSON.String != "" && currentEventJSON.String != "null" {
			var er EventRecord
			if err := json.Unmarshal([]byte(currentEventJSON.String), &er); err == nil {
				sess.CurrentEvent = &er
			}
		}

		sess.CreatedAt = parseTime(createdAtStr)
		sess.LastActiveAt = parseTime(lastActiveAtStr)

		sessions = append(sessions, &sess)
	}
	return sessions, nil
}

func parseTime(s string) time.Time {
	t, _ := time.Parse("2006-01-02 15:04:05.999999999 -0700 MST", s)
	if t.IsZero() {
		t, _ = time.Parse("2006-01-02T15:04:05Z07:00", s)
	}
	return t
}
