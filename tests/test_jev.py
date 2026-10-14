"""Offline wire, validation, fitting and deadline tests for Jev."""

import copy
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from tests.support import IsolatedTest
from tests.test_decisions import noul, stop_answers

FIXTURE = Path(__file__).parent / "fixtures" / "jev" / "response.json"


class JevTests(IsolatedTest):
    def test_verified_response_and_ratio(self):
        from memsift import breaker, jev
        sample = json.loads(FIXTURE.read_text())
        self.fake.push(sample)
        state = {"turn": {"prompt": "non, douze colonnes", "answer": "appliqué", "actions": []}, "notes": {}}
        questions = {k: v for k, v in jev.stop_questions(state).items() if k in sample["answers"]}
        answers = jev.request(self.config, state, questions, timeout=1)
        self.assertEqual(answers, sample["answers"])
        sent = self.fake.requests[0]
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["url"], "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer invented-openrouter-key")
        self.assertEqual(sent["body"]["state"]["turn"]["prompt"], "non, douze colonnes")
        self.assertEqual(self.config["MEMSIFT_JEV_MODEL"], "invented-jev-model")
        self.assertNotEqual(breaker.ratio(), 3)

    def test_repo_without_notes_sends_no_empty_request(self):
        # A fresh repo has no notes: request ② has no question and must not be sent,
        # since the real Jev answers 400 to an empty question list.
        from memsift import breaker, jev
        state = {"prompt": "on reprend le header", "conversation": "", "notes": {}, "datasets": {}}
        for _ in range(2):
            self.fake.push({"answers": {"has_info": noul(0.1), "cognee": noul(0.1)}, "usage": {"input_tokens": 100}})
        jev.request_prompt(self.config, state, timeout=2)
        self.assertTrue(self.fake.requests)
        for sent in self.fake.requests:
            self.assertTrue(sent["body"]["questions"])
        self.assertEqual(breaker.state()["services"]["jev"]["failures"], 0)

    def test_question_text_criteria_and_conditional_fields(self):
        from memsift import jev
        questions = jev.prompt_questions({"notes": {"g1": "grid"}, "datasets": {"lessons": {}}})
        self.assertEqual(questions["has_info"]["instructions"], "Does `conversation` already contain everything the agent needs to act on `prompt`?")
        self.assertEqual(questions["has_info"]["criteria"], {
            "true": "the information the prompt relies on is stated in the conversation, or the prompt needs no past information at all (a new instruction, ok, commit, a general question)",
            "false": "the prompt relies on a past decision, rule, fact or state of the work that the conversation does not contain"})
        self.assertEqual(questions["entry_g1"]["instructions"], {"note": "grid", "question": "Does `note` answer `prompt` or give information needed to act on it?"})
        self.assertEqual(questions["entry_g1"]["criteria"], {
            "true": "the note is about what the prompt asks or needs",
            "false": "the note is about something else, or the prompt needs nothing from it"})
        self.assertEqual(questions["dataset_lessons"]["instructions"], "Given its description and items, would `datasets.lessons` hold what the agent needs for `prompt`?")
        self.assertEqual(questions["cognee"]["instructions"], "Is it worth searching the long-term memory?")
        self.assertEqual(questions["cognee"]["criteria"], {"true": "the prompt refers to past work, a decision or a preference, absent from the conversation and the notes, and the agent would act differently knowing it", "false": "a general question, an answer found in the code or the docs, or memory would only be a bonus without changing what the agent does"})
        passage = jev.passage_questions({"passages": {"p2": "grid"}})
        self.assertEqual(passage, {"passage_2": {"type": "noul", "instructions": {
            "passage": "grid", "question": "Does `passage` answer `prompt` or give information needed to act on it?"},
            "criteria": {"true": "the passage is about what the prompt asks or needs",
                         "false": "the passage is about something else, or the prompt needs nothing from it"}}})
        plain = jev.stop_questions({})
        self.assertNotIn("card_outdated", plain)
        self.assertFalse(any(k.startswith("confirmed_") for k in plain))
        conditional = jev.stop_questions({"project_card": "acme-shop", "pending_corrections": {"d4": "grid"}})
        self.assertEqual(conditional["confirmed_d4"]["instructions"], "Do `turn`, `recent_exchanges` and `recent_actions` confirm `pending_corrections.d4`: applied, and not taken back?")
        self.assertIn("taken back", conditional["confirmed_d4"]["criteria"]["false"])
        self.assertEqual(plain["scope"]["criteria"], ["this turn", "this task", "this project", "always"])
        self.assertEqual(list(plain["nature"]["criteria"]), ["project", "lesson", "nothing"])

    def test_errors_count_once_and_log_tracebacks(self):
        from memsift import breaker, jev
        for status, count, paused in [(401, 1, True), (403, 1, True), (429, 0, False), (529, 0, False), (500, 1, False), (404, 1, False), (422, 1, False)]:
            with self.subTest(status=status):
                breaker.resume("jev")
                self.fake.push({"error": "PASSWORD=invented-private-value"}, status=status)
                with self.assertRaises(breaker.ServiceError):
                    jev.request(self.config, {"prompt": "grid"}, {"ok": {"type": "noul", "instructions": "Ready?"}}, timeout=1)
                service = breaker.state()["services"]["jev"]
                self.assertEqual((service["failures"], service["paused"]), (count, paused))
                log = (self.home / ".memsift/debug.log").read_text()
                self.assertIn("Traceback", log)
                self.assertIn("status=" + str(status), log)
                self.assertNotIn("invented-private-value", log)
                self.assertNotIn("invented-openrouter-key", log)
        self.assertEqual(len(self.fake.requests), 7)

    def test_malformed_responses_never_reset_failures(self):
        from memsift import breaker, jev
        bad = [b'{', {}, {"answers": {}}, {"answers": {"ok": noul(float("nan"))}},
               {"answers": {"ok": noul(True)}}, {"answers": {"ok": noul("1")}},
               {"answers": {"ok": {"type": "choice", "noul": 1}}},
               {"answers": {"ok": noul(1.001)}}, {"answers": {"ok": noul(-0.01)}},
               {"answers": {"ok": noul(1)}, "usage": {"input_tokens": 0}}]
        for body in bad:
            with self.subTest(body=body):
                breaker.resume("jev")
                breaker.record("jev", {"kind": "failure", "reason": "previous", "status": 500})
                if isinstance(body, dict) and "usage" not in body:
                    body["usage"] = {"input_tokens": 100}
                self.fake.push(body)
                with self.assertRaises(breaker.ServiceError):
                    jev.request(self.config, {}, {"ok": {"type": "noul", "instructions": "Ready?"}}, timeout=1)
                self.assertEqual(breaker.state()["services"]["jev"]["failures"], 2)
                self.assertIn("Traceback", (self.home / ".memsift/debug.log").read_text())
        self.assertEqual(len(self.fake.requests), len(bad))

    def test_choice_and_score_validation(self):
        from memsift import breaker, jev
        for field, value in [("nature.choice", "unknown"), ("nature.confidence", float("inf")),
                             ("nature.probabilities", {"project": 1}),
                             ("scope.legend", {"0": "always"}), ("scope.score", "2"),
                             ("scope.probabilities", {"0": 0, "1": 0, "2": 2, "3": -1}),
                             ("scope.confidence", True)]:
            with self.subTest(field=field):
                breaker.resume("jev")
                answers = stop_answers()
                key, item = field.split(".")
                answers[key][item] = value
                questions = jev.stop_questions({"project_card": "acme-shop", "pending_corrections": {"d4": "grid"}})
                self.fake.push({"answers": answers, "usage": {"input_tokens": 100}})
                with self.assertRaises(breaker.ServiceError):
                    jev.request(self.config, {}, questions, timeout=1)
                self.assertEqual(breaker.state()["services"]["jev"]["failures"], 1)

    def test_pause_and_probe_leave_state_unchanged(self):
        from memsift import breaker, jev
        breaker.record("jev", {"kind": "auth", "reason": "bad key", "status": 401})
        before = breaker.state()
        with self.assertRaises(breaker.PausedError):
            jev.request(self.config, {}, {}, timeout=1)
        self.assertEqual(self.fake.requests, [])
        self.fake.push({"answers": {"available": noul(1)}, "usage": {"input_tokens": 40}})
        jev.probe(self.config)
        self.assertEqual(breaker.state(), before)
        self.fake.push({"error": "bad key"}, status=403)
        with self.assertRaises(breaker.ServiceError):
            jev.probe(self.config)
        self.assertEqual(breaker.state(), before)

    def test_connection_and_body_have_one_deadline(self):
        from memsift import breaker, jev
        for body in (False, True):
            with self.subTest(body=body):
                breaker.resume("jev")
                if body:
                    wire = json.dumps({"answers": {"ok": noul(1)}, "usage": {"input_tokens": 10}}).encode()
                    self.fake.push_chunks([wire[:10], wire[10:20], wire[20:30], wire[30:]], interval=0.08)
                else:
                    self.fake.push({"answers": {"ok": noul(1)}}, delay=0.3)
                start = time.monotonic()
                with self.assertRaises(breaker.ServiceError):
                    jev.request(self.config, {}, {"ok": {"type": "noul", "instructions": "Ready?"}}, timeout=0.15)
                self.assertLess(time.monotonic() - start, 0.25)
                self.assertEqual(breaker.state()["services"]["jev"]["failures"], 1)
                time.sleep(0.2)
                self.assertEqual(breaker.state()["services"]["jev"]["failures"], 1)

    def test_size_retry_halves_exchanges_not_characters(self):
        from memsift import breaker, jev
        exchanges = [{"role": "user", "text": text, "actions": []}
                     for text in ("old" * 1000, "second", "third", "newest")]
        state = {"prompt": "grid", "conversation": exchanges}
        questions = {"has_info": jev.prompt_questions(state)["has_info"]}
        self.fake.push({"error": {"code": "context_length_exceeded"}}, status=400, delay=0.05)
        self.fake.push({"answers": {"has_info": noul(0)}, "usage": {"input_tokens": 100}})
        result = jev.request(self.config, state, questions, timeout=1)
        self.assertEqual(result["has_info"], noul(0))
        self.assertEqual(self.fake.requests[1]["body"]["state"]["conversation"], exchanges[2:])
        self.assertLess(self.fake.requests[1]["timeout"], self.fake.requests[0]["timeout"])
        self.assertEqual(state["conversation"], exchanges)
        self.assertEqual(breaker.state()["services"]["jev"]["failures"], 0)

    def test_second_size_refusal_is_one_failure(self):
        from memsift import breaker, jev
        self.fake.push({"error": "request too large"}, status=413)
        self.fake.push({"error": "request too large"}, status=413)
        with self.assertRaises(breaker.ServiceError):
            jev.request(self.config, {}, {"ok": {"type": "noul", "instructions": "Ready?"}}, timeout=1)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(breaker.state()["services"]["jev"]["failures"], 1)

    def test_transport_exception_traceback(self):
        from memsift import breaker, jev
        with patch("memsift.transport.open_request", side_effect=OSError("injected opener fault")):
            with self.assertRaises(breaker.ServiceError):
                jev.request(self.config, {}, {"ok": {"type": "noul", "instructions": "Ready?"}}, timeout=1)
        log = (self.home / ".memsift/debug.log").read_text()
        self.assertIn("Traceback", log)
        self.assertIn("injected opener fault", log)

    def test_fit_stop_limits_and_preserves_original_and_pending(self):
        from memsift import jev, store
        state = {"turn": {"prompt": "begin" + "p" * 200000 + "end", "answer": "a" * 100000,
                          "actions": ["Edit " + "x" * 10000]},
                 "instruction_files": "start" + "i" * 12000,
                 "notes": {"d1": "old" + "n" * 24000, "d2": "new" + "n" * 24000},
                 "pending_corrections": {"d1": "keep correction"}, "project_card": "acme-shop",
                 "recent_exchanges": "old" + "r" * 12000 + "retraction", "recent_actions": ["git commit"]}
        original = copy.deepcopy(state)
        fitted = jev.fit_stop(state, jev.stop_questions(state), 3)
        self.assertEqual(state, original)
        self.assertEqual(fitted["pending_corrections"], original["pending_corrections"])
        self.assertEqual(list(fitted["notes"]), ["d2"])
        self.assertTrue(fitted["instruction_files"].startswith("start"))
        self.assertLessEqual(store.estimate(fitted["instruction_files"], 3), 3000)
        self.assertTrue(fitted["recent_exchanges"].endswith("retraction"))
        self.assertLessEqual(store.estimate(fitted["recent_exchanges"], 3), 3000)
        self.assertTrue(fitted["turn"]["prompt"].startswith("begin"))
        self.assertTrue(fitted["turn"]["prompt"].endswith("end"))
        self.assertIn("[…]", fitted["turn"]["prompt"])
        self.assertLessEqual(store.estimate(json.dumps(fitted, ensure_ascii=False), 3), 32000)
        self.assertGreater(len(fitted["turn"]["answer"]), 3000)

    def test_fitting_drops_unused_exchanges_and_redacts_before_clip(self):
        from memsift import jev
        state = {"turn": {"prompt": "PASSWORD=invented-private-value", "answer": "a" * 12000, "actions": []},
                 "notes": {}, "recent_exchanges": "unrelated"}
        fitted = jev.fit_stop(state, jev.stop_questions(state), 3)
        self.assertNotIn("recent_exchanges", fitted)
        self.assertNotIn("invented-private-value", repr(fitted))
        self.assertEqual(len(fitted["turn"]["answer"]), 12000)

    def test_protected_oversize_logs_and_does_not_send(self):
        from memsift import breaker, jev
        state = {"turn": {"prompt": "grid", "answer": "ok", "actions": []}, "project_card": "x" * 100000}
        with self.assertRaises(breaker.ServiceError):
            jev.fit_stop(state, jev.stop_questions(state), 3)
        self.assertEqual(self.fake.requests, [])
        self.assertIn("Traceback", (self.home / ".memsift/debug.log").read_text())

    def test_passage_state_minimization_and_redaction(self):
        from memsift import jev
        state = {"prompt": "grid PASSWORD=invented-private-value", "passages": {"p1": "twelve"},
                 "compaction_summary": "summary", "recent_exchanges": "conversation",
                 "notes": {"d1": "unused"}, "instruction_files": "unused", "project_card": "unused"}
        self.fake.push({"answers": {"passage_1": noul(0.7)}, "usage": {"input_tokens": 100}})
        jev.request(self.config, state, jev.passage_questions(state), timeout=1)
        sent = self.fake.requests[0]["body"]["state"]
        self.assertEqual(set(sent), {"prompt"})
        self.assertNotIn("invented-private-value", repr(sent))

    def test_token_decisions_log_estimated_and_actual(self):
        from memsift import jev
        os.environ["MEMSIFT_DEBUG"] = "1"
        self.fake.push({"answers": {"ok": noul(0)}, "usage": {"input_tokens": 40}})
        jev.request(self.config, {}, {"ok": {"type": "noul", "instructions": "Ready?"}}, timeout=1)
        log = (self.home / ".memsift/debug.log").read_text()
        self.assertIn("estimated_tokens", log)
        self.assertIn('"input_tokens": 40', log)

    def test_http_error_preserves_original_cause(self):
        import urllib.error
        from memsift import breaker, jev
        self.fake.push({"error": "unavailable"}, status=500)
        with self.assertRaises(breaker.ServiceError) as caught:
            jev.request(self.config, {}, {"ok": {"type": "noul", "instructions": "Ready?"}}, timeout=1)
        cause = caught.exception
        while cause.__cause__ is not None:
            cause = cause.__cause__
        self.assertIsInstance(cause, urllib.error.HTTPError)

    def test_real_prompt_deadline_discards_slow_body(self):
        from memsift import breaker, jev
        state = {"prompt": "grid", "notes": {}, "datasets": {}}
        wire = json.dumps({"answers": {"has_info": noul(0)}, "usage": {"input_tokens": 100}}).encode()
        self.fake.push_chunks([wire[:10], wire[10:20], wire[20:30], wire[30:40], wire[40:]], interval=0.55)
        start = time.monotonic()
        with self.assertRaises(breaker.ServiceError) as caught:
            jev.request(self.config, state, {"has_info": jev.prompt_questions(state)["has_info"]}, timeout=jev.PROMPT_TIMEOUT)
        self.assertLess(time.monotonic() - start, 2.3)
        self.assertEqual(caught.exception.status, 200)
        self.assertEqual(breaker.ratio(), 3)
        self.assertEqual(breaker.state()["services"]["jev"]["failures"], 1)
        time.sleep(0.3)
        self.assertEqual(breaker.ratio(), 3)

    def test_pending_retraction_survives_fitting_and_request(self):
        from memsift import jev
        state = {"turn": {"prompt": "continue", "answer": "ok", "actions": []},
                 "notes": {}, "pending_corrections": {"d4": "Use twelve columns"},
                 "recent_exchanges": [{"role": "user", "text": "old" * 5000},
                                      {"role": "user", "text": "I take back the twelve-column rule"}],
                 "recent_actions": ["git commit (ok)"]}
        fitted = jev.fit_stop(state, jev.stop_questions(state), 3)
        answers = stop_answers(nature="nothing")
        answers["confirmed_d4"] = noul(0)
        del answers["card_outdated"]
        self.fake.push({"answers": answers, "usage": {"input_tokens": 200}})
        measured = jev.request(self.config, fitted, jev.stop_questions(fitted), timeout=1)
        sent = self.fake.requests[0]["body"]["state"]
        self.assertEqual(sent["recent_exchanges"], [{"role": "user", "text": "I take back the twelve-column rule"}])
        self.assertEqual(jev.choose_stop(measured, ["d4"])["confirmed"], [])

    def test_size_retry_rebuilds_stop_questions_preserving_pending_source(self):
        from memsift import jev
        state = {"turn": {"prompt": "continue", "answer": "ok", "actions": []},
                 "notes": {"d1": "old" * 7000, "d2": "new" * 7000},
                 "pending_corrections": {"d1": "Use twelve columns"}, "recent_exchanges": "retracted"}
        answers = stop_answers()
        answers["confirmed_d1"] = answers.pop("confirmed_d4")
        del answers["card_outdated"]
        self.fake.push({"error": "too many tokens"}, status=422)
        self.fake.push({"answers": answers, "usage": {"input_tokens": 200}})
        jev.request(self.config, state, jev.stop_questions(state), timeout=1)
        sent = self.fake.requests[1]["body"]
        self.assertEqual(list(sent["state"]["notes"]), ["d2"])
        self.assertEqual(sent["state"]["pending_corrections"], {"d1": "Use twelve columns"})
        self.assertIn("confirmed_d1", sent["questions"])

    def test_oversized_initial_request_and_total_question_budget_do_not_send(self):
        from memsift import breaker, jev
        for state, questions in [({"prompt": "p" * 100000}, jev.prompt_questions({})),
                                 ({}, {"q%d" % i: {"type": "noul", "instructions": "x" * 5000} for i in range(40)})]:
            with self.subTest(fields=list(state)):
                breaker.resume("jev")
                with self.assertRaises(breaker.ServiceError):
                    jev.request(self.config, state, questions, timeout=1)
                self.assertEqual(breaker.state()["services"]["jev"]["failures"], 1)
        self.assertEqual(self.fake.requests, [])

    def test_retry_still_shares_deadline_when_second_headers_are_slow(self):
        from memsift import breaker, jev
        self.fake.push({"error": "request too large"}, status=413, delay=0.12)
        self.fake.push({"answers": {"ok": noul(1)}, "usage": {"input_tokens": 20}}, delay=0.2)
        start = time.monotonic()
        with self.assertRaises(breaker.ServiceError):
            jev.request(self.config, {}, {"ok": {"type": "noul", "instructions": "Ready?"}}, timeout=0.25)
        self.assertLess(time.monotonic() - start, 0.32)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(breaker.state()["services"]["jev"]["failures"], 1)
        self.assertIn("Traceback", (self.home / ".memsift/debug.log").read_text())

    def test_fit_stop_handles_json_escapes_in_turn_budget(self):
        from memsift import jev
        state = {"turn": {"prompt": "BEGIN" + '\"\n' * 100000 + "END", "answer": "ok", "actions": []}}
        questions = jev.stop_questions(state)
        fitted = jev.fit_stop(state, questions, 3)
        longest = max(len(json.dumps({k: q}, ensure_ascii=False)) for k, q in questions.items())
        self.assertLessEqual(len(json.dumps(fitted, ensure_ascii=False)) + longest, 96000)
        self.assertTrue(fitted["turn"]["prompt"].startswith("BEGIN"))
        self.assertTrue(fitted["turn"]["prompt"].endswith("END"))

    def test_stop_question_strings_and_nature_criteria_are_exact(self):
        from memsift import jev
        questions = jev.stop_questions({"project_card": "acme-shop"})
        expected = {
            "nature": "What kind of information does `turn` carry?",
            "general_rule": "Does `turn` state a preference about how the agent should work in every project, not only this one?",
            "scope": "How long does it stay true?",
            "correction": "Does the user correct the agent in `turn`?",
            "known": 'Is it already in `notes` or `instruction_files`, or written this turn to a file listed under "documented in" in `project_card` (an edit in `turn.actions`)?',
            "card_outdated": "Does `turn` make `project_card` outdated: a new stake, a change of phase, or a contradiction?",
            "reason_missing": "Does `turn` take a structuring decision without giving its reason?",
        }
        self.assertEqual({k: q["instructions"] for k, q in questions.items()}, expected)
        self.assertEqual(questions["nature"]["criteria"], {
            "project": "something that holds for this project and is worth remembering: a decision and its reason, a rule for how to work here, a constraint, a business rule, a fact such as an address or an account, where the work stands",
            "lesson": "a technical problem and what fixed it or did not, useful in any project, not specific to this one",
            "nothing": "status, reaction, small talk, typo, a one-off instruction, a micro-choice already applied in the code, or something left undecided",
        })
        self.assertEqual(questions["general_rule"]["type"], "noul")
        self.assertEqual(questions["general_rule"]["criteria"], {
            "true": "a general working habit of the user, such as asking before committing, or the form of answers",
            "false": "anything specific to this project: its stack, its client, its constraints, its decisions, its rules",
        })

    def test_general_rule_boundary_on_stop_request(self):
        from memsift import jev
        state = {"turn": {"prompt": "Ask before committing.", "answer": "Understood.", "actions": []}}
        for probability, keep in [(0.4999, True), (0.5, False)]:
            answers = stop_answers(general_rule=probability)
            questions = jev.stop_questions(state)
            self.fake.push({"answers": {k: answers[k] for k in questions}, "usage": {"input_tokens": 100}})
            measured = jev.request(self.config, state, questions, timeout=1)
            self.assertEqual(jev.choose_stop(measured, [])["keep"], keep)

    def test_latest_oversized_exchange_keeps_text_only_retraction_tail(self):
        from memsift import jev
        state = {"turn": {"prompt": "continue", "answer": "ok", "actions": []},
                 "pending_corrections": {"d4": "twelve columns"},
                 "recent_exchanges": [{"role": "user", "text": "x" * 10000 + "I take back the correction"}]}
        fitted = jev.fit_stop(state, jev.stop_questions(state), 3)
        self.assertEqual(len(fitted["recent_exchanges"]), 1)
        self.assertTrue(fitted["recent_exchanges"][0]["text"].endswith("I take back the correction"))
        self.assertLessEqual(len(json.dumps(fitted["recent_exchanges"], ensure_ascii=False)), 9000)

    def test_parallel_prompt_failure_counts_once_and_cannot_be_reset_by_siblings(self):
        from memsift import breaker, jev
        for statuses in ((500, 500, 500), (500, 200, 200)):
            with self.subTest(statuses=statuses):
                breaker.resume("jev")
                breaker.record("jev", {"kind": "failure", "reason": "previous failure", "status": 500})
                for status in statuses:
                    body = ({"answers": {"has_info": noul(0), "cognee": noul(0)},
                             "usage": {"input_tokens": 500}} if status == 200 else {"error": "unavailable"})
                    self.fake.push(body, status=status, delay=0.05 if status == 200 else 0)
                with self.assertRaises(breaker.ServiceError):
                    jev.request_prompt(self.config, {"prompt": "grid"}, timeout=0.5)
                self.assertEqual(breaker.state()["services"]["jev"]["failures"], 2)

    def test_embedded_notes_and_passages_are_redacted_on_the_wire(self):
        from memsift import jev
        state = {"prompt": "Use the account", "notes": {"d1": "PASSWORD=invented-private-value"}}
        for _ in range(3):
            self.fake.push({"answers": {"has_info": noul(0), "entry_d1": noul(1), "cognee": noul(0)},
                            "usage": {"input_tokens": 500}})
        jev.request_prompt(self.config, state, timeout=1)
        self.fake.push({"answers": {"passage_1": noul(1)}, "usage": {"input_tokens": 500}})
        passage_state = {"prompt": state["prompt"], "passages": {"p1": "PASSWORD=invented-private-value"}}
        jev.request(self.config, {"prompt": state["prompt"]}, jev.passage_questions(passage_state), timeout=1)
        self.assertEqual(len(self.fake.requests), 4)
        self.assertNotIn("invented-private-value", repr(self.fake.requests))
        entries = next(r["body"]["questions"] for r in self.fake.requests
                       if "entry_d1" in r["body"]["questions"])
        self.assertEqual(entries["entry_d1"]["instructions"]["note"], "[redacted:credential]")
