"""Cognee transport.

descriptions() -> dict; list_datasets(config, probe=False) -> list; rebuild_index
(root, config) -> list[str]; recall(config, prompt, datasets, timeout=12.0) ->
list[Passage]; queue_packet(root, name, text) -> Path; send_outbox(repo_dir,
config) -> int; probe(config) -> None.
"""
