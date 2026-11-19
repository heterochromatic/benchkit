"""benchkit: shared harness for The Prompting Company's public GTM benchmarks.

One harness for both tracks: human operators and AI-agent runs share the same run records,
usage attribution, verifier, scoring and summary. Each bench repo supplies its own
config/bench.json and scoring/scorers.py.
"""
__version__ = "0.1.3"
