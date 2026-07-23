"""Regression: merge resolution once dropped the leading 4-space indent on
``FeishuAdapter._build_outbound_payload``'s ``def`` line, which demoted it to
a module-level function and swallowed every method defined after it (incl.
``_try_mention_post``, ``_release_app_lock``) into an unreachable branch of
that function body. The adapter then lost ~30 methods at runtime and
feishu reconnects failed every 300s with::

    'FeishuAdapter' object has no attribute '_release_app_lock'

This test fails the moment any of those methods disappears from the class
again — typically because someone re-resolved a merge conflict in
adapter.py and lost the indent a second time.
"""
import unittest


class FeishuAdapterClassIntegrityTest(unittest.TestCase):
    def test_critical_methods_remain_on_the_class(self):
        from plugins.platforms.feishu.adapter import FeishuAdapter

        missing = [
            name for name in (
                "_build_outbound_payload",
                "_try_mention_post",
                "_release_app_lock",
                "send",
            )
            if not hasattr(FeishuAdapter, name)
        ]
        self.assertFalse(
            missing,
            f"FeishuAdapter lost methods {missing} — check class-level indent in "
            f"plugins/platforms/feishu/adapter.py (likely _build_outbound_payload "
            f"def line dropped its 4-space indent again).",
        )

    def test_build_outbound_payload_is_a_method_not_module_function(self):
        # If this name reappears at module scope, the def line lost its indent.
        import plugins.platforms.feishu.adapter as mod
        self.assertFalse(
            hasattr(mod, "_build_outbound_payload"),
            "_build_outbound_payload is at module scope — its 'def' line lost the "
            "4-space class indent and is no longer a FeishuAdapter method.",
        )


if __name__ == "__main__":
    unittest.main()
