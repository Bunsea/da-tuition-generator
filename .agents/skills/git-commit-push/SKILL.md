---
name: git-commit-push
description: >-
  Automatically stages, commits, and pushes changes to the remote Git repository.
  Use this skill whenever the user asks to commit, push, sync to GitHub, or uses
  commands like /commit, /push, /git-push, or /ship.
---

# Git Commit and Push Workflow

This skill automates the process of checking repository status, staging modified/created files, generating an informative commit message, and pushing to the remote repository (`origin`).

## Procedure

### Step 1: Inspect Repository Status
Run `git status` and `git diff --stat` to review all modified, created, or deleted files:

```bash
git status
git diff --stat
```

### Step 2: Determine Commit Message
- If the user specifies a message (e.g., `/commit "feat: add worked solutions"`), use their message.
- If no message is provided, inspect the modified files and write a concise, conventional commit message (e.g., `feat: ...`, `fix: ...`, `refactor: ...`).

### Step 3: Stage and Commit
Stage the changed files and commit:

```bash
git add .
git commit -m "<commit message>"
```

### Step 4: Push to Remote
Determine current branch (usually `main` or `master`) and push:

```bash
git push origin $(git branch --show-current)
```

### Step 5: Handle Authentication / Push Status
- **On Success**: Report the commit hash, branch name, and number of files pushed.
- **On Auth Failure** (e.g., password authentication deprecated):
  - If on macOS and GitHub Desktop is installed, notify the user they can click **"Push origin"** in GitHub Desktop.
  - Or advise the user to provide a GitHub Personal Access Token (`ghp_...`).
