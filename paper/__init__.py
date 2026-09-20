"""Paper trading (Stage 11): the Stage 5 engine and the Stage 9/10 strategies, driven by live data.

NOTHING HERE CAN PLACE AN ORDER.  A strategy's orders go to the simulated exchange inside
``backtest.engine``; the package imports no order-entry code (there is none anywhere in
``kalshi_client``: its only HTTP verb is GET and its WebSocket only subscribes to public channels),
and ``tests/test_paper.py`` scans the source to keep it that way.
"""
