"""Apply AstrBot's session controls to a background Agent event."""

from __future__ import annotations


async def session_access(fw, umo, cfg):
    """Return a blocking reason and the effective plugin names (None means all)."""
    services = fw.SessionServiceManager
    if not await services.is_session_enabled(umo):
        return "host_session_disabled", None
    if not await services.is_llm_enabled_for_session(umo):
        return "host_llm_disabled", None
    if not cfg.get("provider_settings", {}).get("enable", True):
        return "model_disabled", None

    configured = cfg.get("plugin_set", ["*"])
    names = {name.rsplit("/", 1)[-1] for name in configured}
    if configured != ["*"] and "astrbot_plugin_heartbeat" not in names:
        return "plugin_disabled_for_session", None
    rules = await fw.sp.get_async(
        scope="umo", scope_id=umo, key="session_plugin_config", default={}
    )
    disabled = set((rules or {}).get(umo, {}).get("disabled_plugins", []))
    if "astrbot_plugin_heartbeat" in disabled:
        return "plugin_disabled_for_session", None
    if configured == ["*"]:
        if not disabled:
            return "", None
        names = {plugin.name for plugin in fw.star_map.values() if plugin.name}
    return "", sorted(names - disabled)
