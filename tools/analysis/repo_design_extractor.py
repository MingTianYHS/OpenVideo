"""Deterministic design-system scanner for product source repositories.

First half of the product-motion pipeline's repo_analysis stage. This tool is
a *scanner*, not the artifact author: it detects the frontend framework,
classifies candidate design files, parses CSS custom properties and Tailwind
v4 ``@theme`` blocks with file+line provenance, statically parses tailwind
config theme literals, and indexes screen/component source files.

The agent then reads the flagged files directly (guided by the
``repo-design-extraction`` skill) and authors the schema-validated
``design_system`` and ``ui_inventory`` artifacts. Determinism: all directory
walks are sorted; the same repo state always yields the same scan report.

Safety contract:

* **The analyzed repository is only ever read, never written.** ``output_path``
  is rejected when it resolves inside ``repo_path``.
* **No code from the target repo is executed by default.** A tailwind config is
  executable JavaScript, so ``tailwind.config.*`` is parsed *statically* (the
  ``theme`` object literal is extracted and converted to JSON; anything
  containing calls, spreads, or template literals is refused rather than
  guessed at). Executing the config via ``node`` is available only as the
  explicit ``allow_config_execution`` opt-in, and every run that uses it says
  so in ``warnings[]`` and ``tailwind_theme_source``. Never enable it for a
  repository you do not trust.

Repo-relative paths in the report are always POSIX (``a/b.css``), on every
platform — they are contract values that flow into artifact provenance, and
downstream consumers (route derivation, candidate classification, the
``design_system`` schema) compare them with ``/`` separators.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

# Directories never worth walking for design tokens.
_SKIP_DIRS = {
    "node_modules", ".git", ".next", ".nuxt", ".svelte-kit", "dist", "build",
    "out", "coverage", ".turbo", ".cache", "__pycache__", ".venv", "venv",
    "storybook-static", ".output", "vendor",
}

# CSS custom property declaration, e.g. `--color-primary: #6366f1;`
_CSS_VAR_RE = re.compile(r"(--[\w-]+)\s*:\s*([^;{}]+);")
# Tailwind v4 CSS-first theme block: `@theme { ... }`
_THEME_BLOCK_RE = re.compile(r"@theme\b")
# @font-face family declaration
_FONT_FACE_RE = re.compile(r"font-family\s*:\s*['\"]?([^'\";,}]+)")
# next/font google imports, e.g. `import { Inter } from "next/font/google"`
_NEXT_FONT_RE = re.compile(r"import\s*\{([^}]+)\}\s*from\s*['\"]next/font/google['\"]")

_STYLE_EXTS = {".css", ".scss", ".sass", ".less"}
_SOURCE_EXTS = {".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte"}

# Screen/route roots checked in order. Only the roots whose URL mapping is a
# documented framework convention yield a route; the rest are reported as
# screen *candidates* with route=None (see _index_screens).
_SCREEN_ROOTS = [
    "app", "src/app", "pages", "src/pages", "src/views", "src/screens",
    "src/routes", "src/features",
]
_COMPONENT_ROOTS = ["components", "src/components", "app/components", "src/ui"]

# --- static tailwind-config parsing ---
# A `theme: { ... }` object literal is extracted and converted to JSON. Any
# construct that would need evaluation to resolve disqualifies the parse — we
# report null rather than a guess.
_JS_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_JS_LINE_COMMENT_RE = re.compile(r"(?<!:)//[^\n]*")
_JS_BARE_KEY_RE = re.compile(r"([{,]\s*)([A-Za-z_$][\w$.-]*)\s*:")
_JS_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_JS_SINGLE_QUOTED_RE = re.compile(r"'([^'\\\n]*)'")
_JS_THEME_KEY_RE = re.compile(r"(?<![\w$])theme\s*:\s*\{")
# Tokens that mean "this literal is really code" — parsing stops.
_JS_DYNAMIC_TOKENS = (
    "require(", "import(", "=>", "function", "`", "...", "process.", "new ",
)


class RepoDesignExtractor(BaseTool):
    name = "repo_design_extractor"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "analysis"
    provider = "local"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = []  # pure stdlib; node is only used under an explicit opt-in
    install_instructions = (
        "No installation required. Tailwind config themes are parsed "
        "statically. `node` on PATH is only used by the "
        "`allow_config_execution` opt-in, which RUNS the target repo's config "
        "as JavaScript — enable it for trusted repos only."
    )
    fallback_tools = []
    agent_skills = ["repo-design-extraction"]

    capabilities = [
        "detect_frontend_framework",
        "scan_design_token_sources",
        "parse_css_custom_properties",
        "index_screens_and_components",
    ]
    supports = {
        "offline": True,
        "readonly_on_target": True,
        # True only in the default configuration; the allow_config_execution
        # opt-in runs the target repo's tailwind config through node.
        "no_target_code_execution": True,
    }
    best_for = [
        "grounding a product-motion run in a repo's real design tokens",
        "finding which files define a web app's design system",
        "indexing screens/components before UI-replica planning",
    ]
    not_good_for = [
        "live-URL extraction (use `npx hyperframes capture` / website-to-video)",
        "non-web repos (backend, mobile) — v1 targets web frontends",
    ]

    input_schema = {
        "type": "object",
        "required": ["repo_path"],
        "properties": {
            "repo_path": {
                "type": "string",
                "description": "Path to the product repository to analyze (read-only)",
            },
            "output_path": {
                "type": "string",
                "description": (
                    "Optional path to write the scan_report JSON (should live "
                    "under projects/<id>/artifacts/). Rejected if it resolves "
                    "inside repo_path — the target repo is never written to."
                ),
            },
            "max_files": {
                "type": "integer",
                "default": 5000,
                "description": "Cap on files walked; larger repos are truncated (reported in the result)",
            },
            "allow_config_execution": {
                "type": "boolean",
                "default": False,
                "description": (
                    "DANGEROUS — opt in to EXECUTING the target repo's "
                    "tailwind.config.js through `node` when static parsing "
                    "cannot resolve the theme. A tailwind config is arbitrary "
                    "JavaScript: requiring it can read secrets, write files, "
                    "and make network calls with your privileges. Only enable "
                    "for a repository you trust; the report records the "
                    "execution in warnings[]."
                ),
            },
        },
    }

    output_schema = {
        "type": "object",
        "properties": {
            "framework": {"type": "string"},
            "styling_systems": {"type": "array"},
            "candidate_files": {"type": "array"},
            "css_custom_properties": {"type": "array"},
            "tailwind_theme": {},
            "tailwind_theme_source": {"type": ["string", "null"]},
            "fonts": {"type": "array"},
            "screen_candidates": {"type": "array"},
            "components_index": {"type": "array"},
            "warnings": {"type": "array"},
            "truncated": {"type": "boolean"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=10, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    idempotency_key_fields = ["repo_path", "max_files", "allow_config_execution"]
    side_effects = [
        "writes scan_report JSON to output_path (never writes to repo_path)",
        "allow_config_execution=true only: runs the target repo's tailwind "
        "config through node, with whatever side effects that config has",
    ]
    user_visible_verification = [
        "Spot-check a few reported css_custom_properties against the cited file+line",
        "Check warnings[] — it reports any execution of target-repo code",
    ]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 15.0

    # ---- execution ----

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        repo = Path(inputs["repo_path"]).expanduser()
        if not repo.is_dir():
            return ToolResult(
                success=False, error=f"repo_path is not a directory: {repo}"
            )
        repo_resolved = repo.resolve()

        # Enforce the "never writes to repo_path" guarantee before scanning:
        # an output_path under the target repo would mutate the analyzed
        # product, and the caller should hear about it up front, not after
        # paying for the walk.
        output_path = inputs.get("output_path")
        out: Path | None = None
        if output_path:
            out = Path(output_path).expanduser()
            if self._is_within(out, repo_resolved):
                return ToolResult(
                    success=False,
                    error=(
                        f"output_path {out} resolves inside repo_path "
                        f"{repo_resolved}. This tool never writes to the "
                        f"analyzed repository — write the scan report under "
                        f"projects/<project-id>/artifacts/ instead."
                    ),
                )

        start = time.time()
        try:
            report = self._scan(
                repo,
                int(inputs.get("max_files", 5000)),
                allow_config_execution=bool(inputs.get("allow_config_execution", False)),
            )
        except Exception as e:
            return ToolResult(success=False, error=f"Repo scan failed: {e}")

        artifacts: list[str] = []
        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2))
            artifacts.append(str(out))

        return ToolResult(
            success=True,
            data=report,
            artifacts=artifacts,
            duration_seconds=round(time.time() - start, 2),
            cost_usd=0.0,
        )

    # ---- scanning ----

    @staticmethod
    def _is_within(candidate: Path, parent_resolved: Path) -> bool:
        """Whether `candidate` would land inside `parent_resolved`.

        Resolves symlinks and `..` so neither can be used to slip a write into
        the target repo. The candidate usually does not exist yet, so
        `resolve()` is used in its non-strict form.
        """
        resolved = candidate.resolve()
        return resolved == parent_resolved or parent_resolved in resolved.parents

    @staticmethod
    def _rel_parts(rel: str) -> tuple[str, ...]:
        """Split a repo-relative path into segments, separator-agnostically.

        Report paths are POSIX-normalized at the walk boundary, but this stays
        tolerant of native separators so a caller (or a future producer of
        these paths) on Windows cannot silently break route derivation.
        """
        return tuple(p for p in re.split(r"[\\/]+", rel) if p and p != ".")

    def _scan(
        self, repo: Path, max_files: int, *, allow_config_execution: bool = False
    ) -> dict[str, Any]:
        files, truncated = self._walk(repo, max_files)
        warnings: list[str] = []

        pkg = self._read_package_json(repo)
        framework = self._detect_framework(pkg)
        candidate_files: list[dict[str, str]] = []
        css_vars: list[dict[str, Any]] = []
        fonts: list[dict[str, Any]] = []
        theme_files: list[Path] = []

        for rel in files:
            path = repo / rel
            name = path.name.lower()
            ext = path.suffix.lower()

            if re.fullmatch(r"tailwind\.config\.(js|cjs|mjs|ts)", name):
                candidate_files.append({"path": rel, "kind": "tailwind_config",
                                        "reason": "Tailwind theme configuration"})
                theme_files.append(path)
                continue

            if ext in _STYLE_EXTS:
                text = self._read_text(path)
                if text is None:
                    continue
                has_root = ":root" in text
                has_theme = bool(_THEME_BLOCK_RE.search(text))
                if has_root or has_theme:
                    candidate_files.append({
                        "path": rel,
                        "kind": "theme_css" if has_theme else "css_variables",
                        "reason": "@theme block (Tailwind v4)" if has_theme
                        else ":root custom properties",
                    })
                    css_vars.extend(self._parse_css_vars(text, rel))
                if "@font-face" in text:
                    for m in _FONT_FACE_RE.finditer(text):
                        family = m.group(1).strip()
                        if family and not family.startswith("var("):
                            fonts.append({"family": family, "source": "font-face",
                                          "file": rel})
                continue

            if ext in _SOURCE_EXTS:
                if "theme" in name or "tokens" in name or "design" in name:
                    candidate_files.append({"path": rel, "kind": "theme_source",
                                            "reason": "theme/token module by name"})
                if framework in ("next", "react") and (
                    name.startswith("layout.") or name.startswith("_app.")
                    or name.startswith("_document.")
                ):
                    text = self._read_text(path)
                    if text:
                        for m in _NEXT_FONT_RE.finditer(text):
                            for f in m.group(1).split(","):
                                f = f.strip()
                                if f:
                                    fonts.append({"family": f, "source": "next/font",
                                                  "file": rel})
                        candidate_files.append({"path": rel, "kind": "app_shell",
                                                "reason": "root layout / app shell"})

        # de-dup fonts deterministically
        seen: set[tuple[str, str]] = set()
        fonts = [f for f in fonts
                 if (key := (f["family"].lower(), f["file"])) not in seen
                 and not seen.add(key)]

        theme, theme_source = self._read_tailwind_theme(
            theme_files, allow_config_execution=allow_config_execution,
            warnings=warnings,
        )

        report: dict[str, Any] = {
            "repo_path": str(repo),
            "framework": framework,
            "styling_systems": self._detect_styling_systems(pkg, candidate_files, css_vars, files),
            "candidate_files": candidate_files,
            "css_custom_properties": css_vars,
            "tailwind_theme": theme,
            "tailwind_theme_source": theme_source,
            "fonts": fonts,
            "screen_candidates": self._index_screens(files, framework),
            "components_index": self._index_components(files),
            "files_scanned": len(files),
            "warnings": warnings,
            "truncated": truncated,
        }
        return report

    def _walk(self, repo: Path, max_files: int) -> tuple[list[str], bool]:
        """Sorted, capped, skip-listed walk.

        Returns repo-relative paths in POSIX form on every platform: these
        strings are contract values (they end up in artifact provenance and
        drive candidate classification and route derivation), so they must not
        vary with the host separator.
        """
        collected: list[str] = []
        truncated = False
        for root, dirs, filenames in os.walk(repo):
            dirs[:] = sorted(d for d in dirs
                             if d not in _SKIP_DIRS and not d.startswith("."))
            rel_root = Path(root).relative_to(repo).as_posix()
            for fname in sorted(filenames):
                if len(collected) >= max_files:
                    return collected, True
                rel = fname if rel_root == "." else f"{rel_root}/{fname}"
                collected.append(rel)
        return collected, truncated

    @staticmethod
    def _read_text(path: Path, limit: int = 512_000) -> str | None:
        try:
            if path.stat().st_size > limit:
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _read_package_json(self, repo: Path) -> dict[str, Any]:
        pkg_path = repo / "package.json"
        text = self._read_text(pkg_path)
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _detect_framework(pkg: dict[str, Any]) -> str:
        deps: dict[str, str] = {}
        for key in ("dependencies", "devDependencies"):
            deps.update(pkg.get(key) or {})
        if "next" in deps:
            return "next"
        if "nuxt" in deps or "nuxt3" in deps:
            return "nuxt"
        if "svelte" in deps or "@sveltejs/kit" in deps:
            return "svelte"
        if "vue" in deps:
            return "vue"
        if "react" in deps:
            return "react"
        return "other"

    @staticmethod
    def _detect_styling_systems(
        pkg: dict[str, Any],
        candidate_files: list[dict[str, str]],
        css_vars: list[dict[str, Any]],
        files: list[str],
    ) -> list[str]:
        deps: dict[str, str] = {}
        for key in ("dependencies", "devDependencies"):
            deps.update(pkg.get(key) or {})
        systems: list[str] = []
        if "tailwindcss" in deps or any(
            c["kind"] in ("tailwind_config", "theme_css") for c in candidate_files
        ):
            systems.append("tailwind")
        if css_vars:
            systems.append("css-variables")
        if "styled-components" in deps:
            systems.append("styled-components")
        if any(d.startswith("@emotion/") for d in deps):
            systems.append("emotion")
        if any(f.endswith((".module.css", ".module.scss")) for f in files):
            systems.append("css-modules")
        if "sass" in deps or "node-sass" in deps or any(
            f.endswith((".scss", ".sass")) for f in files
        ):
            systems.append("sass")
        return systems

    @staticmethod
    def _parse_css_vars(text: str, rel: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i, line in enumerate(text.splitlines(), start=1):
            for m in _CSS_VAR_RE.finditer(line):
                out.append({
                    "name": m.group(1),
                    "value": m.group(2).strip(),
                    "file": rel,
                    "line": i,
                })
        return out

    # ---- tailwind config ----

    def _read_tailwind_theme(
        self,
        theme_files: list[Path],
        *,
        allow_config_execution: bool,
        warnings: list[str],
    ) -> tuple[Any, str | None]:
        """Resolve the tailwind `theme` object without running the repo.

        Static parsing first (works for both .js and .ts configs). Execution is
        attempted only when the caller explicitly opted in, and is recorded in
        `warnings` so the risk is never invisible in the run record.

        Returns (theme_or_None, source) where source is "static", "executed",
        or None.
        """
        for cfg in theme_files:
            text = self._read_text(cfg)
            if text is None:
                continue
            theme = self._static_parse_theme(text)
            if theme is not None:
                return theme, "static"

        if not theme_files:
            return None, None

        if not allow_config_execution:
            warnings.append(
                "tailwind config theme could not be resolved statically "
                "(it computes values at runtime). Read the config file "
                "directly — it is listed in candidate_files. Executing it is "
                "available via allow_config_execution=true, which runs the "
                "target repo's JavaScript and is unsafe for untrusted repos."
            )
            return None, None

        node = shutil.which("node")
        if not node:
            warnings.append(
                "allow_config_execution=true but `node` is not on PATH; "
                "tailwind_theme stays null."
            )
            return None, None

        for cfg in theme_files:
            if cfg.suffix == ".ts":
                continue
            expr = (
                f"const c = require({json.dumps(str(cfg))});"
                "JSON.stringify((c && (c.theme || (c.default && c.default.theme))) || null)"
            )
            try:
                proc = subprocess.run(
                    [node, "-p", expr],
                    capture_output=True, text=True, timeout=20,
                    cwd=str(cfg.parent),
                )
                if proc.returncode == 0 and proc.stdout.strip() not in ("", "null"):
                    warnings.append(
                        f"EXECUTED target-repo code: required {cfg.name} through "
                        f"node under the allow_config_execution opt-in. Any side "
                        f"effects in that config ran with this process's "
                        f"privileges."
                    )
                    return json.loads(proc.stdout), "executed"
            except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
                continue

        warnings.append(
            "allow_config_execution=true but no tailwind config could be "
            "evaluated; tailwind_theme stays null."
        )
        return None, None

    @staticmethod
    def _static_parse_theme(text: str) -> Any:
        """Extract a `theme: { ... }` object literal and convert it to JSON.

        Deliberately conservative: a literal containing anything that needs
        evaluation (function calls, spreads, template literals, `require`)
        returns None rather than a partial guess. The agent then reads the
        config file directly — that is the documented fallback.
        """
        match = _JS_THEME_KEY_RE.search(text)
        if not match:
            return None

        # Balanced-brace scan from the literal's opening brace. Quoted strings
        # are skipped so a `{` inside a value can't unbalance the scan.
        start = match.end() - 1
        depth = 0
        quote: str | None = None
        end = None
        for i in range(start, len(text)):
            ch = text[i]
            if quote:
                if ch == "\\":
                    continue
                if ch == quote:
                    quote = None
                continue
            if ch in "\"'`":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            return None

        src = text[start:end]
        src = _JS_BLOCK_COMMENT_RE.sub("", src)
        src = _JS_LINE_COMMENT_RE.sub("", src)
        if any(token in src for token in _JS_DYNAMIC_TOKENS):
            return None

        src = _JS_SINGLE_QUOTED_RE.sub(lambda m: json.dumps(m.group(1)), src)
        src = _JS_BARE_KEY_RE.sub(lambda m: f'{m.group(1)}"{m.group(2)}":', src)
        src = _JS_TRAILING_COMMA_RE.sub(r"\1", src)
        try:
            parsed = json.loads(src)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    # ---- indexing ----

    @staticmethod
    def _index_screens(files: list[str], framework: str) -> list[dict[str, Any]]:
        """Index screen candidates, deriving a route only where the framework
        convention actually determines one.

        Next's app/pages routers and SvelteKit map directory structure to URLs
        by documented convention, so a route is derivable from the path alone.
        Roots like src/views, src/screens, and src/features do not — their URLs
        live in a router config this scanner does not read. Those are reported
        with `route: None` and `route_source: "not_derivable"` rather than a
        route synthesized from the filename, which would look authoritative
        while being invented.
        """
        screens: list[dict[str, Any]] = []
        for rel in files:
            parts = RepoDesignExtractor._rel_parts(rel)
            if not parts:
                continue
            leaf = Path(parts[-1])
            if leaf.suffix.lower() not in _SOURCE_EXTS:
                continue

            root = None
            for candidate in _SCREEN_ROOTS:
                croot = tuple(candidate.split("/"))
                if parts[: len(croot)] == croot:
                    root = candidate
                    break
            if root is None:
                continue

            inner = parts[len(root.split("/")):]
            if not inner:
                continue
            stem = leaf.stem
            stem_l = stem.lower()
            route: str | None = None
            route_source = "not_derivable"

            if root in ("app", "src/app"):
                # Next app router: only page files are screens. Route groups
                # like (auth) organize files without affecting the URL.
                if stem_l != "page":
                    continue
                segs = [s for s in inner[:-1]
                        if not (s.startswith("(") and s.endswith(")"))]
                route = "/" + "/".join(segs)
                route_source = "next-app-router"
            elif root in ("pages", "src/pages"):
                if stem.startswith("_") or inner[:1] == ("api",):
                    continue
                segs = list(inner[:-1]) + ([] if stem_l == "index" else [stem])
                route = "/" + "/".join(segs)
                route_source = "next-pages-router"
            elif root == "src/routes" and framework == "svelte":
                # SvelteKit: only +page files are screens.
                if stem_l != "+page":
                    continue
                route = "/" + "/".join(inner[:-1])
                route_source = "sveltekit"

            screens.append({
                "path": rel,
                "route": route,
                "route_source": route_source,
                "root": root,
            })
        return screens

    @staticmethod
    def _index_components(files: list[str]) -> list[dict[str, str]]:
        comps: list[dict[str, str]] = []
        for rel in files:
            parts = RepoDesignExtractor._rel_parts(rel)
            if not parts:
                continue
            leaf = Path(parts[-1])
            for candidate in _COMPONENT_ROOTS:
                croot = tuple(candidate.split("/"))
                if parts[: len(croot)] == croot and leaf.suffix.lower() in _SOURCE_EXTS:
                    if leaf.stem.lower() in ("index", "types", "utils"):
                        break
                    comps.append({"name": leaf.stem, "path": rel})
                    break
        return comps
