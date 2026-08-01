"""Focused tests for the repo design-system scanner.

Builds fixture mini-repos in tmp_path and asserts framework detection,
CSS-custom-property parsing with file+line provenance, candidate-file
classification, POSIX path normalization (contract paths must not vary with
the host separator), route derivability, the no-target-code-execution
guarantee, caps, and determinism. No network.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.base_tool import BaseTool, ToolStatus, ToolTier, ToolRuntime
from tools.tool_registry import ToolRegistry
from tools.analysis.repo_design_extractor import RepoDesignExtractor


@pytest.fixture
def next_repo(tmp_path):
    """Minimal Next.js + Tailwind repo with tokens, fonts, screens, components."""
    repo = tmp_path / "next-app"
    (repo / "app" / "settings").mkdir(parents=True)
    (repo / "components" / "ui").mkdir(parents=True)
    (repo / "node_modules" / "junk").mkdir(parents=True)

    (repo / "package.json").write_text(
        '{"name":"mini","dependencies":{"next":"14.0.0","react":"18.2.0"},'
        '"devDependencies":{"tailwindcss":"3.4.0"}}'
    )
    (repo / "tailwind.config.js").write_text(
        'module.exports = { theme: { extend: { colors: { brand: "#6366f1" } } } };'
    )
    (repo / "app" / "globals.css").write_text(
        ":root {\n"
        "  --background: #0a0a0f;\n"
        "  --primary: #6366f1;\n"
        "}\n"
        '@font-face { font-family: "Cal Sans"; src: url(/cal.woff2); }\n'
    )
    (repo / "app" / "layout.tsx").write_text(
        'import { Inter } from "next/font/google"\n'
        "export default function Layout({children}) { return <body>{children}</body> }\n"
    )
    (repo / "app" / "page.tsx").write_text("export default function Page(){return null}")
    (repo / "app" / "settings" / "page.tsx").write_text(
        "export default function Page(){return null}"
    )
    (repo / "components" / "ui" / "button.tsx").write_text("export function Button(){}")
    (repo / "node_modules" / "junk" / "x.css").write_text(":root { --evil: red; }")
    return repo


@pytest.fixture
def vue_repo(tmp_path):
    repo = tmp_path / "vue-app"
    (repo / "src" / "views").mkdir(parents=True)
    (repo / "src" / "components").mkdir(parents=True)
    (repo / "package.json").write_text('{"dependencies":{"vue":"3.4.0"}}')
    (repo / "src" / "views" / "Dashboard.vue").write_text("<template><main/></template>")
    (repo / "src" / "components" / "StatCard.vue").write_text("<template><div/></template>")
    return repo


@pytest.fixture
def svelte_repo(tmp_path):
    repo = tmp_path / "svelte-app"
    (repo / "src" / "routes" / "settings").mkdir(parents=True)
    (repo / "package.json").write_text(
        '{"devDependencies":{"@sveltejs/kit":"2.0.0","svelte":"4.0.0"}}'
    )
    (repo / "src" / "routes" / "+page.svelte").write_text("<main/>")
    (repo / "src" / "routes" / "+layout.svelte").write_text("<slot/>")
    (repo / "src" / "routes" / "settings" / "+page.svelte").write_text("<main/>")
    return repo


# ---- Contract ----

class TestContract:
    def test_inherits_base_tool(self):
        assert issubclass(RepoDesignExtractor, BaseTool)

    def test_identity(self):
        t = RepoDesignExtractor()
        assert t.name == "repo_design_extractor"
        assert t.capability == "analysis"
        assert t.runtime == ToolRuntime.LOCAL
        assert t.tier == ToolTier.ANALYZE
        assert "repo-design-extraction" in t.agent_skills

    def test_always_available_and_free(self):
        t = RepoDesignExtractor()
        assert t.get_status() == ToolStatus.AVAILABLE
        assert t.estimate_cost({}) == 0.0


# ---- Registry discovery ----

class TestDiscovery:
    def test_discoverable(self):
        reg = ToolRegistry()
        reg.discover("tools")
        assert reg.get("repo_design_extractor") is not None

    def test_capability_routing(self):
        reg = ToolRegistry()
        reg.discover("tools")
        names = [t.name for t in reg.get_by_capability("analysis")]
        assert "repo_design_extractor" in names


# ---- Scan behavior ----

class TestScan:
    def test_framework_and_styling_detection(self, next_repo):
        res = RepoDesignExtractor().execute({"repo_path": str(next_repo)})
        assert res.success
        assert res.data["framework"] == "next"
        assert "tailwind" in res.data["styling_systems"]
        assert "css-variables" in res.data["styling_systems"]

    def test_vue_framework_detection(self, vue_repo):
        res = RepoDesignExtractor().execute({"repo_path": str(vue_repo)})
        assert res.success
        assert res.data["framework"] == "vue"
        # views become screen candidates; components indexed
        assert any(s["path"].endswith("Dashboard.vue") for s in res.data["screen_candidates"])
        assert any(c["name"] == "StatCard" for c in res.data["components_index"])

    def test_css_vars_carry_file_and_line_provenance(self, next_repo):
        res = RepoDesignExtractor().execute({"repo_path": str(next_repo)})
        by_name = {v["name"]: v for v in res.data["css_custom_properties"]}
        assert by_name["--primary"]["value"] == "#6366f1"
        assert by_name["--primary"]["file"] == "app/globals.css"
        assert by_name["--primary"]["line"] == 3
        assert by_name["--background"]["line"] == 2

    def test_node_modules_skipped(self, next_repo):
        res = RepoDesignExtractor().execute({"repo_path": str(next_repo)})
        assert not any(v["name"] == "--evil" for v in res.data["css_custom_properties"])
        assert not any("node_modules" in c["path"] for c in res.data["candidate_files"])

    def test_candidate_classification(self, next_repo):
        res = RepoDesignExtractor().execute({"repo_path": str(next_repo)})
        kinds = {c["path"]: c["kind"] for c in res.data["candidate_files"]}
        assert kinds["tailwind.config.js"] == "tailwind_config"
        assert kinds["app/globals.css"] == "css_variables"
        assert kinds["app/layout.tsx"] == "app_shell"

    def test_fonts_from_font_face_and_next_font(self, next_repo):
        res = RepoDesignExtractor().execute({"repo_path": str(next_repo)})
        families = {(f["family"], f["source"]) for f in res.data["fonts"]}
        assert ("Cal Sans", "font-face") in families
        assert ("Inter", "next/font") in families

    def test_max_files_reports_truncation(self, next_repo):
        res = RepoDesignExtractor().execute({"repo_path": str(next_repo), "max_files": 2})
        assert res.success
        assert res.data["truncated"] is True
        assert res.data["files_scanned"] == 2

    def test_deterministic(self, next_repo):
        t = RepoDesignExtractor()
        a = t.execute({"repo_path": str(next_repo)}).data
        b = t.execute({"repo_path": str(next_repo)}).data
        assert a == b

    def test_writes_scan_report_artifact(self, next_repo, tmp_path):
        out = tmp_path / "artifacts" / "scan.json"
        res = RepoDesignExtractor().execute(
            {"repo_path": str(next_repo), "output_path": str(out)}
        )
        assert res.success
        assert res.artifacts == [str(out)]
        assert out.exists()

    def test_missing_repo_errors(self, tmp_path):
        res = RepoDesignExtractor().execute({"repo_path": str(tmp_path / "nope")})
        assert not res.success
        assert "not a directory" in res.error

    def test_never_writes_into_target_repo(self, next_repo):
        before = sorted(p.relative_to(next_repo) for p in next_repo.rglob("*"))
        RepoDesignExtractor().execute({"repo_path": str(next_repo)})
        after = sorted(p.relative_to(next_repo) for p in next_repo.rglob("*"))
        assert before == after


# ---- Route derivation ----

class TestRouteDerivation:
    """A route is emitted only where a framework convention determines it.

    Synthesizing `/` + filename for non-routed roots produced authoritative-
    looking routes that the scanner cannot actually know.
    """

    def test_app_router_routes(self, next_repo):
        res = RepoDesignExtractor().execute({"repo_path": str(next_repo)})
        screens = {s["path"]: s for s in res.data["screen_candidates"]}
        assert screens["app/page.tsx"]["route"] == "/"
        assert screens["app/page.tsx"]["route_source"] == "next-app-router"
        assert screens["app/settings/page.tsx"]["route"] == "/settings"
        # layout is not a screen
        assert "app/layout.tsx" not in screens

    def test_pages_router_routes(self, tmp_path):
        repo = tmp_path / "pages-app"
        (repo / "pages" / "billing").mkdir(parents=True)
        (repo / "pages" / "api").mkdir(parents=True)
        (repo / "package.json").write_text('{"dependencies":{"next":"13.0.0"}}')
        (repo / "pages" / "index.tsx").write_text("export default function P(){}")
        (repo / "pages" / "billing" / "plans.tsx").write_text("export default function P(){}")
        (repo / "pages" / "_app.tsx").write_text("export default function A(){}")
        (repo / "pages" / "api" / "hook.ts").write_text("export default function H(){}")

        res = RepoDesignExtractor().execute({"repo_path": str(repo)})
        screens = {s["path"]: s for s in res.data["screen_candidates"]}
        assert screens["pages/index.tsx"]["route"] == "/"
        assert screens["pages/billing/plans.tsx"]["route"] == "/billing/plans"
        assert screens["pages/billing/plans.tsx"]["route_source"] == "next-pages-router"
        # underscore files and API handlers are not screens
        assert "pages/_app.tsx" not in screens
        assert "pages/api/hook.ts" not in screens

    def test_sveltekit_routes(self, svelte_repo):
        res = RepoDesignExtractor().execute({"repo_path": str(svelte_repo)})
        assert res.data["framework"] == "svelte"
        screens = {s["path"]: s for s in res.data["screen_candidates"]}
        assert screens["src/routes/+page.svelte"]["route"] == "/"
        assert screens["src/routes/settings/+page.svelte"]["route"] == "/settings"
        assert screens["src/routes/settings/+page.svelte"]["route_source"] == "sveltekit"
        # layouts are not screens
        assert "src/routes/+layout.svelte" not in screens

    def test_non_routed_root_reports_no_route(self, vue_repo):
        """src/views has no path→URL convention; the route must not be invented."""
        res = RepoDesignExtractor().execute({"repo_path": str(vue_repo)})
        views = [s for s in res.data["screen_candidates"]
                 if s["path"] == "src/views/Dashboard.vue"]
        assert len(views) == 1
        assert views[0]["route"] is None
        assert views[0]["route_source"] == "not_derivable"

    def test_src_routes_without_sveltekit_is_not_derivable(self, tmp_path):
        repo = tmp_path / "solid-ish"
        (repo / "src" / "routes").mkdir(parents=True)
        (repo / "package.json").write_text('{"dependencies":{"react":"18.2.0"}}')
        (repo / "src" / "routes" / "Home.tsx").write_text("export default function H(){}")

        res = RepoDesignExtractor().execute({"repo_path": str(repo)})
        screen = res.data["screen_candidates"][0]
        assert screen["route"] is None
        assert screen["route_source"] == "not_derivable"


# ---- Path normalization (Windows contract) ----

class TestPathNormalization:
    """Repo-relative paths are contract values: they carry into artifact
    provenance and drive classification and route derivation. They must be
    POSIX on every host, and parsing must not assume the host separator.
    """

    def test_all_contract_paths_are_posix(self, next_repo):
        res = RepoDesignExtractor().execute({"repo_path": str(next_repo)})
        emitted = (
            [c["path"] for c in res.data["candidate_files"]]
            + [v["file"] for v in res.data["css_custom_properties"]]
            + [f["file"] for f in res.data["fonts"]]
            + [s["path"] for s in res.data["screen_candidates"]]
            + [c["path"] for c in res.data["components_index"]]
        )
        assert emitted, "fixture should produce paths to check"
        assert not [p for p in emitted if "\\" in p]
        assert "app/globals.css" in emitted
        assert "components/ui/button.tsx" in emitted

    def test_walk_emits_posix_relative_paths(self, next_repo):
        files, _ = RepoDesignExtractor()._walk(next_repo, 5000)
        assert "app/globals.css" in files
        assert "app/settings/page.tsx" in files
        assert not [f for f in files if "\\" in f]

    def test_screen_indexing_tolerates_native_separators(self):
        """Windows-shaped input must still resolve routes, not fall through."""
        screens = RepoDesignExtractor()._index_screens(
            ["app\\page.tsx", "app\\settings\\page.tsx"], "next"
        )
        routes = {s["path"]: s["route"] for s in screens}
        assert routes["app\\page.tsx"] == "/"
        assert routes["app\\settings\\page.tsx"] == "/settings"

    def test_component_indexing_tolerates_native_separators(self):
        comps = RepoDesignExtractor()._index_components(["components\\ui\\button.tsx"])
        assert [c["name"] for c in comps] == ["button"]

    def test_route_groups_stripped_with_native_separators(self):
        screens = RepoDesignExtractor()._index_screens(
            ["app\\(marketing)\\pricing\\page.tsx"], "next"
        )
        assert screens[0]["route"] == "/pricing"


# ---- Target-repo code execution ----

class TestNoTargetCodeExecution:
    """A tailwind config is executable JavaScript. Scanning an untrusted repo
    must not run it: the theme is parsed statically, and execution is an
    explicit opt-in that is recorded in the report.
    """

    def test_static_parse_resolves_js_theme_without_node(self, next_repo, monkeypatch):
        def fail(*a, **k):  # pragma: no cover - must never be reached
            raise AssertionError("node must not be invoked by default")

        monkeypatch.setattr(subprocess, "run", fail)
        res = RepoDesignExtractor().execute({"repo_path": str(next_repo)})
        assert res.success
        assert res.data["tailwind_theme"]["extend"]["colors"]["brand"] == "#6366f1"
        assert res.data["tailwind_theme_source"] == "static"

    def test_static_parse_handles_ts_config(self, tmp_path):
        """TS configs used to be skipped entirely; static parsing reads them."""
        repo = tmp_path / "ts-app"
        repo.mkdir()
        (repo / "package.json").write_text(
            '{"devDependencies":{"tailwindcss":"3.4.0"}}'
        )
        (repo / "tailwind.config.ts").write_text(
            "import type { Config } from 'tailwindcss'\n"
            "export default {\n"
            "  content: ['./app/**/*.tsx'],\n"
            "  theme: {\n"
            "    // brand scale\n"
            "    extend: { borderRadius: { lg: '12px' } },\n"
            "  },\n"
            "} satisfies Config\n"
        )
        res = RepoDesignExtractor().execute({"repo_path": str(repo)})
        assert res.data["tailwind_theme"]["extend"]["borderRadius"]["lg"] == "12px"
        assert res.data["tailwind_theme_source"] == "static"

    def test_dynamic_config_is_refused_not_executed(self, tmp_path, monkeypatch):
        repo = tmp_path / "dyn-app"
        repo.mkdir()
        (repo / "package.json").write_text(
            '{"devDependencies":{"tailwindcss":"3.4.0"}}'
        )
        (repo / "tailwind.config.js").write_text(
            "const preset = require('./preset');\n"
            "module.exports = { theme: { extend: preset.buildColors() } };\n"
        )

        def fail(*a, **k):  # pragma: no cover - must never be reached
            raise AssertionError("node must not be invoked without opt-in")

        monkeypatch.setattr(subprocess, "run", fail)
        res = RepoDesignExtractor().execute({"repo_path": str(repo)})
        assert res.success
        assert res.data["tailwind_theme"] is None
        assert res.data["tailwind_theme_source"] is None
        assert any("allow_config_execution" in w for w in res.data["warnings"])

    def test_opt_in_execution_is_surfaced_in_warnings(self, tmp_path):
        if not shutil.which("node"):
            pytest.skip("node not on PATH")
        repo = tmp_path / "dyn-app"
        repo.mkdir()
        (repo / "package.json").write_text(
            '{"devDependencies":{"tailwindcss":"3.4.0"}}'
        )
        (repo / "tailwind.config.js").write_text(
            "function build() { return { colors: { brand: '#0ea5e9' } }; }\n"
            "module.exports = { theme: { extend: build() } };\n"
        )
        res = RepoDesignExtractor().execute(
            {"repo_path": str(repo), "allow_config_execution": True}
        )
        assert res.success
        assert res.data["tailwind_theme"]["extend"]["colors"]["brand"] == "#0ea5e9"
        assert res.data["tailwind_theme_source"] == "executed"
        assert any("EXECUTED target-repo code" in w for w in res.data["warnings"])

    def test_opt_in_without_node_degrades_to_null(self, next_repo, monkeypatch, tmp_path):
        repo = tmp_path / "dyn-app"
        repo.mkdir()
        (repo / "tailwind.config.js").write_text(
            "module.exports = { theme: { extend: build() } };\n"
        )
        monkeypatch.setattr(shutil, "which", lambda _: None)
        res = RepoDesignExtractor().execute(
            {"repo_path": str(repo), "allow_config_execution": True}
        )
        assert res.success
        assert res.data["tailwind_theme"] is None
        assert any("node" in w for w in res.data["warnings"])

    def test_opt_in_is_part_of_the_idempotency_key(self):
        t = RepoDesignExtractor()
        safe = t.idempotency_key({"repo_path": "/x"})
        unsafe = t.idempotency_key({"repo_path": "/x", "allow_config_execution": True})
        assert safe != unsafe


# ---- output_path guard ----

class TestOutputPathGuard:
    """"never writes to repo_path" is a guarantee, so it is enforced."""

    def test_output_inside_repo_rejected(self, next_repo):
        res = RepoDesignExtractor().execute({
            "repo_path": str(next_repo),
            "output_path": str(next_repo / "scan.json"),
        })
        assert not res.success
        assert "inside repo_path" in res.error
        assert not (next_repo / "scan.json").exists()

    def test_nested_output_inside_repo_rejected(self, next_repo):
        res = RepoDesignExtractor().execute({
            "repo_path": str(next_repo),
            "output_path": str(next_repo / "app" / "artifacts" / "scan.json"),
        })
        assert not res.success
        assert "inside repo_path" in res.error

    def test_traversal_into_repo_rejected(self, next_repo, tmp_path):
        sneaky = tmp_path / "elsewhere" / ".." / "next-app" / "scan.json"
        res = RepoDesignExtractor().execute({
            "repo_path": str(next_repo),
            "output_path": str(sneaky),
        })
        assert not res.success
        assert "inside repo_path" in res.error

    def test_output_outside_repo_allowed(self, next_repo, tmp_path):
        out = tmp_path / "projects" / "p" / "artifacts" / "scan.json"
        res = RepoDesignExtractor().execute(
            {"repo_path": str(next_repo), "output_path": str(out)}
        )
        assert res.success
        assert out.exists()
