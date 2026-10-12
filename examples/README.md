<h1 align="center">grape-template</h1>

<p align="center">English | <a href="README.zh.md">中文</a></p>

<p align="center">
  <strong>Official project templates for the Grape platform</strong>
  <br>
  The stock templates ship here, versioned independently from the main grape repository.
</p>

<p align="center">
  <a href="#what-is-grape-template">What is grape-template</a> ·
  <a href="#repo-layout">Repo Layout</a> ·
  <a href="#registry-structure">Registry Structure</a> ·
  <a href="#local-templates">Local Templates</a> ·
  <a href="#adding-a-template">Adding a Template</a> ·
  <a href="#usage">Usage</a>
</p>

---

## What is grape-template

`grape-template` is the standalone repo for Grape's official project templates. At the **build / deploy
stage of the main grape repo**, `scripts/sync_templates.sh` pulls these contents into the deployment root:

- template directories → `examples/`
- the template registry → `configs/project-templates.json`

Keeping the templates in their own repo lets them ship and version independently from the platform.

**In one sentence: add a template here + register its id in `project-templates.json` → it appears in the Grape "New Project" flow.**

## Repo Layout

| Path | Description |
|---|---|
| `hello-*` | Official local templates. The directory name is the template `id` and maps one-to-one with `source: "local"` entries in `project-templates.json` |
| `project-templates.json` | The template registry — every entry (local and git) organized by category; versioned with this repo |

## Registry Structure

`project-templates.json` organizes all templates into categories (`categories`). The `cloud-native` and
`iac` categories are synced daily and their counts drift with the sync result:

| category `id` | Category | scene | Entries | Notes |
|---|---|---|---|---|
| `hello-world` | 👋 Hello World starters | hello-world | 15 | 7 local + 8 popular GitCode git templates (random mode) |
| `general` | ⚙ General project templates | default | 3 | General-purpose git templates |
| `mobile` | 🍃 Mobile development templates | harmony | 2 | HarmonyOS / mobile git templates |
| `cloud-native` | ☁️ Cloud-native development | cloud-native | 110 | Curated cloud-native repos, auto-synced daily |
| `iac` | 🏗️ IaC development | iac | 22 | Curated IaC repos, auto-synced daily |
| `skill-dev` | 🔧 Skill development templates | skill-dev | 1 | Local `hello-skill` |
| `ai` | 🤖 AI application templates | agent-dev | 20 | AI application git templates |
| `content` | 📝 Content creation templates | blog-publish | 3 | Blogging / content git templates |

## Local Templates

- `hello-flask` — Mood Tracker: Flask web app with 5 API endpoints and unit tests (hello-world scene)
- `hello-fastapi` — minimal FastAPI service (hello-world scene)
- `hello-todo` — command-line todo list (hello-world scene)
- `hello-calculator` — calculator with unit tests (hello-world scene)
- `hello-html` — static web page (HTML + CSS + JS, hello-world scene)
- `hello-blog` — Flask multi-page blog skeleton (hello-world scene)
- `snake-game` — classic Snake game, converted to a local template (hello-world scene)
- `hello-skill` — minimal Skill project template (skill-dev scene)

## Adding a Template

To add a **local** template, both steps are required at the same time:

1. Create a new template directory whose name is the template `id`
2. Register that same `id` in `project-templates.json` with `"source": "local"`

> Note: every `source: "local"` entry must have a matching directory, otherwise the grape-side gate
> `test_local_template_dir_exists` fails. Template directories that stay `source: "git"` are fine as
> long as the registry entry remains `git`.

A mismatch between the directory and the registry fails the grape-side gate
`test_local_template_dir_exists`. Git-backed templates only need a `source: "git"` registry entry.

## Usage

```bash
# Synced by the grape main repo at deploy time
bash scripts/sync_templates.sh
```