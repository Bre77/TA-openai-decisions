#
# Copyright 2026 Brett Adams
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""decide: score each event against a yes/no prompt with the OpenAI Decisions API.

    ... | decide "<question>" [field=<field>] [output=<field>]
                 [error_field=<field>] [model=<model>] [cache=<bool>]

The question can also be given as ``prompt="<question>"``. Adds the predicate
probability to every event and never drops events; filtering is left to a
following ``where``. The shared settings, cache and streaming code is in
decisions_command.
"""

import sys

import import_declare_test  # noqa: F401  (puts the add-on's lib/ on sys.path)
from splunklib.searchcommands import Configuration, Option, dispatch, validators

from decisions_command import QuestionCommand
from openai_decisions import Predicate

PROMPT_EXAMPLE = '| decide "Is this a failed login?"'


@Configuration(distributed=False)
class DecideCommand(QuestionCommand):
    """
    ##Syntax
    decide (<string> | prompt=<string>) [field=<field>] [output=<field>]
    [error_field=<field>] [model=<string>] [cache=<bool>]

    ##Description
    Asks the OpenAI Decisions API a yes/no question about each event and adds the
    predicate probability (0 to 1) as a field. Events are never dropped. The
    question is the first argument, in double quotes, or the prompt option.
    """

    FROM_CACHE_FIELD = "decision_from_cache"

    prompt = Option(
        doc="The yes/no question each event is evaluated against, when it is not "
        "given as the first argument.",
    )
    output = Option(
        doc="Field that receives the probability. Default: decision_probability.",
        default="decision_probability",
        validate=validators.Fieldname(),
    )
    error_field = Option(
        doc="Field that receives a short error when an event could not be decided. "
        "Default: decision_error.",
        default="decision_error",
        validate=validators.Fieldname(),
    )

    def _resolve_question(self):
        self.prompt = self._resolve_prompt()
        if not self.prompt.strip():
            self._fail("decide: the prompt is empty. " + _prompt_hint())
        return Predicate(self.prompt)

    def _resolve_prompt(self):
        """Return the prompt from the first argument or the prompt option."""
        positional = self.fieldnames or []
        if len(positional) > 1:
            self._fail(
                f"decide: the prompt must be one double-quoted string, but got {len(positional)} "
                f"separate terms. {_prompt_hint()}"
            )
        if positional and self.prompt is not None:
            self._fail(
                "decide: give the prompt either as the first argument or as prompt=, not both"
            )
        if positional:
            return positional[0]
        if self.prompt is None:
            self._fail("decide: a prompt is required. " + _prompt_hint())
        return self.prompt

    def _write_answer(self, record, probability):
        record[self.output] = probability


def _prompt_hint():
    return f"Put the yes/no question in double quotes, for example: {PROMPT_EXAMPLE}"


if __name__ == "__main__":
    dispatch(DecideCommand, sys.argv, sys.stdin, sys.stdout, __name__)
