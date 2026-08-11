# Phase 1b v7 final targeted review (Codex)

Scope: final verification of V3-01's `skills.external_dirs` relative-path handling and confirmation that the v7 rewrite introduces no new concrete D-16 bypass.

## V3-01 — RESOLVED

Section 4.2 step 7a check 2 now mirrors the path-resolution behavior implemented by `agent/skill_utils.py:get_external_skills_dirs()` closely enough that it cannot be less inclusive than Hermes's actual `skills.external_dirs` resolution:

1. A scalar string is normalized to a one-element list; unsupported top-level shapes are treated as empty.
2. Every list entry is normalized with `str(entry).strip()`, and empty entries are skipped.
3. `os.path.expanduser(os.path.expandvars(entry))` performs the same user-home and environment-variable expansion.
4. After constructing `Path(expanded)`, relative paths are resolved as `(hermes_constants.get_hermes_home() / p).resolve()` and absolute paths as `p.resolve()`. This exactly matches the source's load-bearing rule at `agent/skill_utils.py:562-566`: relative paths are based on `HERMES_HOME`, not cwd.
5. Check 2 intentionally omits the final `is_dir()` existence filter. Consequently, a declared-but-not-yet-existing external directory is retained as a candidate even though `get_external_skills_dirs()` and check 1 would omit it.

The former bypass is therefore closed. For example, `../.hh-agent/promote_staging` is resolved from the active Hermes home by both Hermes and check 2, so the staging-overlap/containment test rejects it before any file is written. The union with check 1, the bidirectional containment test (equal, descendant, or ancestor), and fail-closed handling of config read/parse errors remain intact. The v7 rewrite introduces no new concrete D-16 bypass.

## Non-blocking notes

- The phrase that skipping `is_dir()` is the "only" difference is slightly imprecise at the implementation-detail level: Hermes also deduplicates candidates and skips a candidate equal to its built-in local skills root. Check 2 need not reproduce those exclusions; retaining such candidates makes the safety check more inclusive, not less inclusive, and cannot create the bypass under review.
- Contract tests for scalar, absolute, `~`, environment-variable, relative, and nonexistent declarations would help detect future drift between the duplicated resolver logic and `get_external_skills_dirs()`. This is implementation hardening, not a remaining specification blocker.

## Final verdict

**CLEARED FOR IMPLEMENTATION**
