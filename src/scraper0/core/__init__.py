"""Generic learning scraper core: entity specs, recipes (learned layouts), crawler, store."""
from scraper0.core.crawler import CrawlOptions, Crawler
from scraper0.core.entities import BUILTIN_SPECS, EVENT_SPEC, EntitySpec, FieldSpec
from scraper0.core.store import Store

__all__ = ["Crawler", "CrawlOptions", "EntitySpec", "FieldSpec", "EVENT_SPEC", "BUILTIN_SPECS", "Store"]
