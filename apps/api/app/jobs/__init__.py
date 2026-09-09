"""Scheduled work that runs outside a request.

For now these are plain modules a scheduler invokes with `python -m`. Lot L2
brings Celery and its beat schedule; the tasks it registers will call the same
service functions rather than reimplement them, so the logic tested here is the
logic that will run in production.
"""
