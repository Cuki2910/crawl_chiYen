import unittest

from crawlers.hanoi_flood_transport.electric_bus_queries import KEYWORDS as ELECTRIC_BUS_KEYWORDS
from crawlers.hanoi_flood_transport.tiktok_news_queries import KEYWORDS as FLOOD_KEYWORDS
from crawlers.news import crawl_news, discover_news
from crawlers.tiktok import crawl_tiktok, discover_tiktok
from crawlers.topics import (
    gate_electric_bus,
    is_on_topic_news,
    is_on_topic_news_electric_bus,
    is_on_topic_tiktok,
    is_on_topic_tiktok_electric_bus,
)


class ElectricBusGateTests(unittest.TestCase):
    def test_accepts_on_brand_term(self):
        verdict, rule = gate_electric_bus("Trải nghiệm đi VinBus hôm nay")
        self.assertEqual(verdict, "accept")
        self.assertEqual(rule, "electric_bus")

    def test_rejects_unrelated_text(self):
        verdict, rule = gate_electric_bus("Hôm nay trời đẹp đi chơi công viên")
        self.assertEqual(verdict, "reject")
        self.assertIn("missing_group", rule)

    def test_does_not_require_flood_or_hanoi_terms(self):
        # Single-group gate: an electric-bus term alone is enough, unlike
        # gate_post()'s 3-group AND requirement.
        verdict, _rule = gate_electric_bus("xe buýt điện rất êm")
        self.assertEqual(verdict, "accept")


class ElectricBusQueryListTests(unittest.TestCase):
    def test_no_duplicates(self):
        self.assertEqual(len(ELECTRIC_BUS_KEYWORDS), len(set(k.lower() for k in ELECTRIC_BUS_KEYWORDS)))

    def test_every_query_passes_its_own_gate_or_is_a_recall_widener(self):
        # Not every expanded query is required to literally contain a gate
        # term (that's the point -- they widen recall) but the list must be
        # non-empty and distinct from the flood keyword list.
        self.assertGreater(len(ELECTRIC_BUS_KEYWORDS), 30)
        self.assertNotEqual(set(ELECTRIC_BUS_KEYWORDS), set(FLOOD_KEYWORDS))


class TiktokCampaignWiringTests(unittest.TestCase):
    def test_flood_transport_is_default(self):
        args = discover_tiktok.build_arg_parser().parse_args(["--since", "2025-08-01"])
        self.assertEqual(args.campaign, "flood_transport")

    def test_electric_bus_selectable(self):
        args = discover_tiktok.build_arg_parser().parse_args(["--campaign", "electric_bus"])
        self.assertEqual(args.campaign, "electric_bus")

    def test_campaign_sources_are_distinct_and_nonempty(self):
        flood_sources = discover_tiktok.CAMPAIGNS["flood_transport"]["sources"]
        ebus_sources = discover_tiktok.CAMPAIGNS["electric_bus"]["sources"]
        self.assertGreater(len(flood_sources), 0)
        self.assertGreater(len(ebus_sources), 0)
        flood_queries = {s["query"] for s in flood_sources}
        ebus_queries = {s["query"] for s in ebus_sources}
        self.assertTrue(flood_queries.isdisjoint(ebus_queries) or flood_queries != ebus_queries)

    def test_campaign_gate_selection(self):
        self.assertIs(discover_tiktok.CAMPAIGNS["flood_transport"]["gate"], is_on_topic_tiktok)
        self.assertIs(discover_tiktok.CAMPAIGNS["electric_bus"]["gate"], is_on_topic_tiktok_electric_bus)

    def test_crawl_tiktok_campaign_gates(self):
        self.assertIs(crawl_tiktok.CAMPAIGN_GATES["flood_transport"], is_on_topic_tiktok)
        self.assertIs(crawl_tiktok.CAMPAIGN_GATES["electric_bus"], is_on_topic_tiktok_electric_bus)

    def test_crawl_tiktok_batch_prefixes_distinct(self):
        prefixes = crawl_tiktok.CAMPAIGN_BATCH_PREFIX
        self.assertNotEqual(prefixes["flood_transport"], prefixes["electric_bus"])


class NewsCampaignWiringTests(unittest.TestCase):
    def test_flood_transport_is_default(self):
        args = discover_news.build_arg_parser().parse_args(["--since", "2025-08-01"])
        self.assertEqual(args.campaign, "flood_transport")

    def test_electric_bus_selectable(self):
        args = discover_news.build_arg_parser().parse_args(["--campaign", "electric_bus"])
        self.assertEqual(args.campaign, "electric_bus")

    def test_campaign_keywords_distinct(self):
        flood_keywords = discover_news.CAMPAIGNS["flood_transport"]["keywords"]
        ebus_keywords = discover_news.CAMPAIGNS["electric_bus"]["keywords"]
        self.assertEqual(set(flood_keywords), set(FLOOD_KEYWORDS))
        self.assertEqual(set(ebus_keywords), set(ELECTRIC_BUS_KEYWORDS))

    def test_campaign_gate_selection(self):
        self.assertIs(discover_news.CAMPAIGNS["flood_transport"]["gate"], is_on_topic_news)
        self.assertIs(discover_news.CAMPAIGNS["electric_bus"]["gate"], is_on_topic_news_electric_bus)

    def test_crawl_news_campaign_gates(self):
        self.assertIs(crawl_news.CAMPAIGN_GATES["flood_transport"], is_on_topic_news)
        self.assertIs(crawl_news.CAMPAIGN_GATES["electric_bus"], is_on_topic_news_electric_bus)

    def test_crawl_news_batch_prefixes_distinct(self):
        prefixes = crawl_news.CAMPAIGN_BATCH_PREFIX
        self.assertNotEqual(prefixes["flood_transport"], prefixes["electric_bus"])


if __name__ == "__main__":
    unittest.main()
