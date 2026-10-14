"""Jev decisions.

prompt_questions, passage_questions and stop_questions(state) -> dict;
fit_stop(state, questions, ratio) -> dict; request(config, state, questions,
timeout, probe=False) -> dict; choose_prompt, choose_passages and choose_stop
map answers to frozen choices; probe(config) -> None.
"""
