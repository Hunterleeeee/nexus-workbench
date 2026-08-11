import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app


class AIBrowserPlanTests(unittest.TestCase):
    def test_browser_plan_accepts_only_known_structured_actions(self):
        answer = """```json
        {"summary":"先填搜索框再点击", "actions":[
          {"type":"fill", "element_id":"wb-2", "value":"耳机", "reason":"输入关键词"},
          {"type":"click", "element_id":"wb-3", "reason":"开始搜索"},
          {"type":"click", "element_id":"wb-999", "reason":"不存在"},
          {"type":"script", "value":"alert(1)"}
        ]}
        ```"""
        plan = app.parse_browser_action_plan(answer, {"wb-2", "wb-3"})
        self.assertEqual([item["type"] for item in plan["actions"]], ["fill", "click"])
        self.assertEqual(plan["actions"][0]["value"], "耳机")
        self.assertNotIn("wb-999", str(plan))
        self.assertNotIn("script", str(plan))

    def test_browser_plan_rejects_navigation_without_http_url(self):
        plan = app.parse_browser_action_plan(
            '{"actions":[{"type":"navigate","url":"javascript:alert(1)"},{"type":"scroll","amount":999999}]}',
            set(),
        )
        self.assertEqual(len(plan["actions"]), 1)
        self.assertEqual(plan["actions"][0], {"type": "scroll", "reason": "", "amount": 1600})

    def test_browser_plan_supports_explicit_page_edges(self):
        plan = app.parse_browser_action_plan(
            '{"actions":[{"type":"scroll","edge":"top","amount":-999999}]}',
            set(),
        )
        self.assertEqual(plan["actions"][0]["edge"], "top")
        self.assertEqual(plan["actions"][0]["amount"], -1600)

    def test_live_page_context_is_bounded(self):
        request = app.ChatRequest(run_id="run-1", message="这页在说什么？", live_context="实时页面")
        self.assertEqual(request.live_context, "实时页面")
        with self.assertRaises(Exception):
            app.ChatRequest(run_id="run-1", message="问题", live_context="x" * 12_001)


class AIBrowserDesktopSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.main = (cls.root / "desktop" / "main.cjs").read_text(encoding="utf-8")
        cls.preload = (cls.root / "desktop" / "preload.cjs").read_text(encoding="utf-8")
        cls.browser = (cls.root / "static" / "web-research.js").read_text(encoding="utf-8")
        cls.page = (cls.root / "static" / "web-research.html").read_text(encoding="utf-8")

    def test_each_internal_tab_owns_a_real_page_view(self):
        self.assertIn("workspace.docks.set(browserTabId, dock)", self.main)
        self.assertIn("workspace.activeDockId === dock.browserTabId", self.main)
        self.assertIn("browserTabId: dock.browserTabId", self.main)
        self.assertIn('nativeBrowser.open(tabId, url, bounds)', self.browser)
        self.assertIn('nativeBrowser.close(id)', self.browser)
        self.assertIn('event.key === "Tab"', self.browser)
        self.assertIn('workbench-web-research-active-context-v1', self.browser)
        self.assertIn('context.analysisPendingUrl === snapshotUrl', self.browser)

    def test_bookmarks_are_durable_and_manageable(self):
        self.assertIn('browser_bookmarks.json', self.main)
        self.assertIn('browser-bookmarks-save', self.preload)
        self.assertIn('id="bookmark-current"', self.page)
        self.assertIn('id="bookmark-search"', self.page)
        self.assertIn("function openBookmark", self.browser)

    def test_password_vault_uses_os_encryption_and_exact_origin(self):
        self.assertIn("safeStorage.encryptString", self.main)
        self.assertIn("safeStorage.decryptString", self.main)
        self.assertIn('browser_credentials.json', self.main)
        self.assertIn("mode: 0o600", self.main)
        self.assertIn("item.origin === origin", self.main)
        self.assertIn('item.id === String(rawCredentialId || "") && item.origin === origin', self.main)
        self.assertIn('id="credential-capture"', self.page)
        self.assertIn('id="credential-password"', self.page)
        self.assertIn("不会自动登录", self.browser)

    def test_ai_snapshot_never_reads_password_values(self):
        snapshot = self.main.split("const BROWSER_DOCK_SNAPSHOT_SCRIPT", 1)[1].split("const BROWSER_CREDENTIAL_CAPTURE_SCRIPT", 1)[0]
        self.assertIn('inputType', snapshot)
        self.assertNotIn("element.value", snapshot)
        self.assertNotIn("passwordInput.value", snapshot)
        self.assertIn('AI 不能填写密码', self.main)


class MarketDecisionCenterTests(unittest.TestCase):
    @staticmethod
    def history(symbol="sh600000", count=10):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        return [
            {
                "checked_at": (start + timedelta(days=index)).isoformat(),
                "source": "fixture",
                "quotes": [{"symbol": symbol, "price": 80 + index * 2, "volume": 1000 + index * 10}],
            }
            for index in range(count)
        ]

    def test_decision_center_prioritizes_user_buy_line_and_labels_reference(self):
        snapshot = {
            "checked_at": "2026-08-11T00:00:00+00:00",
            "source": "fixture",
            "quotes": [{"symbol": "sh600000", "name": "浦发银行", "price": 90, "previous_close": 91, "change_pct": -1.1, "volume": 1200}],
        }
        with patch.object(app, "load_market_watchlist", return_value=[{"symbol": "sh600000", "name": "浦发银行"}]), \
             patch.object(app, "market_watchlist_rules", return_value={"sh600000": {"buy_below": 95.0, "sell_above": 115.0, "stop_below": 85.0, "note": "只按计划看"}}), \
             patch.object(app, "analyze_market_snapshot", return_value={"freshness": {"status": "fresh", "label": "数据新鲜"}}):
            decision = app.build_market_decision_center(snapshot, self.history())
        card = decision["cards"][0]
        self.assertEqual(card["group"], "must")
        self.assertEqual(card["action_key"], "buy")
        self.assertTrue(card["reference"]["available"])
        self.assertIn(card["reference"]["quality"], {"medium", "high"})
        self.assertTrue(card["position_example"]["available"])
        self.assertIn("不构成投资建议", decision["disclaimer"])

    def test_decision_center_keeps_missing_quote_as_unknown(self):
        snapshot = {"checked_at": "2026-08-11T00:00:00+00:00", "source": "fixture", "quotes": []}
        with patch.object(app, "load_market_watchlist", return_value=[{"symbol": "sz000001", "name": "平安银行"}]), \
             patch.object(app, "market_watchlist_rules", return_value={}), \
             patch.object(app, "analyze_market_snapshot", return_value={"freshness": {"status": "fresh", "label": "数据新鲜"}}):
            decision = app.build_market_decision_center(snapshot, [])
        self.assertEqual(decision["counts"]["unknown"], 1)
        self.assertEqual(decision["cards"][0]["action_key"], "unknown")
        self.assertFalse(decision["cards"][0]["reference"]["available"])

    def test_today_does_not_render_quotes_removed_from_watchlist(self):
        snapshot = {
            "checked_at": "2026-08-11T00:00:00+00:00",
            "quotes": [{"symbol": "sh600000", "name": "已删除股票", "price": 10, "change_pct": 1.2}],
        }
        with patch.object(app, "load_market_watchlist", return_value=[]), \
             patch.object(app, "market_watchlist_rules", return_value={}):
            today = app.build_market_today(snapshot)
        self.assertEqual(today["cards"], [])
        self.assertEqual(today["counts"]["total"], 0)
        self.assertEqual(today["tone"], "empty")

    def test_loading_snapshot_filters_stale_quotes_by_current_watchlist(self):
        stored = {
            "watchlist": [{"symbol": "sh600000"}, {"symbol": "sz000001"}],
            "quotes": [
                {"symbol": "sh600000", "price": 10},
                {"symbol": "sz000001", "price": 20},
            ],
        }
        with patch.object(app, "load_json_file", return_value=stored), \
             patch.object(app, "load_market_watchlist", return_value=[{"symbol": "sh600000"}]):
            snapshot = app.load_market_snapshot()
        self.assertEqual(snapshot["watchlist"], [{"symbol": "sh600000"}])
        self.assertEqual([item["symbol"] for item in snapshot["quotes"]], ["sh600000"])

    def test_watchlist_edit_keeps_rules_and_clears_removed_quotes(self):
        existing = [
            {"symbol": "sh600000", "buy_below": 9.5, "sell_above": 12.0, "note": "保留计划"},
            {"symbol": "sz000001", "stop_below": 8.0},
        ]
        snapshot = {
            "watchlist": existing,
            "quotes": [
                {"symbol": "sh600000", "price": 10},
                {"symbol": "sz000001", "price": 9},
            ],
            "missing_symbols": ["sz000001"],
            "checked_at": "2026-08-11T00:00:00+00:00",
        }
        with patch.object(app, "load_market_watchlist", return_value=existing), \
             patch.object(app, "save_market_watchlist") as save_watchlist, \
             patch.object(app, "load_market_snapshot", return_value=snapshot), \
             patch.object(app, "save_market_snapshot") as save_snapshot, \
             patch.object(app, "record_market_snapshot", return_value=None), \
             patch.object(app, "list_market_history", return_value=[]), \
             patch.object(app, "list_work_items", return_value=[]), \
             patch.object(app, "analyze_market_snapshot", return_value={}):
            result = asyncio.run(app.update_market_watchlist(app.MarketWatchlistRequest(symbols=["600000"])))
        saved_watchlist = save_watchlist.call_args.args[0]
        saved_snapshot = save_snapshot.call_args.args[0]
        self.assertEqual(saved_watchlist, [{"symbol": "sh600000", "buy_below": 9.5, "sell_above": 12.0, "note": "保留计划"}])
        self.assertEqual([item["symbol"] for item in saved_snapshot["quotes"]], ["sh600000"])
        self.assertEqual(saved_snapshot["missing_symbols"], [])
        self.assertEqual([item["symbol"] for item in result["market"]["quotes"]], ["sh600000"])

    def test_deleting_all_watchlist_items_clears_current_market_snapshot(self):
        existing = [{"symbol": "sh600000", "buy_below": 9.5}]
        snapshot = {
            "watchlist": existing,
            "quotes": [{"symbol": "sh600000", "price": 10}],
            "checked_at": "2026-08-11T00:00:00+00:00",
            "status": "ok",
        }
        with patch.object(app, "load_market_watchlist", return_value=existing), \
             patch.object(app, "save_market_watchlist"), \
             patch.object(app, "load_market_snapshot", return_value=snapshot), \
             patch.object(app, "save_market_snapshot") as save_snapshot, \
             patch.object(app, "record_market_snapshot", return_value=None), \
             patch.object(app, "list_market_history", return_value=[]), \
             patch.object(app, "list_work_items", return_value=[]), \
             patch.object(app, "analyze_market_snapshot", return_value={}):
            result = asyncio.run(app.update_market_watchlist(app.MarketWatchlistRequest(symbols=[])))
        saved_snapshot = save_snapshot.call_args.args[0]
        self.assertEqual(saved_snapshot["watchlist"], [])
        self.assertEqual(saved_snapshot["quotes"], [])
        self.assertEqual(saved_snapshot["checked_at"], "")
        self.assertEqual(saved_snapshot["status"], "empty")
        self.assertEqual(result["market"]["quotes"], [])

    def test_reference_zones_do_not_claim_confidence_with_too_few_samples(self):
        points = self.history(count=4)
        normalized = [
            {"checked_at": item["checked_at"], "price": item["quotes"][0]["price"], "source": item["source"]}
            for item in points
        ]
        reference = app._market_reference_zones(normalized, 86)
        self.assertFalse(reference["available"])
        self.assertEqual(reference["quality"], "low")
        self.assertIsNone(reference["buy_zone"])


if __name__ == "__main__":
    unittest.main()
