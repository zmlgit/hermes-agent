## [LRN-20260710-001] correction

**Logged**: 2026-07-10T10:06:00+08:00
**Priority**: high
**Status**: pending
**Area**: backend

### Summary
Feishu is no longer a port-binding platform in current Hermes multiplex mode.

### Details
The old gateway guard treated `feishu` like `webhook` and rejected secondary profiles when `gateway.multiplex_profiles` was enabled. Current Feishu/Lark support uses outbound websocket mode, so different profiles may configure different Feishu apps.

### Suggested Action
Do not remove per-profile Feishu config to fix multiplex startup. Remove stale `feishu` entries from port-binding guards and keep regression tests proving secondary-profile Feishu is allowed.

### Metadata
- Source: user_feedback
- Related Files: gateway/run.py, tests/gateway/test_multiplex_adapter_registry.py
- Tags: gateway, multiplex, feishu

---
