# feat: Workspace Isolation — Control Plane vs. Data Plane Directory Management

## What do you want to build?

Implement the directory management layer that enforces strict separation between the Loop Troop Control Plane workspace (where Loop Troop's own code lives) and the Data Plane workspaces (where target repositories are cloned and manipulated). This prevents accidental or malicious cross-contamination per ADR-0001.

## Acceptance Criteria

- [ ] A `WorkspaceManager` class that manages target repository cloning, branch management, and cleanup.
- [ ] Target repositories are cloned into a configurable base directory (default: `~/.loop-troop/workspaces/`), never inside the Loop Troop source tree.
- [ ] Provides `clone_or_update(repo_url)`, `checkout_branch(repo_path, branch)`, `create_branch(repo_path, branch)`, and `cleanup(repo_path)` methods.
- [ ] All methods validate that the provided path resolves to within the configured workspace base directory (no path traversal via `../` or symlinks).
- [ ] Git operations use `subprocess.run(["git", ...], cwd=target_path)` — never `os.chdir()` which would affect the Control Plane's host process.
- [ ] Cloned repositories are checked for the expected template structure (Makefile, Dockerfile, `docs/architecture/` folder) before being accepted for work.
- [ ] Unit tests covering: clone, update, path traversal rejection, symlink rejection, template validation.

## Implementation Notes (Optional)

Use `pathlib.Path.resolve()` and check `.is_relative_to(workspace_base)` for path validation. The `subprocess.run` calls should always set `cwd=` rather than changing the Control Plane's host process working directory. Consider adding a `.loop-troop-workspace` sentinel file to workspace directories to distinguish managed clones from user directories.
