import unittest

import app


class WebResearchContextTests(unittest.TestCase):
    def test_bookmarklet_context_is_kept_separate_from_research_question(self):
        request = app.CrawlRequest(
            urls=["https://example.com/article"],
            task="核对这段内容",
            source_title="示例文章",
            source_context="用户选中的原文",
        )
        payload = app.crawl_request_payload(request)
        self.assertEqual(payload["source_title"], "示例文章")
        self.assertEqual(payload["source_context"], "用户选中的原文")
        self.assertEqual(payload["task"], "核对这段内容")

    def test_bookmarklet_context_is_visible_to_research_llm_before_crawled_pages(self):
        context = app.context_for_llm(
            {
                "source_title": "示例文章",
                "source_context": "这是用户选中的内容。",
                "documents": [
                    {"url": "https://example.com/article", "title": "示例文章", "markdown": "页面正文。"}
                ],
            }
        )
        self.assertLess(context.index("用户从当前网页带入的上下文"), context.index("## 文档 1"))
        self.assertIn("这是用户选中的内容。", context)


if __name__ == "__main__":
    unittest.main()
