"""Offline tests for the opt-in calibration tools."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from tests.test_decisions import prompt_kind
from tests.support import IsolatedTest, child_env


def noul(value):
    return {"type": "noul", "noul": value}


def prompt_response(questions, has_info=0.1, cognee=0.9, dataset=0.6):
    answers = {}
    for name, question in questions.items():
        if name == "has_info":
            answers[name] = prompt_kind(has_info)
        elif question["type"] == "noul":
            answers[name] = noul(cognee if name == "cognee" else dataset)
    return answers


def stop_response(questions):
    natures = list(questions["nature"]["criteria"])
    nature = natures[0]
    levels = questions["scope"]["criteria"]
    answers = {
        "nature": {"type": "choice", "choice": nature, "confidence": 1.0,
                   "probabilities": {name: float(name == nature) for name in natures}},
        "scope": {"type": "score", "score": 2, "confidence": 1.0,
                  "legend": {str(index): name for index, name in enumerate(levels)},
                  "probabilities": {str(index): float(index == 2) for index in range(len(levels))}},
        "correction": noul(0), "known": noul(0), "general_rule": noul(0), "reason_missing": noul(0),
    }
    if "card_outdated" in questions:
        answers["card_outdated"] = noul(0)
    for name in questions:
        if name.startswith("confirmed_"):
            answers[name] = noul(0)
    return answers


def compacted(text="The acme-shop header uses the footer grid."):
    return {
        "digest": {
            "in_progress": [],
            "decisions": [{"text": text, "date": "2026-10-14"}],
            "project_facts": [],
            "working_rules": [],
        },
        "packet": {"topics": "header grid", "text": text},
    }


def envelope(answer):
    return {"choices": [{"message": {"content": json.dumps(answer)}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 20}}


class CalibrationTests(IsolatedTest):
    def test_compactor_comparison_reads_project_and_lesson_entries(self):
        from tests import compare_compactors
        notes = compare_compactors._notes(
            "# Draft: acme-shop\n- [d1] 2026-10-14 project\n  User: Prices include VAT.\n"
            "- [d2] 2026-10-14 lesson\n  User: Build needs sharp.\n- [d1] confirmed\n")
        self.assertEqual([(e["id"], e["nature"]) for e in notes["entries"]],
                         [("d1", "project"), ("d2", "lesson")])

    def setUp(self):
        super().setUp()
        os.environ["MEMSIFT_JEV_MODEL"] = "typesafe/jev-1.13"

    def label(self, **changes):
        value = {"prompt": "Why did acme-shop choose this payment flow?", "notes": {},
                 "conversation": {}, "expected": {"cognee": True}}
        value.update(changes)
        path = self.home / "labels.jsonl"
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        return path

    def draft(self, text="Keep the acme-shop header grid."):
        path = self.home / "draft.md"
        path.write_text("# Draft: acme-shop\n\n- [d1] 2026-10-14 project\n  User: " + text + "\n",
                        encoding="utf-8")
        return path

    def run_calibration(self, labels, answer=prompt_response):
        from tests import calibrate_jev

        output = io.StringIO()
        with patch("builtins.input", return_value="yes"), \
                patch("memsift.jev._request", side_effect=lambda config, state, questions, **kwargs:
                      answer(questions)) as request, redirect_stdout(output):
            result = calibrate_jev.main([str(labels)])
        return result, output.getvalue(), request

    def test_declined_calibration_makes_no_model_request(self):
        from tests import calibrate_jev

        labels = self.home / "labels.jsonl"
        labels.write_text(
            '{"prompt":"header","notes":{},"conversation":{},"expected":"nothing"}\n',
            encoding="utf-8",
        )
        with patch("builtins.input", return_value="n"), \
                patch("memsift.jev._request") as request:
            self.assertEqual(calibrate_jev.main([str(labels)]), 0)
        request.assert_not_called()

    def test_declined_compactor_comparison_makes_no_model_request(self):
        from tests import compare_compactors

        with patch("builtins.input", return_value=""), \
                patch("memsift.compactor.request") as request:
            self.assertEqual(compare_compactors.main([str(self.draft())]), 0)
        request.assert_not_called()

    def test_eof_declines_and_logs_tracebacks(self):
        from tests import calibrate_jev, compare_compactors

        for main, path, service in ((calibrate_jev.main, self.label(), "calibrate-jev-consent"),
                                    (compare_compactors.main, self.draft(), "compare-compactors-consent")):
            with self.subTest(service=service), patch("builtins.input", side_effect=EOFError), \
                    patch("memsift.jev._request") as jev_request, \
                    patch("memsift.compactor.request") as compactor_request:
                self.assertEqual(main([str(path)]), 0)
            jev_request.assert_not_called()
            compactor_request.assert_not_called()
            log = (self.home / ".memsift/debug.log").read_text(encoding="utf-8")
            self.assertIn(service, log)
            self.assertIn("Traceback", log)

    def test_all_labels_are_validated_before_any_request(self):
        from tests import calibrate_jev

        labels = self.label(expected="nothing")
        with labels.open("a", encoding="utf-8") as stream:
            stream.write('{"prompt": 3}\n')
        stderr = io.StringIO()
        with patch("builtins.input") as consent, patch("memsift.jev._request") as request, \
                redirect_stderr(stderr):
            self.assertEqual(calibrate_jev.main([str(labels)]), 2)
        consent.assert_not_called()
        request.assert_not_called()
        self.assertIn("line 2", stderr.getvalue())
        self.assertIn("Traceback", (self.home / ".memsift/debug.log").read_text(encoding="utf-8"))

    def test_missing_files_and_inconsistent_cognee_label_are_rejected(self):
        from tests import calibrate_jev, compare_compactors

        empty_datasets = self.label(datasets={})
        cases = ((calibrate_jev.main, [str(self.home / "missing.jsonl")]),
                 (calibrate_jev.main, [str(empty_datasets)]),
                 (compare_compactors.main, [str(self.home / "missing.md")]))
        for main, argv in cases:
            with self.subTest(argv=argv), patch("builtins.input") as consent, \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(main(argv), 2)
            consent.assert_not_called()

    def test_three_requests_match_the_hook_and_ignore_summary_and_card(self):
        from memsift import jev
        from tests import calibrate_jev
        labels = self.label(project_card="excluded card", session_start="resume",
                            conversation={"summary": "excluded summary", "exchanges": []},
                            notes={"d1": "First note", "g2": "Second note"})
        label = calibrate_jev._load(labels)[0]
        plans = calibrate_jev._plans([label])
        self.assertEqual(len(plans), 3)
        self.assertEqual([(s, q) for _, _, s, q in plans], jev.prompt_requests({
            "prompt": label["prompt"], "conversation": [],
            "notes": label["notes"], "datasets": label["datasets"]}))
        self.assertNotIn("excluded", repr(plans))
        result, output, request = self.run_calibration(labels)
        self.assertEqual(result, 0)
        self.assertEqual(request.call_count, 3)
        self.assertIn("Prompt: 0/1 correct", output)
        self.assertNotIn("Minimal", output)

    def test_missing_optional_fields_use_defaults_without_memory_reads(self):
        from tests import calibrate_jev

        labels = self.label(expected="nothing")
        with patch("builtins.input", return_value="n"), \
                patch("memsift.store.read_notes") as read_notes, \
                patch("memsift.cognee.recall") as recall:
            self.assertEqual(calibrate_jev.main([str(labels)]), 0)
        read_notes.assert_not_called()
        recall.assert_not_called()
        loaded = calibrate_jev._load(labels)[0]
        self.assertEqual(loaded["session_start"], "new session")
        self.assertEqual(loaded["datasets"], calibrate_jev.DEFAULT_DATASETS)
        self.assertEqual(loaded["conversation"], {"summary": "", "exchanges": []})

    def test_request_count_includes_order_variants_and_reports_sensitivity(self):
        labels = self.label(stop={
            "turn": {"prompt": "Use the footer grid.", "answer": "Applied.", "actions": []},
            "project_card": "acme-shop",
        })

        def answers(questions):
            return stop_response(questions) if "nature" in questions else prompt_response(questions)

        result, output, request = self.run_calibration(labels, answers)
        self.assertEqual(result, 0)
        self.assertEqual(request.call_count, 5)
        self.assertIn("(5 requests;", output)
        self.assertIn("Nature-choice ordering sensitivity: 1/1 choices changed.", output)
        baseline = request.call_args_list[3].args[2]["nature"]["criteria"]
        alternate = request.call_args_list[4].args[2]["nature"]["criteria"]
        self.assertEqual(list(baseline), list(reversed(list(alternate))))
        self.assertEqual(set(baseline), set(alternate))

    def test_threshold_ties_keep_current_values(self):
        from memsift import jev
        from tests import calibrate_jev

        label = calibrate_jev._load(self.label(expected="nothing"))[0]
        result = {"label": label, "answers": {"has_info": prompt_kind(0.69), "cognee": noul(0.9),
                                               "dataset_memory": noul(0.6)}}
        thresholds, correct = calibrate_jev._best_thresholds([result])
        self.assertEqual(thresholds, (jev.HAS_INFO, jev.ENTRY, jev.COGNEE))
        self.assertEqual(correct, 1)

    def test_unknown_price_and_missing_comparative_metadata_gate_before_consent(self):
        from tests import calibrate_jev, compare_compactors

        os.environ["MEMSIFT_JEV_MODEL"] = "invented-unknown-model"
        with patch("builtins.input") as consent, patch("memsift.jev._request") as request, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(calibrate_jev.main([str(self.label())]), 2)
        consent.assert_not_called()
        request.assert_not_called()
        with patch.dict(compare_compactors.MODELS, {}, clear=True), \
                patch("builtins.input") as consent, patch("memsift.compactor.request") as request, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(compare_compactors.main([str(self.draft())]), 2)
        consent.assert_not_called()
        request.assert_not_called()

    def test_explicit_unknown_model_price_is_used(self):
        from tests import calibrate_jev

        os.environ["MEMSIFT_JEV_MODEL"] = "invented-unknown-model"
        output = io.StringIO()
        with patch("builtins.input", return_value="n"), redirect_stdout(output):
            self.assertEqual(calibrate_jev.main([str(self.label()), "--input-price", "1.25"]), 0)
        self.assertIn("$1.250000/M input", output.getvalue())

    def test_calibration_redacts_requests_and_does_not_write_repo_or_upload(self):
        secret = "invented-private-value"
        labels = self.label(prompt="PASSWORD=" + secret)
        before = set(self.repo.rglob("*"))
        result, _, request = self.run_calibration(labels)
        self.assertEqual(result, 0)
        self.assertNotIn(secret, repr(request.call_args_list))
        self.assertEqual(set(self.repo.rglob("*")), before)
        self.assertEqual(self.fake.requests, [])

    def test_compactor_comparison_uses_same_redacted_draft_and_models(self):
        from tests import compare_compactors

        draft = self.draft("PASSWORD=invented-private-value")
        before = draft.read_text(encoding="utf-8")
        output = io.StringIO()
        with patch("builtins.input", return_value="y"), \
                patch("memsift.compactor.request", return_value=compacted()) as request, \
                redirect_stdout(output):
            self.assertEqual(compare_compactors.main([str(draft)]), 0)
        self.assertEqual(request.call_count, 2)
        models = [call.args[0]["MEMSIFT_COMPACTOR_MODEL"] for call in request.call_args_list]
        self.assertEqual(tuple(models), compare_compactors.REQUIRED_MODELS)
        sent = [call.args[1] for call in request.call_args_list]
        self.assertEqual(sent[0], sent[1])
        self.assertNotIn("invented-private-value", repr(sent))
        self.assertEqual(draft.read_text(encoding="utf-8"), before)
        self.assertIn("Validation: valid", output.getvalue())
        self.assertIn("Token use", output.getvalue())
        self.assertIn("Digest:", output.getvalue())
        self.assertIn("Packet:", output.getvalue())

    def test_model_exceptions_return_failure_and_log_tracebacks(self):
        from tests import calibrate_jev, compare_compactors

        cases = ((calibrate_jev.main, [str(self.label())], "memsift.jev._request"),
                 (compare_compactors.main, [str(self.draft())], "memsift.compactor.request"))
        for main, argv, target in cases:
            with self.subTest(target=target), patch("builtins.input", return_value="yes"), \
                    patch(target, side_effect=RuntimeError("injected calibration failure")), \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(main(argv), 1)
            log = (self.home / ".memsift/debug.log").read_text(encoding="utf-8")
            self.assertIn("injected calibration failure", log)
            self.assertIn("Traceback", log)

    def test_direct_clis_bootstrap_from_unrelated_cwd_and_decline(self):
        scripts = Path(__file__).parent
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["HOME"] = str(self.home)
        env["MEMSIFT_JEV_MODEL"] = "typesafe/jev-1.13"
        cases = ((scripts / "calibrate_jev.py", self.label().name),
                 (scripts / "compare_compactors.py", self.draft().name))
        for script, argument in cases:
            with self.subTest(script=script.name):
                result = subprocess.run([sys.executable, str(script), argument], input="n\n", text=True,
                                        capture_output=True, cwd=self.home, env=env, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Estimated cost", result.stdout)
        self.assertEqual(self.fake.requests, [])

    def test_direct_cli_help_and_invalid_input(self):
        scripts = Path(__file__).parent
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["HOME"] = str(self.home)
        for script in (scripts / "calibrate_jev.py", scripts / "compare_compactors.py"):
            with self.subTest(script=script.name):
                help_result = subprocess.run([sys.executable, str(script), "--help"], text=True,
                                             capture_output=True, cwd=self.home, env=env, timeout=5)
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                invalid = subprocess.run([sys.executable, str(script), "missing"], text=True,
                                         capture_output=True, cwd=self.home, env=env, timeout=5)
                self.assertEqual(invalid.returncode, 2)
        self.assertEqual(self.fake.requests, [])

    def test_accepted_direct_clis_use_only_guarded_fake_models(self):
        scripts = Path(__file__).parent
        labels = self.label()
        answers = {"has_info": prompt_kind(0), "cognee": noul(0.9), "dataset_memory": noul(0.6)}
        for _ in range(2):
            self.fake.push({"answers": answers, "usage": {"input_tokens": 100}})
        env = child_env(self.fake.url, self.home)
        env["MEMSIFT_JEV_MODEL"] = "typesafe/jev-1.13"
        result = subprocess.run([sys.executable, str(scripts / "calibrate_jev.py"), labels.name],
                                input="yes\n", text=True, capture_output=True, cwd=self.home,
                                env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Prompt: 1/1 correct", result.stdout)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual({tuple(request["body"]["questions"]) for request in self.fake.requests},
                         {("has_info",), ("cognee", "dataset_memory")})

        draft = self.draft()
        self.fake.push(envelope(compacted()))
        self.fake.push(envelope(compacted()))
        result = subprocess.run([sys.executable, str(scripts / "compare_compactors.py"), draft.name],
                                input="yes\n", text=True, capture_output=True, cwd=self.home,
                                env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.fake.requests), 4)
        self.assertEqual(result.stdout.count("Measured provider token use"), 2)
        self.assertEqual(sum(request["url"].endswith("/chat/completions")
                             for request in self.fake.requests), 2)
        self.assertEqual(sum("/api/alpha/decisions" in request["url"]
                             for request in self.fake.requests), 2)
