"""Threshold and scope regressions for the memory classifier."""

from tests.support import IsolatedTest


def noul(value):
    return {"type": "noul", "noul": value}


def stop_answers(nature="project", scope="this project", confidence=0.6,
                 correction=0.0, known=0.0, general_rule=0.0):
    natures = ["project", "lesson", "nothing"]
    levels = ["this turn", "this task", "this project", "always"]
    return {
        "nature": {"type": "choice", "choice": nature, "confidence": confidence,
                   "probabilities": {n: float(n == nature) for n in natures}},
        "scope": {"type": "score", "score": levels.index(scope), "confidence": 1.0,
                  "legend": {str(i): s for i, s in enumerate(levels)},
                  "probabilities": {str(i): float(s == scope) for i, s in enumerate(levels)}},
        "correction": noul(correction), "known": noul(known),
        "general_rule": noul(general_rule),
        "card_outdated": noul(0.7), "reason_missing": noul(0.7),
        "confirmed_d4": noul(0.7),
    }


class DecisionTests(IsolatedTest):
    def test_prompt_cascade_boundaries(self):
        from memsift import jev
        for has_info, entry, recall, ids, search in [
            (0.5, 1, 1, [], False), (0.4999, 0.5, 1, ["d1"], False),
            (0, 0.5001, 1, ["d1"], False),
            (0, 0.4999, 0.7999, [], False),
            (0, 0.4999, 0.8, [], True), (0, 0.4999, 0.8001, [], True),
        ]:
            with self.subTest(has_info=has_info, entry=entry, recall=recall):
                result = jev.choose_prompt({"has_info": noul(has_info), "entry_d1": noul(entry),
                                           "cognee": noul(recall), "dataset_lessons": noul(0.4)},
                                          ["d1"], ["lessons"])
                self.assertEqual(result, {"entries": ids, "recall": search,
                                          "datasets": ["lessons"] if search else []})

    def test_dataset_threshold_fallback_ties_and_empty(self):
        from memsift import jev
        answers = {"has_info": noul(0), "cognee": noul(1),
                   "dataset_lessons": noul(0.4999), "dataset_projects": noul(0.4999)}
        self.assertEqual(jev.choose_prompt(answers, [], ["projects", "lessons"])["datasets"], ["projects"])
        answers["dataset_lessons"] = noul(0.5)
        self.assertEqual(jev.choose_prompt(answers, [], ["projects", "lessons"])["datasets"], ["lessons"])
        answers["dataset_projects"] = noul(0.5001)
        self.assertEqual(jev.choose_prompt(answers, [], ["projects", "lessons"])["datasets"], ["projects", "lessons"])
        self.assertFalse(jev.choose_prompt(answers, [], [])["recall"])

    def test_entries_keep_notes_order(self):
        from memsift import jev
        answers = {"has_info": noul(0), "cognee": noul(1),
                   "entry_d1": noul(1), "entry_g2": noul(1)}
        self.assertEqual(jev.choose_prompt(answers, ["g2", "d1"], [])["entries"], ["g2", "d1"])

    def test_passage_threshold(self):
        from memsift import jev
        passages = [{"id": "p1", "dataset": "lessons", "text": "Allow sharp."}]
        for probability, keep in [(0.69, False), (0.7, True), (0.71, True)]:
            with self.subTest(probability=probability):
                self.assertEqual(jev.choose_passages({"passage_1": noul(probability)}, passages),
                                 passages if keep else [])

    def test_all_natures_and_scopes(self):
        from memsift import jev
        for nature in ("project", "lesson", "nothing"):
            for scope in ("this turn", "this task", "this project", "always"):
                with self.subTest(nature=nature, scope=scope):
                    result = jev.choose_stop(stop_answers(nature, scope), ["d4"])
                    keep = nature != "nothing" and scope != "this turn"
                    self.assertEqual(result["keep"], keep)
                    self.assertEqual(result["reason_missing"], keep and nature == "project")
                    self.assertEqual(result["confirmed"], ["d4"])
                    self.assertTrue(result["card_outdated"])

    def test_stop_ignores_confidence_and_keeps_correction_and_known_boundaries(self):
        from memsift import jev
        for confidence, correction, known, keep, corrected in [
            (0, 0, 0, True, False), (0.5999, 0, 0, True, False), (0.6, 0, 0, True, False),
            (0.4, 0.4999, 0, True, False), (0.3999, 0.5, 0, True, True),
            (0.4, 0.5, 0, True, True), (0.4001, 0.5001, 0, True, True),
            (1, 0, 0.4999, True, False), (1, 0, 0.5, False, False), (1, 0, 0.5001, False, False),
        ]:
            with self.subTest(confidence=confidence, correction=correction, known=known):
                result = jev.choose_stop(stop_answers(confidence=confidence, correction=correction, known=known), [])
                self.assertEqual((result["keep"], result["correction"]), (keep, corrected))

    def test_general_rule_boundary_drops_ask_before_committing(self):
        from memsift import jev
        for probability, keep in [(0.4999, True), (0.5, False), (0.5001, False)]:
            with self.subTest(general_rule=probability):
                result = jev.choose_stop(stop_answers(general_rule=probability), [])
                self.assertEqual(result["keep"], keep)
                self.assertEqual(result["reason_missing"], keep)

    def test_uncertain_project_nature_is_kept(self):
        from memsift import jev
        answers = stop_answers(confidence=0.55)
        answers["nature"]["probabilities"] = {"project": 0.55, "lesson": 0, "nothing": 0.45}
        self.assertTrue(jev.choose_stop(answers, [])["keep"])

    def test_flags_boundaries_and_absent_card(self):
        from memsift import jev
        for probability, flag in [(0.6999, False), (0.7, True), (0.7001, True)]:
            answers = stop_answers()
            for key in ("confirmed_d4", "card_outdated", "reason_missing"):
                answers[key] = noul(probability)
            result = jev.choose_stop(answers, ["d4"])
            self.assertEqual(result["confirmed"], ["d4"] if flag else [])
            self.assertEqual(result["card_outdated"], flag)
            self.assertEqual(result["reason_missing"], flag)
        del answers["card_outdated"]
        self.assertFalse(jev.choose_stop(answers, ["d4"])["card_outdated"])

    def test_scope_uses_modal_level_and_lower_tie_not_rounded_score(self):
        from memsift import jev
        answers = stop_answers()
        answers["scope"]["score"] = 1.6
        answers["scope"]["probabilities"] = {"3": 0.25, "2": 0.25, "1": 0.25, "0": 0.25}
        self.assertFalse(jev.choose_stop(answers, [])["keep"])
        answers["scope"]["probabilities"] = {"0": 0.1, "1": 0.1, "2": 0.35, "3": 0.45}
        self.assertTrue(jev.choose_stop(answers, [])["keep"])
