"""Model API proxy (not written yet).

Record mode: forward each request to the provider, record request + full
(streamed) reply via ``Recording.append(kind="model")`` before returning.
Rebuild mode: answer from ``RebuildCursor.next_model_reply()``; go live when
it returns None. One adapter per provider. See docs/DESIGN.md.
"""
