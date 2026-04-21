"""Tests for load_project_skills functionality."""

from openhands.sdk.skills import (
    KeywordTrigger,
    load_project_skills,
)
from openhands.sdk.skills.skill import _discover_git_repos


def test_load_project_skills_no_directories(tmp_path):
    """Test load_project_skills when no project skills directories exist."""
    skills = load_project_skills(tmp_path)
    assert skills == []


def test_load_project_skills_agents_md_without_skills_directory(tmp_path):
    """Test that AGENTS.md is loaded even when .openhands/skills doesn't exist.

    This is a regression test for the bug where third-party skill files like
    AGENTS.md were not loaded when the .openhands/skills directory didn't exist.
    """
    # Create AGENTS.md in the work directory (no .openhands/skills)
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# Project Guidelines\n\nThis is the AGENTS.md content.")

    skills = load_project_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "agents"
    assert "Project Guidelines" in skills[0].content
    assert skills[0].trigger is None  # Third-party skills are always active


def test_load_project_skills_agents_md_case_insensitive(tmp_path):
    """Test that AGENTS.md is loaded with case-insensitive matching."""
    # Create agents.md (lowercase) in the work directory
    agents_md = tmp_path / "agents.md"
    agents_md.write_text("# Lowercase agents.md content")

    skills = load_project_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "agents"


def test_load_project_skills_multiple_third_party_files(tmp_path):
    """Test loading multiple third-party skill files."""
    # Create AGENTS.md
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md content")

    # Create .cursorrules
    (tmp_path / ".cursorrules").write_text("# Cursor rules content")

    skills = load_project_skills(tmp_path)
    assert len(skills) == 2
    skill_names = {s.name for s in skills}
    assert "agents" in skill_names
    assert "cursorrules" in skill_names


def test_load_project_skills_third_party_with_skills_directory(tmp_path):
    """Test third-party files are loaded alongside skills from .openhands/skills."""
    # Create AGENTS.md in work directory
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md content")

    # Create .openhands/skills directory with a skill
    skills_dir = tmp_path / ".openhands" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "test_skill.md").write_text(
        "---\nname: test_skill\ntriggers:\n  - test\n---\nTest skill content."
    )

    skills = load_project_skills(tmp_path)
    assert len(skills) == 2
    skill_names = {s.name for s in skills}
    assert "agents" in skill_names
    assert "test_skill" in skill_names


def test_load_project_skills_with_skills_directory(tmp_path):
    """Test load_project_skills loads from .openhands/skills directory."""
    # Create .openhands/skills directory
    skills_dir = tmp_path / ".openhands" / "skills"
    skills_dir.mkdir(parents=True)

    # Create a test skill file
    skill_file = skills_dir / "test_skill.md"
    skill_file.write_text(
        "---\nname: test_skill\ntriggers:\n  - test\n---\nThis is a test skill."
    )

    skills = load_project_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "test_skill"
    assert skills[0].content == "This is a test skill."
    assert isinstance(skills[0].trigger, KeywordTrigger)


def test_load_project_skills_with_agents_directory(tmp_path):
    """Test load_project_skills loads from .agents/skills directory."""
    # Create .agents/skills directory
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)

    # Create a test skill file
    skill_file = skills_dir / "agent_skill.md"
    skill_file.write_text(
        "---\nname: agent_skill\ntriggers:\n  - agent\n---\nAgent skill content."
    )

    skills = load_project_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "agent_skill"
    assert skills[0].content == "Agent skill content."
    assert isinstance(skills[0].trigger, KeywordTrigger)


def test_load_project_skills_agents_directory_precedence(tmp_path):
    """Test .agents/skills takes precedence over other directories."""
    agents_dir = tmp_path / ".agents" / "skills"
    skills_dir = tmp_path / ".openhands" / "skills"
    microagents_dir = tmp_path / ".openhands" / "microagents"
    agents_dir.mkdir(parents=True)
    skills_dir.mkdir(parents=True)
    microagents_dir.mkdir(parents=True)

    (agents_dir / "duplicate.md").write_text(
        "---\nname: duplicate\n---\nFrom .agents/skills."
    )
    (skills_dir / "duplicate.md").write_text(
        "---\nname: duplicate\n---\nFrom .openhands/skills."
    )
    (microagents_dir / "duplicate.md").write_text(
        "---\nname: duplicate\n---\nFrom .openhands/microagents."
    )

    skills = load_project_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "duplicate"
    assert skills[0].content == "From .agents/skills."


def test_load_project_skills_merges_agents_and_openhands(tmp_path):
    """Test loading unique skills from .agents/skills and .openhands/skills."""
    agents_dir = tmp_path / ".agents" / "skills"
    openhands_dir = tmp_path / ".openhands" / "skills"
    agents_dir.mkdir(parents=True)
    openhands_dir.mkdir(parents=True)

    (agents_dir / "agent_skill.md").write_text(
        "---\nname: agent_skill\n---\nAgent skill content."
    )
    (openhands_dir / "legacy_skill.md").write_text(
        "---\nname: legacy_skill\n---\nLegacy skill content."
    )

    skills = load_project_skills(tmp_path)
    assert len(skills) == 2
    skill_names = {skill.name for skill in skills}
    assert skill_names == {"agent_skill", "legacy_skill"}


def test_load_project_skills_with_microagents_directory(tmp_path):
    """Test load_project_skills loads from .openhands/microagents directory (legacy)."""
    # Create .openhands/microagents directory
    microagents_dir = tmp_path / ".openhands" / "microagents"
    microagents_dir.mkdir(parents=True)

    # Create a test microagent file
    microagent_file = microagents_dir / "legacy_skill.md"
    microagent_file.write_text(
        "---\n"
        "name: legacy_skill\n"
        "triggers:\n"
        "  - legacy\n"
        "---\n"
        "This is a legacy microagent skill."
    )

    skills = load_project_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "legacy_skill"
    assert skills[0].content == "This is a legacy microagent skill."


def test_load_project_skills_priority_order(tmp_path):
    """Test that skills/ directory takes precedence over microagents/."""
    # Create both directories
    skills_dir = tmp_path / ".openhands" / "skills"
    microagents_dir = tmp_path / ".openhands" / "microagents"
    skills_dir.mkdir(parents=True)
    microagents_dir.mkdir(parents=True)

    # Create duplicate skill in both directories
    (skills_dir / "duplicate.md").write_text(
        "---\nname: duplicate\n---\nFrom skills directory."
    )

    (microagents_dir / "duplicate.md").write_text(
        "---\nname: duplicate\n---\nFrom microagents directory."
    )

    skills = load_project_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "duplicate"
    # Should be from skills directory (takes precedence)
    assert skills[0].content == "From skills directory."


def test_load_project_skills_both_directories(tmp_path):
    """Test loading unique skills from both directories."""
    # Create both directories
    skills_dir = tmp_path / ".openhands" / "skills"
    microagents_dir = tmp_path / ".openhands" / "microagents"
    skills_dir.mkdir(parents=True)
    microagents_dir.mkdir(parents=True)

    # Create different skills in each directory
    (skills_dir / "skill1.md").write_text("---\nname: skill1\n---\nSkill 1 content.")
    (microagents_dir / "skill2.md").write_text(
        "---\nname: skill2\n---\nSkill 2 content."
    )

    skills = load_project_skills(tmp_path)
    assert len(skills) == 2
    skill_names = {s.name for s in skills}
    assert skill_names == {"skill1", "skill2"}


def test_load_project_skills_handles_errors_gracefully(tmp_path):
    """Test that errors in loading are handled gracefully."""
    # Create .openhands/skills directory
    skills_dir = tmp_path / ".openhands" / "skills"
    skills_dir.mkdir(parents=True)

    # Create an invalid skill file
    invalid_file = skills_dir / "invalid.md"
    invalid_file.write_text(
        "---\n"
        "triggers: not_a_list\n"  # Invalid: triggers must be a list
        "---\n"
        "Invalid skill."
    )

    # Should not raise exception, just return empty list
    skills = load_project_skills(tmp_path)
    assert skills == []


def test_load_project_skills_one_bad_skill_does_not_break_others(tmp_path):
    """Test that one invalid skill doesn't prevent other valid skills from loading.

    This is a regression test for the bug where a single skill validation error
    would cause ALL skills in the directory to fail loading.
    """
    # Create .openhands/skills directory
    skills_dir = tmp_path / ".openhands" / "skills"
    skills_dir.mkdir(parents=True)

    # Create a valid skill
    valid_skill = skills_dir / "valid-skill.md"
    valid_skill.write_text(
        "---\nname: valid-skill\ntriggers:\n  - valid\n---\nThis is a valid skill."
    )

    # Create an invalid skill (name doesn't match filename)
    invalid_skill_dir = skills_dir / "bad-skill"
    invalid_skill_dir.mkdir()
    (invalid_skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: wrong_name\n"  # Name has underscore, doesn't match dir
        "---\n"
        "This skill has a mismatched name."
    )

    # Create another valid skill
    another_valid = skills_dir / "another-valid.md"
    another_valid.write_text(
        "---\nname: another-valid\ntriggers:\n  - another\n---\nAnother valid skill."
    )

    # Should load valid skills despite the invalid one
    skills = load_project_skills(tmp_path)

    # Both valid skills should be loaded
    skill_names = {s.name for s in skills}
    assert "valid-skill" in skill_names
    assert "another-valid" in skill_names
    # Invalid skill should NOT be loaded
    assert "wrong_name" not in skill_names
    assert "bad-skill" not in skill_names


def test_long_description_skill_does_not_break_other_skills(tmp_path):
    """Regression test: a skill with a very long description should not
    prevent other valid skills in the same directory from loading.

    The description should be silently truncated (via maybe_truncate)
    rather than raising an error.
    """
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)

    # Create a valid skill
    (skills_dir / "good-skill.md").write_text(
        "---\nname: good-skill\ntriggers:\n  - good\n---\nGood skill content."
    )

    # Create a skill with a description exceeding 1024 chars
    long_desc = "A" * 2000
    bad_skill_dir = skills_dir / "bad-skill"
    bad_skill_dir.mkdir()
    (bad_skill_dir / "SKILL.md").write_text(
        f"---\nname: bad-skill\ndescription: {long_desc}\n---\n"
        "# Bad Skill\nContent here."
    )

    skills = load_project_skills(tmp_path)
    skill_names = {s.name for s in skills}

    # The good skill must load regardless
    assert "good-skill" in skill_names

    # The bad skill should also load (description truncated, not rejected)
    assert "bad-skill" in skill_names
    bad = next(s for s in skills if s.name == "bad-skill")
    assert bad.description is not None
    assert len(bad.description) <= 1024


def test_load_project_skills_with_string_path(tmp_path):
    """Test that load_project_skills accepts string paths."""
    # Create .openhands/skills directory
    skills_dir = tmp_path / ".openhands" / "skills"
    skills_dir.mkdir(parents=True)

    # Create a test skill file
    skill_file = skills_dir / "test_skill.md"
    skill_file.write_text("---\nname: test_skill\n---\nTest skill content.")

    # Pass path as string
    skills = load_project_skills(str(tmp_path))
    assert len(skills) == 1
    assert skills[0].name == "test_skill"


def test_load_project_skills_loads_from_git_root_when_called_from_subdir(tmp_path):
    """Running from a subdir should still load repo-level skills (git root)."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("# Project Guidelines\n\nFrom root")

    subdir = tmp_path / "subdir"
    subdir.mkdir()

    skills = load_project_skills(subdir)
    assert any(s.name == "agents" and "From root" in s.content for s in skills)


def test_load_project_skills_workdir_takes_precedence_over_git_root(tmp_path):
    """More local (work dir) skills should override repo root skills."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("# Project Guidelines\n\nFrom root")

    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "AGENTS.md").write_text("# Project Guidelines\n\nFrom subdir")

    skills = load_project_skills(subdir)
    agents = [s for s in skills if s.name == "agents"]
    assert len(agents) == 1
    assert agents[0].content.strip() == "# Project Guidelines\n\nFrom subdir"


def test_load_project_skills_loads_skills_directories_from_git_root(tmp_path):
    """Skills directories (.agents/skills etc.) should be loaded from git root."""
    (tmp_path / ".git").mkdir()

    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "root_skill.md").write_text(
        "---\nname: root_skill\ntriggers:\n  - root\n---\nLoaded from root"
    )

    subdir = tmp_path / "subdir"
    subdir.mkdir()

    skills = load_project_skills(subdir)
    assert any(
        s.name == "root_skill" and "Loaded from root" in s.content for s in skills
    )


# Tests for _discover_git_repos and discover_all_repos functionality


def test_discover_git_repos_empty_dir(tmp_path):
    """Test _discover_git_repos returns empty list for directory without git repos."""
    repos = _discover_git_repos(tmp_path)
    assert repos == []


def test_discover_git_repos_base_is_repo(tmp_path):
    """Test _discover_git_repos finds repo when base_dir itself is a git repo."""
    (tmp_path / ".git").mkdir()
    repos = _discover_git_repos(tmp_path)
    assert repos == [tmp_path]


def test_discover_git_repos_single_child(tmp_path):
    """Test _discover_git_repos finds a single child git repo."""
    child_repo = tmp_path / "child-repo"
    child_repo.mkdir()
    (child_repo / ".git").mkdir()

    repos = _discover_git_repos(tmp_path)
    assert repos == [child_repo]


def test_discover_git_repos_multiple_children(tmp_path):
    """Test _discover_git_repos finds multiple child git repos."""
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    (repo_a / ".git").mkdir()
    (repo_b / ".git").mkdir()

    repos = _discover_git_repos(tmp_path)
    # Should be sorted alphabetically
    assert repos == [repo_a, repo_b]


def test_discover_git_repos_skips_hidden_dirs(tmp_path):
    """Test _discover_git_repos skips directories starting with dot."""
    hidden_repo = tmp_path / ".hidden-repo"
    hidden_repo.mkdir()
    (hidden_repo / ".git").mkdir()

    repos = _discover_git_repos(tmp_path)
    assert repos == []


def test_discover_git_repos_depth_zero(tmp_path):
    """Test _discover_git_repos with max_depth=0 only checks base_dir."""
    child_repo = tmp_path / "child-repo"
    child_repo.mkdir()
    (child_repo / ".git").mkdir()

    repos = _discover_git_repos(tmp_path, max_depth=0)
    assert repos == []  # base_dir is not a git repo, children not checked


def test_discover_git_repos_base_and_children(tmp_path):
    """Test _discover_git_repos finds base and child repos."""
    (tmp_path / ".git").mkdir()
    child_repo = tmp_path / "child-repo"
    child_repo.mkdir()
    (child_repo / ".git").mkdir()

    repos = _discover_git_repos(tmp_path)
    assert tmp_path in repos
    assert child_repo in repos


def test_load_project_skills_discover_all_repos_single_repo(tmp_path):
    """Test discover_all_repos with a single git repo.

    When discover_all_repos=True, the function searches the parent of work_dir
    to find sibling repositories. In this case, work_dir is a git repo and
    its skills should be loaded.
    """
    # Create a main repo as the work_dir
    main_repo = tmp_path / "main-repo"
    main_repo.mkdir()
    (main_repo / ".git").mkdir()
    (main_repo / "AGENTS.md").write_text("# Main repo guidelines")

    skills = load_project_skills(main_repo, discover_all_repos=True)
    assert len(skills) == 1
    assert skills[0].name == "agents"
    assert "Main repo" in skills[0].content


def test_load_project_skills_discover_all_repos_multiple_repos(tmp_path):
    """Test discover_all_repos loads skills from sibling repos.

    When discover_all_repos=True and work_dir is /workspace/project/main-repo,
    the function searches under /workspace/project to find:
    - /workspace/project/main-repo
    - /workspace/project/other-repo
    etc.
    """
    # Create workspace with two sibling repos
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    (repo_a / ".git").mkdir()
    (repo_b / ".git").mkdir()

    # Each repo has its own AGENTS.md
    (repo_a / "AGENTS.md").write_text("# Repo A guidelines")
    (repo_b / "AGENTS.md").write_text("# Repo B guidelines")

    # Also create skills in one repo
    skills_dir = repo_b / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "repo-b-skill.md").write_text(
        "---\nname: repo-b-skill\ntriggers:\n  - repob\n---\nSkill from repo B"
    )

    # Call with repo_a as work_dir - should discover repo_b as sibling
    skills = load_project_skills(repo_a, discover_all_repos=True)
    skill_names = {s.name for s in skills}

    # Should have skills from both repos
    # Note: "agents" appears in both, repo_a comes first (alphabetically)
    assert "agents" in skill_names
    assert "repo-b-skill" in skill_names


def test_load_project_skills_discover_all_repos_precedence(tmp_path):
    """Test that work_dir takes precedence, then alphabetical order."""
    repo_a = tmp_path / "aaa-repo"
    repo_z = tmp_path / "zzz-repo"
    repo_a.mkdir()
    repo_z.mkdir()
    (repo_a / ".git").mkdir()
    (repo_z / ".git").mkdir()

    (repo_a / "AGENTS.md").write_text("# From aaa-repo")
    (repo_z / "AGENTS.md").write_text("# From zzz-repo")

    # When called with zzz-repo as work_dir, zzz-repo's skills come first
    # (work_dir always inserted at position 0 if not in list)
    skills = load_project_skills(repo_z, discover_all_repos=True)
    agents_skills = [s for s in skills if s.name == "agents"]

    assert len(agents_skills) == 1
    # zzz-repo is work_dir, so it should be first and win
    assert "zzz-repo" in agents_skills[0].content


def test_load_project_skills_discover_all_repos_false_default(tmp_path):
    """Test that discover_all_repos=False uses original behavior."""
    # Workspace has two sibling repos
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    (repo_a / ".git").mkdir()
    (repo_b / ".git").mkdir()
    (repo_a / "AGENTS.md").write_text("# Repo A guidelines")
    (repo_b / "AGENTS.md").write_text("# Repo B guidelines")

    # With discover_all_repos=False (default), only work_dir (repo_a) is searched
    skills = load_project_skills(repo_a, discover_all_repos=False)
    assert len(skills) == 1
    assert "Repo A" in skills[0].content

    # With discover_all_repos=True, sibling repos are discovered
    skills = load_project_skills(repo_a, discover_all_repos=True)
    # Both repos have AGENTS.md, but repo_a wins (it's work_dir)
    assert len(skills) == 1
    assert "Repo A" in skills[0].content


def test_load_project_skills_discover_all_repos_sibling_skills(tmp_path):
    """Test that discover_all_repos loads unique skills from sibling repos."""
    repo_main = tmp_path / "main-repo"
    repo_other = tmp_path / "other-repo"
    repo_main.mkdir()
    repo_other.mkdir()
    (repo_main / ".git").mkdir()
    (repo_other / ".git").mkdir()

    # main-repo has AGENTS.md
    (repo_main / "AGENTS.md").write_text("# Main repo guidelines")

    # other-repo has .cursorrules (different skill)
    (repo_other / ".cursorrules").write_text("# Other repo cursor rules")

    # Call with main-repo as work_dir
    skills = load_project_skills(repo_main, discover_all_repos=True)
    skill_names = {s.name for s in skills}

    # Should have skills from both repos (no name collision)
    assert "agents" in skill_names
    assert "cursorrules" in skill_names


def test_load_project_skills_discover_work_dir_not_git_repo(tmp_path):
    """Test discover_all_repos when work_dir is not a git repo.

    Example: /workspace/project (starting without a specific repo).
    When work_dir is NOT a git repo, we should search work_dir itself
    (not its parent) to find child repos.
    """
    # work_dir is not a git repo (like /workspace/project)
    # It has child repos
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    (repo_a / ".git").mkdir()
    (repo_b / ".git").mkdir()
    (repo_a / "AGENTS.md").write_text("# Repo A guidelines")
    (repo_b / ".cursorrules").write_text("# Repo B cursor rules")

    # work_dir also has its own skill file
    (tmp_path / "AGENTS.md").write_text("# Workspace guidelines")

    # Call with tmp_path (not a git repo) as work_dir
    skills = load_project_skills(tmp_path, discover_all_repos=True)
    skill_names = {s.name for s in skills}

    # Should find skills from work_dir AND its child repos
    # work_dir's AGENTS.md takes precedence over repo_a's
    assert "agents" in skill_names
    assert "cursorrules" in skill_names
    # Verify work_dir's skill wins (it's searched first)
    agents_skill = next(s for s in skills if s.name == "agents")
    assert "Workspace guidelines" in agents_skill.content


def test_load_project_skills_discover_work_dir_is_git_repo(tmp_path):
    """Test discover_all_repos when work_dir IS a git repo.

    Example: /workspace/project/main-repo (starting with a specific repo).
    When work_dir IS a git repo, we should search the parent directory
    to find sibling repos.
    """
    # Create workspace with sibling repos
    repo_main = tmp_path / "main-repo"
    repo_other = tmp_path / "other-repo"
    repo_main.mkdir()
    repo_other.mkdir()
    (repo_main / ".git").mkdir()
    (repo_other / ".git").mkdir()
    (repo_main / "AGENTS.md").write_text("# Main repo guidelines")
    (repo_other / ".cursorrules").write_text("# Other repo cursor rules")

    # Call with main-repo (a git repo) as work_dir
    skills = load_project_skills(repo_main, discover_all_repos=True)
    skill_names = {s.name for s in skills}

    # Should find skills from work_dir AND sibling repos
    assert "agents" in skill_names
    assert "cursorrules" in skill_names
