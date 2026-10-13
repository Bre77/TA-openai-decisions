import logging

import pytest

import choose
import conftest
import grade
from decision_cache import KVStoreCache
from decisions_command import probability_fields
from openai_decisions import Choice, ChoiceAnswer, Evaluator, ScoreAnswer


def run_choose(monkeypatch, raw_args):
    return conftest.run_getinfo(monkeypatch, choose.ChooseCommand, raw_args)


def run_grade(monkeypatch, raw_args):
    return conftest.run_getinfo(monkeypatch, grade.GradeCommand, raw_args)


# --- choose arguments -------------------------------------------------------


@pytest.mark.parametrize(
    "raw_args, question, choices",
    [
        (
            ['"Which team owns this?"', "network", '"Routers and DNS"', "storage", '"Disks"'],
            "Which team owns this?",
            (("network", "Routers and DNS"), ("storage", "Disks")),
        ),
        # Quoted values and descriptions may contain spaces, commas and "=".
        (
            ['"Which team?"', '"service desk"', '"Laptops, phones"', '"status=500"', "Errors"],
            "Which team?",
            (("service desk", "Laptops, phones"), ("status=500", "Errors")),
        ),
        # Options may come before, between and after the keyword options.
        (
            ["field=msg", '"Which team?"', "network", "Routers", "cache=false", "storage", "Disks"],
            "Which team?",
            (("network", "Routers"), ("storage", "Disks")),
        ),
        # A quoted question may contain "=" and escaped quotes and backslashes.
        (
            ['"Is status=500 an error?"', "yes", "Yes.", "no", "No."],
            "Is status=500 an error?",
            (("yes", "Yes."), ("no", "No.")),
        ),
        (
            ['"Is \\"x\\" a C:\\\\ path?"', '"say \\"yes\\""', '"C:\\\\ is a path"', "no", "No."],
            'Is "x" a C:\\ path?',
            (('say "yes"', "C:\\ is a path"), ("no", "No.")),
        ),
        # Options are case-sensitive, so these are distinct, and descriptions may repeat.
        (
            ['"Which?"', "Network", "Same", "network", "Same"],
            "Which?",
            (("Network", "Same"), ("network", "Same")),
        ),
    ],
)
def test_choose_question_and_choices(monkeypatch, raw_args, question, choices):
    command, errors = run_choose(monkeypatch, raw_args)
    assert errors == []
    assert (command._question.instructions, command._question.choices) == (question, choices)


def test_choose_sends_the_descriptions(monkeypatch):
    command, errors = run_choose(monkeypatch, ['"Which?"', "a", '"First one"', "b", "Second"])
    assert errors == []
    assert command._question.to_request()["choices"] == [
        {"value": "a", "description": "First one"},
        {"value": "b", "description": "Second"},
    ]


def test_choose_keeps_the_keyword_options(monkeypatch):
    command, errors = run_choose(
        monkeypatch,
        [
            '"Which?"',
            "a",
            "A",
            "b",
            "B",
            "field=msg",
            "output=team",
            "error_field=why",
            "model=m",
            "cache=false",
        ],
    )
    assert errors == []
    assert (command.field, command.output, command.error_field, command.model) == (
        "msg",
        "team",
        "why",
        "m",
    )
    assert command.cache is False
    assert command._use_cache is False


def pairs(count):
    return [term for i in range(count) for term in (f"o{i}", f"Option {i}")]


@pytest.mark.parametrize(
    "raw_args, error",
    [
        ([], "choose: a double-quoted question and its options are required."),
        (["field=msg"], "choose: a double-quoted question and its options are required."),
        (['"Which team?"'], "choose: at least 2 options are required, but got 0."),
        (
            ['"Which team?"', "network", "Routers"],
            "choose: at least 2 options are required, but got 1.",
        ),
        # Bare options without descriptions are no longer accepted.
        (
            ['"Which team?"', "network", "storage", "identity"],
            "choose: each option must be followed by its description, but got an odd number "
            "of terms (3) after the question.",
        ),
        (
            ['"Which team?"', "a", "A", "b", "B", "a", "Again"],
            "choose: duplicate option 'a'.",
        ),
        (['"Which team?"', "a", "A", '" "', "B"], "choose: options must not be empty."),
        (['"Which team?"', "a", "A", "b", '""'], "choose: the description of option 'b' is empty."),
        (
            ['"Which team?"', *pairs(256)],
            "choose: at most 255 options are allowed, but got 256.",
        ),
        (
            ["Which", "team?", "network", "storage"],
            "choose: put the question in double quotes. The first term, 'Which', is not quoted.",
        ),
        (['""', "a", "A", "b", "B"], "choose: the question is empty."),
        (['"Which?"', "a", "A", "b", "B", "prompt=x"], "Unrecognized option: prompt=x"),
    ],
)
def test_choose_argument_errors(monkeypatch, raw_args, error):
    _, errors = run_choose(monkeypatch, raw_args)
    assert len(errors) == 1 and errors[0].startswith(error), errors
    if error.startswith("choose:"):
        assert errors[0].endswith(f"Example: {choose.EXAMPLE}")


def test_choose_accepts_255_options(monkeypatch):
    command, errors = run_choose(monkeypatch, ['"Which?"', *pairs(255)])
    assert errors == []
    assert len(command._question.options) == 255


def test_choose_command_defaults():
    command = choose.ChooseCommand()
    command.options.reset()
    assert (command.field, command.output, command.error_field, command.model) == (
        "_raw",
        "choice",
        "choice_error",
        "gpt-6-luna",
    )
    assert command.cache is None


# --- grade arguments --------------------------------------------------------


def test_grade_question_and_levels(monkeypatch):
    command, errors = run_grade(
        monkeypatch,
        [
            '"How severe is this?"',
            "low",
            '"Routine, no action needed"',
            "field=msg",
            '"high risk"',
            '"Needs action: status=500"',
        ],
    )
    assert errors == []
    assert command._question.instructions == "How severe is this?"
    assert command._question.levels == (
        ("low", "Routine, no action needed"),
        ("high risk", "Needs action: status=500"),
    )
    assert command.field == "msg"


@pytest.mark.parametrize(
    "raw_args, error",
    [
        ([], "grade: a double-quoted question and its levels are required."),
        (['"How severe?"'], "grade: at least 2 levels are required, but got 0."),
        (['"How severe?"', "low", '"Fine"'], "grade: at least 2 levels are required, but got 1."),
        (
            ['"How severe?"', "low", '"Fine"', "high"],
            "grade: each level must be followed by its description, but got an odd number "
            "of terms (3) after the question.",
        ),
        (
            ['"How severe?"', "low", '"Fine"', "low", '"Again"'],
            "grade: duplicate level 'low'.",
        ),
        (['"How severe?"', "low", '"Fine"', "high", '""'], "grade: the description of level"),
        (['"How severe?"', '""', '"Fine"', "high", '"Bad"'], "grade: levels must not be empty."),
        (
            ["How", "severe?", "low", '"Fine"'],
            "grade: put the question in double quotes. The first term, 'How', is not quoted.",
        ),
    ],
)
def test_grade_argument_errors(monkeypatch, raw_args, error):
    _, errors = run_grade(monkeypatch, raw_args)
    assert len(errors) == 1 and errors[0].startswith(error), errors
    assert errors[0].endswith(f"Example: {grade.EXAMPLE}")


def test_argument_errors_fit_splunks_error_banner(monkeypatch):
    # Splunk cuts "Error in '<command>' command: <message>" at about 280 characters.
    for run, raw_args in [
        (run_choose, ["Which", "team?"]),
        (run_choose, ['"Which?"', "a"]),
        (run_grade, ['"How severe?"', "low", '"Fine"', "high"]),
        (run_grade, ["How", "severe?"]),
    ]:
        _, (error,) = run(monkeypatch, raw_args)
        assert len(f"Error in 'choose' command: {error}") < 280, error


def test_grade_command_defaults():
    command = grade.GradeCommand()
    command.options.reset()
    assert (command.output, command.error_field) == ("grade", "grade_error")


# --- field names ------------------------------------------------------------


def test_probability_fields_are_safe_and_unique():
    labels = ["network", "service desk", "a-b", "a b", "R&D / Ops", "日本", "!!", "Network", "_x_"]
    assert probability_fields("choice", labels) == [
        "choice_probability_network",
        "choice_probability_service_desk",
        "choice_probability_a_b",
        "choice_probability_a_b_4",
        "choice_probability_R_D_Ops",
        "choice_probability_6",
        "choice_probability_7",
        "choice_probability_Network",
        "choice_probability_x",
    ]


def test_probability_field_suffixes_never_collide():
    assert probability_fields("p", ["a b", "a_b_2", "a-b"]) == [
        "p_probability_a_b",
        "p_probability_a_b_2",
        "p_probability_a_b_3",
    ]
    assert probability_fields("p", ["a_b_2", "a b", "a-b"]) == [
        "p_probability_a_b_2",
        "p_probability_a_b",
        "p_probability_a_b_3",
    ]


# --- streaming --------------------------------------------------------------


class FakeClient:
    """Answers the question it is asked: the first option named in the text wins."""

    def __init__(self):
        self.inputs = []

    def ask(self, question, text, model):
        self.inputs.append(text)
        labels = question.options if isinstance(question, Choice) else question.labels
        index = next((i for i, label in enumerate(labels) if label in text), 0)
        probabilities = tuple(
            0.9 if i == index else 0.1 / (len(labels) - 1) for i in range(len(labels))
        )
        if isinstance(question, Choice):
            return ChoiceAnswer(labels[index], probabilities[index], 0.8, probabilities)
        return ScoreAnswer(float(index), 0.7, probabilities)


def streaming_command(command_class, raw_args, monkeypatch, cache=None):
    command, errors = conftest.run_getinfo(monkeypatch, command_class, raw_args)
    assert errors == []
    command._use_cache = cache is not None
    command._evaluator = Evaluator(FakeClient(), command._question, cache=cache)
    command._log = logging.getLogger("test")
    command.warnings = []
    command.write_warning = lambda message, *args: command.warnings.append(message.format(*args))
    return command


def test_choose_adds_every_field_and_drops_no_events(monkeypatch):
    command = streaming_command(
        choose.ChooseCommand,
        ['"Which team?"', "network", "Routers", '"service desk"', "Laptops"],
        monkeypatch,
    )
    out = list(command.stream([{"_raw": "ask the service desk"}, {"_raw": ""}, {"host": "x"}]))
    assert out[0] == {
        "_raw": "ask the service desk",
        "choice": "service desk",
        "choice_probability": 0.9,
        "choice_confidence": 0.8,
        "choice_probability_network": pytest.approx(0.1),
        "choice_probability_service_desk": 0.9,
        "choice_error": None,
    }
    empty = {
        "choice": None,
        "choice_probability": None,
        "choice_confidence": None,
        "choice_probability_network": None,
        "choice_probability_service_desk": None,
        "choice_error": "empty input",
    }
    assert out[1] == {"_raw": "", **empty}
    assert out[2] == {"host": "x", **empty}
    assert command.warnings == ["choose: some events could not be decided: empty input"]


def test_choose_custom_output_and_error_fields(monkeypatch):
    command = streaming_command(
        choose.ChooseCommand,
        ['"Which?"', "a", "A", "b", "B", "output=team", "error_field=why", "field=msg"],
        monkeypatch,
    )
    (out,) = command.stream([{"msg": ["x", "b"]}])
    assert out == {
        "msg": ["x", "b"],
        "team": "b",
        "team_probability": 0.9,
        "team_confidence": 0.8,
        "team_probability_a": pytest.approx(0.1),
        "team_probability_b": 0.9,
        "why": None,
    }
    assert command._evaluator._client.inputs == ["x\nb"]


def test_grade_adds_every_field(monkeypatch):
    command = streaming_command(
        grade.GradeCommand,
        ['"How bad?"', "low", '"Fine"', "medium", '"Look later"', '"high risk"', '"Act now"'],
        monkeypatch,
    )
    out = list(command.stream([{"_raw": "a medium problem"}, {"_raw": None}]))
    assert out[0] == {
        "_raw": "a medium problem",
        "grade": 1.0,
        "grade_confidence": 0.7,
        "grade_probability_low": pytest.approx(0.05),
        "grade_probability_medium": 0.9,
        "grade_probability_high_risk": pytest.approx(0.05),
        "grade_error": None,
    }
    assert out[1] == {
        "_raw": None,
        "grade": None,
        "grade_confidence": None,
        "grade_probability_low": None,
        "grade_probability_medium": None,
        "grade_probability_high_risk": None,
        "grade_error": "empty input",
    }


@pytest.mark.parametrize(
    "command_class, raw_args, from_cache_field",
    [
        (choose.ChooseCommand, ['"Which?"', "network", "N", "storage", "S"], "choice_from_cache"),
        (grade.GradeCommand, ['"How bad?"', "low", '"Fine"', "high", '"Bad"'], "grade_from_cache"),
    ],
)
def test_from_cache_field_only_when_the_cache_is_enabled(
    monkeypatch, kv, command_class, raw_args, from_cache_field
):
    def records(*texts):
        return [{"_raw": text} for text in texts]

    command = streaming_command(command_class, raw_args, monkeypatch, cache=KVStoreCache(kv))
    list(command.stream(records("storage high")))

    command = streaming_command(command_class, raw_args, monkeypatch, cache=KVStoreCache(kv))
    out = list(command.stream(records("storage high", "network low", "")))
    assert [r[from_cache_field] for r in out] == ["true", "false", "false"]
    assert command._evaluator._client.inputs == ["network low"]

    command = streaming_command(command_class, raw_args, monkeypatch)
    (out,) = command.stream(records("storage high"))
    assert from_cache_field not in out
