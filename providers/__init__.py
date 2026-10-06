"""
providers/__init__.py — Public exports for the providers package.

Import providers from here rather than individual modules:
    from providers import NewsAPI, GNews, NewsDataIO, WebFetch, Provider, ProviderArticle
"""

from providers.base import Provider, ProviderArticle, parse_time
from providers.gnews import GNews
from providers.newsapi import NewsAPI
from providers.newsdata import NewsDataIO
from providers.webfetch import WebFetch

__all__ = [
    "Provider",
    "ProviderArticle",
    "parse_time",
    "NewsAPI",
    "GNews",
    "NewsDataIO",
    "WebFetch",
]
