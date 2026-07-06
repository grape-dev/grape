package prompt

import (
	"bytes"
	"embed"
	"fmt"
	"text/template"

	"github.com/devops-bot/devops-bot/internal/event"
)

//go:embed templates/*
var templateFS embed.FS

type Builder struct {
	templates map[string]*template.Template
}

func NewBuilder() (*Builder, error) {
	b := &Builder{
		templates: make(map[string]*template.Template),
	}
	entries, err := templateFS.ReadDir("templates")
	if err != nil {
		return nil, fmt.Errorf("read template dir: %w", err)
	}
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		data, err := templateFS.ReadFile("templates/" + entry.Name())
		if err != nil {
			return nil, fmt.Errorf("read template %s: %w", entry.Name(), err)
		}
		name := entry.Name()
		if len(name) > 5 && name[len(name)-5:] == ".tmpl" {
			name = name[:len(name)-5]
		}
		tmpl, err := template.New(name).Parse(string(data))
		if err != nil {
			return nil, fmt.Errorf("parse template %s: %w", entry.Name(), err)
		}
		b.templates[name] = tmpl
	}
	return b, nil
}

func (b *Builder) Build(evt *event.NormalizedEvent, templateName string) (string, error) {
	tmpl, ok := b.templates[templateName]
	if !ok {
		tmpl, ok = b.templates["default"]
		if !ok {
			return b.buildFallback(evt), nil
		}
	}

	var buf bytes.Buffer
	if err := tmpl.Execute(&buf, evt); err != nil {
		return "", fmt.Errorf("execute template %s: %w", templateName, err)
	}
	return buf.String(), nil
}

func (b *Builder) buildFallback(evt *event.NormalizedEvent) string {
	return fmt.Sprintf(
		"DevOps事件通知：\n\n事件源: %s\n事件类型: %s\n仓库: %s\n分支: %s\n提交: %s\n触发者: %s\n时间: %s\n\n原始事件数据：\n%s",
		evt.Source, evt.Type, evt.Repository, evt.Branch, evt.Commit, evt.Actor, evt.Timestamp.Format("2006-01-02 15:04:05"), string(evt.RawPayload),
	)
}

func (b *Builder) BuildFailurePrompt(evt *event.NormalizedEvent, failReason string, stderr string) string {
	return fmt.Sprintf(
		"你之前处理一个DevOps事件时失败了，请分析失败原因并决定下一步行动。\n\n"+
			"=== 事件信息 ===\n事件源: %s\n事件类型: %s\n仓库: %s\n分支: %s\n触发者: %s\n\n"+
			"=== 失败信息 ===\n失败原因: %s\n错误输出:\n%s\n\n"+
			"=== 原始事件数据 ===\n%s\n\n"+
			"请分析失败原因，如果可以修复请给出修复方案，如果无法自动修复请说明需要人工介入的原因。",
		evt.Source, evt.Type, evt.Repository, evt.Branch, evt.Actor,
		failReason, stderr, string(evt.RawPayload),
	)
}
