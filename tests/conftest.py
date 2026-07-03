"""
Loads .env before test collection so environment-gated tests (e.g. the
ANTHROPIC_API_KEY-skipped fixture tests in test_news_sentiment.py and
test_portfolio_manager.py) see real credentials when they're present,
without every test module needing its own dotenv call.
"""

from dotenv import load_dotenv

load_dotenv()
